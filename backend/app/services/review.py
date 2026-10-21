"""Human-assisted verification and the manual-review queue.

A reviewer (or the optional browser adapter) records what Seller Central *showed*. The rules engine
derives the status from that, exactly as it does for API evidence: nobody can type a verdict.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.observations import BrowserSignals, Observation, ObservationContext
from app.enums import (
    BrandMatch,
    BrandStatus,
    CheckState,
    EligibilityStatus,
    ObservationOutcome,
    ObservationSource,
)
from app.models import CatalogCandidate, EligibilityCheck, Job, JobBrand, SellerAccount
from app.schemas import JobSettings
from app.services import audit
from app.services.brands import compute_brand_summary
from app.services.evidence import add_observation, reclassify_check


class ReviewError(Exception):
    """The request cannot be applied to the current state of the check; safe to show."""


def refresh_brand_summary(session: Session, brand: JobBrand, job: Job) -> None:
    """After a check's status changed, bring a *finished* brand's summary back in line.

    Dead-lettered (FAILED) brands keep their override: only a retry resolves those."""
    if brand.status != BrandStatus.COMPLETED:
        return
    summary = compute_brand_summary(session, brand, JobSettings.model_validate(job.settings))
    brand.brand_status = summary.status
    brand.brand_summary = summary.model_dump(mode="json")


def record_screen_evidence(
    session: Session,
    check: EligibilityCheck,
    *,
    source: ObservationSource,
    observer: str,
    signals: BrowserSignals | None,
    outcome: ObservationOutcome = ObservationOutcome.OK,
    error_detail: str | None = None,
    note: str | None = None,
    evidence_ref: str | None = None,
    extra_raw: dict[str, Any] | None = None,
) -> tuple[EligibilityStatus | None, EligibilityStatus]:
    """Append screen evidence to a completed check and re-derive its status.
    Returns (previous status, new status)."""
    if check.state != CheckState.DONE:
        raise ReviewError("This check has not completed yet; wait for the API result first.")
    brand = session.get(JobBrand, check.job_brand_id)
    job = session.get(Job, check.job_id)
    seller = session.get(SellerAccount, check.seller_account_id)
    candidate = session.get(CatalogCandidate, check.candidate_id)

    context = ObservationContext(
        asin=check.asin,
        marketplace_id=check.marketplace_id,
        condition=check.condition,
        seller_ref=seller.amazon_seller_id or "",
        expected_brand=brand.display_name,
        brand_validated=candidate.brand_match in (BrandMatch.EXACT, BrandMatch.NORMALIZED),
    )
    previous = check.status
    raw = {"observer": observer, **(extra_raw or {})}
    if note:
        raw["note"] = note
    add_observation(
        session,
        check,
        Observation(
            source=source,
            outcome=outcome,
            context=context,
            browser=signals,
            error_detail=error_detail,
            raw=raw,
            evidence_ref=evidence_ref,
        ),
    )
    result = reclassify_check(session, check)
    refresh_brand_summary(session, brand, job)
    audit.record(
        session,
        seller_account_id=check.seller_account_id,
        job_id=check.job_id,
        job_brand_id=check.job_brand_id,
        check_id=check.id,
        event_type="MANUAL_EVIDENCE_ADDED"
        if source == ObservationSource.MANUAL
        else "BROWSER_EVIDENCE_ADDED",
        observer=observer,
        source=source.value,
        previous_status=previous.value if previous else None,
        new_status=result.status.value,
        rule_id=result.rule_id,
    )
    return previous, result.status


def get_check(session: Session, job_id: uuid.UUID, check_id: uuid.UUID) -> EligibilityCheck:
    check = session.scalar(
        select(EligibilityCheck).where(
            EligibilityCheck.id == check_id, EligibilityCheck.job_id == job_id
        )
    )
    if check is None:
        raise ReviewError("Check not found.")
    return check
