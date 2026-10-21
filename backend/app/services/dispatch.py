"""Windowed dispatch and crash recovery.

The database is the source of truth; queue messages are only wake-up hints. A seller has at
most `window` brands QUEUED or IN_PROGRESS at any time, across all of their jobs, so one
large job cannot starve other sellers and workers are never flooded with parked messages.
A lost message or a crashed worker is repaired by `recover`, which is idempotent.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.domain.observations import utcnow
from app.enums import BrandStatus, JobStatus
from app.models import Job, JobBrand
from app.schemas import JobSettings
from app.services import audit
from app.services.brands import dead_letter_brand
from app.services.jobs import maybe_complete_job

log = logging.getLogger(__name__)


class Dispatcher(Protocol):
    def enqueue_brand(self, brand_id: uuid.UUID, delay_seconds: float = 0.0) -> None: ...

    def enqueue_export(self, export_id: uuid.UUID) -> None: ...


class InlineDispatcher:
    """Records messages instead of sending them; tests and scripts drain them explicitly."""

    def __init__(self) -> None:
        self.messages: list[tuple[uuid.UUID, float]] = []
        self.export_messages: list[uuid.UUID] = []

    def enqueue_export(self, export_id: uuid.UUID) -> None:
        self.export_messages.append(export_id)

    def enqueue_brand(self, brand_id: uuid.UUID, delay_seconds: float = 0.0) -> None:
        self.messages.append((brand_id, delay_seconds))

    def pop(self) -> tuple[uuid.UUID, float] | None:
        return self.messages.pop(0) if self.messages else None


def lock_seller(session: Session, seller_id: uuid.UUID) -> None:
    """Serialise window accounting per seller. PostgreSQL only; SQLite has one writer anyway."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": seller_id.int % (2**63)})


def dispatch_next(
    dispatcher: Dispatcher, seller_id: uuid.UUID, *, window: int, now: datetime | None = None
) -> list[uuid.UUID]:
    """Send as many PENDING brands as the seller's free window allows, oldest job first."""
    now = now or utcnow()
    with session_scope() as session:
        lock_seller(session, seller_id)
        in_flight = session.scalar(
            select(func.count())
            .select_from(JobBrand)
            .where(
                JobBrand.seller_account_id == seller_id,
                JobBrand.status.in_([BrandStatus.QUEUED, BrandStatus.IN_PROGRESS]),
            )
        )
        free = window - in_flight
        if free <= 0:
            return []
        brands = session.scalars(
            select(JobBrand)
            .join(Job, Job.id == JobBrand.job_id)
            .where(
                JobBrand.seller_account_id == seller_id,
                JobBrand.status == BrandStatus.PENDING,
                Job.status == JobStatus.RUNNING,
            )
            .order_by(Job.created_at, JobBrand.position)
            .limit(free)
        ).all()
        for brand in brands:
            brand.status = BrandStatus.QUEUED
            brand.queued_at = now
        ids = [b.id for b in brands]

    sent: list[uuid.UUID] = []
    for brand_id in ids:
        try:
            dispatcher.enqueue_brand(brand_id)
            sent.append(brand_id)
        except Exception:
            log.exception("could not enqueue brand %s; leaving it for recovery", brand_id)
            break
    unsent = [i for i in ids if i not in sent]
    if unsent:
        with session_scope() as session:
            session.execute(
                update(JobBrand)
                .where(JobBrand.id.in_(unsent), JobBrand.status == BrandStatus.QUEUED)
                .values(status=BrandStatus.PENDING, queued_at=None)
            )
    return sent


@dataclass
class RecoveryStats:
    leases_expired: int = 0
    dead_lettered: int = 0
    requeued_stale: int = 0
    dispatched: int = 0
    jobs_completed: int = 0
    exports_purged: int = 0


def recover(
    dispatcher: Dispatcher,
    settings: Settings,
    *,
    seller_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> RecoveryStats:
    """Repair interrupted work and keep every seller's window full. Safe to run any time."""
    now = now or utcnow()
    stats = RecoveryStats()
    with session_scope() as session:
        expired_q = select(JobBrand).where(
            JobBrand.status == BrandStatus.IN_PROGRESS, JobBrand.lease_expires_at < now
        )
        stale_q = select(JobBrand).where(
            JobBrand.status == BrandStatus.QUEUED,
            JobBrand.queued_at < now - timedelta(seconds=settings.queue_stale_seconds),
        )
        if seller_id:
            expired_q = expired_q.where(JobBrand.seller_account_id == seller_id)
            stale_q = stale_q.where(JobBrand.seller_account_id == seller_id)

        for brand in session.scalars(expired_q).all():
            stats.leases_expired += 1
            brand.attempts += 1  # counts toward the poison-pill bound
            brand.lease_owner = None
            brand.lease_expires_at = None
            audit.record(
                session,
                seller_account_id=brand.seller_account_id,
                job_id=brand.job_id,
                job_brand_id=brand.id,
                event_type="LEASE_EXPIRED",
                attempts=brand.attempts,
            )
            if brand.attempts >= settings.max_brand_attempts:
                job = session.get(Job, brand.job_id)
                dead_letter_brand(
                    session,
                    brand,
                    reason="worker repeatedly died or hung",
                    settings=JobSettings.model_validate(job.settings),
                )
                audit.record(
                    session,
                    seller_account_id=brand.seller_account_id,
                    job_id=brand.job_id,
                    job_brand_id=brand.id,
                    event_type="BRAND_DEAD_LETTERED",
                )
                stats.dead_lettered += 1
            else:
                brand.status = BrandStatus.PENDING
                brand.last_error = "Worker lease expired; resuming from checkpoint."

        for brand in session.scalars(stale_q).all():
            brand.status = BrandStatus.PENDING
            brand.queued_at = None
            stats.requeued_stale += 1

    with session_scope() as session:
        q = select(Job.id, Job.seller_account_id).where(Job.status == JobStatus.RUNNING)
        if seller_id:
            q = q.where(Job.seller_account_id == seller_id)
        running = session.execute(q).all()
        for job_id, _ in running:
            if maybe_complete_job(session, job_id):
                stats.jobs_completed += 1

    from app.services.exports import purge

    if seller_id is None:  # housekeeping is global; skip it for seller-scoped calls
        stats.exports_purged = purge(settings)["purged"]

    for seller in {s for _, s in running}:
        stats.dispatched += len(
            dispatch_next(dispatcher, seller, window=settings.seller_max_concurrent_brands, now=now)
        )
    return stats
