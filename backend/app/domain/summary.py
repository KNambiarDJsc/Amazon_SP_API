"""Conservative brand-level summary computed from individually checked ASINs.

A summary describes only the ASINs that were actually checked. It never claims a brand
is "ungated" everywhere: even a fully listable sample says so about that sample.
"""

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, Field

from app.domain.classification import RULES_VERSION
from app.enums import EligibilityStatus


class CheckedAsin(BaseModel):
    asin: str
    condition: str
    status: EligibilityStatus


class BrandSummary(BaseModel):
    status: EligibilityStatus
    rule_id: str
    rationale: str
    candidates_total: int
    candidates_brand_matched: int
    checks_planned: int
    checks_completed: int
    asins_checked: int
    sample_sufficient: bool
    status_counts: dict[str, int] = Field(default_factory=dict)
    listable_asins: list[str] = Field(default_factory=list)
    # Always False. Present so no consumer can mistake a sample result for a brand-wide one.
    brand_wide_claim: bool = False
    rules_version: str = RULES_VERSION


def summarize_brand(
    checks: Sequence[CheckedAsin],
    *,
    candidates_total: int,
    candidates_brand_matched: int,
    checks_planned: int,
    min_sample: int,
    exhaustive: bool,
) -> BrandSummary:
    """`exhaustive` means every brand-matched ASIN Amazon returned was checked and the
    catalog search was not cut short by a page limit."""
    counts = Counter(c.status.value for c in checks)
    asins = {c.asin for c in checks}
    listable = sorted({c.asin for c in checks if c.status == EligibilityStatus.VERIFIED_LISTABLE})
    sample_sufficient = len(asins) >= min_sample or (exhaustive and len(asins) > 0)

    def build(status: EligibilityStatus, rule_id: str, rationale: str) -> BrandSummary:
        return BrandSummary(
            status=status,
            rule_id=rule_id,
            rationale=rationale,
            candidates_total=candidates_total,
            candidates_brand_matched=candidates_brand_matched,
            checks_planned=checks_planned,
            checks_completed=len(checks),
            asins_checked=len(asins),
            sample_sufficient=sample_sufficient,
            status_counts=dict(sorted(counts.items())),
            listable_asins=listable,
        )

    if candidates_brand_matched == 0:
        return build(
            EligibilityStatus.NO_PRODUCT_FOUND,
            "BRAND_NO_MATCHING_PRODUCT",
            f"{candidates_total} catalog candidate(s) found, none whose brand attribute matches.",
        )
    if not checks:
        return build(
            EligibilityStatus.NEEDS_MANUAL_CHECK,
            "BRAND_NO_CHECK_RESULTS",
            "Brand-matched products exist but none could be checked.",
        )

    incomplete = len(checks) < checks_planned
    if len(counts) > 1:
        return build(
            EligibilityStatus.MIXED_RESULTS,
            "BRAND_MIXED",
            "Checked ASINs disagree: "
            + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
            + ".",
        )

    (only_status,) = counts
    status = EligibilityStatus(only_status)
    if status != EligibilityStatus.VERIFIED_LISTABLE:
        return build(
            status, f"BRAND_UNIFORM_{status.value}", f"All {len(checks)} checked: {status.value}."
        )
    if incomplete:
        return build(
            EligibilityStatus.NEEDS_MANUAL_CHECK,
            "BRAND_INCOMPLETE",
            f"{len(checks)} of {checks_planned} planned checks completed; all verified listable so "
            "far, but the sample is not finished.",
        )
    if not sample_sufficient:
        return build(
            EligibilityStatus.NEEDS_MANUAL_CHECK,
            "BRAND_INSUFFICIENT_SAMPLE",
            f"Only {len(asins)} ASIN(s) checked (need {min_sample} or every matching ASIN). "
            "The checked ASINs are listable; nothing can be said about the brand as a whole.",
        )
    return build(
        EligibilityStatus.VERIFIED_LISTABLE,
        "BRAND_SAMPLE_LISTABLE",
        f"All {len(asins)} sampled ASIN(s) verified listable. This describes the sample only, "
        "not every product of the brand.",
    )
