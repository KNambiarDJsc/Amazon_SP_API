"""Helpers shared by the orchestration tests: a controllable clock, a runtime wired to it, and
readers that return detached rows so assertions never share a transaction with the code
under test."""

import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.amazon.mock import MockAmazonClient
from app.config import Settings
from app.db import session_scope
from app.enums import DataSource
from app.models import (
    AuditEvent,
    CatalogCandidate,
    CheckObservation,
    EligibilityCheck,
    Job,
    JobBrand,
)
from app.schemas import JobSettings
from app.services.ingestion import rows_from_list
from app.services.jobs import create_job
from app.services.orchestrator import Runtime
from app.services.ratelimit import InMemoryRateLimiter


class FakeClock:
    """Time only moves when a test (or a worker's sleep) moves it."""

    def __init__(self, start: datetime = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)):
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    sleep = advance

    def monotonic(self) -> float:
        return self.current.timestamp()


class Crash(BaseException):
    """Stands in for a killed process: it is not an Exception, so nothing in the worker
    swallows it and no clean-up code gets to run."""


def make_settings(**overrides) -> Settings:
    values = dict(
        database_url="sqlite://",
        amazon_mode="mock",
        seller_max_concurrent_brands=2,
        queue_lease_seconds=120,
        brand_lease_seconds=300,
        max_brand_attempts=3,
        backoff_base_seconds=10.0,
        backoff_cap_seconds=60.0,
        max_rate_wait_seconds=3.0,
    )
    values.update(overrides)
    return Settings(**values)


def make_runtime(client: MockAmazonClient, clock: FakeClock, **settings) -> Runtime:
    return Runtime(
        client=client,
        limiter=InMemoryRateLimiter(clock.monotonic),
        settings=make_settings(**settings),
        clock=clock.now,
        sleep=clock.sleep,
        rng=random.Random(7),
    )


def submit(
    seller,
    brands: list[str],
    clock: FakeClock,
    *,
    data_source: DataSource = DataSource.MOCK,
    **job_settings,
) -> uuid.UUID:
    with session_scope() as session:
        job = create_job(
            session,
            seller=seller,
            rows=rows_from_list(brands),
            settings=JobSettings(**job_settings),
            data_source=data_source,
            now=clock.now(),
        )
        return job.id


# ---- readers -------------------------------------------------------------------------------


def job_of(job_id) -> Job:
    with session_scope() as session:
        return session.get(Job, job_id)


def brands_of(job_id) -> dict[str, JobBrand]:
    with session_scope() as session:
        rows = session.scalars(select(JobBrand).where(JobBrand.job_id == job_id)).all()
        return {b.display_name: b for b in rows}


def checks_of(job_id) -> dict[tuple[str, str], EligibilityCheck]:
    """Keyed by (asin, condition)."""
    with session_scope() as session:
        rows = session.scalars(
            select(EligibilityCheck).where(EligibilityCheck.job_id == job_id)
        ).all()
        return {(c.asin, c.condition): c for c in rows}


def candidates_of(job_brand_id) -> dict[str, CatalogCandidate]:
    with session_scope() as session:
        rows = session.scalars(
            select(CatalogCandidate).where(CatalogCandidate.job_brand_id == job_brand_id)
        ).all()
        return {c.asin: c for c in rows}


def observations_of(job_id) -> list[CheckObservation]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(CheckObservation)
                .join(EligibilityCheck, EligibilityCheck.id == CheckObservation.check_id)
                .where(EligibilityCheck.job_id == job_id)
                .order_by(CheckObservation.id)
            )
        )


def audit_types(job_id) -> list[str]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(AuditEvent.event_type)
                .where(AuditEvent.job_id == job_id)
                .order_by(AuditEvent.id)
            )
        )
