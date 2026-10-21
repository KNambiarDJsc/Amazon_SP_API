"""Brand-level state transitions and summaries."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.observations import utcnow
from app.domain.summary import BrandSummary, CheckedAsin, summarize_brand
from app.enums import BrandMatch, BrandStatus, CheckState, EligibilityStatus
from app.models import CatalogCandidate, EligibilityCheck, JobBrand
from app.schemas import JobSettings


def compute_brand_summary(session: Session, brand: JobBrand, settings: JobSettings) -> BrandSummary:
    candidates_total = session.scalar(
        select(func.count())
        .select_from(CatalogCandidate)
        .where(CatalogCandidate.job_brand_id == brand.id)
    )
    matched = session.scalar(
        select(func.count())
        .select_from(CatalogCandidate)
        .where(
            CatalogCandidate.job_brand_id == brand.id,
            CatalogCandidate.brand_match.in_([BrandMatch.EXACT, BrandMatch.NORMALIZED]),
        )
    )
    selected = session.scalar(
        select(func.count())
        .select_from(CatalogCandidate)
        .where(CatalogCandidate.job_brand_id == brand.id, CatalogCandidate.selected.is_(True))
    )
    rows = session.execute(
        select(EligibilityCheck.asin, EligibilityCheck.condition, EligibilityCheck.status).where(
            EligibilityCheck.job_brand_id == brand.id, EligibilityCheck.state == CheckState.DONE
        )
    ).all()
    planned = session.scalar(
        select(func.count())
        .select_from(EligibilityCheck)
        .where(EligibilityCheck.job_brand_id == brand.id)
    )
    discovery_complete = bool(brand.checkpoint.get("discovery_complete"))
    return summarize_brand(
        [CheckedAsin(asin=a, condition=c, status=s) for a, c, s in rows],
        candidates_total=candidates_total,
        candidates_brand_matched=matched,
        checks_planned=planned,
        min_sample=settings.min_sample_size,
        # Every matching ASIN was checked AND the search was not cut short by the page limit.
        exhaustive=discovery_complete
        and not brand.checkpoint.get("truncated", False)
        and matched <= selected,
        discovery_complete=discovery_complete,
    )


def complete_brand(session: Session, brand: JobBrand, summary: BrandSummary) -> None:
    brand.status = BrandStatus.COMPLETED
    brand.brand_status = summary.status
    brand.brand_summary = summary.model_dump(mode="json")
    brand.finished_at = utcnow()
    brand.lease_owner = None
    brand.lease_expires_at = None
    brand.queued_at = None
    brand.last_error = None


def dead_letter_brand(
    session: Session, brand: JobBrand, *, reason: str, settings: JobSettings
) -> None:
    """Retries exhausted. Per-ASIN results already stored are kept; the brand itself is routed
    to manual review and is never reported as verified."""
    summary = compute_brand_summary(session, brand, settings)
    summary = summary.model_copy(
        update={
            "status": EligibilityStatus.NEEDS_MANUAL_CHECK,
            "rule_id": "BRAND_DEAD_LETTERED",
            "rationale": (
                f"Processing failed repeatedly ({reason}). Partial result: {summary.rationale}"
            ),
        }
    )
    complete_brand(session, brand, summary)
    brand.status = BrandStatus.FAILED
    brand.last_error = reason
