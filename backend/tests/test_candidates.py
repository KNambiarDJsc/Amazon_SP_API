import random

from app.amazon.mock import catalog_item
from app.amazon.parsing import parse_catalog_page
from app.domain.candidates import evaluate, select_representatives
from app.enums import MARKETPLACE_ID_IN, BrandMatch


def items(*specs):
    """specs: (asin, brand, category)"""
    raw = [catalog_item(a, f"Title {a}", b, category=c, category_id=c) for a, b, c in specs]
    return parse_catalog_page({"items": raw}, MARKETPLACE_ID_IN).items


def test_title_keyword_hits_and_other_brands_are_rejected_with_reasons():
    ev = evaluate(
        "eSSL",
        items(
            ("B000000001", "eSSL", "Electronics"),
            ("B000000002", "ESSL", "Electronics"),
            ("B000000003", "e-SSL", "Electronics"),
            ("B000000004", "Generic Mounts", "Electronics"),
            ("B000000005", None, "Electronics"),
        ),
    )
    assert [e.match for e in ev] == [
        BrandMatch.EXACT,
        BrandMatch.EXACT,
        BrandMatch.NORMALIZED,
        BrandMatch.MISMATCH,
        BrandMatch.MISSING,
    ]
    assert [e.rejection_reason for e in ev] == [
        None,
        None,
        None,
        "BRAND_ATTRIBUTE_MISMATCH",
        "BRAND_ATTRIBUTE_MISSING",
    ]


def test_selection_ignores_rejected_candidates():
    ev = evaluate("eSSL", items(("B000000001", "Other", "A"), ("B000000002", "eSSL", "A")))
    assert [e.item.asin for e in select_representatives(ev, 5)] == ["B000000002"]


def test_selection_spreads_across_categories_largest_first():
    ev = evaluate(
        "X",
        items(
            ("B000000001", "X", "Tools"),
            ("B000000002", "X", "Tools"),
            ("B000000003", "X", "Tools"),
            ("B000000004", "X", "Office"),
            ("B000000005", "X", "Office"),
            ("B000000006", "X", "Toys"),
        ),
    )
    picked = [e.item.asin for e in select_representatives(ev, 4)]
    # round 1: Tools, Office, Toys (one each); round 2: Tools
    assert picked == ["B000000001", "B000000004", "B000000006", "B000000002"]


def test_exact_matches_outrank_normalised_ones_within_a_category():
    ev = evaluate("eSSL", items(("B000000001", "e-SSL", "A"), ("B000000009", "eSSL", "A")))
    assert [e.item.asin for e in select_representatives(ev, 2)] == ["B000000009", "B000000001"]


def test_selection_is_independent_of_the_order_amazon_returned_results():
    base = items(
        *[(f"B00000{i:04d}", "X", cat) for i, cat in enumerate(["A", "B", "A", "C", "B", "A", "D"])]
    )
    expected = [e.item.asin for e in select_representatives(evaluate("X", base), 5)]
    rng = random.Random(7)
    for _ in range(50):
        shuffled = base[:]
        rng.shuffle(shuffled)
        assert [e.item.asin for e in select_representatives(evaluate("X", shuffled), 5)] == expected


def test_limit_and_fewer_candidates_than_limit():
    ev = evaluate("X", items(("B000000001", "X", "A"), ("B000000002", "X", "A")))
    assert len(select_representatives(ev, 1)) == 1
    assert len(select_representatives(ev, 10)) == 2
    assert select_representatives([], 3) == []
