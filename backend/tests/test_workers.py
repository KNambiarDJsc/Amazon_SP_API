import threading
import time

import dramatiq
import pytest
from dramatiq import Worker
from dramatiq.brokers.stub import StubBroker
from support import brands_of, job_of, make_runtime, make_settings, submit

from app.amazon.mock import demo_client
from app.db import configure, make_engine, session_scope
from app.enums import BrandStatus, JobStatus
from app.models import Base, SellerAccount
from app.services.orchestrator import Runtime
from app.services.ratelimit import InMemoryRateLimiter
from app.services.scheduling import Dispatch
from app.services.tick import TickReport, tick
from app.workers import scheduler
from app.workers.actors import ACTOR_NAME, QUEUE_NAME, build_actor
from app.workers.inline import InlineEngine


@pytest.fixture
def stub_broker():
    broker = StubBroker()
    broker.emit_after("process_boot")
    dramatiq.set_broker(broker)
    yield broker
    broker.flush_all()
    broker.close()


def run_worker(broker):
    # One thread: the in-memory test database is a single shared connection.
    worker = Worker(broker, worker_threads=1, worker_timeout=50)
    worker.start()
    return worker


# ---- the Dramatiq actor --------------------------------------------------------------------


def test_actor_runs_a_dispatched_brand_through_a_real_worker(engine, seller, clock, stub_broker):
    rt = make_runtime(demo_client(), clock)
    actor = build_actor(stub_broker, lambda: rt)
    job_id = submit(seller, ["Havells", "eSSL"], clock)
    worker = run_worker(stub_broker)
    try:
        sent = []
        tick(rt, lambda d: (sent.append(d), actor.send(str(d.brand_id), d.token)))
        stub_broker.join(QUEUE_NAME)
        worker.join()
    finally:
        worker.stop()

    assert len(sent) == 2
    brands = brands_of(job_id)
    assert {b.status for b in brands.values()} == {BrandStatus.COMPLETED}
    assert brands["Havells"].brand_status.value == "BRAND_APPROVAL_REQUIRED"


def test_actor_has_dramatiq_retries_disabled(stub_broker):
    actor = build_actor(stub_broker, lambda: None)
    assert actor.actor_name == ACTOR_NAME and actor.queue_name == QUEUE_NAME
    assert actor.options["max_retries"] == 0  # retry policy lives in the database instead


def test_a_redelivered_message_is_harmless(engine, seller, clock, stub_broker):
    rt = make_runtime(demo_client(), clock)
    actor = build_actor(stub_broker, lambda: rt)
    job_id = submit(seller, ["Havells"], clock)
    worker = run_worker(stub_broker)
    try:
        sent: list[Dispatch] = []
        tick(rt, sent.append)
        for _ in range(3):  # at-least-once delivery: the same message arrives three times
            actor.send(str(sent[0].brand_id), sent[0].token)
        stub_broker.join(QUEUE_NAME)
        worker.join()
    finally:
        worker.stop()

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.COMPLETED and brand.attempts == 1
    assert len(rt.client.calls_to("searchCatalogItems")) == 1


def test_a_garbage_message_does_not_kill_the_worker(engine, seller, clock, stub_broker):
    rt = make_runtime(demo_client(), clock)
    actor = build_actor(stub_broker, lambda: rt)
    job_id = submit(seller, ["Havells"], clock)
    worker = run_worker(stub_broker)
    try:
        actor.send("00000000-0000-0000-0000-000000000000", "no-such-token")  # unknown brand
        sent: list[Dispatch] = []
        tick(rt, lambda d: (sent.append(d), actor.send(str(d.brand_id), d.token)))
        stub_broker.join(QUEUE_NAME)
        worker.join()
    finally:
        worker.stop()

    assert brands_of(job_id)["Havells"].status == BrandStatus.COMPLETED


# ---- the scheduler loop --------------------------------------------------------------------


class StopAfter(threading.Event):
    """An Event that reports 'stop' after a fixed number of loop checks, recording each wait."""

    def __init__(self, passes: int):
        super().__init__()
        self.passes = passes
        self.waits: list[float] = []

    def is_set(self) -> bool:
        self.passes -= 1
        return self.passes < 0

    def wait(self, timeout=None) -> bool:
        self.waits.append(timeout)
        return False


def test_scheduler_survives_a_failing_pass_and_backs_off(monkeypatch, caplog):
    calls = {"n": 0}

    def flaky_tick(_rt, _enqueue):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database restarting")
        return TickReport()

    monkeypatch.setattr(scheduler, "tick", flaky_tick)
    stop = StopAfter(3)

    scheduler.run_forever(object(), lambda d: None, stop, interval=1.0)

    assert calls["n"] == 3  # kept going after the exception
    assert stop.waits[0] >= 5.0  # paused longer after the failure
    assert stop.waits[1] == 1.0  # and returned to normal cadence
    assert "scheduler pass failed" in caplog.text


# ---- single-machine mode -------------------------------------------------------------------


def test_inline_engine_runs_jobs_on_a_background_thread(tmp_path, clock):
    """A real thread and a file database: the in-memory test database is one shared connection
    and could not honestly exercise two threads."""
    engine = make_engine(f"sqlite:///{tmp_path / 'inline.db'}")
    Base.metadata.create_all(engine)
    configure(engine)
    account = SellerAccount(name="Local", api_key_hash="d" * 64, api_key_prefix="loc")
    with session_scope() as s:
        s.add(account)
    job_id = submit(account, ["Havells", "eSSL", "Oddy"], clock)

    runtime = Runtime(client=demo_client(), limiter=InMemoryRateLimiter(), settings=make_settings())
    inline = InlineEngine(runtime, interval=0.05)
    inline.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and job_of(job_id).status != JobStatus.COMPLETED:
            time.sleep(0.05)
        final = job_of(job_id).status
        brands = brands_of(job_id)
    finally:
        inline.stop()
        engine.dispose()

    assert final == JobStatus.COMPLETED
    assert {n: b.brand_status.value for n, b in brands.items()} == {
        "Havells": "BRAND_APPROVAL_REQUIRED",
        "eSSL": "VERIFIED_LISTABLE",
        "Oddy": "NO_PRODUCT_FOUND",
    }
