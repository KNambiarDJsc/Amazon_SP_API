import pytest

from app.domain.summary import CheckedAsin, summarize_brand
from app.enums import EligibilityStatus as S


def checks(*pairs):
    return [CheckedAsin(asin=a, condition="new_new", status=s) for a, s in pairs]


def summarize(cs, **kw):
    defaults = dict(
        candidates_total=len(cs),
        candidates_brand_matched=len(cs),
        checks_planned=len(cs),
        min_sample=3,
        exhaustive=False,
    )
    return summarize_brand(cs, **{**defaults, **kw})


def test_one_listable_asin_is_never_a_brand_level_verdict():
    s = summarize(
        checks(("A1", S.VERIFIED_LISTABLE)), candidates_total=40, candidates_brand_matched=40
    )
    assert s.status == S.NEEDS_MANUAL_CHECK
    assert s.rule_id == "BRAND_INSUFFICIENT_SAMPLE"
    assert s.listable_asins == ["A1"]  # the product-level finding is still preserved
    assert not s.sample_sufficient


def test_enough_listable_asins_gives_a_sample_scoped_verdict():
    s = summarize(checks(*[(f"A{i}", S.VERIFIED_LISTABLE) for i in range(3)]))
    assert s.status == S.VERIFIED_LISTABLE
    assert s.rule_id == "BRAND_SAMPLE_LISTABLE"
    assert "sample only" in s.rationale
    assert s.brand_wide_claim is False


def test_exhaustive_check_of_a_tiny_brand_is_sufficient():
    s = summarize(checks(("A1", S.VERIFIED_LISTABLE)), exhaustive=True)
    assert s.status == S.VERIFIED_LISTABLE


def test_incomplete_sample_is_not_reported_as_listable():
    s = summarize(checks(*[(f"A{i}", S.VERIFIED_LISTABLE) for i in range(3)]), checks_planned=5)
    assert s.status == S.NEEDS_MANUAL_CHECK and s.rule_id == "BRAND_INCOMPLETE"


def test_any_disagreement_is_mixed_results():
    s = summarize(
        checks(
            ("A1", S.VERIFIED_LISTABLE),
            ("A2", S.VERIFIED_LISTABLE),
            ("A3", S.BRAND_APPROVAL_REQUIRED),
        )
    )
    assert s.status == S.MIXED_RESULTS
    assert s.status_counts == {"BRAND_APPROVAL_REQUIRED": 1, "VERIFIED_LISTABLE": 2}
    assert s.listable_asins == ["A1", "A2"]


def test_listable_plus_manual_check_is_mixed_not_listable():
    s = summarize(checks(("A1", S.VERIFIED_LISTABLE), ("A2", S.NEEDS_MANUAL_CHECK)))
    assert s.status == S.MIXED_RESULTS


@pytest.mark.parametrize(
    "status",
    [S.BRAND_APPROVAL_REQUIRED, S.OTHER_APPROVAL_REQUIRED, S.NOT_ELIGIBLE, S.NEEDS_MANUAL_CHECK],
)
def test_uniform_non_listable_status_passes_through(status):
    s = summarize(checks(("A1", status), ("A2", status)))
    assert s.status == status
    assert s.listable_asins == []


def test_uniform_blockers_pass_through_even_when_incomplete():
    s = summarize(checks(("A1", S.BRAND_APPROVAL_REQUIRED)), checks_planned=4)
    assert s.status == S.BRAND_APPROVAL_REQUIRED


def test_no_brand_matched_candidates_is_no_product_found():
    s = summarize([], candidates_total=12, candidates_brand_matched=0, checks_planned=0)
    assert s.status == S.NO_PRODUCT_FOUND
    assert "12 catalog candidate" in s.rationale


def test_no_candidates_at_all_is_no_product_found():
    s = summarize([], candidates_total=0, candidates_brand_matched=0, checks_planned=0)
    assert s.status == S.NO_PRODUCT_FOUND


def test_matched_products_but_no_check_results_needs_manual_check():
    s = summarize([], candidates_total=3, candidates_brand_matched=3, checks_planned=3)
    assert s.status == S.NEEDS_MANUAL_CHECK and s.rule_id == "BRAND_NO_CHECK_RESULTS"


def test_multiple_conditions_of_one_asin_count_as_one_asin_for_sample_size():
    cs = [
        CheckedAsin(asin="A1", condition="new_new", status=S.VERIFIED_LISTABLE),
        CheckedAsin(asin="A1", condition="used_good", status=S.VERIFIED_LISTABLE),
        CheckedAsin(asin="A2", condition="new_new", status=S.VERIFIED_LISTABLE),
        CheckedAsin(asin="A2", condition="used_good", status=S.VERIFIED_LISTABLE),
    ]
    s = summarize(cs, candidates_brand_matched=10, candidates_total=10)
    assert s.asins_checked == 2 and not s.sample_sufficient
    assert s.status == S.NEEDS_MANUAL_CHECK
