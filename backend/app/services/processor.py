"""Processes one brand: catalog discovery, representative selection, per-ASIN eligibility checks.

Every step commits before the next begins, so a crash at any point resumes from the last
committed checkpoint. Re-running a brand is always safe: candidates upsert by ASIN, checks
are keyed by an idempotency key, and a check that is already DONE is never repeated.

Failure handling, from most to least specific:
  * Amazon says 429               -> defer the brand (bounded), no ASIN is penalised
  * one ASIN keeps failing        -> that ASIN goes to manual review, the brand carries on
  * credentials rejected          -> pause the whole job (retrying cannot help)
  * anything else, repeatedly     -> dead-letter the brand into manual review
Pause and cancel are honoured at every checkpoint.
"""

import logging
import os
import random
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.amazon.client import AmazonClient
from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.amazon.parsing import (
    failure_observation,
    parse_catalog_page,
    parse_restrictions_observation,
)
from app.amazon.types import CatalogItem
from app.config import Settings
from app.db import session_scope
from app.domain.candidates import Evaluated, evaluate, select_representatives
from app.domain.check_key import SPAPI_RESTRICTIONS_CONTEXT, make_check_key
from app.domain.conditions import check_seq
from app.domain.observations import Observation, ObservationContext, utcnow
from app.enums import (
    BrandStatus,
    CheckState,
    DataSource,
    JobStatus,
    ObservationOutcome,
)
from app.logging_config import log_context
from app.models import CatalogCandidate, EligibilityCheck, Job, JobBrand, SellerAccount
from app.observability import BRAND_TASKS, CHECKS_CLASSIFIED, timed_amazon_call
from app.schemas import JobSettings
from app.services import audit
from app.services.brands import complete_brand, compute_brand_summary, dead_letter_brand
from app.services.dispatch import Dispatcher, dispatch_next
from app.services.evidence import add_observation, reclassify_check
from app.services.jobs import maybe_complete_job

log = logging.getLogger(__name__)

ClientFactory = Callable[[SellerAccount], AmazonClient]


@dataclass(frozen=True)
class BrandContext:
    brand_id: uuid.UUID
    job_id: uuid.UUID
    seller: SellerAccount
    seller_ref: str  # the seller ID sent to Amazon
    display_name: str
    settings: JobSettings
    data_source: DataSource


class _Interrupted(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # "paused" | "cancelled" | "lease_lost"


class _Permanent(Exception):
    """Retrying cannot help (e.g. Amazon rejected this brand's search outright)."""


class _Defer(Exception):
    def __init__(self, delay: float, reason: str, *, throttle: bool = False, audited: bool = True):
        super().__init__(reason)
        self.delay = delay
        self.reason = reason
        self.throttle = throttle
        self.audited = audited


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class BrandProcessor:
    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        limiter,
        dispatcher: Dispatcher,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        worker_id: str | None = None,
    ):
        self.client_factory = client_factory
        self.limiter = limiter
        self.dispatcher = dispatcher
        self.cfg = settings
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.worker_id = worker_id or default_worker_id()

    # ---- entry point -------------------------------------------------------------------------

    def process(self, brand_id: uuid.UUID) -> None:
        ctx = self._claim(brand_id)
        if ctx is None:
            return
        with log_context(job_id=ctx.job_id, brand_id=ctx.brand_id, seller=str(ctx.seller.id)[:8]):
            self._process_claimed(ctx)

    def _process_claimed(self, ctx: BrandContext) -> None:
        brand_id = ctx.brand_id
        try:
            self._run(ctx)
        except _Interrupted as stop:
            BRAND_TASKS.labels(stop.kind).inc()  # paused | cancelled | lease_lost
            self._on_interrupt(ctx, stop)
        except _Defer as defer:
            BRAND_TASKS.labels("deferred").inc()
            self._on_defer(ctx, defer)
        except AmazonAuthError as exc:
            BRAND_TASKS.labels("auth_paused").inc()
            self._on_auth_failure(ctx, exc)
        except _Permanent as exc:
            self._on_permanent(ctx, str(exc))
        except Exception as exc:  # noqa: BLE001 - last line of defence; classified and bounded
            log.exception("brand %s failed", brand_id)
            self._on_failure(ctx, exc)
        finally:
            dispatch_next(
                self.dispatcher,
                ctx.seller.id,
                window=self.cfg.seller_max_concurrent_brands,
                now=self.clock(),
            )

    # ---- claiming and leases -----------------------------------------------------------------

    def _claim(self, brand_id: uuid.UUID) -> BrandContext | None:
        now = self.clock()
        with session_scope() as s:
            claimed = s.execute(
                update(JobBrand)
                .where(JobBrand.id == brand_id, JobBrand.status == BrandStatus.QUEUED)
                .values(
                    status=BrandStatus.IN_PROGRESS,
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.cfg.brand_lease_seconds),
                    started_at=func.coalesce(JobBrand.started_at, now),
                    queued_at=None,
                )
            )
            if claimed.rowcount != 1:
                return None  # already handled; a duplicate or stale message
            brand = s.get(JobBrand, brand_id)
            job = s.get(Job, brand.job_id)
            seller = s.get(SellerAccount, brand.seller_account_id)
            if job.status != JobStatus.RUNNING:
                brand.status = (
                    BrandStatus.CANCELLED
                    if job.status == JobStatus.CANCELLED
                    else BrandStatus.PENDING
                )
                if brand.status == BrandStatus.CANCELLED:
                    brand.finished_at = now
                brand.lease_owner = brand.lease_expires_at = None
                return None
            seller_ref = seller.amazon_seller_id or ""
            if not seller_ref:
                if job.data_source == DataSource.LIVE:
                    seller_ref = ""  # refused in _run: a live job needs a connected account
                else:
                    seller_ref = f"MOCK-{str(seller.id)[:8]}"
            return BrandContext(
                brand_id=brand.id,
                job_id=job.id,
                seller=seller,
                seller_ref=seller_ref,
                display_name=brand.display_name,
                settings=JobSettings.model_validate(job.settings),
                data_source=job.data_source,
            )

    def _guard(self, ctx: BrandContext) -> None:
        """Heartbeat the lease and honour pause/cancel. Called before every Amazon request."""
        now = self.clock()
        with session_scope() as s:
            beat = s.execute(
                update(JobBrand)
                .where(
                    JobBrand.id == ctx.brand_id,
                    JobBrand.status == BrandStatus.IN_PROGRESS,
                    JobBrand.lease_owner == self.worker_id,
                )
                .values(lease_expires_at=now + timedelta(seconds=self.cfg.brand_lease_seconds))
            )
            if beat.rowcount != 1:
                raise _Interrupted("lease_lost")
            status = s.scalar(select(Job.status).where(Job.id == ctx.job_id))
        if status == JobStatus.PAUSED:
            raise _Interrupted("paused")
        if status != JobStatus.RUNNING:
            raise _Interrupted("cancelled")

    # ---- pacing ------------------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        base = min(self.cfg.backoff_cap_seconds, self.cfg.backoff_base_seconds * 2 ** (attempt - 1))
        return base * (0.8 + 0.4 * self.jitter())  # +/-20% so retries do not synchronise

    def _throttle(self, ctx: BrandContext, operation: str, rate: float, burst: int) -> None:
        decision = self.limiter.acquire(
            f"{ctx.seller.id}:{operation}",
            rate=rate,
            burst=burst,
            max_wait=self.cfg.max_rate_wait_seconds,
        )
        if not decision.granted:
            raise _Defer(max(decision.wait, 1.0), f"local rate limit on {operation}", audited=False)
        if decision.wait > 0:
            self.sleep(decision.wait)

    # ---- the work ----------------------------------------------------------------------------

    def _run(self, ctx: BrandContext) -> None:
        if ctx.data_source == DataSource.LIVE and not ctx.seller_ref:
            raise AmazonAuthError("This seller account is not connected to Amazon yet.")
        client = self.client_factory(ctx.seller)
        with session_scope() as s:
            checkpoint = dict(s.get(JobBrand, ctx.brand_id).checkpoint)
        if not checkpoint.get("discovery_complete"):
            self._discover(ctx, client, checkpoint)
        self._check_candidates(ctx, client)
        self._finish(ctx)

    def _discover(self, ctx: BrandContext, client: AmazonClient, checkpoint: dict) -> None:
        token = checkpoint.get("catalog_next_token")
        pages = checkpoint.get("pages_fetched", 0)
        while True:
            self._guard(ctx)
            self._throttle(
                ctx,
                "searchCatalogItems",
                self.cfg.rate_search_catalog_per_sec,
                self.cfg.rate_search_catalog_burst,
            )
            try:
                body = timed_amazon_call(
                    "searchCatalogItems",
                    client.search_catalog_items,
                    keywords=ctx.display_name,
                    marketplace_id=ctx.seller.marketplace_id,
                    page_token=token,
                )
            except AmazonRateLimited as exc:
                raise self._throttled(exc) from exc
            except AmazonRequestError as exc:
                raise _Permanent(
                    f"Catalog search rejected (HTTP {exc.status}): {exc.detail}"
                ) from exc
            page = parse_catalog_page(body, ctx.seller.marketplace_id)
            token, pages = page.next_token, pages + 1
            with session_scope() as s:
                brand = self._fenced_brand(s, ctx)
                self._save_candidates(s, ctx, page.items)
                brand.checkpoint = {
                    **brand.checkpoint,
                    "catalog_next_token": token,
                    "pages_fetched": pages,
                }
                brand.attempts = 0  # progress was made: only consecutive failures count
            if not token or pages >= ctx.settings.max_catalog_pages:
                break
        with session_scope() as s:
            self._fenced_brand(s, ctx)
            self._select_and_plan(s, ctx, truncated=bool(token))

    def _save_candidates(self, s, ctx: BrandContext, items: list[CatalogItem]) -> None:
        known = set(
            s.scalars(
                select(CatalogCandidate.asin).where(CatalogCandidate.job_brand_id == ctx.brand_id)
            )
        )
        for evaluated in evaluate(ctx.display_name, items):
            item = evaluated.item
            if item.asin in known:
                continue
            known.add(item.asin)
            s.add(
                CatalogCandidate(
                    job_brand_id=ctx.brand_id,
                    seller_account_id=ctx.seller.id,
                    asin=item.asin,
                    marketplace_id=item.marketplace_id,
                    title=item.title,
                    brand_attribute=item.brand,
                    category_name=item.category_name,
                    category_id=item.category_id,
                    product_type=item.product_type,
                    brand_match=evaluated.match,
                    rejection_reason=evaluated.rejection_reason,
                    raw=item.raw,
                )
            )

    def _select_and_plan(self, s, ctx: BrandContext, *, truncated: bool) -> None:
        rows = s.scalars(
            select(CatalogCandidate).where(CatalogCandidate.job_brand_id == ctx.brand_id)
        ).all()
        by_asin = {r.asin: r for r in rows}
        evaluated = [
            Evaluated(
                CatalogItem(
                    asin=r.asin,
                    brand=r.brand_attribute,
                    marketplace_id=r.marketplace_id,
                    category_id=r.category_id,
                    category_name=r.category_name,
                ),
                r.brand_match,
            )
            for r in rows
        ]
        picked = select_representatives(evaluated, ctx.settings.max_asins_per_brand)
        existing_keys = set(
            s.scalars(
                select(EligibilityCheck.check_key).where(
                    EligibilityCheck.job_brand_id == ctx.brand_id
                )
            )
        )
        position = s.get(JobBrand, ctx.brand_id).position
        for rank, pick in enumerate(picked, start=1):
            row = by_asin[pick.item.asin]
            row.selected, row.rank = True, rank
            for condition in ctx.settings.conditions:
                key = make_check_key(
                    seller_account_id=str(ctx.seller.id),
                    marketplace_id=row.marketplace_id,
                    asin=row.asin,
                    condition=condition,
                    verification_context=SPAPI_RESTRICTIONS_CONTEXT,
                )
                if key in existing_keys:
                    continue
                s.add(
                    EligibilityCheck(
                        seller_account_id=ctx.seller.id,
                        job_id=ctx.job_id,
                        job_brand_id=ctx.brand_id,
                        candidate_id=row.id,
                        asin=row.asin,
                        marketplace_id=row.marketplace_id,
                        condition=condition,
                        verification_context=SPAPI_RESTRICTIONS_CONTEXT,
                        check_key=key,
                        seq=check_seq(position, rank, condition),
                    )
                )
        brand = s.get(JobBrand, ctx.brand_id)
        brand.checkpoint = {**brand.checkpoint, "discovery_complete": True, "truncated": truncated}

    def _check_candidates(self, ctx: BrandContext, client: AmazonClient) -> None:
        while True:
            with session_scope() as s:
                check = s.scalars(
                    select(EligibilityCheck)
                    .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
                    .where(
                        EligibilityCheck.job_brand_id == ctx.brand_id,
                        EligibilityCheck.state == CheckState.PENDING,
                    )
                    .order_by(CatalogCandidate.rank, EligibilityCheck.condition)
                    .limit(1)
                ).first()
            if check is None:
                return
            self._guard(ctx)
            self._throttle(
                ctx,
                "getListingsRestrictions",
                self.cfg.rate_restrictions_per_sec,
                self.cfg.rate_restrictions_burst,
            )
            observation = self._observe(ctx, client, check)
            with session_scope() as s:
                brand = self._fenced_brand(s, ctx)
                fresh = s.get(EligibilityCheck, check.id)
                add_observation(s, fresh, observation)
                result = reclassify_check(s, fresh)
                CHECKS_CLASSIFIED.labels(result.status.value).inc()
                brand.attempts = 0  # progress was made: only consecutive failures count

    def _observe(
        self, ctx: BrandContext, client: AmazonClient, check: EligibilityCheck
    ) -> Observation:
        context = ObservationContext(
            asin=check.asin,
            marketplace_id=check.marketplace_id,
            condition=check.condition,
            seller_ref=ctx.seller_ref,
            expected_brand=ctx.display_name,
            brand_validated=True,  # only brand-matched candidates are ever selected for checking
        )
        try:
            body = timed_amazon_call(
                "getListingsRestrictions",
                client.get_listings_restrictions,
                asin=check.asin,
                seller_id=ctx.seller_ref,
                marketplace_id=check.marketplace_id,
                condition=check.condition,
            )
            return parse_restrictions_observation(body, context=context, http_status=200)
        except AmazonRateLimited as exc:
            raise self._throttled(exc) from exc
        except AmazonRequestError as exc:  # this ASIN's request is rejected for good
            return failure_observation(
                outcome=ObservationOutcome.HTTP_ERROR,
                context=context,
                http_status=exc.status,
                detail=exc.detail[:500],
            )
        except (AmazonTransientError, AmazonMalformedResponse) as exc:
            attempts = self._bump_check_attempts(check.id, str(exc))
            if attempts < self.cfg.max_check_attempts:
                raise _Defer(
                    self._backoff(attempts), f"transient error on {check.asin}: {exc}"
                ) from exc
            outcome = (
                ObservationOutcome.MALFORMED_RESPONSE
                if isinstance(exc, AmazonMalformedResponse)
                else ObservationOutcome.TIMEOUT
            )
            return failure_observation(
                outcome=outcome,
                context=context,
                detail=f"gave up after {attempts} attempts: {exc}"[:500],
            )

    def _bump_check_attempts(self, check_id: uuid.UUID, error: str) -> int:
        with session_scope() as s:
            check = s.get(EligibilityCheck, check_id)
            check.attempts += 1
            check.last_error = error[:500]
            return check.attempts

    @staticmethod
    def _throttled(exc: AmazonRateLimited) -> _Defer:
        return _Defer(exc.retry_after or 1.0, "Amazon rate limit (HTTP 429)", throttle=True)

    def _fenced_brand(self, s, ctx: BrandContext) -> JobBrand:
        """Load the brand for update and prove this worker still owns it, in the same transaction
        as the write that follows. A worker whose lease expired must not write anything: another
        worker may already be running the brand. The row lock (PostgreSQL) stops the sweeper
        from reassigning it between this check and the commit."""
        brand = s.get(JobBrand, ctx.brand_id, with_for_update=True, populate_existing=True)
        if not self._owns(brand):
            raise _Interrupted("lease_lost")
        return brand

    def _owns(self, brand: JobBrand) -> bool:
        return brand.status == BrandStatus.IN_PROGRESS and brand.lease_owner == self.worker_id

    def _finish(self, ctx: BrandContext) -> None:
        with session_scope() as s:
            brand = self._fenced_brand(s, ctx)
            summary = compute_brand_summary(s, brand, ctx.settings)
            complete_brand(s, brand, summary)
            BRAND_TASKS.labels("completed").inc()
            audit.record(
                s,
                seller_account_id=ctx.seller.id,
                job_id=ctx.job_id,
                job_brand_id=ctx.brand_id,
                event_type="BRAND_COMPLETED",
                brand_status=summary.status.value,
                asins_checked=summary.asins_checked,
            )
            maybe_complete_job(s, ctx.job_id)

    # ---- outcomes other than success ---------------------------------------------------------

    def _release(self, s, ctx: BrandContext, status: BrandStatus, **values) -> bool:
        """Leave IN_PROGRESS, but only if this worker still owns the lease."""
        result = s.execute(
            update(JobBrand)
            .where(
                JobBrand.id == ctx.brand_id,
                JobBrand.status == BrandStatus.IN_PROGRESS,
                JobBrand.lease_owner == self.worker_id,
            )
            .values(status=status, lease_owner=None, lease_expires_at=None, **values)
        )
        return result.rowcount == 1

    def _on_interrupt(self, ctx: BrandContext, stop: _Interrupted) -> None:
        if stop.kind == "lease_lost":
            return  # another worker owns this brand now; touching it would corrupt its state
        with session_scope() as s:
            if stop.kind == "paused":
                self._release(s, ctx, BrandStatus.PENDING)
            else:
                self._release(s, ctx, BrandStatus.CANCELLED, finished_at=self.clock())

    def _on_defer(self, ctx: BrandContext, defer: _Defer) -> None:
        now = self.clock()
        with session_scope() as s:
            brand = s.get(JobBrand, ctx.brand_id)
            if not self._owns(brand):
                return
            throttles = brand.checkpoint.get("throttle_deferrals", 0) + (1 if defer.throttle else 0)
            if throttles > self.cfg.max_throttle_deferrals:
                self._dead_letter(s, ctx, "Amazon kept rate-limiting this seller")
                return
            delay = defer.delay
            if defer.throttle:
                delay = max(delay, self._backoff(throttles))
            released = self._release(
                s,
                ctx,
                BrandStatus.QUEUED,
                queued_at=now,
                next_attempt_at=now + timedelta(seconds=delay),
                checkpoint={**brand.checkpoint, "throttle_deferrals": throttles},
            )
            if not released:
                return
            if defer.audited:
                audit.record(
                    s,
                    seller_account_id=ctx.seller.id,
                    job_id=ctx.job_id,
                    job_brand_id=ctx.brand_id,
                    event_type="BRAND_DEFERRED",
                    reason=defer.reason,
                    delay_seconds=round(delay, 1),
                )
        self._enqueue_or_park(ctx.brand_id, delay)

    def _enqueue_or_park(self, brand_id: uuid.UUID, delay: float) -> None:
        try:
            self.dispatcher.enqueue_brand(brand_id, delay)
        except Exception:
            log.exception("could not re-enqueue brand %s; recovery will pick it up", brand_id)
            with session_scope() as s:
                s.execute(
                    update(JobBrand)
                    .where(JobBrand.id == brand_id, JobBrand.status == BrandStatus.QUEUED)
                    .values(status=BrandStatus.PENDING, queued_at=None)
                )

    def _on_auth_failure(self, ctx: BrandContext, exc: Exception) -> None:
        """Credentials are missing, expired or revoked. Retrying every brand would only burn
        quota, so the whole job pauses until the seller reconnects and resumes it."""
        with session_scope() as s:
            self._release(s, ctx, BrandStatus.PENDING, last_error=str(exc)[:500])
            paused = s.execute(
                update(Job)
                .where(Job.id == ctx.job_id, Job.status == JobStatus.RUNNING)
                .values(status=JobStatus.PAUSED, pause_reason=f"AUTH_REQUIRED: {exc}"[:200])
            )
            if paused.rowcount == 1:
                audit.record(
                    s,
                    seller_account_id=ctx.seller.id,
                    job_id=ctx.job_id,
                    event_type="JOB_PAUSED_AUTH_ERROR",
                    detail=str(exc)[:500],
                )

    def _on_failure(self, ctx: BrandContext, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"[:500]
        now = self.clock()
        with session_scope() as s:
            brand = s.get(JobBrand, ctx.brand_id)
            if not self._owns(brand):
                return
            attempts = brand.attempts + 1
            if attempts >= self.cfg.max_brand_attempts:
                brand.attempts = attempts
                self._dead_letter(s, ctx, detail)
                return
            delay = self._backoff(attempts)
            self._release(
                s,
                ctx,
                BrandStatus.QUEUED,
                attempts=attempts,
                last_error=detail,
                queued_at=now,
                next_attempt_at=now + timedelta(seconds=delay),
            )
            BRAND_TASKS.labels("retry").inc()
            audit.record(
                s,
                seller_account_id=ctx.seller.id,
                job_id=ctx.job_id,
                job_brand_id=ctx.brand_id,
                event_type="BRAND_RETRY",
                attempt=attempts,
                delay_seconds=round(delay, 1),
                error=detail,
            )
        self._enqueue_or_park(ctx.brand_id, delay)

    def _on_permanent(self, ctx: BrandContext, reason: str) -> None:
        with session_scope() as s:
            if self._owns(s.get(JobBrand, ctx.brand_id)):
                self._dead_letter(s, ctx, reason[:500])

    def _dead_letter(self, s, ctx: BrandContext, reason: str) -> None:
        brand = s.get(JobBrand, ctx.brand_id)
        dead_letter_brand(s, brand, reason=reason, settings=ctx.settings)
        BRAND_TASKS.labels("dead_lettered").inc()
        audit.record(
            s,
            seller_account_id=ctx.seller.id,
            job_id=ctx.job_id,
            job_brand_id=ctx.brand_id,
            event_type="BRAND_DEAD_LETTERED",
            reason=reason,
        )
        maybe_complete_job(s, ctx.job_id)
