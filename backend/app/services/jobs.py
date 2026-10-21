"""Job lifecycle: creation, pause/resume/cancel, dead-letter retry, and progress.

Every lookup is scoped by seller_account_id. A job that belongs to another seller is
indistinguishable from one that does not exist.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session

from app.enums import (
    BLOCKING_STATUSES,
    AuthorizationStatus,
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    JobStatus,
)
from app.models import EligibilityCheck, Job, JobBrand, SellerAccount, SellerAuthorization
from app.schemas import IngestionReport, JobOut, JobProgress, JobSettings
from app.services.audit import audit
from app.services.evidence import reset_check
from app.services.ingestion import IngestionError, Row, ingest_brands
from app.services.results import brand_checks

MOCK_NOTICE = (
    "DEMO DATA: results come from offline fixtures, not from Amazon. "
    "They say nothing about what this seller can actually list."
)
_OPEN_JOB = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)
_ACTIVE_BRAND = (BrandStatus.PENDING, BrandStatus.IN_PROGRESS)


class JobStateError(Exception):
    """The requested transition is not allowed from the job's current state."""


def get_job(session: Session, seller_account_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
    return session.scalar(
        select(Job).where(Job.id == job_id, Job.seller_account_id == seller_account_id)
    )


def create_job(
    session: Session,
    *,
    seller: SellerAccount,
    rows: Sequence[Row],
    settings: JobSettings,
    data_source: DataSource,
    name: str | None = None,
    source_filename: str | None = None,
    now: datetime,
) -> Job:
    result = ingest_brands(rows)
    if not result.accepted:
        raise IngestionError("no valid brand names found")

    job = Job(
        seller_account_id=seller.id,
        name=name,
        status=JobStatus.QUEUED,
        data_source=data_source,
        source_filename=source_filename,
        settings=settings.model_dump(mode="json"),
        ingestion_report=result.report.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    session.add_all(
        JobBrand(
            job_id=job.id,
            seller_account_id=seller.id,
            position=b.position,
            original_name=b.original_name,
            display_name=b.display_name,
            dedupe_key=b.dedupe_key,
            created_at=now,
        )
        for b in result.accepted
    )
    audit(
        session,
        seller_account_id=seller.id,
        job_id=job.id,
        event_type="job_created",
        brands=len(result.accepted),
        duplicates=len(result.report.duplicates),
        data_source=data_source.value,
    )
    return job


# ---- controls ------------------------------------------------------------------------------


def pause_job(session: Session, job: Job, *, reason: str, now: datetime) -> None:
    if job.status == JobStatus.PAUSED:
        return
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise JobStateError(f"cannot pause a {job.status.value} job")
    job.status = JobStatus.PAUSED
    job.pause_reason = reason
    job.updated_at = now
    audit(
        session,
        seller_account_id=job.seller_account_id,
        job_id=job.id,
        event_type="job_paused",
        reason=reason,
    )


def resume_job(session: Session, job: Job, *, now: datetime) -> None:
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return
    if job.status != JobStatus.PAUSED:
        raise JobStateError(f"cannot resume a {job.status.value} job")
    if job.data_source == DataSource.LIVE and not _authorization_active(
        session, job.seller_account_id
    ):
        raise JobStateError("connect the seller's Amazon account again before resuming")
    job.status = JobStatus.RUNNING if job.started_at else JobStatus.QUEUED
    job.pause_reason = None
    job.updated_at = now
    audit(session, seller_account_id=job.seller_account_id, job_id=job.id, event_type="job_resumed")


def _authorization_active(session: Session, seller_account_id: uuid.UUID) -> bool:
    status = session.scalar(
        select(SellerAuthorization.status).where(
            SellerAuthorization.seller_account_id == seller_account_id
        )
    )
    return status == AuthorizationStatus.ACTIVE


def cancel_job(session: Session, job: Job, *, now: datetime) -> None:
    """Brands not yet started are cancelled here; a brand mid-flight notices at its next
    checkpoint and stops there, keeping the checks it already finished."""
    if job.status == JobStatus.CANCELLED:
        return
    if job.status not in _OPEN_JOB:
        raise JobStateError(f"cannot cancel a {job.status.value} job")
    session.execute(
        update(JobBrand)
        .where(JobBrand.job_id == job.id, JobBrand.status == BrandStatus.PENDING)
        .values(status=BrandStatus.CANCELLED, finished_at=now)
    )
    job.status = JobStatus.CANCELLED
    job.pause_reason = None
    job.finished_at = now
    job.updated_at = now
    audit(
        session, seller_account_id=job.seller_account_id, job_id=job.id, event_type="job_cancelled"
    )


def retry_failed_brands(session: Session, job: Job, *, now: datetime) -> int:
    """Give dead-lettered brands a fresh set of attempts. Their earlier observations stay as
    history; only checks that were left unresolved are asked again."""
    if job.status in (JobStatus.CANCELLED,):
        raise JobStateError("cannot retry brands of a cancelled job")
    failed = list(
        session.scalars(
            select(JobBrand).where(JobBrand.job_id == job.id, JobBrand.status == BrandStatus.FAILED)
        )
    )
    for brand in failed:
        for check in brand_checks(session, brand):
            if check.status == EligibilityStatus.NEEDS_MANUAL_CHECK:
                reset_check(check)
        brand.status = BrandStatus.PENDING
        brand.attempts = 0
        brand.next_attempt_at = None
        brand.last_error = None
        brand.finished_at = None
        brand.brand_status = None
        brand.brand_summary = None
        audit(
            session,
            seller_account_id=job.seller_account_id,
            job_id=job.id,
            job_brand_id=brand.id,
            event_type="brand_retry_requested",
        )
    if failed and job.status == JobStatus.COMPLETED:
        job.status = JobStatus.RUNNING
        job.finished_at = None
        job.updated_at = now
    return len(failed)


def pause_seller_jobs(
    session: Session, seller_account_id: uuid.UUID, *, reason: str, now: datetime
):
    """Authorisation is per seller, so when Amazon rejects it every running job must stop."""
    jobs = session.scalars(
        select(Job).where(
            Job.seller_account_id == seller_account_id,
            Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
    )
    for job in jobs:
        pause_job(session, job, reason=reason, now=now)


def maybe_complete_job(session: Session, job: Job, *, now: datetime) -> bool:
    if job.status != JobStatus.RUNNING:
        return False
    unfinished = session.scalar(
        select(exists().where(JobBrand.job_id == job.id, JobBrand.status.in_(_ACTIVE_BRAND)))
    )
    if unfinished:
        return False
    job.status = JobStatus.COMPLETED
    job.finished_at = now
    job.updated_at = now
    audit(
        session, seller_account_id=job.seller_account_id, job_id=job.id, event_type="job_completed"
    )
    return True


# ---- progress ------------------------------------------------------------------------------


def compute_progress(session: Session, job_id: uuid.UUID) -> JobProgress:
    brand_counts = {
        status: n
        for status, n in session.execute(
            select(JobBrand.status, func.count())
            .where(JobBrand.job_id == job_id)
            .group_by(JobBrand.status)
        )
    }
    check_counts: dict[tuple[CheckState, EligibilityStatus | None], int] = {
        (state, status): n
        for state, status, n in session.execute(
            select(EligibilityCheck.state, EligibilityCheck.status, func.count())
            .where(EligibilityCheck.job_id == job_id)
            .group_by(EligibilityCheck.state, EligibilityCheck.status)
        )
    }

    by_status = {
        status.value: n
        for (state, status), n in check_counts.items()
        if state == CheckState.DONE and status is not None
    }
    brands_total = sum(brand_counts.values())
    finished = sum(
        brand_counts.get(s, 0)
        for s in (BrandStatus.COMPLETED, BrandStatus.FAILED, BrandStatus.CANCELLED)
    )
    return JobProgress(
        brands_total=brands_total,
        brands_pending=brand_counts.get(BrandStatus.PENDING, 0),
        brands_in_progress=brand_counts.get(BrandStatus.IN_PROGRESS, 0),
        brands_completed=brand_counts.get(BrandStatus.COMPLETED, 0),
        brands_failed=brand_counts.get(BrandStatus.FAILED, 0),
        brands_cancelled=brand_counts.get(BrandStatus.CANCELLED, 0),
        checks_total=sum(check_counts.values()),
        checks_done=sum(by_status.values()),
        verified_listable=by_status.get(EligibilityStatus.VERIFIED_LISTABLE.value, 0),
        restricted=sum(by_status.get(s.value, 0) for s in BLOCKING_STATUSES),
        needs_manual_check=by_status.get(EligibilityStatus.NEEDS_MANUAL_CHECK.value, 0),
        by_status=dict(sorted(by_status.items())),
        percent_complete=round(100 * finished / brands_total, 1) if brands_total else 0.0,
    )


def build_job_out(session: Session, job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        name=job.name,
        status=job.status,
        data_source=job.data_source,
        pause_reason=job.pause_reason,
        source_filename=job.source_filename,
        settings=JobSettings.model_validate(job.settings),
        ingestion_report=IngestionReport.model_validate(job.ingestion_report),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress=compute_progress(session, job.id),
        notice=MOCK_NOTICE if job.data_source == DataSource.MOCK else None,
    )
