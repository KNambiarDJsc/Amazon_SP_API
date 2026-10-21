"""Read-side queries for the API. Callers pass a job they already fetched through
services.jobs.get_job, which is what scopes everything here to one seller."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.observations import ApprovalLink
from app.enums import BrandStatus, CheckState, EligibilityStatus
from app.models import (
    AuditEvent,
    CatalogCandidate,
    CheckObservation,
    EligibilityCheck,
    Job,
    JobBrand,
)
from app.schemas import (
    BrandOut,
    CandidateOut,
    CheckDetailOut,
    CheckOut,
    ObservationOut,
)


def like_pattern(text: str) -> str:
    """A contains-pattern in which the user's own % and _ are literals."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_jobs(
    session: Session, seller_account_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Job], int]:
    base = select(Job).where(Job.seller_account_id == seller_account_id)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset))
    return list(rows), total or 0


def list_brands(
    session: Session,
    job: Job,
    *,
    status: BrandStatus | None,
    result: EligibilityStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[BrandOut], int]:
    stmt = select(JobBrand).where(JobBrand.job_id == job.id)
    if status is not None:
        stmt = stmt.where(JobBrand.status == status)
    if result is not None:
        stmt = stmt.where(JobBrand.brand_status == result)
    if q:
        stmt = stmt.where(JobBrand.display_name.ilike(like_pattern(q), escape="\\"))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(stmt.order_by(JobBrand.position).limit(limit).offset(offset))
    return [BrandOut.model_validate(b) for b in rows], total or 0


def _check_out(check: EligibilityCheck, brand_name: str, candidate: CatalogCandidate) -> CheckOut:
    return CheckOut(
        id=check.id,
        job_brand_id=check.job_brand_id,
        brand=brand_name,
        asin=check.asin,
        title=candidate.title,
        marketplace_id=check.marketplace_id,
        condition=check.condition,
        state=check.state,
        status=check.status,
        rule_id=check.rule_id,
        rationale=check.rationale,
        reason_codes=list(check.reason_codes or []),
        messages=list(check.messages or []),
        approval_links=[ApprovalLink.model_validate(x) for x in check.approval_links or []],
        verification_methods=list(check.verification_methods or []),
        checked_at=check.checked_at,
    )


def list_checks(
    session: Session,
    job: Job,
    *,
    status: EligibilityStatus | None,
    state: CheckState | None,
    brand_id: uuid.UUID | None,
    q: str | None,
    limit: int,
    offset: int,
) -> tuple[list[CheckOut], int]:
    stmt = (
        select(EligibilityCheck, JobBrand.display_name, CatalogCandidate)
        .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
        .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
        .where(EligibilityCheck.job_id == job.id)
    )
    if status is not None:
        stmt = stmt.where(EligibilityCheck.status == status)
    if state is not None:
        stmt = stmt.where(EligibilityCheck.state == state)
    if brand_id is not None:
        stmt = stmt.where(EligibilityCheck.job_brand_id == brand_id)
    if q:
        pattern = like_pattern(q)
        stmt = stmt.where(
            or_(
                EligibilityCheck.asin.ilike(pattern, escape="\\"),
                CatalogCandidate.title.ilike(pattern, escape="\\"),
                JobBrand.display_name.ilike(pattern, escape="\\"),
            )
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.execute(
        stmt.order_by(JobBrand.position, EligibilityCheck.asin, EligibilityCheck.condition)
        .limit(limit)
        .offset(offset)
    )
    return [_check_out(c, name, cand) for c, name, cand in rows], total or 0


def get_check_detail(session: Session, job: Job, check_id: uuid.UUID) -> CheckDetailOut | None:
    row = session.execute(
        select(EligibilityCheck, JobBrand.display_name, CatalogCandidate)
        .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
        .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
        .where(EligibilityCheck.id == check_id, EligibilityCheck.job_id == job.id)
    ).first()
    if row is None:
        return None
    check, brand_name, candidate = row
    observations = session.scalars(
        select(CheckObservation)
        .where(CheckObservation.check_id == check.id)
        .order_by(CheckObservation.observed_at, CheckObservation.id)
    )
    base = _check_out(check, brand_name, candidate)
    return CheckDetailOut(
        **base.model_dump(),
        rules_version=check.rules_version,
        candidate=CandidateOut.model_validate(candidate),
        observations=[ObservationOut.model_validate(o) for o in observations],
    )


def list_audit(session: Session, job: Job, *, limit: int, offset: int) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.job_id == job.id)
            .order_by(AuditEvent.id)
            .limit(limit)
            .offset(offset)
        )
    )
