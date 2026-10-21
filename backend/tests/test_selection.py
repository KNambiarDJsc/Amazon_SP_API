from app.domain.selection import Rankable, rank_matched, select_sample
from app.enums import BrandMatch

EXACT, NORM, WRONG, NONE = (
    BrandMatch.EXACT,
    BrandMatch.NORMALIZED,
    BrandMatch.MISMATCH,
    BrandMatch.MISSING,
)


def r(asin, category, match=EXACT):
    return Rankable(asin=asin, category=category, brand_match=match)


def test_rejected_matches_never_rank():
    ranked = rank_matched([r("A1", "Tools"), r("A2", "Tools", WRONG), r("A3", "Tools", NONE)])
    assert [i.asin for i in ranked] == ["A1"]


def test_sample_spreads_across_categories_before_repeating_one():
    items = [
        r("A1", "Tools"),
        r("A2", "Tools"),
        r("A3", "Tools"),
        r("B1", "Office"),
        r("C1", "Industrial"),
    ]
    sample = select_sample(rank_matched(items), 3)
    assert {i.category for i in sample} == {"Tools", "Office", "Industrial"}


def test_ranking_is_independent_of_input_order():
    items = [
        r("A2", "Tools"),
        r("B1", "Office"),
        r("A1", "Tools"),
        r("C1", None),
        r("B2", "Office"),
    ]
    forward = [i.asin for i in rank_matched(items)]
    backward = [i.asin for i in rank_matched(list(reversed(items)))]
    assert forward == backward


def test_exact_outranks_normalized_within_a_category():
    ranked = rank_matched([r("A1", "Tools", NORM), r("A9", "Tools", EXACT)])
    assert [i.asin for i in ranked] == ["A9", "A1"]


def test_category_names_group_case_and_whitespace_insensitively():
    ranked = rank_matched([r("A1", "Home  Kitchen"), r("A2", "home kitchen"), r("B1", "Office")])
    # One "home kitchen" group and one "office" group, dealt alternately. If the two spellings
    # were treated as different categories this would come out A1, A2, B1.
    assert [i.asin for i in ranked] == ["A1", "B1", "A2"]


def test_uncategorised_products_go_last_but_are_not_dropped():
    ranked = rank_matched([r("Z1", None), r("A1", "Tools"), r("Z2", "")])
    assert [i.asin for i in ranked] == ["A1", "Z1", "Z2"]


def test_limit_larger_than_pool_returns_everything():
    assert len(select_sample(rank_matched([r("A1", "x"), r("A2", "y")]), 10)) == 2


def test_empty_input():
    assert rank_matched([]) == []
