"""Handing brands to workers, and taking them back.

The database is the source of truth; the Redis queue is only a doorbell. A brand's lease says
who is allowed to work on it:

    PENDING --dispatch--> IN_PROGRESS (lease "queued:<token>", short)
            --start-----> IN_PROGRESS (lease "worker:<id>", renewed by heartbeats, attempt counted)
            --complete / retry / dead-letter / release

A lost queue message leaves a "queued" lease that simply expires and is dispatched again. A
worker that dies leaves a "worker" lease that expires and the brand is retried (or
dead-lettered once attempts run out). A duplicate or redelivered message cannot start a brand
that is already running, because only the dispatcher's token turns a queued lease into a
worker lease.

Only the scheduler decides who may run, under a lock on the seller's row, which is what makes
the per-seller concurrency cap hold however many workers exist.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import BrandStatus, JobStatus
from app.models import Job, JobBrand, SellerAccount
from app.services.audit import audit
from app.services.results import finalize_brand

QUEUED_PREFIX = "queued:"
WORKER_PREFIX = "worker:"


class LeaseLost(Exception):
    """This worker no longer holds the brand's lease, so it must stop without writing."""


@dataclass(frozen=True)
class Dispatch:
    brand_id: uuid.UUID
    token: str


def worker_owner(worker_id: str) -> str:
    return WORKER_PREFIX + worker_id


def backoff_delay(attempts: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Exponential backoff with equal jitter: half fixed, half random, never above `cap`."""
    ceiling = min(cap, base * 2 ** max(0, attempts - 1))
    return ceiling / 2 + rng.uniform(0, ceiling / 2)


def _clear_lease(brand: JobBrand) -> None:
    brand.lease_owner = None
    brand.lease_expires_at = None


# ---- dispatch ------------------------------------------------------------------------------


def sellers_with_ready_brands(session: Session, now: datetime) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(JobBrand.seller_account_id)
            .join(
                Job,
                and_(
                    Job.id == JobBrand.job_id,
                    Job.seller_account_id == JobBrand.seller_account_id,
                ),
            )
            .where(
                JobBrand.status == BrandStatus.PENDING,
                or_(JobBrand.next_attempt_at.is_(None), JobBrand.next_attempt_at <= now),
                Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .distinct()
        )
    )


def dispatch_for_seller(
    session: Session,
    seller_account_id: uuid.UUID,
    *,
    now: datetime,
    settings: Settings,
    budget: int,
) -> list[Dispatch]:
    """Take a short lease on up to the seller's free concurrency slots and return them.

    The seller row is locked so two schedulers cannot both see a free slot. (SQLite, used in
    tests and single-process runs, serialises writers itself and ignores the lock.)
    """
    session.execute(
        select(SellerAccount.id).where(SellerAccount.id == seller_account_id).with_for_update()
    )
    active = session.scalar(
        select(func.count())
        .select_from(JobBrand)
        .where(
            JobBrand.seller_account_id == seller_account_id,
            JobBrand.status == BrandStatus.IN_PROGRESS,
            JobBrand.lease_expires_at > now,
        )
    )
    room = min(settings.seller_max_concurrent_brands - (active or 0), budget)
    if room <= 0:
        return []

    ready = session.scalars(
        select(JobBrand)
        .join(
            Job,
            and_(Job.id == JobBrand.job_id, Job.seller_account_id == JobBrand.seller_account_id),
        )
        .where(
            JobBrand.seller_account_id == seller_account_id,
            JobBrand.status == BrandStatus.PENDING,
            or_(JobBrand.next_attempt_at.is_(None), JobBrand.next_attempt_at <= now),
            Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
        )
        .order_by(Job.created_at, JobBrand.position)
        .limit(room)
        .with_for_update(skip_locked=True, of=JobBrand)
    ).all()

    dispatches: list[Dispatch] = []
    for brand in ready:
        token = uuid.uuid4().hex
        brand.status = BrandStatus.IN_PROGRESS
        brand.lease_owner = QUEUED_PREFIX + token
        brand.lease_expires_at = now + timedelta(seconds=settings.queue_lease_seconds)
        brand.next_attempt_at = None
        job = session.get(Job, brand.job_id)
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.RUNNING
            job.started_at = job.started_at or now
            job.updated_at = now
        dispatches.append(Dispatch(brand.id, token))
    return dispatches


# ---- worker side ---------------------------------------------------------------------------


def start_brand(
    session: Session,
    brand_id: uuid.UUID,
    token: str,
    *,
    worker_id: str,
    now: datetime,
    settings: Settings,
) -> JobBrand | None:
    """Turn a dispatch into a running lease. Returns None when the message is stale: the
    brand was already started, reclaimed, re-dispatched under a new token, paused or cancelled."""
    brand = session.scalar(select(JobBrand).where(JobBrand.id == brand_id).with_for_update())
    if (
        brand is None
        or brand.status != BrandStatus.IN_PROGRESS
        or brand.lease_owner != QUEUED_PREFIX + token
    ):
        return None

    job = session.get(Job, brand.job_id)
    if job.status == JobStatus.CANCELLED:
        brand.status = BrandStatus.CANCELLED
        brand.finished_at = now
        _clear_lease(brand)
        return None
    if job.status != JobStatus.RUNNING:  # paused between dispatch and pickup
        brand.status = BrandStatus.PENDING
        _clear_lease(brand)
        return None

    brand.lease_owner = worker_owner(worker_id)
    brand.lease_expires_at = now + timedelta(seconds=settings.brand_lease_seconds)
    brand.attempts += 1
    brand.started_at = brand.started_at or now
    return brand


def heartbeat(
    session: Session, brand_id: uuid.UUID, worker_id: str, *, now: datetime, settings: Settings
) -> JobStatus:
    """Renew the lease and report the job's status. Raises LeaseLost if the lease moved on."""
    renewed = session.execute(
        update(JobBrand)
        .where(
            JobBrand.id == brand_id,
            JobBrand.lease_owner == worker_owner(worker_id),
            JobBrand.status == BrandStatus.IN_PROGRESS,
        )
        .values(lease_expires_at=now + timedelta(seconds=settings.brand_lease_seconds))
    )
    if renewed.rowcount != 1:
        raise LeaseLost(str(brand_id))
    return session.scalar(
        select(Job.status).join(JobBrand, JobBrand.job_id == Job.id).where(JobBrand.id == brand_id)
    )


# ---- ending a lease ------------------------------------------------------------------------


def complete_brand(session: Session, brand: JobBrand, *, now: datetime) -> None:
    brand.status = BrandStatus.COMPLETED
    brand.finished_at = now
    brand.last_error = None
    brand.next_attempt_at = None
    _clear_lease(brand)
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="brand_completed",
        status=brand.brand_status.value if brand.brand_status else None,
    )


def release_brand(
    session: Session,
    brand: JobBrand,
    *,
    refund_attempt: bool,
    retry_at: datetime | None = None,
    reason: str,
) -> None:
    """Put a brand back in the pool without treating it as a failure (pause, throttle)."""
    brand.status = BrandStatus.PENDING
    brand.next_attempt_at = retry_at
    if refund_attempt:
        brand.attempts = max(0, brand.attempts - 1)
    _clear_lease(brand)
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="brand_released",
        reason=reason,
        retry_at=retry_at.isoformat() if retry_at else None,
    )


def cancel_brand(session: Session, brand: JobBrand, *, now: datetime) -> None:
    brand.status = BrandStatus.CANCELLED
    brand.finished_at = now
    _clear_lease(brand)
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="brand_cancelled",
    )


def dead_letter_brand(session: Session, brand: JobBrand, *, error: str, now: datetime) -> None:
    """Retries are over. The brand is parked as FAILED and whatever evidence exists is run
    through the rules engine so it lands in manual review instead of vanishing."""
    discovered = brand.checkpoint.get("stage") == "CHECKING"
    finalize_brand(
        session,
        brand,
        now=now,
        resolve_pending=True,
        discovery_failed=None if discovered else error,
    )
    brand.status = BrandStatus.FAILED
    brand.last_error = error
    brand.finished_at = now
    brand.next_attempt_at = None
    _clear_lease(brand)
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="brand_failed",
        error=error,
        attempts=brand.attempts,
    )


def retry_or_dead_letter(
    session: Session,
    brand: JobBrand,
    *,
    error: str,
    now: datetime,
    settings: Settings,
    rng: random.Random,
) -> BrandStatus:
    if brand.attempts >= settings.max_brand_attempts:
        dead_letter_brand(session, brand, error=error, now=now)
        return BrandStatus.FAILED

    delay = backoff_delay(
        brand.attempts,
        base=settings.backoff_base_seconds,
        cap=settings.backoff_cap_seconds,
        rng=rng,
    )
    retry_at = now + timedelta(seconds=delay)
    brand.last_error = error
    brand.status = BrandStatus.PENDING
    brand.next_attempt_at = retry_at
    _clear_lease(brand)
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="brand_retry_scheduled",
        error=error,
        attempt=brand.attempts,
        retry_at=retry_at.isoformat(),
    )
    return BrandStatus.PENDING


# ---- recovery ------------------------------------------------------------------------------


def reclaim_expired(
    session: Session, *, now: datetime, settings: Settings, rng: random.Random
) -> int:
    """Recover brands whose lease ran out: lost queue messages and crashed or stalled workers."""
    expired = session.scalars(
        select(JobBrand)
        .where(JobBrand.status == BrandStatus.IN_PROGRESS, JobBrand.lease_expires_at <= now)
        .with_for_update(skip_locked=True)
    ).all()

    for brand in expired:
        job = session.get(Job, brand.job_id)
        owner = brand.lease_owner or ""
        if job.status == JobStatus.CANCELLED:
            cancel_brand(session, brand, now=now)
        elif owner.startswith(QUEUED_PREFIX):
            # Never picked up (message lost or the queue was flushed). Not the brand's fault.
            release_brand(session, brand, refund_attempt=False, reason="dispatch expired")
        else:
            audit(
                session,
                seller_account_id=brand.seller_account_id,
                job_id=brand.job_id,
                job_brand_id=brand.id,
                event_type="lease_expired",
                owner=owner,
            )
            retry_or_dead_letter(
                session,
                brand,
                error="worker stopped responding (crashed or stalled); lease expired",
                now=now,
                settings=settings,
                rng=rng,
            )
    return len(expired)
