"""Job lifecycle: creation, pause/resume/cancel, progress, completion."""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.domain.observations import utcnow
from app.enums import (
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    JobStatus,
)
from app.models import EligibilityCheck, Job, JobBrand, SellerAccount
from app.schemas import JobProgress, JobSettings
from app.services import audit
from app.services.ingestion import IngestionResult

ACTIVE_BRAND_STATES = (BrandStatus.PENDING, BrandStatus.QUEUED, BrandStatus.IN_PROGRESS)
RESTRICTED = (
    EligibilityStatus.BRAND_APPROVAL_REQUIRED,
    EligibilityStatus.OTHER_APPROVAL_REQUIRED,
    EligibilityStatus.NOT_ELIGIBLE,
)


class JobNotFound(Exception):
    pass


class JobStateError(Exception):
    """The requested transition is not allowed from the job's current state."""


def create_job(
    session: Session,
    seller: SellerAccount,
    *,
    ingestion: IngestionResult,
    name: str | None,
    settings: JobSettings,
    data_source: DataSource,
    source_filename: str | None = None,
) -> Job:
    now = utcnow()
    job = Job(
        seller_account_id=seller.id,
        name=name,
        status=JobStatus.RUNNING,
        data_source=data_source,
        settings=settings.model_dump(mode="json"),
        ingestion_report=ingestion.report.model_dump(mode="json"),
        source_filename=source_filename,
        started_at=now,
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
        )
        for b in ingestion.accepted
    )
    audit.record(
        session,
        seller_account_id=seller.id,
        job_id=job.id,
        event_type="JOB_CREATED",
        brands=len(ingestion.accepted),
        duplicates=len(ingestion.report.duplicates),
        invalid=len(ingestion.report.invalid),
        empty=len(ingestion.report.empty_rows),
        data_source=data_source.value,
    )
    return job


def get_job(session: Session, seller_id: uuid.UUID, job_id: uuid.UUID) -> Job:
    """Jobs of other sellers are indistinguishable from jobs that do not exist."""
    job = session.scalar(select(Job).where(Job.id == job_id, Job.seller_account_id == seller_id))
    if job is None:
        raise JobNotFound(str(job_id))
    return job


def pause_job(session: Session, job: Job, reason: str = "USER_REQUESTED") -> None:
    if job.status == JobStatus.PAUSED:
        return
    if job.status != JobStatus.RUNNING:
        raise JobStateError(f"cannot pause a {job.status.value} job")
    job.status = JobStatus.PAUSED
    job.pause_reason = reason
    audit.record(
        session,
        seller_account_id=job.seller_account_id,
        job_id=job.id,
        event_type="JOB_PAUSED",
        reason=reason,
    )


def resume_job(session: Session, job: Job) -> None:
    """Back to RUNNING. Brands that were waiting on a message are made dispatchable again;
    a duplicate message is harmless because claiming a brand is a compare-and-set."""
    if job.status == JobStatus.RUNNING:
        return
    if job.status != JobStatus.PAUSED:
        raise JobStateError(f"cannot resume a {job.status.value} job")
    job.status = JobStatus.RUNNING
    job.pause_reason = None
    session.execute(
        update(JobBrand)
        .where(JobBrand.job_id == job.id, JobBrand.status == BrandStatus.QUEUED)
        .values(status=BrandStatus.PENDING, queued_at=None)
    )
    audit.record(
        session, seller_account_id=job.seller_account_id, job_id=job.id, event_type="JOB_RESUMED"
    )


def cancel_job(session: Session, job: Job) -> None:
    if job.status == JobStatus.CANCELLED:
        return
    if job.status == JobStatus.COMPLETED:
        raise JobStateError("cannot cancel a completed job")
    now = utcnow()
    job.status = JobStatus.CANCELLED
    job.finished_at = now
    # Work not yet started is cancelled now; a brand mid-flight notices at its next checkpoint.
    session.execute(
        update(JobBrand)
        .where(
            JobBrand.job_id == job.id,
            JobBrand.status.in_([BrandStatus.PENDING, BrandStatus.QUEUED]),
        )
        .values(status=BrandStatus.CANCELLED, finished_at=now, queued_at=None)
    )
    audit.record(
        session, seller_account_id=job.seller_account_id, job_id=job.id, event_type="JOB_CANCELLED"
    )


def maybe_complete_job(session: Session, job_id: uuid.UUID) -> bool:
    # Serialise completers on the job row. Without this, two brands finishing at the same instant
    # each see the other as still active (neither has committed) and the job is never completed.
    # The second to take the lock counts after the first has committed. Lock order is always
    # brand row, then job row, so this cannot deadlock with the other writers.
    session.execute(select(Job.id).where(Job.id == job_id).with_for_update())
    active = session.scalar(
        select(func.count())
        .select_from(JobBrand)
        .where(JobBrand.job_id == job_id, JobBrand.status.in_(ACTIVE_BRAND_STATES))
    )
    if active:
        return False
    now = utcnow()
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
        .values(status=JobStatus.COMPLETED, finished_at=now)
    )
    if result.rowcount != 1:  # already completed by another worker, paused, or cancelled
        return False
    seller_id = session.scalar(select(Job.seller_account_id).where(Job.id == job_id))
    audit.record(session, seller_account_id=seller_id, job_id=job_id, event_type="JOB_COMPLETED")
    return True


def job_progress(session: Session, job_id: uuid.UUID) -> JobProgress:
    brand_counts = dict(
        session.execute(
            select(JobBrand.status, func.count())
            .where(JobBrand.job_id == job_id)
            .group_by(JobBrand.status)
        ).all()
    )
    check_rows = session.execute(
        select(EligibilityCheck.state, EligibilityCheck.status, func.count())
        .where(EligibilityCheck.job_id == job_id)
        .group_by(EligibilityCheck.state, EligibilityCheck.status)
    ).all()

    by_status: dict[str, int] = {}
    checks_total = checks_done = 0
    for state, status, n in check_rows:
        checks_total += n
        if state == CheckState.DONE:
            checks_done += n
            by_status[status.value] = by_status.get(status.value, 0) + n

    total = sum(brand_counts.values())
    finished = sum(
        brand_counts.get(s, 0)
        for s in (BrandStatus.COMPLETED, BrandStatus.FAILED, BrandStatus.CANCELLED)
    )
    return JobProgress(
        brands_total=total,
        brands_pending=brand_counts.get(BrandStatus.PENDING, 0)
        + brand_counts.get(BrandStatus.QUEUED, 0),
        brands_in_progress=brand_counts.get(BrandStatus.IN_PROGRESS, 0),
        brands_completed=brand_counts.get(BrandStatus.COMPLETED, 0),
        brands_failed=brand_counts.get(BrandStatus.FAILED, 0),
        brands_cancelled=brand_counts.get(BrandStatus.CANCELLED, 0),
        checks_total=checks_total,
        checks_done=checks_done,
        verified_listable=by_status.get(EligibilityStatus.VERIFIED_LISTABLE.value, 0),
        restricted=sum(by_status.get(s.value, 0) for s in RESTRICTED),
        needs_manual_check=by_status.get(EligibilityStatus.NEEDS_MANUAL_CHECK.value, 0),
        by_status=dict(sorted(by_status.items())),
        percent_complete=round(100 * finished / total, 1) if total else 0.0,
    )
