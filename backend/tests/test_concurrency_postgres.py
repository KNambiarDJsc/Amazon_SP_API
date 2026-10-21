"""Races that only exist with real concurrent transactions, so they run only on PostgreSQL:

    TEST_DATABASE_URL=postgresql+psycopg://... pytest backend/tests/test_concurrency_postgres.py

SQLite serialises writers by itself, which would make these pass whatever the code did.
"""

import threading

import pytest
from conftest import IS_POSTGRES
from sqlalchemy import event
from sqlalchemy.orm import Session
from support import brands_of, make_runtime, submit

from app.amazon.mock import demo_client
from app.db import session_scope
from app.enums import BrandStatus
from app.services.scheduling import dispatch_for_seller, start_brand
from app.services.tick import tick

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="needs real PostgreSQL row locks")

THREADS = 8


def run_together(target, count=THREADS):
    """Start `count` threads on a barrier so their transactions genuinely overlap."""
    barrier = threading.Barrier(count)
    results, errors = [], []

    def runner(index):
        try:
            barrier.wait(timeout=10)
            results.append(target(index))
        except BaseException as exc:  # surface failures from threads in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


def test_concurrent_schedulers_cannot_exceed_a_sellers_cap(engine, seller, clock):
    rt = make_runtime(demo_client(), clock, seller_max_concurrent_brands=2)
    submit(seller, [f"Brand{n:02d}" for n in range(12)], clock)

    def one_scheduler_pass(_index):
        with session_scope() as session:
            return dispatch_for_seller(
                session, seller.id, now=clock.now(), settings=rt.settings, budget=100
            )

    handed_out = [d for batch in run_together(one_scheduler_pass) for d in batch]

    assert len(handed_out) == 2  # not 2 per scheduler
    assert len({d.brand_id for d in handed_out}) == 2  # and never the same brand twice


def test_whole_ticks_racing_still_respect_the_cap(engine, seller, clock):
    rt = make_runtime(demo_client(), clock, seller_max_concurrent_brands=3)
    job_id = submit(seller, [f"Brand{n:02d}" for n in range(20)], clock)
    sent, lock = [], threading.Lock()

    def enqueue(dispatch):
        with lock:
            sent.append(dispatch)

    run_together(lambda _i: tick(rt, enqueue), count=6)

    assert len(sent) == 3
    leased = [b for b in brands_of(job_id).values() if b.status == BrandStatus.IN_PROGRESS]
    assert len(leased) == 3


def test_only_one_of_many_simultaneous_deliveries_starts_a_brand(engine, seller, clock):
    """Forces the dangerous interleaving instead of hoping for it: every delivery is held after
    reading the brand and before writing its claim. With the row lock only one delivery can
    get that far (the rest wait on the lock), so its barrier times out and it carries on alone.
    Without the lock all of them arrive, are released together, and each would claim the brand.
    """
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["Havells"], clock)
    sent = []
    tick(rt, sent.append)
    (dispatch,) = sent
    hold = threading.Barrier(THREADS, timeout=2)

    def hold_before_writing(_session, _context, _instances):
        if threading.current_thread() is not threading.main_thread():
            try:
                hold.wait()
            except threading.BrokenBarrierError:
                pass  # expected with the lock in place: nobody else could reach this point

    def deliver(index):
        with session_scope() as session:
            started = start_brand(
                session,
                dispatch.brand_id,
                dispatch.token,
                worker_id=f"worker-{index}",
                now=clock.now(),
                settings=rt.settings,
            )
            return started is not None

    event.listen(Session, "before_flush", hold_before_writing)
    try:
        outcomes = run_together(deliver)
    finally:
        event.remove(Session, "before_flush", hold_before_writing)

    assert sum(outcomes) == 1  # exactly one worker won the brand
    brand = brands_of(job_id)["Havells"]
    assert brand.attempts == 1 and brand.lease_owner.startswith("worker:worker-")
