"""Deterministic ranking and sampling of brand-matched catalog candidates. Pure functions.

Sales rank and Amazon's own result order are deliberately not inputs: neither is stable
between runs, and neither says anything about eligibility. The same candidates always
produce the same sample.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.brand import clean_whitespace
from app.enums import BrandMatch

_TIER = {BrandMatch.EXACT: 0, BrandMatch.NORMALIZED: 1}


@dataclass(frozen=True)
class Rankable:
    asin: str
    category: str | None
    brand_match: BrandMatch


def _category_key(category: str | None) -> tuple[int, str]:
    cleaned = clean_whitespace(category).casefold() if category else ""
    return (1, "") if not cleaned else (0, cleaned)  # uncategorised products sort last


def rank_matched(items: Sequence[Rankable]) -> list[Rankable]:
    """Order brand-matched products so that taking a prefix gives a spread across categories.

    Products are grouped by category and dealt out round-robin, so a sample of three from a
    brand selling in three categories covers all three. Inside a category an exact brand
    match outranks a punctuation-normalised one, then ASIN breaks ties.
    """
    matched = [i for i in items if i.brand_match in _TIER]
    groups: defaultdict[tuple[int, str], list[Rankable]] = defaultdict(list)
    for item in matched:
        groups[_category_key(item.category)].append(item)

    ordered_groups = [
        sorted(groups[key], key=lambda i: (_TIER[i.brand_match], i.asin)) for key in sorted(groups)
    ]
    ranked: list[Rankable] = []
    depth = 0
    while len(ranked) < len(matched):
        for group in ordered_groups:
            if depth < len(group):
                ranked.append(group[depth])
        depth += 1
    return ranked


def select_sample(ranked: Sequence[Rankable], limit: int) -> list[Rankable]:
    return list(ranked[:limit])
