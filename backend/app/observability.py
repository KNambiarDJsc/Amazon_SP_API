"""Prometheus metrics.

Event counters and latency histograms are recorded in whichever process does the work (API or
worker) and aggregated through prometheus_client's multiprocess mode when PROMETHEUS_MULTIPROC_DIR
is set (the API and workers share that directory). State gauges (how many jobs/brands/checks are
in each state) are computed from the database at scrape time, because the database, not any
process, is the source of truth; they are cached briefly so a busy scraper cannot load it.
"""

import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select

from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.db import session_scope
from app.domain.observations import utcnow
from app.enums import BrandStatus, CheckState, JobStatus
from app.models import EligibilityCheck, Job, JobBrand, SellerCredential

_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_AMAZON = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20)

HTTP_REQUESTS = Counter(
    "eligibility_http_requests", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_SECONDS = Histogram(
    "eligibility_http_request_seconds", "HTTP request latency", ["method", "route"], buckets=_FAST
)
AMAZON_REQUESTS = Counter(
    "eligibility_amazon_requests", "Selling Partner API calls", ["operation", "outcome"]
)
AMAZON_SECONDS = Histogram(
    "eligibility_amazon_request_seconds",
    "Selling Partner API latency",
    ["operation"],
    buckets=_AMAZON,
)
BRAND_TASKS = Counter(
    "eligibility_brand_tasks", "Brand task outcomes", ["result"]
)  # completed, deferred, paused, cancelled, lost_lease, retry, dead_lettered, auth_paused
CHECKS_CLASSIFIED = Counter("eligibility_checks_classified", "ASIN checks classified", ["status"])
LWA_REFRESHES = Counter("eligibility_lwa_refreshes", "LWA access-token refreshes", ["outcome"])

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def route_label(path: str) -> str:
    """Bounded label cardinality: ids collapse to {id}; anything outside our API is 'other'."""
    if path.startswith(("/api/", "/healthz", "/readyz", "/metrics")):
        return _UUID.sub("{id}", path)
    return "other"


def timed_amazon_call(operation: str, fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Call into Amazon (live or mocked), recording outcome and latency."""
    outcome = "ok"
    started = time.perf_counter()
    try:
        return fn(**kwargs)
    except AmazonRateLimited:
        outcome = "rate_limited"
        raise
    except AmazonAuthError:
        outcome = "auth"
        raise
    except AmazonRequestError:
        outcome = "request_error"
        raise
    except AmazonTransientError:
        outcome = "transient"
        raise
    except AmazonMalformedResponse:
        outcome = "malformed"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        AMAZON_REQUESTS.labels(operation, outcome).inc()
        AMAZON_SECONDS.labels(operation).observe(time.perf_counter() - started)


class DbStateCollector:
    """Gauges read from the database at scrape time, cached for `ttl` seconds."""

    def __init__(self, ttl: float = 10.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def _snapshot(self) -> dict[str, Any]:
        with session_scope() as s:
            now = utcnow()
            oldest = s.scalar(
                select(func.min(JobBrand.queued_at)).where(JobBrand.status == BrandStatus.QUEUED)
            )
            return {
                "jobs": dict(
                    s.execute(select(Job.status, func.count()).group_by(Job.status)).all()
                ),
                "brands": dict(
                    s.execute(select(JobBrand.status, func.count()).group_by(JobBrand.status)).all()
                ),
                "checks": dict(
                    s.execute(
                        select(EligibilityCheck.status, func.count())
                        .where(EligibilityCheck.state == CheckState.DONE)
                        .group_by(EligibilityCheck.status)
                    ).all()
                ),
                "oldest_queued_age": (now - oldest).total_seconds() if oldest else 0.0,
                "expired_leases": s.scalar(
                    select(func.count())
                    .select_from(JobBrand)
                    .where(
                        JobBrand.status == BrandStatus.IN_PROGRESS, JobBrand.lease_expires_at < now
                    )
                ),
                "paused_auth": s.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.status == JobStatus.PAUSED, Job.pause_reason.like("AUTH_REQUIRED%"))
                ),
                "revoked": s.scalar(
                    select(func.count())
                    .select_from(SellerCredential)
                    .where(SellerCredential.status != "ACTIVE")
                ),
            }

    def collect(self):
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached[0] > self._ttl:
                self._cached = (now, self._snapshot())
            data = self._cached[1]

        for name, key, doc in (
            ("eligibility_jobs", "jobs", "Jobs by status"),
            ("eligibility_brands", "brands", "Brand tasks by status"),
            ("eligibility_checks", "checks", "Completed ASIN checks by derived status"),
        ):
            family = GaugeMetricFamily(name, doc, labels=["status"])
            for status, count in data[key].items():
                family.add_metric([getattr(status, "value", str(status))], count)
            yield family
        for name, key, doc in (
            (
                "eligibility_oldest_queued_brand_age_seconds",
                "oldest_queued_age",
                "Age of the longest-waiting queued brand",
            ),
            (
                "eligibility_expired_leases",
                "expired_leases",
                "In-progress brands whose worker lease has expired (should be ~0)",
            ),
            (
                "eligibility_jobs_paused_for_auth",
                "paused_auth",
                "Jobs paused because Amazon credentials need attention",
            ),
            (
                "eligibility_credentials_not_active",
                "revoked",
                "Seller credentials that are revoked and need reconnecting",
            ),
        ):
            family = GaugeMetricFamily(name, doc)
            family.add_metric([], data[key] or 0)
            yield family


_db_collector = DbStateCollector()
_registered: set[int] = set()


def metrics_registry() -> CollectorRegistry:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    if id(registry) not in _registered or registry is not REGISTRY:
        try:
            registry.register(_db_collector)
        except ValueError:
            pass
        _registered.add(id(registry))
    return registry


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(metrics_registry()), CONTENT_TYPE_LATEST
