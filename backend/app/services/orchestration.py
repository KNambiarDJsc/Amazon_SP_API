"""Entry points that combine a state change with the dispatch that must follow it.

Each commits its database change *before* anything is enqueued, so a worker that picks up
a message always sees the state it was sent for.
"""

import uuid

from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.enums import BrandStatus, CheckState, DataSource, JobStatus
from app.models import EligibilityCheck, JobBrand, SellerAccount
from app.schemas import JobSettings
from app.services import audit, jobs
from app.services.dispatch import Dispatcher, dispatch_next, recover
from app.services.ingestion import IngestionResult


def submit_job(
    dispatcher: Dispatcher,
    cfg: Settings,
    seller_id: uuid.UUID,
    *,
    ingestion: IngestionResult,
    name: str | None,
    settings: JobSettings,
    data_source: DataSource,
    source_filename: str | None = None,
) -> uuid.UUID:
    with session_scope() as session:
        seller = session.get(SellerAccount, seller_id)
        job = jobs.create_job(
            session,
            seller,
            ingestion=ingestion,
            name=name,
            settings=settings,
            data_source=data_source,
            source_filename=source_filename,
        )
        job_id = job.id
    dispatch_next(dispatcher, seller_id, window=cfg.seller_max_concurrent_brands)
    return job_id


def pause(seller_id: uuid.UUID, job_id: uuid.UUID, reason: str = "USER_REQUESTED") -> None:
    with session_scope() as session:
        jobs.pause_job(session, jobs.get_job(session, seller_id, job_id), reason)


def resume(dispatcher: Dispatcher, cfg: Settings, seller_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with session_scope() as session:
        jobs.resume_job(session, jobs.get_job(session, seller_id, job_id))
    recover(dispatcher, cfg, seller_id=seller_id)  # also repairs anything interrupted meanwhile


def cancel(seller_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with session_scope() as session:
        jobs.cancel_job(session, jobs.get_job(session, seller_id, job_id))


_BUSY = (BrandStatus.PENDING, BrandStatus.QUEUED, BrandStatus.IN_PROGRESS)


def _reopen_job(job) -> None:
    if job.status == JobStatus.COMPLETED:
        job.status = JobStatus.RUNNING
        job.finished_at = None


def _owned_brand(session, seller_id: uuid.UUID, job_id: uuid.UUID, brand_id: uuid.UUID) -> JobBrand:
    brand = session.scalar(
        select(JobBrand).where(
            JobBrand.id == brand_id,
            JobBrand.job_id == job_id,
            JobBrand.seller_account_id == seller_id,
        )
    )
    if brand is None:
        raise jobs.JobNotFound(str(brand_id))
    return brand


def retry_brand(
    dispatcher: Dispatcher,
    cfg: Settings,
    seller_id: uuid.UUID,
    job_id: uuid.UUID,
    brand_id: uuid.UUID,
    *,
    rediscover: bool = False,
) -> None:
    """Run a finished or dead-lettered brand again. Completed checks are never repeated; a brand
    that failed part-way continues from its checkpoint. `rediscover` searches the catalog again."""
    with session_scope() as session:
        job = jobs.get_job(session, seller_id, job_id)
        if job.status == JobStatus.CANCELLED:
            raise jobs.JobStateError("cannot retry a brand of a cancelled job")
        brand = _owned_brand(session, seller_id, job_id, brand_id)
        if brand.status in _BUSY:
            raise jobs.JobStateError("this brand is already queued or running")
        brand.status = BrandStatus.PENDING
        brand.attempts = 0
        brand.last_error = None
        brand.finished_at = None
        brand.lease_owner = brand.lease_expires_at = brand.queued_at = None
        if rediscover or not brand.checkpoint.get("discovery_complete"):
            brand.checkpoint = {}
        else:
            brand.checkpoint = {
                k: v for k, v in brand.checkpoint.items() if k != "throttle_deferrals"
            }
        _reopen_job(job)
        audit.record(
            session,
            seller_account_id=seller_id,
            job_id=job_id,
            job_brand_id=brand_id,
            event_type="BRAND_RETRY_REQUESTED",
            rediscover=rediscover,
        )
    dispatch_next(dispatcher, seller_id, window=cfg.seller_max_concurrent_brands)


def recheck_check(
    dispatcher: Dispatcher,
    cfg: Settings,
    seller_id: uuid.UUID,
    job_id: uuid.UUID,
    check_id: uuid.UUID,
) -> None:
    """Ask the API again about one ASIN. Earlier observations stay as history; the new API
    observation supersedes the old one, and any screen evidence is combined with it."""
    with session_scope() as session:
        job = jobs.get_job(session, seller_id, job_id)
        if job.status == JobStatus.CANCELLED:
            raise jobs.JobStateError("cannot recheck a check of a cancelled job")
        check = session.scalar(
            select(EligibilityCheck).where(
                EligibilityCheck.id == check_id,
                EligibilityCheck.job_id == job_id,
                EligibilityCheck.seller_account_id == seller_id,
            )
        )
        if check is None:
            raise jobs.JobNotFound(str(check_id))
        brand = session.get(JobBrand, check.job_brand_id)
        if brand.status in _BUSY:
            raise jobs.JobStateError("this brand is already queued or running")
        if check.state != CheckState.DONE:
            raise jobs.JobStateError("this check has not completed yet")
        check.state = CheckState.PENDING
        check.status = None
        check.rule_id = check.rationale = check.rules_version = check.checked_at = None
        check.reason_codes, check.messages, check.approval_links = [], [], []
        check.verification_methods = []
        check.attempts = 0
        brand.status = BrandStatus.PENDING
        brand.attempts = 0
        brand.finished_at = None
        brand.lease_owner = brand.lease_expires_at = brand.queued_at = None
        brand.last_error = None
        _reopen_job(job)
        audit.record(
            session,
            seller_account_id=seller_id,
            job_id=job_id,
            job_brand_id=brand.id,
            check_id=check_id,
            event_type="CHECK_RECHECK_REQUESTED",
        )
    dispatch_next(dispatcher, seller_id, window=cfg.seller_max_concurrent_brands)
