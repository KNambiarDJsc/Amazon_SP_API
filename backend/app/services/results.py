"""Brand-level results, computed conservatively from the checks that actually ran."""

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.domain.summary import BrandSummary, CheckedAsin, summarize_brand
from app.enums import BrandMatch, CheckState, EligibilityStatus
from app.models import CatalogCandidate, EligibilityCheck, Job, JobBrand
from app.schemas import JobSettings
from app.services.evidence import classify_and_store

_MATCHED = (BrandMatch.EXACT, BrandMatch.NORMALIZED)


def brand_checks(session: Session, brand: JobBrand) -> list[EligibilityCheck]:
    """The checks covering this brand's sampled ASINs.

    Looked up by ASIN across the job, not by job_brand_id: when two brands in one job match
    the same ASIN (say "e-SSL" and "eSSL") only one check exists for it, and both brands
    must see its result.
    """
    sampled = select(CatalogCandidate.asin).where(
        CatalogCandidate.job_brand_id == brand.id, CatalogCandidate.selected.is_(True)
    )
    return list(
        session.scalars(
            select(EligibilityCheck)
            .where(EligibilityCheck.job_id == brand.job_id, EligibilityCheck.asin.in_(sampled))
            .order_by(EligibilityCheck.asin, EligibilityCheck.condition)
        )
    )


def finalize_brand(
    session: Session,
    brand: JobBrand,
    *,
    now: datetime,
    resolve_pending: bool = False,
    discovery_failed: str | None = None,
) -> BrandSummary:
    """Compute and store the brand's derived status.

    `resolve_pending` is for dead-lettering: checks that never got an answer are run through
    the rules engine so they surface as NEEDS_MANUAL_CHECK with their recorded evidence.
    `discovery_failed` covers a brand whose catalog search never succeeded: that is not
    "no product found", so it must not go through the normal summary.
    """
    if discovery_failed is not None:
        summary = BrandSummary(
            status=EligibilityStatus.NEEDS_MANUAL_CHECK,
            rule_id="BRAND_DISCOVERY_FAILED",
            rationale=f"Catalog search did not complete: {discovery_failed}",
            candidates_total=0,
            candidates_brand_matched=0,
            checks_planned=0,
            checks_completed=0,
            asins_checked=0,
            sample_sufficient=False,
        )
    else:
        checks = brand_checks(session, brand)
        if resolve_pending:
            for check in checks:
                if check.state == CheckState.PENDING:
                    classify_and_store(session, check, now)

        job = session.get(Job, brand.job_id)
        settings = JobSettings.model_validate(job.settings)
        candidates_total, matched, selected = session.execute(
            select(
                func.count(CatalogCandidate.id),
                func.coalesce(
                    func.sum(case((CatalogCandidate.brand_match.in_(_MATCHED), 1), else_=0)), 0
                ),
                func.coalesce(func.sum(case((CatalogCandidate.selected.is_(True), 1), else_=0)), 0),
            ).where(CatalogCandidate.job_brand_id == brand.id)
        ).one()
        matched, selected = int(matched), int(selected)

        exhaustive = bool(brand.checkpoint.get("catalog_complete")) and selected == matched
        summary = summarize_brand(
            [
                CheckedAsin(asin=c.asin, condition=c.condition, status=c.status)
                for c in checks
                if c.state == CheckState.DONE and c.status is not None
            ],
            candidates_total=candidates_total,
            candidates_brand_matched=matched,
            checks_planned=len(checks),
            min_sample=settings.min_sample_size,
            exhaustive=exhaustive,
        )

    brand.brand_status = summary.status
    brand.brand_summary = summary.model_dump(mode="json")
    return summary
