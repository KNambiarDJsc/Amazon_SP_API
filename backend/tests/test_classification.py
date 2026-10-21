import random
from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_fixture

from app.amazon.parsing import failure_observation, parse_restrictions_observation
from app.domain.classification import (
    RULES_VERSION,
    Verdict,
    approval_scope,
    classify_check,
    classify_observation,
    combine_verdicts,
)
from app.domain.observations import Observation, ObservationContext
from app.enums import EligibilityStatus as S
from app.enums import ObservationOutcome, ObservationSource

CTX = ObservationContext(
    asin="B08Y9C5H38", marketplace_id="A21TJRUUN4KGV", condition="new_new", seller_ref="seller-1"
)
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def obs_from(body, **kw) -> Observation:
    return parse_restrictions_observation(body, context=CTX, **kw)


def classify_body(body):
    return classify_observation(obs_from(body))


# ---- the explicit cases from the product spec ------------------------------------------------


def test_empty_restrictions_list_is_verified_listable():
    v = classify_body(load_fixture("spapi/restrictions_empty.json"))
    assert v.status == S.VERIFIED_LISTABLE
    assert v.rule_id == "SPAPI_NO_RESTRICTIONS"
    assert "B08Y9C5H38" in v.rationale and "new_new" in v.rationale


def test_brand_approval_required_keeps_message_and_link():
    v = classify_body(load_fixture("spapi/restrictions_brand_approval.json"))
    assert v.status == S.BRAND_APPROVAL_REQUIRED
    assert v.reason_codes == ["APPROVAL_REQUIRED"]
    assert v.messages == ["You need approval to list in this brand."]
    assert v.approval_links[0].title == "Apply to sell"
    assert v.approval_links[0].resource.startswith("https://sellercentral.amazon.in/")


def test_category_approval_is_other_approval_not_brand():
    v = classify_body(load_fixture("spapi/restrictions_category_approval.json"))
    assert v.status == S.OTHER_APPROVAL_REQUIRED


def test_not_eligible():
    v = classify_body(load_fixture("spapi/restrictions_not_eligible.json"))
    assert v.status == S.NOT_ELIGIBLE
    assert v.reason_codes == ["NOT_ELIGIBLE"]


def test_asin_not_found_needs_manual_check():
    v = classify_body(load_fixture("spapi/restrictions_asin_not_found.json"))
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.rule_id == "SPAPI_ASIN_NOT_FOUND"


# ---- approval message scope ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "scope"),
    [
        ("You need approval to list in this brand.", "BRAND"),
        ("You need approval to sell this brand.", "BRAND"),
        ("You need approval to list in this category.", "OTHER"),
        ("You need approval to sell in the Grocery sub-category.", "OTHER"),
        # Mentions both: approval is required, but we do not claim the brand is what is gated.
        ("You need approval to list this brand in the Toys category.", "OTHER"),
        ("Approval required.", "OTHER"),
        ("", "OTHER"),
        (None, "OTHER"),
        ("Rebrand your listing", "OTHER"),  # 'brand' inside another word must not count
    ],
)
def test_approval_scope(message, scope):
    assert approval_scope(message) == scope


# ---- missing / malformed data is never permission --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"restrictions": None},
        {"restrictions": "none"},
        {"restrictions": {}},
        {"errors": []},
        None,
    ],
)
def test_missing_or_malformed_restrictions_field_is_not_listable(body):
    v = classify_body(body)
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.evidence_missing
    assert v.rule_id == "SPAPI_NO_RESTRICTIONS_FIELD"


def test_non_object_entry_is_malformed_response():
    o = obs_from({"restrictions": ["oops"]})
    assert o.outcome == ObservationOutcome.MALFORMED_RESPONSE
    assert classify_observation(o).status == S.NEEDS_MANUAL_CHECK


@pytest.mark.parametrize("outcome", [o for o in ObservationOutcome if o != ObservationOutcome.OK])
def test_every_failed_outcome_needs_manual_check(outcome):
    o = failure_observation(outcome=outcome, context=CTX, http_status=500, detail="boom")
    v = classify_observation(o)
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.evidence_missing
    assert v.rule_id == f"SPAPI_{outcome.value}"
    assert "boom" in v.rationale


def test_entry_for_a_different_marketplace_is_not_evidence_of_no_restriction():
    body = {
        "restrictions": [
            {
                "marketplaceId": "ATVPDKIKX0DER",
                "conditionType": "new_new",
                "reasons": [{"reasonCode": "APPROVAL_REQUIRED", "message": "brand"}],
            }
        ]
    }
    v = classify_body(body)
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.rule_id == "SPAPI_CONTEXT_MISMATCH"


def test_entry_for_a_different_condition_is_not_applied():
    body = {
        "restrictions": [
            {
                "marketplaceId": "A21TJRUUN4KGV",
                "conditionType": "used_good",
                "reasons": [{"reasonCode": "NOT_ELIGIBLE", "message": "x"}],
            }
        ]
    }
    assert classify_body(body).rule_id == "SPAPI_CONTEXT_MISMATCH"


def test_applicable_blocker_wins_over_foreign_entry():
    brand = load_fixture("spapi/restrictions_brand_approval.json")
    foreign = {"marketplaceId": "ATVPDKIKX0DER", "conditionType": "new_new", "reasons": []}
    brand["restrictions"].append(foreign)
    assert classify_body(brand).status == S.BRAND_APPROVAL_REQUIRED


def test_entry_without_reasons_is_ambiguous():
    body = {"restrictions": [{"marketplaceId": "A21TJRUUN4KGV", "conditionType": "new_new"}]}
    v = classify_body(body)
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.rule_id == "SPAPI_EMPTY_RESTRICTION_ENTRY"


def test_unrecognised_reason_code_needs_manual_check():
    body = {
        "restrictions": [
            {
                "marketplaceId": "A21TJRUUN4KGV",
                "conditionType": "new_new",
                "reasons": [{"reasonCode": "SOMETHING_NEW", "message": "?"}],
            }
        ]
    }
    v = classify_body(body)
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.reason_codes == ["SOMETHING_NEW"]


def test_unknown_code_does_not_hide_a_known_blocker():
    body = load_fixture("spapi/restrictions_brand_approval.json")
    body["restrictions"][0]["reasons"].append({"reasonCode": "SOMETHING_NEW", "message": "?"})
    assert classify_body(body).status == S.BRAND_APPROVAL_REQUIRED


def test_reason_codes_are_case_insensitive():
    body = load_fixture("spapi/restrictions_brand_approval.json")
    body["restrictions"][0]["reasons"][0]["reasonCode"] = "approval_required"
    assert classify_body(body).status == S.BRAND_APPROVAL_REQUIRED


def test_blocker_precedence_not_eligible_then_brand_then_other():
    body = load_fixture("spapi/restrictions_brand_approval.json")
    reasons = body["restrictions"][0]["reasons"]
    reasons.append({"reasonCode": "APPROVAL_REQUIRED", "message": "need approval in this category"})
    assert classify_body(body).status == S.BRAND_APPROVAL_REQUIRED
    reasons.append({"reasonCode": "NOT_ELIGIBLE", "message": "no"})
    v = classify_body(body)
    assert v.status == S.NOT_ELIGIBLE
    assert v.reason_codes == ["APPROVAL_REQUIRED", "APPROVAL_REQUIRED", "NOT_ELIGIBLE"]


# ---- combining observations ------------------------------------------------------------------


def test_no_observations_is_no_evidence():
    c = classify_check([])
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "NO_EVIDENCE"


def test_classification_carries_method_and_rules_version():
    c = classify_check([obs_from(load_fixture("spapi/restrictions_empty.json"))])
    assert c.verification_methods == [ObservationSource.SP_API_RESTRICTIONS]
    assert c.rules_version == RULES_VERSION


def test_later_observation_from_same_source_supersedes_earlier():
    gated = obs_from(load_fixture("spapi/restrictions_brand_approval.json"))
    gated.observed_at = T0
    cleared = obs_from(load_fixture("spapi/restrictions_empty.json"))
    cleared.observed_at = T0 + timedelta(days=1)
    assert classify_check([gated, cleared]).status == S.VERIFIED_LISTABLE
    assert (
        classify_check([cleared, gated]).status == S.VERIFIED_LISTABLE
    )  # input order is irrelevant


def test_failed_attempt_then_success_is_the_success():
    failed = failure_observation(outcome=ObservationOutcome.TIMEOUT, context=CTX)
    failed.observed_at = T0
    ok = obs_from(load_fixture("spapi/restrictions_empty.json"))
    ok.observed_at = T0 + timedelta(seconds=5)
    assert classify_check([failed, ok]).status == S.VERIFIED_LISTABLE


def test_success_then_failed_recheck_falls_back_to_manual_check():
    ok = obs_from(load_fixture("spapi/restrictions_empty.json"))
    ok.observed_at = T0
    failed = failure_observation(outcome=ObservationOutcome.AUTH_ERROR, context=CTX)
    failed.observed_at = T0 + timedelta(seconds=5)
    assert classify_check([ok, failed]).status == S.NEEDS_MANUAL_CHECK


def _v(status, source=ObservationSource.SP_API_RESTRICTIONS, *, missing=False, rule="R"):
    return Verdict(
        status=status,
        rule_id=rule,
        rationale=f"{source.value} says {status.value}",
        source=source,
        evidence_missing=missing,
    )


def test_conflicting_sources_never_pick_a_side():
    c = combine_verdicts(
        [_v(S.VERIFIED_LISTABLE), _v(S.BRAND_APPROVAL_REQUIRED, ObservationSource.BROWSER)]
    )
    assert c.status == S.NEEDS_MANUAL_CHECK
    assert c.rule_id == "SOURCE_CONFLICT"
    assert set(c.verification_methods) == {
        ObservationSource.SP_API_RESTRICTIONS,
        ObservationSource.BROWSER,
    }


def test_ambiguous_second_source_blocks_a_listable_verdict():
    c = combine_verdicts(
        [_v(S.VERIFIED_LISTABLE), _v(S.NEEDS_MANUAL_CHECK, ObservationSource.BROWSER)]
    )
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "SOURCE_CONFLICT"


def test_unusable_second_source_does_not_cancel_a_real_answer():
    c = combine_verdicts(
        [_v(S.VERIFIED_LISTABLE), _v(S.NEEDS_MANUAL_CHECK, ObservationSource.BROWSER, missing=True)]
    )
    assert c.status == S.VERIFIED_LISTABLE


def test_blocker_beats_ambiguity_from_another_source():
    c = combine_verdicts(
        [_v(S.BRAND_APPROVAL_REQUIRED), _v(S.NEEDS_MANUAL_CHECK, ObservationSource.BROWSER)]
    )
    assert c.status == S.BRAND_APPROVAL_REQUIRED


def test_all_sources_unusable_reports_manual_check():
    c = combine_verdicts([_v(S.NEEDS_MANUAL_CHECK, missing=True, rule="SPAPI_TIMEOUT")])
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "SPAPI_TIMEOUT"


def test_source_without_rules_is_never_listable(monkeypatch):
    from app.domain import classification

    monkeypatch.delitem(classification._CLASSIFIERS, ObservationSource.MANUAL)
    o = Observation(source=ObservationSource.MANUAL, context=CTX)
    v = classify_observation(o)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "NO_CLASSIFIER"


# ---- the safety invariant --------------------------------------------------------------------


def _random_body(rng: random.Random):
    """Bodies Amazon might plausibly send, plus a lot it should not."""
    shape = rng.random()
    if shape < 0.12:
        return rng.choice([None, {}, [], "x", {"restrictions": None}, {"restrictions": "[]"}])
    if shape < 0.30:
        return {"restrictions": []}
    entries = []
    for _ in range(rng.randint(1, 3)):
        entry: dict = {}
        if rng.random() < 0.9:
            entry["marketplaceId"] = rng.choice(["A21TJRUUN4KGV", "ATVPDKIKX0DER", None])
        if rng.random() < 0.9:
            entry["conditionType"] = rng.choice(["new_new", "used_good", None])
        if rng.random() < 0.9:
            entry["reasons"] = [
                {
                    "reasonCode": rng.choice(
                        ["APPROVAL_REQUIRED", "NOT_ELIGIBLE", "ASIN_NOT_FOUND", "WHAT", "", None]
                    ),
                    "message": rng.choice(["brand", "category", "", None, "You need approval"]),
                }
                for _ in range(rng.randint(0, 3))
            ]
        entries.append(entry)
    return {"restrictions": entries}


def test_verified_listable_only_ever_comes_from_an_empty_restrictions_list():
    rng = random.Random(20261021)
    saw_listable = saw_other = 0
    for _ in range(3000):
        body = _random_body(rng)
        status = classify_observation(obs_from(body)).status
        empty = (
            isinstance(body, dict)
            and body.get("restrictions") == []
            and isinstance(body.get("restrictions"), list)
        )
        if status == S.VERIFIED_LISTABLE:
            saw_listable += 1
            assert empty, body
        else:
            saw_other += 1
        if empty:
            assert status == S.VERIFIED_LISTABLE, body
    assert saw_listable > 100 and saw_other > 100  # the generator exercised both sides
