"""Rules for what Seller Central showed, modelled on the supplied screenshots."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.amazon.parsing import parse_restrictions_observation
from app.domain.classification import classify_check, classify_observation
from app.domain.observations import BrowserSignals, Observation, ObservationContext
from app.enums import EligibilityStatus as S
from app.enums import ObservationOutcome, ObservationSource

LIMITATION = (
    "Used, Collectible, Refurbished conditions: "
    "This product has other listing limitations. Learn more"
)
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def ctx(asin="B08PDL14VR", condition="new_new", *, validated=True, brand="eSSL"):
    return ObservationContext(
        asin=asin,
        marketplace_id="A21TJRUUN4KGV",
        condition=condition,
        seller_ref="A1",
        expected_brand=brand,
        brand_validated=validated,
    )


def sees(source=ObservationSource.BROWSER, *, context=None, outcome=ObservationOutcome.OK, **sig):
    context = context or ctx()
    base = dict(page_asin=context.asin, selected_condition=context.condition)
    return Observation(
        source=source,
        outcome=outcome,
        context=context,
        browser=BrowserSignals(**{**base, **sig}),
    )


def verdict(**kw):
    return classify_observation(sees(**kw))


# ---- screenshot 2: Havells, approval required ------------------------------------------------


def test_screenshot_2_brand_approval_message_with_apply_button():
    v = verdict(
        context=ctx("B08Y9C5H38", brand="Havells"),
        notices=["You need approval to list in this brand."],
        action_label="Apply to sell",
        action_enabled=True,
    )
    assert v.status == S.BRAND_APPROVAL_REQUIRED
    assert v.rule_id == "BROWSER_BRAND_APPROVAL_REQUIRED"
    assert v.messages == ["You need approval to list in this brand."]


def test_a_category_approval_notice_is_other_approval():
    v = verdict(
        notices=["You need approval to list in this category."], action_label="Apply to sell"
    )
    assert v.status == S.OTHER_APPROVAL_REQUIRED


def test_not_eligible_notice():
    v = verdict(notices=["You are not eligible to list this product."], action_label=None)
    assert v.status == S.NOT_ELIGIBLE


def test_apply_to_sell_without_any_message_still_means_approval_is_needed():
    v = verdict(action_label="Apply to sell", action_enabled=True)
    assert v.status == S.OTHER_APPROVAL_REQUIRED and v.rule_id == "BROWSER_APPLY_TO_SELL"


# ---- screenshot 1: eSSL K30 PRO, no approval message -----------------------------------------


def test_screenshot_1_no_message_but_no_condition_chosen_and_sell_disabled_proves_nothing():
    """The trap in the original spec: 'no approval message' is not 'ungated'."""
    v = verdict(
        selected_condition=None,
        notices=[LIMITATION],
        action_label="Sell this product",
        action_enabled=False,
    )
    assert v.status == S.NEEDS_MANUAL_CHECK
    assert v.rule_id == "BROWSER_CONDITION_NOT_SELECTED"


def test_screenshot_1_after_choosing_new_with_sell_enabled_is_listable():
    v = verdict(notices=[LIMITATION], action_label="Sell this product", action_enabled=True)
    assert v.status == S.VERIFIED_LISTABLE and v.rule_id == "BROWSER_SELL_ENABLED"


def test_the_used_collectible_refurbished_notice_does_not_block_a_new_condition_check():
    assert (
        verdict(notices=[LIMITATION], action_label="Sell this product", action_enabled=True).status
        == S.VERIFIED_LISTABLE
    )


@pytest.mark.parametrize(
    "condition", ["used_good", "refurbished_refurbished", "collectible_like_new"]
)
def test_the_same_notice_does_block_the_conditions_it_names(condition):
    c = ctx(condition=condition)
    v = classify_observation(
        sees(context=c, notices=[LIMITATION], action_label="Sell this product", action_enabled=True)
    )
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_LISTING_LIMITATION"
    assert v.signals_restriction is True  # contradicts any "listable" from another source


def test_sell_button_present_but_disabled_with_the_right_condition_is_not_listable():
    v = verdict(action_label="Sell this product", action_enabled=False)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_ACTION_DISABLED"


@pytest.mark.parametrize("label", [None, "", "Copy listing", "Continue", "Sell"])
def test_an_unrecognised_or_missing_action_is_not_listable(label):
    v = verdict(action_label=label, action_enabled=True)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_NO_RECOGNISED_ACTION"


def test_an_unknown_notice_is_never_ignored():
    v = verdict(
        notices=["Something Amazon has never said before"],
        action_label="Sell this product",
        action_enabled=True,
    )
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_UNRECOGNIZED_NOTICE"


def test_label_matching_ignores_case_and_whitespace():
    v = verdict(action_label="  SELL   this product ", action_enabled=True)
    assert v.status == S.VERIFIED_LISTABLE


# ---- is it even the right product? -----------------------------------------------------------


def test_a_different_asin_on_screen_is_no_evidence_about_this_product():
    v = verdict(page_asin="B0OTHER000", action_label="Sell this product", action_enabled=True)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_ASIN_MISMATCH"
    assert v.signals_restriction is False


def test_no_asin_on_screen_is_unconfirmed():
    v = verdict(page_asin=None, action_label="Sell this product", action_enabled=True)
    assert v.rule_id == "BROWSER_ASIN_UNCONFIRMED"


def test_the_wrong_asin_never_yields_a_blocker_either():
    v = verdict(
        page_asin="B0OTHER000",
        notices=["You need approval to list in this brand."],
        action_label="Apply to sell",
    )
    assert v.status == S.NEEDS_MANUAL_CHECK  # evidence about another product is not used


def test_a_different_brand_on_screen_is_a_mismatch():
    v = verdict(page_brand="Someone Else", action_label="Sell this product", action_enabled=True)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_BRAND_MISMATCH"


def test_a_matching_brand_on_screen_is_fine_even_if_the_catalog_did_not_validate_it():
    c = ctx(validated=False)
    v = classify_observation(
        sees(context=c, page_brand="E-SSL", action_label="Sell this product", action_enabled=True)
    )
    assert v.status == S.VERIFIED_LISTABLE


def test_brand_that_cannot_be_confirmed_anywhere_is_not_listable():
    c = ctx(validated=False)
    v = classify_observation(sees(context=c, action_label="Sell this product", action_enabled=True))
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_BRAND_UNCONFIRMED"


# ---- no usable answer ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "rule"),
    [
        (dict(signed_in=False), "BROWSER_SESSION_EXPIRED"),
        (dict(challenge_detected=True), "BROWSER_CHALLENGE"),
        (dict(page_loaded=False), "BROWSER_PAGE_NOT_LOADED"),
    ],
)
def test_signed_out_challenged_or_unloaded_pages_give_no_answer(sig, rule):
    v = verdict(action_label="Sell this product", action_enabled=True, **sig)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == rule and v.evidence_missing


def test_product_unavailable_is_not_listable():
    v = verdict(product_unavailable=True)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.rule_id == "BROWSER_PRODUCT_UNAVAILABLE"
    assert not v.evidence_missing


@pytest.mark.parametrize("outcome", [o for o in ObservationOutcome if o != ObservationOutcome.OK])
def test_failed_outcomes_give_no_answer(outcome):
    o = Observation(source=ObservationSource.BROWSER, outcome=outcome, context=ctx())
    v = classify_observation(o)
    assert v.status == S.NEEDS_MANUAL_CHECK and v.evidence_missing


def test_missing_signals_give_no_answer():
    v = classify_observation(Observation(source=ObservationSource.BROWSER, context=ctx()))
    assert v.rule_id == "BROWSER_NO_SIGNALS" and v.evidence_missing


# ---- a human reviewer records the same thing --------------------------------------------------


def test_a_reviewers_entry_is_classified_by_the_same_rules_and_labelled_manual():
    c = classify_check(
        [sees(ObservationSource.MANUAL, action_label="Sell this product", action_enabled=True)]
    )
    assert c.status == S.VERIFIED_LISTABLE
    assert c.verification_methods == [ObservationSource.MANUAL]


def test_a_reviewer_cannot_assert_a_verdict_only_screen_state():
    # Even a reviewer who typed the right ASIN gets nothing if the screen state is not listable.
    c = classify_check(
        [
            sees(
                ObservationSource.MANUAL,
                selected_condition=None,
                action_label="Sell this product",
                action_enabled=False,
            )
        ]
    )
    assert c.status == S.NEEDS_MANUAL_CHECK


# ---- combining SP-API with browser / manual evidence -----------------------------------------


def spapi(body, context=None, at=T0):
    obs = parse_restrictions_observation(body, context=context or ctx())
    obs.observed_at = at
    return obs


def listable_screen(source=ObservationSource.BROWSER, at=T0 + timedelta(minutes=1)):
    o = sees(source, action_label="Sell this product", action_enabled=True)
    o.observed_at = at
    return o


ASIN_NOT_FOUND = {
    "restrictions": [
        {
            "marketplaceId": "A21TJRUUN4KGV",
            "conditionType": "new_new",
            "reasons": [{"reasonCode": "ASIN_NOT_FOUND", "message": "x"}],
        }
    ]
}
UNKNOWN_REASON = {
    "restrictions": [
        {
            "marketplaceId": "A21TJRUUN4KGV",
            "conditionType": "new_new",
            "reasons": [{"reasonCode": "SOMETHING_NEW", "message": "?"}],
        }
    ]
}
BRAND_GATED = {
    "restrictions": [
        {
            "marketplaceId": "A21TJRUUN4KGV",
            "conditionType": "new_new",
            "reasons": [
                {
                    "reasonCode": "APPROVAL_REQUIRED",
                    "message": "You need approval to list in this brand.",
                }
            ],
        }
    ]
}
EMPTY = {"restrictions": []}


@pytest.mark.parametrize("source", [ObservationSource.BROWSER, ObservationSource.MANUAL])
def test_screen_evidence_resolves_an_asin_the_api_did_not_know(source):
    c = classify_check([spapi(ASIN_NOT_FOUND), listable_screen(source)])
    assert c.status == S.VERIFIED_LISTABLE
    assert c.verification_methods == [source]  # attributed to who actually established it


def test_screen_evidence_cannot_override_an_api_that_reports_an_unrecognised_restriction():
    c = classify_check([spapi(UNKNOWN_REASON), listable_screen()])
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "SOURCE_CONFLICT"


def test_listable_on_screen_but_gated_per_api_is_a_conflict_not_a_win():
    c = classify_check([spapi(BRAND_GATED), listable_screen()])
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "SOURCE_CONFLICT"
    assert (
        "SP_API_RESTRICTIONS=BRAND_APPROVAL_REQUIRED" in c.rationale
        and "BROWSER=VERIFIED_LISTABLE" in c.rationale
    )


def test_gated_on_screen_but_listable_per_api_is_also_a_conflict():
    gated = sees(notices=["You need approval to list in this brand."], action_label="Apply to sell")
    gated.observed_at = T0 + timedelta(minutes=1)
    c = classify_check([spapi(EMPTY), gated])
    assert c.status == S.NEEDS_MANUAL_CHECK and c.rule_id == "SOURCE_CONFLICT"


def test_agreeing_sources_are_merged():
    gated = sees(notices=["You need approval to list in this brand."], action_label="Apply to sell")
    c = classify_check([spapi(BRAND_GATED), gated])
    assert c.status == S.BRAND_APPROVAL_REQUIRED
    assert set(c.verification_methods) == {
        ObservationSource.SP_API_RESTRICTIONS,
        ObservationSource.BROWSER,
    }


def test_a_screen_about_the_wrong_product_does_not_dilute_a_real_api_answer():
    wrong = sees(page_asin="B0OTHER000", action_label="Sell this product", action_enabled=True)
    assert classify_check([spapi(EMPTY), wrong]).status == S.VERIFIED_LISTABLE
    assert classify_check([spapi(BRAND_GATED), wrong]).status == S.BRAND_APPROVAL_REQUIRED


def test_a_session_that_expired_does_not_undo_a_real_answer():
    expired = sees(signed_in=False)
    assert classify_check([spapi(EMPTY), expired]).status == S.VERIFIED_LISTABLE


def test_a_later_reviewer_entry_replaces_an_earlier_one_from_the_same_source():
    wrong = sees(ObservationSource.MANUAL, action_label="Sell this product", action_enabled=False)
    wrong.observed_at = T0
    fixed = listable_screen(ObservationSource.MANUAL, at=T0 + timedelta(hours=1))
    assert classify_check([wrong, fixed]).status == S.VERIFIED_LISTABLE
    assert classify_check([fixed, wrong]).status == S.VERIFIED_LISTABLE


# ---- the safety invariant for browser evidence -----------------------------------------------


def test_listable_requires_every_positive_condition_to_hold():
    rng = random.Random(4242)
    notices_pool = [
        LIMITATION,
        "You need approval to list in this brand.",
        "Used conditions: limited",
        "Add images to improve your listing",
        "You are not eligible to list this product.",
        "",
    ]
    labels = ["Sell this product", "Apply to sell", "Copy listing", None, "sell this product"]
    conditions = ["new_new", "used_good", None]
    seen_listable = seen_not = 0
    for _ in range(6000):
        c = ctx(condition=rng.choice(["new_new", "used_good"]), validated=rng.random() < 0.8)
        sig = BrowserSignals(
            signed_in=rng.random() < 0.9,
            challenge_detected=rng.random() < 0.1,
            page_loaded=rng.random() < 0.9,
            product_unavailable=rng.random() < 0.1,
            page_asin=rng.choice([c.asin, c.asin, "B0OTHER000", None]),
            page_brand=rng.choice([None, None, "eSSL", "Someone Else"]),
            selected_condition=rng.choice(conditions),
            notices=rng.sample(notices_pool, rng.randint(0, 2)),
            action_label=rng.choice(labels),
            action_enabled=rng.choice([True, False, None]),
        )
        v = classify_observation(
            Observation(source=ObservationSource.BROWSER, context=c, browser=sig)
        )
        if v.status != S.VERIFIED_LISTABLE:
            seen_not += 1
            continue
        seen_listable += 1
        assert sig.signed_in and not sig.challenge_detected and sig.page_loaded
        assert not sig.product_unavailable
        assert sig.page_asin == c.asin
        assert sig.page_brand in (None, "eSSL") and (sig.page_brand or c.brand_validated)
        assert sig.selected_condition == c.condition
        assert (sig.action_label or "").casefold() == "sell this product"
        assert sig.action_enabled is True
        for n in sig.notices:
            # The only notice that may be on screen is one about *other* conditions.
            applies_to_other_conditions = n.startswith(
                "Used, Collectible"
            ) and c.condition.startswith("new")
            assert n == "" or applies_to_other_conditions, (n, c.condition)
    assert seen_listable > 20 and seen_not > 1000
