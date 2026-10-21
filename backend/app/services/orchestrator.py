"""Runs one brand: discover products, check each ASIN, summarise. Resumable at every step.

Each unit of work (a catalog page, one ASIN check) commits on its own, and every commit is
guarded by the brand's lease. A worker that is killed mid-brand loses nothing it already
committed, and its replacement picks up the remaining checks (check keys make repeating
any step harmless).

What happens when something goes wrong is decided here, once:

    Amazon throttles us / local rate limit   -> brand deferred, attempt refunded
    Amazon rejects the seller's authorisation -> all of the seller's jobs paused, attempt refunded
    timeout, 5xx, network, scheduling bug      -> bounded retry with backoff, then dead-letter
    request rejected (4xx) while searching      -> dead-letter immediately
    request rejected / malformed body on a check -> that check becomes NEEDS_MANUAL_CHECK
    job paused or cancelled                     -> stop at the next checkpoint
    job made in the other data mode (mock/live) -> job paused before anything is asked
"""

import logging
import random
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon.client import AmazonClient
from app.amazon.errors import (
    AmazonAuthError,
    AmazonError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTimeout,
    AmazonTransientError,
)
from app.amazon.factory import ClientProvider, SharedClientProvider
from app.amazon.parsing import (
    failure_observation,
    parse_catalog_page,
    parse_restrictions_observation,
)
from app.amazon.types import CatalogItem
from app.config import Settings
from app.db import session_scope
from app.domain.brand import match_brand
from app.domain.check_key import SPAPI_RESTRICTIONS_CONTEXT, make_check_key
from app.domain.observations import ObservationContext, utcnow
from app.domain.selection import Rankable, rank_matched, select_sample
from app.enums import (
    BrandMatch,
    BrandStatus,
    CheckState,
    DataSource,
    JobStatus,
    ObservationOutcome,
)
from app.models import CatalogCandidate, EligibilityCheck, Job, JobBrand, SellerAccount
from app.schemas import JobSettings
from app.services.audit import audit
from app.services.authorization import mark_needs_reauthorization
from app.services.evidence import classify_and_store, store_observation
from app.services.jobs import maybe_complete_job, pause_job, pause_seller_jobs
from app.services.ratelimit import RateLimiter
from app.services.results import brand_checks, finalize_brand
from app.services.scheduling import (
    LeaseLost,
    cancel_brand,
    complete_brand,
    dead_letter_brand,
    heartbeat,
    release_brand,
    retry_or_dead_letter,
    start_brand,
    worker_owner,
)

log = logging.getLogger(__name__)

MOCK_SELLER_REF = "MOCK-SELLER"
_CATALOG_PAGE_SIZE = 20
_MAX_CONSECUTIVE_TRANSIENT = 2
_MAX_ERROR_LENGTH = 500


@dataclass
class Runtime:
    """Everything the orchestrator depends on, injectable so tests need no network, Redis or
    real clock. Give it either one `client` for every seller (offline demo, tests) or a
    `clients` provider that builds each seller's client from their own grant (live)."""

    limiter: RateLimiter
    settings: Settings
    client: AmazonClient | None = None
    clients: ClientProvider | None = None
    session: Callable[[], AbstractContextManager[Session]] = session_scope
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.clients is None:
            if self.client is None:
                raise ValueError("Runtime needs a client or a client provider")
            self.clients = SharedClientProvider(self.client)


# ---- control-flow signals ------------------------------------------------------------------


class _Defer(Exception):
    def __init__(self, delay: float, reason: str):
        super().__init__(reason)
        self.delay, self.reason = delay, reason


class _Yield(Exception):
    def __init__(self, job_status: JobStatus):
        super().__init__(job_status.value)
        self.job_status = job_status


class _AuthFailed(Exception):
    pass


class _Retry(Exception):
    pass


class _Fatal(Exception):
    pass


class _WrongMode(Exception):
    """The job was created against a different data source than this worker serves."""


@dataclass(frozen=True)
class Plan:
    brand_id: uuid.UUID
    job_id: uuid.UUID
    seller_id: uuid.UUID
    seller_ref: str
    marketplace_id: str
    display_name: str
    settings: JobSettings
    stage: str
    client: AmazonClient


# ---- entry point ---------------------------------------------------------------------------


def run_brand(brand_id, token: str, worker_id: str, rt: Runtime) -> str:
    """Run a dispatched brand to a stopping point and return what happened (for logs/tests)."""
    with rt.session() as session:
        brand = start_brand(
            session, brand_id, token, worker_id=worker_id, now=rt.clock(), settings=rt.settings
        )
        if brand is None:
            return "STALE"

    try:
        _execute(brand_id, worker_id, rt)
        return "COMPLETED"
    except LeaseLost:
        log.warning("lease lost for brand %s; stopping without writing", brand_id)
        return "LEASE_LOST"
    except _Yield as stop:
        return _on_yield(brand_id, worker_id, rt, stop.job_status)
    except _Defer as deferral:
        return _end(rt, brand_id, worker_id, _defer, deferral, "DEFERRED")
    except _AuthFailed as failure:
        return _end(rt, brand_id, worker_id, _auth_pause, failure, "AUTH_PAUSED")
    except _WrongMode as failure:
        return _end(rt, brand_id, worker_id, _wrong_mode_pause, failure, "PAUSED")
    except _Retry as failure:
        return _end(rt, brand_id, worker_id, _retry, failure, "RETRY")
    except _Fatal as failure:
        return _end(rt, brand_id, worker_id, _fatal, failure, "FAILED")
    except Exception as exc:  # a bug or surprise must hurt one brand, not the worker
        log.exception("unexpected error running brand %s", brand_id)
        failure = _Retry(f"unexpected {type(exc).__name__}: {exc}")
        return _end(rt, brand_id, worker_id, _retry, failure, "RETRY")


def _on_yield(brand_id, worker_id: str, rt: Runtime, job_status: JobStatus) -> str:
    if job_status == JobStatus.CANCELLED:
        return _end(rt, brand_id, worker_id, _cancel, None, "CANCELLED")
    return _end(rt, brand_id, worker_id, _pause, None, "PAUSED")


def _end(rt: Runtime, brand_id, worker_id: str, handler, signal, outcome: str) -> str:
    try:
        with _owned(rt, brand_id, worker_id) as (session, brand):
            now = rt.clock()
            handler(session, brand, signal, rt, now)
            maybe_complete_job(session, session.get(Job, brand.job_id), now=now)
    except LeaseLost:
        return "LEASE_LOST"
    return outcome


def _defer(session, brand, signal: _Defer, rt, now) -> None:
    release_brand(
        session,
        brand,
        refund_attempt=True,
        retry_at=now + timedelta(seconds=signal.delay),
        reason=signal.reason,
    )


def _pause(session, brand, _signal, rt, now) -> None:
    release_brand(session, brand, refund_attempt=True, reason="job paused")


def _cancel(session, brand, _signal, rt, now) -> None:
    cancel_brand(session, brand, now=now)


def _auth_pause(session, brand, signal: _AuthFailed, rt, now) -> None:
    reason = "Amazon authorisation failed; reauthorise the seller account, then resume"
    audit(
        session,
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        event_type="authorisation_failed",
        detail=str(signal)[:_MAX_ERROR_LENGTH],
    )
    mark_needs_reauthorization(session, brand.seller_account_id, detail=str(signal), now=now)
    pause_seller_jobs(session, brand.seller_account_id, reason=reason, now=now)
    release_brand(session, brand, refund_attempt=True, reason="authorisation failed")


def _wrong_mode_pause(session, brand, signal: _WrongMode, rt, now) -> None:
    job = session.get(Job, brand.job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        pause_job(session, job, reason=str(signal), now=now)
    release_brand(session, brand, refund_attempt=True, reason="data source mismatch")


def _retry(session, brand, signal: _Retry, rt, now) -> None:
    retry_or_dead_letter(
        session,
        brand,
        error=str(signal)[:_MAX_ERROR_LENGTH],
        now=now,
        settings=rt.settings,
        rng=rt.rng,
    )


def _fatal(session, brand, signal: _Fatal, rt, now) -> None:
    dead_letter_brand(session, brand, error=str(signal)[:_MAX_ERROR_LENGTH], now=now)


# ---- leased transactions -------------------------------------------------------------------


@contextmanager
def _owned(rt: Runtime, brand_id, worker_id: str) -> Iterator[tuple[Session, JobBrand]]:
    """A transaction in which the worker provably still holds the brand's lease."""
    with rt.session() as session:
        brand = session.scalar(select(JobBrand).where(JobBrand.id == brand_id).with_for_update())
        if (
            brand is None
            or brand.lease_owner != worker_owner(worker_id)
            or brand.status != BrandStatus.IN_PROGRESS
        ):
            raise LeaseLost(str(brand_id))
        yield session, brand


def _guard(rt: Runtime, brand_id, worker_id: str) -> None:
    """Checkpoint between units of work: renew the lease and honour pause/cancel."""
    with rt.session() as session:
        status = heartbeat(session, brand_id, worker_id, now=rt.clock(), settings=rt.settings)
    if status in (JobStatus.PAUSED, JobStatus.CANCELLED):
        raise _Yield(status)


# ---- the work ------------------------------------------------------------------------------


def _load_plan(rt: Runtime, brand_id) -> Plan:
    with rt.session() as session:
        brand = session.get(JobBrand, brand_id)
        job = session.get(Job, brand.job_id)
        seller = session.get(SellerAccount, brand.seller_account_id)
        if job.data_source != rt.clients.data_source:
            # A demo job answered by Amazon, or a live job answered by fixtures, would carry
            # a label that misdescribes every result in it.
            raise _WrongMode(
                f"job uses {job.data_source.value} data but the workers run "
                f"{rt.clients.data_source.value}; resume it once they match"
            )
        seller_ref = seller.amazon_seller_id
        if not seller_ref:
            if rt.clients.data_source == DataSource.LIVE:
                raise _AuthFailed("seller account has no Amazon seller ID; complete authorisation")
            seller_ref = MOCK_SELLER_REF
        try:
            client = rt.clients.for_seller(session, seller)
        except AmazonAuthError as exc:
            raise _AuthFailed(str(exc)) from exc
        return Plan(
            client=client,
            brand_id=brand.id,
            job_id=job.id,
            seller_id=seller.id,
            seller_ref=seller_ref,
            marketplace_id=seller.marketplace_id,
            display_name=brand.display_name,
            settings=JobSettings.model_validate(job.settings),
            stage=brand.checkpoint.get("stage", "NEW"),
        )


def _execute(brand_id, worker_id: str, rt: Runtime) -> None:
    plan = _load_plan(rt, brand_id)
    if plan.stage != "CHECKING":
        _guard(rt, brand_id, worker_id)
        _discover(plan, worker_id, rt)

    with rt.session() as session:
        brand = session.get(JobBrand, brand_id)
        pending = [c.id for c in brand_checks(session, brand) if c.state == CheckState.PENDING]
    # One flaky ASIN must not starve its siblings: a transient failure is remembered and only
    # that check is retried later. Several in a row means Amazon itself is struggling, so stop.
    first_failure: _Retry | None = None
    consecutive = 0
    for check_id in pending:
        _guard(rt, brand_id, worker_id)
        try:
            _run_check(plan, check_id, worker_id, rt)
            consecutive = 0
        except _Retry as failure:
            first_failure = first_failure or failure
            consecutive += 1
            if consecutive >= _MAX_CONSECUTIVE_TRANSIENT:
                break
    if first_failure is not None:
        raise first_failure

    with _owned(rt, brand_id, worker_id) as (session, brand):
        now = rt.clock()
        finalize_brand(session, brand, now=now)
        complete_brand(session, brand, now=now)
        maybe_complete_job(session, session.get(Job, brand.job_id), now=now)


def _throttle(plan: Plan, rt: Runtime, operation: str) -> None:
    settings = rt.settings
    rate, burst = {
        "catalog": (settings.rate_search_catalog_per_sec, settings.rate_search_catalog_burst),
        "restrictions": (settings.rate_restrictions_per_sec, settings.rate_restrictions_burst),
    }[operation]
    wait = rt.limiter.reserve(
        f"{plan.seller_id}:{operation}",
        rate=rate,
        burst=burst,
        max_wait=settings.max_rate_wait_seconds,
    )
    if wait is None:
        # Never return sooner than one token interval, or the brand spins on an empty bucket.
        raise _Defer(
            max(settings.max_rate_wait_seconds, 1 / rate), f"local rate limit on {operation}"
        )
    if wait > 0:
        rt.sleep(wait)


def _throttled_by_amazon(exc: AmazonRateLimited, rt: Runtime) -> _Defer:
    delay = exc.retry_after if exc.retry_after is not None else rt.settings.max_rate_wait_seconds
    delay = min(max(delay, 1.0), rt.settings.backoff_cap_seconds)
    return _Defer(delay, "Amazon rate limit (HTTP 429)")


def _discover(plan: Plan, worker_id: str, rt: Runtime) -> None:
    items: dict[str, CatalogItem] = {}
    evidence: list[str] = []
    token: str | None = None
    pages = 0
    complete = True

    while True:
        _throttle(plan, rt, "catalog")
        try:
            body = plan.client.search_catalog_items(
                keywords=plan.display_name,
                marketplace_id=plan.marketplace_id,
                page_token=token,
                page_size=_CATALOG_PAGE_SIZE,
            )
            page = parse_catalog_page(body, plan.marketplace_id)
        except AmazonRateLimited as exc:
            raise _throttled_by_amazon(exc, rt) from exc
        except AmazonAuthError as exc:
            raise _AuthFailed(str(exc)) from exc
        except AmazonRequestError as exc:
            raise _Fatal(f"catalog search rejected: {exc}") from exc
        except (AmazonTransientError, AmazonMalformedResponse) as exc:
            raise _Retry(f"catalog search failed: {exc}") from exc

        for item in page.items:
            items.setdefault(item.asin, item)
        if ref := _evidence_ref(body):
            evidence.append(ref)
        pages += 1
        token = page.next_token
        if token is None:
            break
        if pages >= plan.settings.max_catalog_pages:
            complete = False  # Amazon has more results than we are willing to page through
            break
        _guard(rt, plan.brand_id, worker_id)

    with _owned(rt, plan.brand_id, worker_id) as (session, brand):
        _persist_discovery(session, brand, plan, list(items.values()), complete, pages, rt.clock())
        if evidence:
            brand.checkpoint = {**brand.checkpoint, "catalog_evidence": evidence}


def _persist_discovery(
    session: Session,
    brand: JobBrand,
    plan: Plan,
    items: list[CatalogItem],
    complete: bool,
    pages: int,
    now: datetime,
) -> None:
    """Candidates, sampled checks and the checkpoint commit together or not at all."""
    matches = {i.asin: match_brand(plan.display_name, i.brand) for i in items}
    ranked = rank_matched([Rankable(i.asin, i.category_name, matches[i.asin]) for i in items])
    rank_of = {r.asin: n for n, r in enumerate(ranked, start=1)}
    sampled = {r.asin for r in select_sample(ranked, plan.settings.max_asins_per_brand)}

    candidates: dict[str, CatalogCandidate] = {}
    for item in items:
        match = matches[item.asin]
        candidate = CatalogCandidate(
            job_brand_id=plan.brand_id,
            seller_account_id=plan.seller_id,
            asin=item.asin,
            marketplace_id=plan.marketplace_id,
            title=item.title,
            brand_attribute=item.brand,
            category_name=item.category_name,
            category_id=item.category_id,
            product_type=item.product_type,
            brand_match=match,
            rejection_reason=_rejection_reason(match, item.brand),
            rank=rank_of.get(item.asin),
            selected=item.asin in sampled,
            # Rejected candidates keep their identifying fields; the full payload is kept only
            # for the ASINs that were actually checked.
            raw=item.raw if item.asin in sampled else None,
            discovered_at=now,
        )
        candidates[item.asin] = candidate
        session.add(candidate)
    session.flush()

    existing_keys = set(
        session.scalars(
            select(EligibilityCheck.check_key).where(EligibilityCheck.job_id == plan.job_id)
        )
    )
    for asin in sorted(sampled):
        for condition in plan.settings.conditions:
            key = make_check_key(
                seller_account_id=str(plan.seller_id),
                marketplace_id=plan.marketplace_id,
                asin=asin,
                condition=condition,
                verification_context=SPAPI_RESTRICTIONS_CONTEXT,
            )
            if key in existing_keys:
                continue  # another brand in this job already owns the check for this ASIN
            existing_keys.add(key)
            session.add(
                EligibilityCheck(
                    seller_account_id=plan.seller_id,
                    job_id=plan.job_id,
                    job_brand_id=plan.brand_id,
                    candidate_id=candidates[asin].id,
                    asin=asin,
                    marketplace_id=plan.marketplace_id,
                    condition=condition,
                    verification_context=SPAPI_RESTRICTIONS_CONTEXT,
                    check_key=key,
                    created_at=now,
                )
            )

    brand.checkpoint = {
        "stage": "CHECKING",
        "catalog_complete": complete,
        "catalog_pages": pages,
        "discovered_at": now.isoformat(),
    }


def _rejection_reason(match: BrandMatch, brand_attribute: str | None) -> str | None:
    if match == BrandMatch.MISMATCH:
        return f"brand attribute {brand_attribute!r} does not match the searched brand"
    if match == BrandMatch.MISSING:
        return "catalog item has no brand attribute, so its brand cannot be confirmed"
    return None


def _run_check(plan: Plan, check_id, worker_id: str, rt: Runtime) -> None:
    with rt.session() as session:
        check = session.get(EligibilityCheck, check_id)
        if check.state == CheckState.DONE:
            return
        asin, condition = check.asin, check.condition
    context = ObservationContext(
        asin=asin,
        marketplace_id=plan.marketplace_id,
        condition=condition,
        seller_ref=plan.seller_ref,
        expected_brand=plan.display_name,
    )

    _throttle(plan, rt, "restrictions")
    signal: Exception | None = None
    finalise = True
    try:
        body = plan.client.get_listings_restrictions(
            asin=asin,
            seller_id=plan.seller_ref,
            marketplace_id=plan.marketplace_id,
            condition=condition,
        )
        observation = parse_restrictions_observation(
            body, context=context, http_status=_status_of(body), evidence_ref=_evidence_ref(body)
        )
    except AmazonRateLimited as exc:
        raise _throttled_by_amazon(exc, rt) from exc
    except AmazonAuthError as exc:
        observation = _failure(ObservationOutcome.AUTH_ERROR, context, exc)
        signal, finalise = _AuthFailed(str(exc)), False
    except AmazonTimeout as exc:
        observation = _failure(ObservationOutcome.TIMEOUT, context, exc)
        signal, finalise = _Retry(f"{asin}: timed out: {exc}"), False
    except AmazonTransientError as exc:
        observation = _failure(ObservationOutcome.HTTP_ERROR, context, exc)
        signal, finalise = _Retry(f"{asin}: transient error: {exc}"), False
    except AmazonRequestError as exc:
        observation = _failure(
            ObservationOutcome.HTTP_ERROR, context, exc, http_status=exc.status, detail=exc.detail
        )
    except AmazonMalformedResponse as exc:
        observation = _failure(ObservationOutcome.MALFORMED_RESPONSE, context, exc)
    except AmazonError as exc:  # any other Amazon failure is unusable evidence, never permission
        observation = _failure(ObservationOutcome.HTTP_ERROR, context, exc)

    with _owned(rt, plan.brand_id, worker_id) as (session, _brand):
        check = session.scalar(
            select(EligibilityCheck).where(EligibilityCheck.id == check_id).with_for_update()
        )
        if check.state == CheckState.DONE:
            return  # a brand sharing this ASIN finished it while we were waiting on Amazon
        check.attempts += 1
        check.last_error = observation.error_detail
        store_observation(session, check, observation)
        if finalise:
            classify_and_store(session, check, rt.clock())
    if signal is not None:
        raise signal


def _evidence_ref(source) -> str | None:
    """Amazon's request ID: the reference Amazon support needs to look up the exact call."""
    request_id = getattr(source, "request_id", None)
    return f"x-amzn-RequestId:{request_id}" if request_id else None


def _status_of(body) -> int:
    return getattr(body, "http_status", 200)


def _failure(outcome, context, exc: AmazonError, *, http_status=None, detail=None):
    return failure_observation(
        outcome=outcome,
        context=context,
        http_status=http_status,
        detail=detail if detail is not None else str(exc),
        evidence_ref=_evidence_ref(exc),
    )
