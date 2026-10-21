"""Brand processing outcomes: what gets discovered, checked, classified and summarised."""

from app.amazon.errors import AmazonRequestError
from app.amazon.mock import (
    NO_RESTRICTIONS,
    MockAmazonClient,
    brand_gated,
    catalog_item,
    demo_client,
    reason,
    restrictions_body,
)
from app.enums import BrandMatch, BrandStatus, CheckState, JobStatus, ObservationOutcome
from app.enums import EligibilityStatus as S
from app.services.evidence import reclassify_check


def items(brand, n, *, category="Tools", prefix="B0TEST", title_brand=None):
    return [
        catalog_item(
            f"{prefix}{i:04d}",
            f"{title_brand or brand} product {i}",
            brand,
            category=category,
            category_id=category,  # distinct IDs, as Amazon has, so categories really differ
        )
        for i in range(1, n + 1)
    ]


# ---- the demo dataset: every classification path end to end ----------------------------------


def test_listable_brand_with_a_keyword_only_match_rejected(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["eSSL"])
    h.drain()

    assert h.job(job).status == JobStatus.COMPLETED
    brand = h.brand(job, "eSSL")
    assert brand.status == BrandStatus.COMPLETED
    assert brand.brand_status == S.VERIFIED_LISTABLE
    assert brand.brand_summary["rule_id"] == "BRAND_SAMPLE_LISTABLE"
    assert brand.brand_summary["brand_wide_claim"] is False
    assert sorted(brand.brand_summary["listable_asins"]) == [
        "B084FZXB48",
        "B08PDL14VR",
        "B0B63WV6CW",
        "B0BXM5CGRM",
    ]

    # Every candidate is preserved, the wrong-brand one with the reason it was rejected ...
    cands = {c.asin: c for c in h.candidates(job, "eSSL")}
    assert len(cands) == 5
    assert cands["B0MOCKWR01"].brand_match == BrandMatch.MISMATCH
    assert cands["B0MOCKWR01"].rejection_reason == "BRAND_ATTRIBUTE_MISMATCH"
    assert cands["B0MOCKWR01"].brand_attribute == "Generic Mounts"
    assert not cands["B0MOCKWR01"].selected
    # ... and it was never sent to the eligibility API.
    assert "B0MOCKWR01" not in h.restriction_calls()
    assert sorted(h.restriction_calls()) == sorted(brand.brand_summary["listable_asins"])


def test_brand_approval_required_keeps_evidence_and_apply_link(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["Havells"])
    h.drain()

    assert h.brand(job, "Havells").brand_status == S.BRAND_APPROVAL_REQUIRED
    check = next(c for c in h.checks(job) if c.asin == "B08Y9C5H38")
    assert check.status == S.BRAND_APPROVAL_REQUIRED
    assert check.messages == ["You need approval to list in this brand."]
    assert check.reason_codes == ["APPROVAL_REQUIRED"]
    assert check.approval_links[0]["title"] == "Apply to sell"
    assert check.verification_methods == ["SP_API_RESTRICTIONS"]
    assert check.rule_id == "SPAPI_BRAND_APPROVAL_REQUIRED"


def test_mixed_results_are_not_summarised_as_listable(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["Kangaro"])
    h.drain()
    brand = h.brand(job, "Kangaro")
    assert brand.brand_status == S.MIXED_RESULTS
    assert brand.brand_summary["status_counts"] == {
        "BRAND_APPROVAL_REQUIRED": 1,
        "VERIFIED_LISTABLE": 2,
    }
    assert sorted(brand.brand_summary["listable_asins"]) == ["B0MOCKKG01", "B0MOCKKG02"]


def test_category_approval_is_other_approval(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["Taparia"])
    h.drain()
    assert h.brand(job, "Taparia").brand_status == S.OTHER_APPROVAL_REQUIRED


def test_brand_with_no_catalog_results_is_no_product_found(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["Oddy"])
    h.drain()
    assert h.brand(job, "Oddy").brand_status == S.NO_PRODUCT_FOUND
    assert h.checks(job) == []
    assert h.restriction_calls() == []
    assert h.job(job).status == JobStatus.COMPLETED


def test_only_wrong_brand_results_is_no_product_found_but_candidates_are_kept(make_harness):
    client = MockAmazonClient(
        catalog={"eSSL": items("Somebody Else", 3, title_brand="eSSL compatible")}
    )
    h = make_harness(client)
    job = h.submit(["eSSL"])
    h.drain()
    brand = h.brand(job, "eSSL")
    assert brand.brand_status == S.NO_PRODUCT_FOUND
    assert "3 catalog candidate" in brand.brand_summary["rationale"]
    assert len(h.candidates(job, "eSSL")) == 3
    assert h.restriction_calls() == []


def _truncated_catalog():
    """Page 1 (20 items) holds only 2 Plantex products; 5 more sit on page 2."""
    others = items("Someone Else", 18, prefix="B0OTHR")
    mine_p1 = items("Plantex", 2, prefix="B0PLN")
    mine_p2 = items("Plantex", 5, prefix="B0PLX")
    return others + mine_p1 + mine_p2


ALL_LISTABLE = {f"B0PLN{i:04d}": NO_RESTRICTIONS for i in range(1, 3)} | {
    f"B0PLX{i:04d}": NO_RESTRICTIONS for i in range(1, 6)
}


def test_listable_sample_from_a_truncated_search_is_not_a_brand_verdict(make_harness):
    h = make_harness(
        MockAmazonClient(catalog={"Plantex": _truncated_catalog()}, restrictions=ALL_LISTABLE)
    )
    job = h.submit(["Plantex"], max_catalog_pages=1)  # the rest of the brand was never seen
    h.drain()
    brand = h.brand(job, "Plantex")
    assert brand.checkpoint["truncated"] is True
    assert set(h.statuses(job).values()) == {
        "VERIFIED_LISTABLE"
    }  # the two products are real findings
    assert brand.brand_status == S.NEEDS_MANUAL_CHECK
    assert brand.brand_summary["rule_id"] == "BRAND_INSUFFICIENT_SAMPLE"
    assert sorted(brand.brand_summary["listable_asins"]) == ["B0PLN0001", "B0PLN0002"]


def test_same_catalog_read_in_full_gives_a_sample_scoped_verdict(make_harness):
    h = make_harness(
        MockAmazonClient(catalog={"Plantex": _truncated_catalog()}, restrictions=ALL_LISTABLE)
    )
    job = h.submit(["Plantex"], max_catalog_pages=2)
    h.drain()
    brand = h.brand(job, "Plantex")
    assert brand.brand_status == S.VERIFIED_LISTABLE
    assert brand.brand_summary["asins_checked"] == 5  # sampled 5 of 7, not "all of the brand"
    assert brand.brand_summary["candidates_brand_matched"] == 7
    assert brand.brand_summary["brand_wide_claim"] is False


def test_tiny_brand_checked_exhaustively_is_verified(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 1)}, restrictions={"B0TEST0001": NO_RESTRICTIONS}
    )
    h = make_harness(client)
    job = h.submit(["Oddy"])
    h.drain()
    assert h.brand(job, "Oddy").brand_status == S.VERIFIED_LISTABLE


def test_a_min_sample_of_one_chosen_by_the_client_is_respected(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 10)}, restrictions={"B0TEST0001": NO_RESTRICTIONS}
    )
    h = make_harness(client)
    job = h.submit(["Oddy"], max_asins_per_brand=1, min_sample_size=1)
    h.drain()
    brand = h.brand(job, "Oddy")
    assert brand.brand_status == S.VERIFIED_LISTABLE
    assert brand.brand_summary["asins_checked"] == 1
    assert brand.brand_summary["candidates_brand_matched"] == 10


def test_unrecognised_asin_is_manual_check_never_listable(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 2)}
    )  # no restriction behaviours at all
    h = make_harness(client)
    job = h.submit(["Oddy"])
    h.drain()
    assert set(h.statuses(job).values()) == {"NEEDS_MANUAL_CHECK"}
    assert h.brand(job, "Oddy").brand_status == S.NEEDS_MANUAL_CHECK


def test_not_eligible(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 1)},
        restrictions={"B0TEST0001": restrictions_body(reason("NOT_ELIGIBLE", "no"))},
    )
    h = make_harness(client)
    job = h.submit(["Oddy"])
    h.drain()
    assert h.statuses(job) == {"B0TEST0001/new_new": "NOT_ELIGIBLE"}
    assert h.brand(job, "Oddy").brand_status == S.NOT_ELIGIBLE


def test_malformed_restrictions_body_is_never_listable(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 1)}, restrictions={"B0TEST0001": {"unexpected": True}}
    )
    h = make_harness(client)
    job = h.submit(["Oddy"])
    h.drain()
    assert h.statuses(job) == {"B0TEST0001/new_new": "NEEDS_MANUAL_CHECK"}
    assert h.checks(job)[0].rule_id == "SPAPI_NO_RESTRICTIONS_FIELD"


# ---- evidence is stored apart from the derived status ----------------------------------------


def test_every_check_has_raw_evidence_and_a_reproducible_status(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["Havells", "Kangaro"])
    h.drain()
    obs = h.observations(job)
    assert len(obs) == len(h.checks(job)) == 5
    for o in obs:
        assert o.raw is not None and "restrictions" in o.raw
        assert o.signals["restrictions_present"] is True
        assert o.evidence_ref == f"db:check_observations/{o.id}"
        assert o.context["marketplace_id"] == "A21TJRUUN4KGV"
        assert o.context["expected_brand"] in ("Havells", "Kangaro")


def test_derived_status_can_always_be_recomputed_from_stored_evidence(make_harness):
    from app.db import session_scope
    from app.models import EligibilityCheck

    h = make_harness(demo_client())
    job = h.submit(["Havells"])
    h.drain()
    check = next(c for c in h.checks(job) if c.asin == "B08Y9C5H38")
    with session_scope() as s:
        row = s.get(EligibilityCheck, check.id)
        row.status = S.VERIFIED_LISTABLE  # corrupt the derived value
        reclassify_check(s, row)
    assert next(c for c in h.checks(job) if c.id == check.id).status == S.BRAND_APPROVAL_REQUIRED


# ---- discovery -------------------------------------------------------------------------------


def test_multi_page_discovery_respects_the_page_limit(make_harness):
    client = MockAmazonClient(catalog={"Plantex": items("Plantex", 45)})
    h = make_harness(client)
    job = h.submit(["Plantex"], max_catalog_pages=2, max_asins_per_brand=5)
    h.drain()
    assert len(client.calls_to("searchCatalogItems")) == 2
    assert len(h.candidates(job, "Plantex")) == 40
    brand = h.brand(job, "Plantex")
    assert brand.checkpoint["truncated"] is True
    assert brand.brand_summary["sample_sufficient"] is True


def test_selected_asins_span_categories_and_are_ranked(make_harness):
    catalog = items("Plantex", 3, category="Tools") + items(
        "Plantex", 2, category="Garden", prefix="B0GARD"
    )
    h = make_harness(MockAmazonClient(catalog={"Plantex": catalog}))
    job = h.submit(["Plantex"], max_asins_per_brand=3)
    h.drain()
    chosen = sorted((c for c in h.candidates(job, "Plantex") if c.selected), key=lambda c: c.rank)
    assert [c.category_name for c in chosen] == ["Tools", "Garden", "Tools"]
    assert [c.rank for c in chosen] == [1, 2, 3]


def test_multiple_conditions_are_checked_independently(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 1)},
        restrictions={
            ("B0TEST0001", "new_new"): NO_RESTRICTIONS,
            ("B0TEST0001", "used_good"): brand_gated("B0TEST0001", "used_good"),
        },
    )
    h = make_harness(client)
    job = h.submit(["Oddy"], conditions=["new_new", "used_good"])
    h.drain()
    assert h.statuses(job) == {
        "B0TEST0001/new_new": "VERIFIED_LISTABLE",
        "B0TEST0001/used_good": "BRAND_APPROVAL_REQUIRED",
    }
    assert h.brand(job, "Oddy").brand_status == S.MIXED_RESULTS
    assert len({c.check_key for c in h.checks(job)}) == 2


def test_check_rows_exist_only_for_selected_matches(make_harness):
    h = make_harness(demo_client())
    job = h.submit(["eSSL"])
    h.drain()
    assert {c.asin for c in h.checks(job)} == {
        "B08PDL14VR",
        "B0BXM5CGRM",
        "B084FZXB48",
        "B0B63WV6CW",
    }
    assert all(c.state == CheckState.DONE and c.status for c in h.checks(job))


# ---- one ASIN's failure is isolated ----------------------------------------------------------


def test_a_rejected_request_for_one_asin_goes_to_manual_review_and_others_continue(make_harness):
    client = MockAmazonClient(
        catalog={"Oddy": items("Oddy", 3)},
        restrictions={
            "B0TEST0001": NO_RESTRICTIONS,
            "B0TEST0002": AmazonRequestError(400, "InvalidInput: bad ASIN"),
            "B0TEST0003": NO_RESTRICTIONS,
        },
    )
    h = make_harness(client)
    job = h.submit(["Oddy"])
    h.drain()
    assert h.statuses(job) == {
        "B0TEST0001/new_new": "VERIFIED_LISTABLE",
        "B0TEST0002/new_new": "NEEDS_MANUAL_CHECK",
        "B0TEST0003/new_new": "VERIFIED_LISTABLE",
    }
    failed = next(o for o in h.observations(job) if o.outcome == ObservationOutcome.HTTP_ERROR)
    assert failed.http_status == 400 and "InvalidInput" in failed.error_detail
    assert h.brand(job, "Oddy").brand_status == S.MIXED_RESULTS
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
