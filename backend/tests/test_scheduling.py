import random

import pytest
from support import (
    brands_of,
    checks_of,
    job_of,
    make_runtime,
    make_settings,
    observations_of,
    submit,
)

from app.amazon.mock import demo_client
from app.db import session_scope
from app.enums import BrandStatus, JobStatus
from app.models import SellerAccount
from app.services.jobs import cancel_job, pause_job
from app.services.orchestrator import run_brand
from app.services.scheduling import (
    Dispatch,
    backoff_delay,
    heartbeat,
    reclaim_expired,
    start_brand,
)
from app.services.tick import run_until_idle, tick

BRANDS = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]


def collect(rt) -> list[Dispatch]:
    sent: list[Dispatch] = []
    tick(rt, sent.append)
    return sent


def other_seller(name="Other") -> SellerAccount:
    account = SellerAccount(name=name, api_key_hash=name.ljust(64, "x"), api_key_prefix=name[:5])
    with session_scope() as s:
        s.add(account)
    return account


# ---- backoff -------------------------------------------------------------------------------


def test_backoff_grows_exponentially_with_jitter_and_respects_the_cap():
    rng = random.Random(1)
    delays = [backoff_delay(n, base=5.0, cap=300.0, rng=rng) for n in range(1, 12)]
    for attempt, delay in enumerate(delays, start=1):
        ceiling = min(300.0, 5.0 * 2 ** (attempt - 1))
        assert ceiling / 2 <= delay <= ceiling  # never zero (no thundering herd), never above
    assert max(delays) <= 300.0
    assert delays[5] > delays[0]


def test_backoff_is_deterministic_for_a_given_rng():
    a = [backoff_delay(3, base=5.0, cap=300.0, rng=random.Random(9)) for _ in range(3)]
    assert len(set(a)) == 1


# ---- per-seller concurrency ----------------------------------------------------------------


def test_a_seller_never_has_more_brands_in_flight_than_the_cap(engine, seller, clock):
    rt = make_runtime(demo_client(), clock, seller_max_concurrent_brands=2)
    submit(seller, BRANDS, clock)

    first = collect(rt)
    assert len(first) == 2
    assert collect(rt) == []  # both slots are still leased; nothing more is handed out

    run_brand(first[0].brand_id, first[0].token, "w", rt)  # one finishes, freeing a slot
    assert len(collect(rt)) == 1


def test_one_sellers_backlog_does_not_starve_another_seller(engine, seller, clock):
    other = other_seller()
    rt = make_runtime(demo_client(), clock, seller_max_concurrent_brands=2)
    mine = submit(seller, BRANDS, clock)
    theirs = submit(other, ["Havells", "eSSL"], clock)

    sent = collect(rt)

    owners = {d.brand_id: None for d in sent}
    for name, job_id in (("mine", mine), ("theirs", theirs)):
        for brand in brands_of(job_id).values():
            if brand.id in owners:
                owners[brand.id] = name
    assert sorted(owners.values()) == ["mine", "mine", "theirs", "theirs"]


def test_dispatch_budget_per_tick_is_respected(engine, seller, clock):
    rt = make_runtime(
        demo_client(), clock, seller_max_concurrent_brands=10, max_dispatch_per_tick=3
    )
    submit(seller, BRANDS, clock)
    assert len(collect(rt)) == 3


def test_brands_are_dispatched_oldest_job_first_then_by_position(engine, seller, clock):
    rt = make_runtime(demo_client(), clock, seller_max_concurrent_brands=3)
    first = submit(seller, ["eSSL", "Havells"], clock)
    clock.advance(1)
    second = submit(seller, ["Kangaro"], clock)

    sent = collect(rt)

    expected = [
        brands_of(first)["eSSL"].id,
        brands_of(first)["Havells"].id,
        brands_of(second)["Kangaro"].id,
    ]
    assert [d.brand_id for d in sent] == expected


def test_paused_jobs_and_brands_in_backoff_are_not_dispatched(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    paused = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        pause_job(s, s.get(type(job_of(paused)), paused), reason="hold", now=clock.now())
    assert collect(rt) == []


def test_brand_waiting_out_a_backoff_becomes_dispatchable_only_when_due(engine, seller, clock):
    from datetime import timedelta

    from app.models import JobBrand

    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["eSSL"], clock)
    brand_id = brands_of(job_id)["eSSL"].id
    with session_scope() as s:
        s.get(JobBrand, brand_id).next_attempt_at = clock.now() + timedelta(seconds=30)

    assert collect(rt) == []
    clock.advance(31)
    assert len(collect(rt)) == 1


# ---- stale messages ------------------------------------------------------------------------


def test_pause_between_dispatch_and_pickup_returns_the_brand_to_the_pool(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL"], clock)
    (dispatch,) = collect(rt)

    with session_scope() as s:
        pause_job(s, s.get(type(job_of(job_id)), job_id), reason="hold", now=clock.now())

    assert run_brand(dispatch.brand_id, dispatch.token, "w", rt) == "STALE"
    assert client.calls == []
    brand = brands_of(job_id)["eSSL"]
    assert brand.status == BrandStatus.PENDING and brand.lease_owner is None


def test_cancel_between_dispatch_and_pickup_cancels_the_brand(engine, seller, clock):
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["eSSL", "Havells"], clock, max_asins_per_brand=5)
    dispatches = collect(rt)
    with session_scope() as s:
        cancel_job(s, s.get(type(job_of(job_id)), job_id), now=clock.now())

    for d in dispatches:
        assert run_brand(d.brand_id, d.token, "w", rt) == "STALE"
    assert client.calls == []
    assert {b.status for b in brands_of(job_id).values()} == {BrandStatus.CANCELLED}


# ---- leases --------------------------------------------------------------------------------


def test_a_worker_that_outlives_its_lease_stops_without_writing(engine, seller, clock):
    """Worker A stalls past its lease, the scheduler hands the brand on, and A wakes up. A must
    notice it no longer owns the brand and leave the database alone."""
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    (dispatch,) = collect(rt)
    original = client.get_listings_restrictions
    stalled = {"done": False}

    def stall_once(**kwargs):
        if not stalled["done"]:
            stalled["done"] = True
            clock.advance(rt.settings.brand_lease_seconds + 1)  # A freezes; its lease lapses
            with session_scope() as s:
                reclaim_expired(s, now=clock.now(), settings=rt.settings, rng=random.Random(1))
        return original(**kwargs)

    client.get_listings_restrictions = stall_once

    assert run_brand(dispatch.brand_id, dispatch.token, "worker-a", rt) == "LEASE_LOST"

    assert observations_of(job_id) == []  # A's answer was discarded, not committed
    assert all(c.status is None for c in checks_of(job_id).values())
    # The brand is back in the pool and a healthy worker finishes it.
    client.get_listings_restrictions = original
    run_until_idle(rt)
    assert brands_of(job_id)["Havells"].status == BrandStatus.COMPLETED


def test_a_stale_message_cannot_start_a_brand_that_was_dispatched_again(engine, seller, clock):
    """The first message is delayed past its lease. The brand is reclaimed and dispatched
    afresh under a new token; when the old message finally arrives it must be refused, or two
    workers would run the same brand."""
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    (first,) = collect(rt)

    clock.advance(rt.settings.queue_lease_seconds + 1)
    (second,) = collect(rt)  # tick reclaims the expired dispatch, then dispatches again
    assert second.brand_id == first.brand_id and second.token != first.token
    assert brands_of(job_id)["Havells"].status == BrandStatus.IN_PROGRESS  # leased to `second`

    assert run_brand(first.brand_id, first.token, "slow-worker", rt) == "STALE"
    assert client.calls == []  # the stale message did no work
    assert brands_of(job_id)["Havells"].attempts == 0  # and was not charged as an attempt

    assert run_brand(second.brand_id, second.token, "worker-b", rt) == "COMPLETED"
    assert brands_of(job_id)["Havells"].attempts == 1


def test_a_duplicate_message_is_refused_while_the_brand_is_running(engine, seller, clock):
    """Redelivery while the first copy is mid-flight: the second must not start a parallel run."""
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    (dispatch,) = collect(rt)
    original = client.get_listings_restrictions
    seen = {}

    def redeliver_midway(**kwargs):
        if "duplicate" not in seen:
            seen["duplicate"] = run_brand(dispatch.brand_id, dispatch.token, "worker-2", rt)
        return original(**kwargs)

    client.get_listings_restrictions = redeliver_midway

    assert run_brand(dispatch.brand_id, dispatch.token, "worker-1", rt) == "COMPLETED"

    assert seen["duplicate"] == "STALE"
    assert brands_of(job_id)["Havells"].attempts == 1
    assert len(client.calls_to("searchCatalogItems")) == 1  # discovery ran once, not twice


def test_a_worker_whose_brand_now_belongs_to_another_worker_does_not_write(engine, seller, clock):
    """The nastiest case: A stalls, its lease lapses, the brand is handed to worker B and B is
    running it when A wakes up. The brand is IN_PROGRESS the whole time, so only the owner
    check, not the status check, stands between A and B's data."""
    client = demo_client()
    rt = make_runtime(client, clock)
    job_id = submit(seller, ["Havells"], clock)
    (dispatch,) = collect(rt)
    original = client.get_listings_restrictions
    state = {"stalled": False}

    def stall_while_b_takes_over(**kwargs):
        if not state["stalled"]:
            state["stalled"] = True
            clock.advance(rt.settings.brand_lease_seconds + 1)  # A freezes; its lease lapses
            with session_scope() as s:
                reclaim_expired(s, now=clock.now(), settings=rt.settings, rng=random.Random(1))
            clock.advance(rt.settings.backoff_cap_seconds + 1)  # past the retry backoff
            (b_dispatch,) = collect(rt)
            with session_scope() as s:  # B picks the brand up and is mid-run
                start_brand(
                    s,
                    b_dispatch.brand_id,
                    b_dispatch.token,
                    worker_id="worker-b",
                    now=clock.now(),
                    settings=rt.settings,
                )
        return original(**kwargs)

    client.get_listings_restrictions = stall_while_b_takes_over

    assert run_brand(dispatch.brand_id, dispatch.token, "worker-a", rt) == "LEASE_LOST"

    brand = brands_of(job_id)["Havells"]
    assert brand.status == BrandStatus.IN_PROGRESS
    assert brand.lease_owner == "worker:worker-b"  # A neither stole nor released B's lease
    assert observations_of(job_id) == []  # nothing from A reached the database
    assert all(c.status is None for c in checks_of(job_id).values())


def test_heartbeat_extends_the_lease_and_reports_job_status(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    job_id = submit(seller, ["eSSL"], clock)
    (dispatch,) = collect(rt)
    from app.services.scheduling import start_brand

    with session_scope() as s:
        start_brand(
            s,
            dispatch.brand_id,
            dispatch.token,
            worker_id="w",
            now=clock.now(),
            settings=rt.settings,
        )
    before = brands_of(job_id)["eSSL"].lease_expires_at
    clock.advance(100)
    with session_scope() as s:
        status = heartbeat(s, dispatch.brand_id, "w", now=clock.now(), settings=rt.settings)
    assert status == JobStatus.RUNNING
    assert brands_of(job_id)["eSSL"].lease_expires_at > before

    from app.services.scheduling import LeaseLost

    with session_scope() as s, pytest.raises(LeaseLost):
        heartbeat(s, dispatch.brand_id, "someone-else", now=clock.now(), settings=rt.settings)


def test_unexpired_leases_are_left_alone(engine, seller, clock):
    rt = make_runtime(demo_client(), clock)
    submit(seller, ["eSSL"], clock)
    collect(rt)
    clock.advance(rt.settings.queue_lease_seconds - 5)
    with session_scope() as s:
        assert (
            reclaim_expired(s, now=clock.now(), settings=make_settings(), rng=random.Random(1)) == 0
        )
