"""Brand validation and deterministic selection of representative ASINs. Pure functions."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from app.amazon.types import CatalogItem
from app.domain.brand import match_brand
from app.enums import BrandMatch

MATCHED = (BrandMatch.EXACT, BrandMatch.NORMALIZED)


@dataclass(frozen=True)
class Evaluated:
    item: CatalogItem
    match: BrandMatch

    @property
    def matched(self) -> bool:
        return self.match in MATCHED

    @property
    def rejection_reason(self) -> str | None:
        if self.match == BrandMatch.MISSING:
            return "BRAND_ATTRIBUTE_MISSING"
        if self.match == BrandMatch.MISMATCH:
            return "BRAND_ATTRIBUTE_MISMATCH"
        return None


def evaluate(searched_brand: str, items: Sequence[CatalogItem]) -> list[Evaluated]:
    """Compare each item's brand *attribute* with the searched brand.

    A keyword hit in the title is not a match. Sales rank is deliberately ignored.
    """
    return [Evaluated(item, match_brand(searched_brand, item.brand)) for item in items]


def _category_key(item: CatalogItem) -> str:
    return (item.category_id or item.category_name or "").casefold()


def select_representatives(candidates: Sequence[Evaluated], limit: int) -> list[Evaluated]:
    """Pick up to `limit` brand-matched ASINs, spreading across categories.

    Deterministic and independent of the order Amazon returned results in: categories are
    visited largest first (ties by key), one ASIN per category per round, and within a
    category exact brand matches come before normalised ones, then ASIN order.
    The returned list is in rank order.
    """
    groups: dict[str, list[Evaluated]] = defaultdict(list)
    for candidate in candidates:
        if candidate.matched:
            groups[_category_key(candidate.item)].append(candidate)
    for members in groups.values():
        members.sort(key=lambda c: (c.match != BrandMatch.EXACT, c.item.asin))
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    selected: list[Evaluated] = []
    depth = 0
    while len(selected) < limit and any(depth < len(members) for _, members in ordered):
        for _, members in ordered:
            if depth < len(members) and len(selected) < limit:
                selected.append(members[depth])
        depth += 1
    return selected
