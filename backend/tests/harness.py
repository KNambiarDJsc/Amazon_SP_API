"""Deterministic orchestration harness: fake clock, in-memory queue, injectable jitter."""

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
    SellerAccount,
)
from app.schemas import JobSettings
from app.services import orchestration
from app.services.dispatch import InlineDispatcher, dispatch_next, recover
from app.services.ingestion import ingest, rows_from_list
from app.services.processor import BrandProcessor
from app.services.rate_limit import InMemoryTokenBucket


class SimulatedCrash(BaseException):
    """Stands in for a killed worker: it is not an Exception, so nothing in the code under
    test is allowed to catch and tidy up after it."""


class FakeClock:
    def __init__(self):
        # Starts at the real current time because some timestamps (created_at defaults) are real.
        self.t = datetime.now(UTC)

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.t.timestamp()

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class Harness:
    def __init__(self, seller: SellerAccount, client: MockAmazonClient, **overrides):
        defaults = dict(
            database_url="sqlite://",
            seller_max_concurrent_brands=2,
            rate_search_catalog_per_sec=1000,
            rate_search_catalog_burst=1000,
            rate_restrictions_per_sec=1000,
            rate_restrictions_burst=1000,
            brand_lease_seconds=300,
            queue_stale_seconds=900,
            max_brand_attempts=3,
        )
        self.cfg = Settings(**{**defaults, **overrides})
        self.seller = seller
        self.client = client
        self.clock = FakeClock()
        self.dispatcher = InlineDispatcher()
        self.limiter = InMemoryTokenBucket(self.clock.monotonic)
        self.processor = BrandProcessor(
            client_factory=lambda _seller: self.client,
            limiter=self.limiter,
            dispatcher=self.dispatcher,
            settings=self.cfg,
            clock=self.clock.now,
            sleep=self.clock.advance,
            jitter=lambda: 0.5,  # exactly 1.0x backoff
            worker_id="worker-1",
        )
        self.delays: list[float] = []

    # ---- driving ---------------------------------------------------------------------------
    def submit(self, brands, *, seller=None, data_source=DataSource.MOCK, **settings) -> uuid.UUID:
        seller = seller or self.seller
        ingestion = ingest(rows_from_list(brands), max_brands=1000, max_length=200)
        return orchestration.submit_job(
            self.dispatcher,
            self.cfg,
            seller.id,
            ingestion=ingestion,
            name="test",
            settings=JobSettings(**settings),
            data_source=data_source,
        )

    def drain(self, limit: int = 500, on_step=None) -> int:
        steps = 0
        while (msg := self.dispatcher.pop()) is not None:
            brand_id, delay = msg
            self.delays.append(delay)
            self.clock.advance(delay)
            if on_step:
                on_step()
            self.processor.process(brand_id)
            steps += 1
            assert steps <= limit, "queue never drained"
        return steps

    def recover(self):
        return recover(self.dispatcher, self.cfg, now=self.clock.now())

    def dispatch(self, seller=None):
        return dispatch_next(
            self.dispatcher,
            (seller or self.seller).id,
            window=self.cfg.seller_max_concurrent_brands,
        )

    # ---- inspection ------------------------------------------------------------------------
    def job(self, job_id) -> Job:
        with session_scope() as s:
            return s.get(Job, job_id)

    def brands(self, job_id) -> dict[str, JobBrand]:
        with session_scope() as s:
            rows = s.scalars(select(JobBrand).where(JobBrand.job_id == job_id)).all()
        return {b.display_name: b for b in rows}

    def brand(self, job_id, name) -> JobBrand:
        return self.brands(job_id)[name]

    def checks(self, job_id, brand_name=None) -> list[EligibilityCheck]:
        with session_scope() as s:
            q = select(EligibilityCheck).where(EligibilityCheck.job_id == job_id)
            if brand_name:
                q = q.join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id).where(
                    JobBrand.display_name == brand_name
                )
            return list(s.scalars(q.order_by(EligibilityCheck.asin, EligibilityCheck.condition)))

    def statuses(self, job_id, brand_name=None) -> dict[str, str]:
        return {
            f"{c.asin}/{c.condition}": (c.status.value if c.status else None)
            for c in self.checks(job_id, brand_name)
        }

    def candidates(self, job_id, brand_name) -> list[CatalogCandidate]:
        brand = self.brand(job_id, brand_name)
        with session_scope() as s:
            return list(
                s.scalars(select(CatalogCandidate).where(CatalogCandidate.job_brand_id == brand.id))
            )

    def observations(self, job_id) -> list[CheckObservation]:
        with session_scope() as s:
            return list(
                s.scalars(
                    select(CheckObservation)
                    .join(EligibilityCheck, EligibilityCheck.id == CheckObservation.check_id)
                    .where(EligibilityCheck.job_id == job_id)
                )
            )

    def events(self, job_id, event_type=None) -> list[AuditEvent]:
        with session_scope() as s:
            q = select(AuditEvent).where(AuditEvent.job_id == job_id)
            if event_type:
                q = q.where(AuditEvent.event_type == event_type)
            return list(s.scalars(q.order_by(AuditEvent.id)))

    def restriction_calls(self) -> list[str]:
        return [c["asin"] for c in self.client.calls_to("getListingsRestrictions")]
