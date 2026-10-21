import os
import threading
import time
import uuid

import pytest
import redis
from conftest import IS_POSTGRES
from dramatiq import Worker
from dramatiq.brokers.redis import RedisBroker
from dramatiq.brokers.stub import StubBroker

from app.amazon.mock import demo_client
from app.config import Settings
from app.db import session_scope
from app.enums import BrandStatus, DataSource, JobStatus
from app.models import Job, JobBrand
from app.schemas import JobSettings
from app.services.ingestion import ingest, rows_from_list
from app.services.jobs import job_progress
from app.services.orchestration import submit_job
from app.services.rate_limit import InMemoryTokenBucket, RedisTokenBucket
from app.workers.runtime import QUEUE, SweeperMiddleware, build_runtime

REDIS_URL = os.environ.get("REDIS_TEST_URL")
BRANDS = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy", "Nobody"]


def cfg(**kw):
    return Settings(
        database_url="x",
        seller_max_concurrent_brands=3,
        rate_restrictions_per_sec=10_000,
        rate_restrictions_burst=10_000,
        rate_search_catalog_per_sec=10_000,
        rate_search_catalog_burst=10_000,
        **kw,
    )


def submit(runtime, seller, brands=BRANDS):
    return submit_job(
        runtime.dispatcher,
        runtime.settings,
        seller.id,
        ingestion=ingest(rows_from_list(brands), max_brands=100, max_length=200),
        name="queue test",
        settings=JobSettings(),
        data_source=DataSource.MOCK,
    )


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def job_done(job_id):
    with session_scope() as s:
        return s.get(Job, job_id).status == JobStatus.COMPLETED


def test_actor_is_registered_with_bounded_broker_retries():
    runtime = build_runtime(StubBroker(), cfg(), limiter=InMemoryTokenBucket())
    assert runtime.actor.actor_name == "process_brand"
    assert runtime.actor.queue_name == QUEUE
    assert runtime.actor.options["max_retries"] == 0  # retries live in the database, bounded


def test_job_runs_to_completion_through_a_dramatiq_broker(seller):
    broker = StubBroker()
    broker.emit_after("process_boot")
    runtime = build_runtime(
        broker, cfg(), client_factory=lambda _s: demo_client(), limiter=InMemoryTokenBucket()
    )
    worker = Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        job_id = submit(runtime, seller)
        wait_for(lambda: job_done(job_id))
    finally:
        worker.stop()

    with session_scope() as s:
        progress = job_progress(s, job_id)
    assert progress.brands_completed == 6 and progress.verified_listable == 6
    assert progress.restricted == 6


def test_sweeper_middleware_runs_in_the_background_and_stops():
    calls = threading.Event()
    sweeper = SweeperMiddleware(calls.set, interval_seconds=0.05)
    sweeper.after_process_boot(None)
    assert calls.wait(2)
    sweeper.before_process_stop(None)


def test_a_failing_sweep_does_not_kill_the_sweeper():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("db hiccup")

    sweeper = SweeperMiddleware(flaky, interval_seconds=0.05)
    sweeper.after_process_boot(None)
    wait_for(lambda: len(attempts) >= 3, timeout=3)
    sweeper.before_process_stop(None)


@pytest.mark.skipif(
    not (REDIS_URL and IS_POSTGRES), reason="needs Redis and PostgreSQL for concurrent workers"
)
def test_full_pipeline_on_real_redis_with_concurrent_workers(seller):
    client = redis.Redis.from_url(REDIS_URL)
    namespace = f"test-{uuid.uuid4().hex[:8]}"
    broker = RedisBroker(url=REDIS_URL, namespace=namespace)
    runtime = build_runtime(
        broker,
        cfg(),
        client_factory=lambda _s: demo_client(),
        limiter=RedisTokenBucket(client, prefix=f"{namespace}:rl:"),
    )
    worker = Worker(broker, worker_threads=4, worker_timeout=100)
    worker.start()
    try:
        names = BRANDS * 1 + [f"Extra{i}" for i in range(14)]
        job_id = submit(runtime, seller, names)
        wait_for(lambda: job_done(job_id), timeout=60)
    finally:
        worker.stop()
        broker.flush_all()
        for key in client.scan_iter(f"{namespace}*"):
            client.delete(key)

    with session_scope() as s:
        progress = job_progress(s, job_id)
        statuses = {b.status for b in s.query(JobBrand).filter(JobBrand.job_id == job_id)}
    assert progress.brands_total == 20 and progress.brands_completed == 20
    assert statuses == {BrandStatus.COMPLETED}
    assert progress.verified_listable == 6 and progress.restricted == 6
