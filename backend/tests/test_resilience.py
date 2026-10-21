"""Failure, interruption and recovery behaviour."""

import uuid

import pytest
from harness import SimulatedCrash
from sqlalchemy import select

from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonTransientError,
)
from app.amazon.mock import NO_RESTRICTIONS, MockAmazonClient, catalog_item
from app.db import session_scope
from app.enums import BrandStatus, DataSource, JobStatus, ObservationOutcome
from app.enums import EligibilityStatus as S
from app.models import JobBrand, SellerAccount
from app.services import orchestration
from app.services.jobs import JobNotFound, get_job, job_progress


def products(brand, n, prefix="B0TEST"):
    return [
        catalog_item(
            f"{prefix}{i:04d}", f"{brand} item {i}", brand, category="Tools", category_id="t"
        )
        for i in range(1, n + 1)
    ]


def all_listable(prefix, n):
    return {f"{prefix}{i:04d}": NO_RESTRICTIONS for i in range(1, n + 1)}


def simple_client(brand="Oddy", n=3, **extra):
    return MockAmazonClient(
        catalog={brand: products(brand, n)}, restrictions={**all_listable("B0TEST", n), **extra}
    )


# ---- Amazon rate limiting --------------------------------------------------------------------


def test_429_defers_the_brand_without_penalising_any_asin(make_harness):
    client = simple_client(B0TEST0002=[AmazonRateLimited(retry_after=7), NO_RESTRICTIONS])
    h = make_harness(client)
    job = h.submit(["Oddy"])

    h.processor.process(h.dispatcher.pop()[0])  # first attempt only; inspect before draining

    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.QUEUED  # parked, not failed
    assert brand.attempts == 0
    assert h.dispatcher.messages[-1][1] == pytest.approx(7)  # Amazon's retry-after is honoured
    assert h.statuses(job)["B0TEST0001/new_new"] == "VERIFIED_LISTABLE"  # progress kept
    assert h.statuses(job)["B0TEST0002/new_new"] is None  # not recorded, not marked failed
    assert [e.payload["reason"] for e in h.events(job, "BRAND_DEFERRED")] == [
        "Amazon rate limit (HTTP 429)"
    ]

    h.drain()
    assert set(h.statuses(job).values()) == {"VERIFIED_LISTABLE"}
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
    assert all(c.attempts == 0 for c in h.checks(job))


def test_endless_429s_are_bounded_then_dead_lettered(make_harness):
    client = simple_client(B0TEST0001=AmazonRateLimited(retry_after=1))
    h = make_harness(client, max_throttle_deferrals=3)
    job = h.submit(["Oddy"])
    steps = h.drain()
    assert steps == 4  # three deferrals, then the fourth gives up
    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.FAILED
    assert brand.brand_status == S.NEEDS_MANUAL_CHECK
    assert brand.brand_summary["rule_id"] == "BRAND_DEAD_LETTERED"
    assert "rate-limiting" in brand.last_error
    assert h.job(job).status == JobStatus.COMPLETED  # a dead-lettered brand does not wedge the job
    assert [e.event_type for e in h.events(job, "BRAND_DEAD_LETTERED")] == ["BRAND_DEAD_LETTERED"]


def test_our_own_limiter_paces_requests_and_defers_instead_of_blocking(make_harness):
    h = make_harness(
        simple_client(n=3),
        rate_restrictions_per_sec=1,
        rate_restrictions_burst=1,
        max_rate_wait_seconds=0.5,
    )
    job = h.submit(["Oddy"])
    h.drain()
    assert set(h.statuses(job).values()) == {"VERIFIED_LISTABLE"}
    assert max(h.delays) >= 1.0  # at least one deferral because the wait exceeded the cap
    assert h.brand(job, "Oddy").attempts == 0
    assert h.events(job, "BRAND_DEFERRED") == []  # local pacing is not an incident


def test_requests_within_max_wait_sleep_instead_of_deferring(make_harness):
    h = make_harness(
        simple_client(n=3),
        rate_restrictions_per_sec=2,
        rate_restrictions_burst=1,
        max_rate_wait_seconds=5,
    )
    job = h.submit(["Oddy"])
    before = h.clock.t
    steps = h.drain()
    assert steps == 1  # no deferral at all
    assert (h.clock.t - before).total_seconds() == pytest.approx(1.0)  # 2 sleeps of 0.5s
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED


# ---- transient failures ----------------------------------------------------------------------


def test_transient_error_on_one_asin_is_retried_with_backoff_then_succeeds(make_harness):
    h = make_harness(simple_client(B0TEST0002=[AmazonTransientError("502"), NO_RESTRICTIONS]))
    job = h.submit(["Oddy"])
    h.drain()
    assert set(h.statuses(job).values()) == {"VERIFIED_LISTABLE"}
    flaky = next(c for c in h.checks(job) if c.asin == "B0TEST0002")
    assert flaky.attempts == 1
    assert 5.0 in h.delays  # base backoff, jitter pinned to 1.0x


def test_asin_that_keeps_failing_goes_to_manual_review_and_the_brand_finishes(make_harness):
    h = make_harness(simple_client(B0TEST0002=AmazonTransientError("503 always")))
    job = h.submit(["Oddy"])
    h.drain()
    assert h.statuses(job) == {
        "B0TEST0001/new_new": "VERIFIED_LISTABLE",
        "B0TEST0002/new_new": "NEEDS_MANUAL_CHECK",
        "B0TEST0003/new_new": "VERIFIED_LISTABLE",
    }
    bad = next(o for o in h.observations(job) if o.outcome == ObservationOutcome.TIMEOUT)
    assert "gave up after 3 attempts" in bad.error_detail
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
    assert h.brand(job, "Oddy").brand_status == S.MIXED_RESULTS


def test_malformed_response_that_persists_is_recorded_as_such(make_harness):
    h = make_harness(simple_client(n=1, B0TEST0001=AmazonMalformedResponse("html, not json")))
    job = h.submit(["Oddy"])
    h.drain()
    assert h.statuses(job) == {"B0TEST0001/new_new": "NEEDS_MANUAL_CHECK"}
    assert h.observations(job)[0].outcome == ObservationOutcome.MALFORMED_RESPONSE


def test_unexpected_errors_back_off_exponentially_then_dead_letter(make_harness):
    client = simple_client()
    client.catalog_behaviour = RuntimeError("bug in a dependency")
    h = make_harness(client, max_brand_attempts=4)
    job = h.submit(["Oddy"])
    h.drain()
    assert h.delays == [0, 5.0, 10.0, 20.0]  # first message is immediate; then 5s, 10s, 20s
    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.FAILED and brand.attempts == 4
    assert "RuntimeError: bug in a dependency" in brand.last_error
    assert len(h.events(job, "BRAND_RETRY")) == 3


def test_backoff_is_exponential_capped_and_jittered(make_harness):
    h = make_harness(simple_client())
    p = h.processor
    assert [p._backoff(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]
    p.jitter = lambda: 0.0
    assert p._backoff(1) == pytest.approx(4.0)  # -20%
    p.jitter = lambda: 1.0
    assert p._backoff(1) == pytest.approx(6.0)  # +20%


# ---- expired credentials ---------------------------------------------------------------------


def test_auth_failure_pauses_the_job_then_resumes_cleanly_after_reconnect(make_harness, seller):
    state = {"expired": True}

    def restrictions(_n):
        return AmazonAuthError("LWA token expired") if state["expired"] else NO_RESTRICTIONS

    client = MockAmazonClient(
        catalog={
            b: products(b, 2, prefix=f"B0{b[:2].upper()}") for b in ("Oddy", "Kangaro", "Taparia")
        },
        restrictions={
            f"B0{b[:2].upper()}000{i}": restrictions
            for b in ("Oddy", "Kangaro", "Taparia")
            for i in (1, 2)
        },
    )
    h = make_harness(client)
    job = h.submit(["Oddy", "Kangaro", "Taparia"])
    h.drain()

    j = h.job(job)
    assert j.status == JobStatus.PAUSED
    assert j.pause_reason.startswith("AUTH_REQUIRED: LWA token expired")
    assert {b.status for b in h.brands(job).values()} == {BrandStatus.PENDING}
    assert h.checks(job) == [] or all(c.status is None for c in h.checks(job))
    assert h.observations(job) == []  # an auth failure is not evidence about any product
    assert len(h.events(job, "JOB_PAUSED_AUTH_ERROR")) == 1  # only the first failure pauses

    state["expired"] = False
    orchestration.resume(h.dispatcher, h.cfg, seller.id, job)
    h.drain()
    assert h.job(job).status == JobStatus.COMPLETED
    assert {b.brand_status for b in h.brands(job).values()} == {S.VERIFIED_LISTABLE}
    assert len(h.checks(job)) == 6 and all(c.status == S.VERIFIED_LISTABLE for c in h.checks(job))


def test_live_job_for_an_unconnected_seller_pauses_instead_of_guessing(make_harness):
    h = make_harness(simple_client())
    job = h.submit(["Oddy"], data_source=DataSource.LIVE)
    h.drain()
    assert h.job(job).status == JobStatus.PAUSED
    assert "not connected" in h.job(job).pause_reason
    assert h.client.calls == []  # not a single request was sent


# ---- pause / resume / cancel -----------------------------------------------------------------


def test_pause_between_brands_then_resume_does_not_repeat_finished_work(make_harness, seller):
    client = MockAmazonClient(
        catalog={
            b: products(b, 1, prefix=f"B0{b[:2].upper()}") for b in ("Oddy", "Kangaro", "Taparia")
        },
        restrictions={
            f"B0{b[:2].upper()}0001": NO_RESTRICTIONS for b in ("Oddy", "Kangaro", "Taparia")
        },
    )
    h = make_harness(client, seller_max_concurrent_brands=1)
    job = h.submit(["Oddy", "Kangaro", "Taparia"])

    h.processor.process(h.dispatcher.pop()[0])  # Oddy completes; Kangaro is dispatched next
    orchestration.pause(seller.id, job)
    h.drain()  # Kangaro's message arrives while paused

    assert h.job(job).status == JobStatus.PAUSED
    states = {n: b.status for n, b in h.brands(job).items()}
    assert states == {
        "Oddy": BrandStatus.COMPLETED,
        "Kangaro": BrandStatus.PENDING,
        "Taparia": BrandStatus.PENDING,
    }
    calls_so_far = list(h.restriction_calls())
    assert calls_so_far == ["B0OD0001"]

    orchestration.resume(h.dispatcher, h.cfg, seller.id, job)
    h.drain()
    assert h.job(job).status == JobStatus.COMPLETED
    assert h.restriction_calls() == ["B0OD0001", "B0KA0001", "B0TA0001"]  # Oddy not repeated


def test_pause_in_the_middle_of_a_brand_resumes_from_the_checkpoint(make_harness, seller):
    ctx = {}

    def pause_during_second_check(n):
        if n == 0:
            orchestration.pause(seller.id, ctx["job"])
        return NO_RESTRICTIONS

    client = simple_client(n=4, B0TEST0002=pause_during_second_check)
    h = make_harness(client)
    ctx["job"] = job = h.submit(["Oddy"])
    h.drain()

    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.PENDING and h.job(job).status == JobStatus.PAUSED
    done = {k for k, v in h.statuses(job).items() if v}
    assert done == {"B0TEST0001/new_new", "B0TEST0002/new_new"}  # the in-flight check finished
    assert h.restriction_calls() == ["B0TEST0001", "B0TEST0002"]

    orchestration.resume(h.dispatcher, h.cfg, seller.id, job)
    h.drain()
    assert h.restriction_calls() == [f"B0TEST000{i}" for i in (1, 2, 3, 4)]  # no repeats
    assert len(h.client.calls_to("searchCatalogItems")) == 1  # discovery not repeated either
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED


def test_cancel_stops_work_keeps_partial_results_and_is_final(make_harness, seller):
    ctx = {}

    def cancel_during_second_check(n):
        orchestration.cancel(seller.id, ctx["job"])
        return NO_RESTRICTIONS

    client = simple_client(n=4, B0TEST0002=cancel_during_second_check)
    h = make_harness(client, seller_max_concurrent_brands=1)
    ctx["job"] = job = h.submit(["Oddy", "Kangaro", "Taparia"])
    h.drain()

    assert h.job(job).status == JobStatus.CANCELLED
    assert {b.status for b in h.brands(job).values()} == {BrandStatus.CANCELLED}
    assert {k for k, v in h.statuses(job).items() if v} == {
        "B0TEST0001/new_new",
        "B0TEST0002/new_new",
    }
    assert h.restriction_calls() == ["B0TEST0001", "B0TEST0002"]
    with pytest.raises(Exception, match="cancelled|cannot"):
        orchestration.resume(h.dispatcher, h.cfg, seller.id, job)
    assert job_progress_of(job).brands_cancelled == 3


def job_progress_of(job_id):
    with session_scope() as s:
        return job_progress(s, job_id)


# ---- crashes ---------------------------------------------------------------------------------


def test_worker_crash_mid_brand_is_recovered_without_repeating_finished_checks(make_harness):
    h = make_harness(simple_client(n=4, B0TEST0003=[SimulatedCrash(), NO_RESTRICTIONS]))
    job = h.submit(["Oddy"])
    with pytest.raises(SimulatedCrash):
        h.drain()

    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.IN_PROGRESS  # the dead worker's lease is still on it
    assert {k for k, v in h.statuses(job).items() if v} == {
        "B0TEST0001/new_new",
        "B0TEST0002/new_new",
    }

    assert h.recover().leases_expired == 0  # lease not expired yet: leave the worker alone
    h.clock.advance(301)
    stats = h.recover()
    assert (stats.leases_expired, stats.dispatched) == (1, 1)
    assert h.brand(job, "Oddy").attempts == 1

    h.drain()
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
    assert h.restriction_calls() == [
        "B0TEST0001",
        "B0TEST0002",
        "B0TEST0003",
        "B0TEST0003",
        "B0TEST0004",
    ]
    assert len(h.checks(job)) == 4  # no duplicate check rows
    assert h.job(job).status == JobStatus.COMPLETED


def test_crash_during_discovery_resumes_at_the_next_page(make_harness):
    client = MockAmazonClient(
        catalog={"Plantex": products("Plantex", 45)}, restrictions=all_listable("B0TEST", 45)
    )
    client.catalog_behaviour = [None, SimulatedCrash(), None]  # page 2 crashes once
    h = make_harness(client)
    job = h.submit(["Plantex"], max_catalog_pages=3)
    with pytest.raises(SimulatedCrash):
        h.drain()
    assert h.brand(job, "Plantex").checkpoint["catalog_next_token"] == "page:20"
    assert len(h.candidates(job, "Plantex")) == 20  # page 1 is safely stored

    h.clock.advance(301)
    h.recover()
    h.drain()
    tokens = [c["page_token"] for c in client.calls_to("searchCatalogItems")]
    assert tokens == [None, "page:20", "page:20", "page:40"]  # page 1 was never fetched twice
    assert len(h.candidates(job, "Plantex")) == 45  # no duplicate candidates
    assert h.brand(job, "Plantex").status == BrandStatus.COMPLETED


def test_a_brand_that_always_kills_its_worker_is_dead_lettered_and_the_job_still_finishes(
    make_harness,
):
    client = MockAmazonClient(
        catalog={"Oddy": products("Oddy", 1), "Kangaro": products("Kangaro", 1, prefix="B0KANG")},
        restrictions={"B0TEST0001": SimulatedCrash(), "B0KANG0001": NO_RESTRICTIONS},
    )
    h = make_harness(client, max_brand_attempts=3, seller_max_concurrent_brands=1)
    job = h.submit(["Oddy", "Kangaro"])
    for _ in range(3):
        with pytest.raises(SimulatedCrash):
            h.drain()
        h.clock.advance(301)
        stats = h.recover()
    assert stats.dead_lettered == 1
    h.drain()

    oddy = h.brand(job, "Oddy")
    assert oddy.status == BrandStatus.FAILED and oddy.brand_status == S.NEEDS_MANUAL_CHECK
    assert oddy.brand_summary["rule_id"] == "BRAND_DEAD_LETTERED"
    assert h.brand(job, "Kangaro").brand_status == S.VERIFIED_LISTABLE
    assert h.job(job).status == JobStatus.COMPLETED


def test_a_worker_that_lost_its_lease_stops_without_touching_state(make_harness):
    ctx = {}

    def take_over(n):
        if n == 0:  # only the first worker is "slow"; its lease expires and the sweeper moves on
            ctx["h"].clock.advance(301)
            ctx["h"].recover()
        return NO_RESTRICTIONS

    h = make_harness(simple_client(n=2, B0TEST0001=take_over))
    ctx["h"] = h
    job = h.submit(["Oddy"])
    h.processor.process(h.dispatcher.pop()[0])  # first worker; loses its lease mid-check

    # The first worker was fenced out: its in-flight result was discarded and it wrote nothing,
    # in particular it did not reset the attempt counter of a brand it no longer owns.
    brand = h.brand(job, "Oddy")
    assert brand.status == BrandStatus.QUEUED and brand.lease_owner is None
    assert brand.attempts == 1
    assert h.observations(job) == []

    h.drain()  # the new owner redoes the check from scratch and finishes
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
    assert len(h.checks(job)) == 2 and len(h.observations(job)) == 2
    assert h.restriction_calls() == ["B0TEST0001", "B0TEST0001", "B0TEST0002"]


# ---- messages are hints, the database is the truth -------------------------------------------


def test_duplicate_and_stale_messages_are_harmless(make_harness):
    h = make_harness(simple_client(n=2))
    job = h.submit(["Oddy"])
    brand_id = h.brand(job, "Oddy").id
    h.dispatcher.enqueue_brand(brand_id)  # a redelivery
    h.drain()
    assert h.restriction_calls() == ["B0TEST0001", "B0TEST0002"]  # processed once
    h.dispatcher.enqueue_brand(brand_id)  # arrives after completion
    h.drain()
    assert len(h.restriction_calls()) == 2
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED


def test_a_lost_message_is_recovered_by_the_sweeper(make_harness):
    h = make_harness(simple_client(n=2))
    job = h.submit(["Oddy"])
    h.dispatcher.messages.clear()  # broker flushed / worker died after dequeue
    assert h.brand(job, "Oddy").status == BrandStatus.QUEUED

    h.clock.advance(600)
    assert h.recover().requeued_stale == 0  # not stale yet (15 min)
    h.clock.advance(400)
    stats = h.recover()
    assert (stats.requeued_stale, stats.dispatched) == (1, 1)
    h.drain()
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED


def test_enqueue_failure_leaves_work_recoverable(make_harness):
    h = make_harness(simple_client(n=1))

    def broken(*_a, **_k):
        raise ConnectionError("redis is down")

    h.dispatcher.enqueue_brand = broken
    job = h.submit(["Oddy"])  # must not raise: the job is stored
    assert h.brand(job, "Oddy").status == BrandStatus.PENDING

    del h.dispatcher.enqueue_brand  # redis is back
    h.recover()
    h.drain()
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED


# ---- seller concurrency window and isolation -------------------------------------------------


def test_a_seller_never_has_more_than_the_window_in_flight(make_harness):
    names = [f"Brand{i}" for i in range(6)]
    client = MockAmazonClient(
        catalog={n: products(n, 1, prefix=f"B0B{i}") for i, n in enumerate(names)},
        restrictions={f"B0B{i}0001": NO_RESTRICTIONS for i in range(6)},
    )
    h = make_harness(client, seller_max_concurrent_brands=2)
    job = h.submit(names)
    assert len(h.dispatcher.messages) == 2  # not 6

    def in_flight():
        with session_scope() as s:
            n = s.scalar(
                select(__import__("sqlalchemy").func.count())
                .select_from(JobBrand)
                .where(JobBrand.status.in_([BrandStatus.QUEUED, BrandStatus.IN_PROGRESS]))
            )
        assert n <= 2

    h.drain(on_step=in_flight)
    assert {b.status for b in h.brands(job).values()} == {BrandStatus.COMPLETED}
    assert h.job(job).status == JobStatus.COMPLETED


def test_sellers_have_independent_windows_and_cannot_see_each_others_jobs(make_harness, seller):
    with session_scope() as s:
        other = SellerAccount(
            id=uuid.uuid4(),
            name="Other",
            api_key_hash="d" * 64,
            api_key_prefix="oth",
            amazon_seller_id="X",
        )
        s.add(other)
    client = MockAmazonClient(
        catalog={f"B{i}": products(f"B{i}", 1, prefix=f"B0S{i}") for i in range(4)},
        restrictions={f"B0S{i}0001": NO_RESTRICTIONS for i in range(4)},
    )
    h = make_harness(client, seller_max_concurrent_brands=1)
    mine = h.submit(["B0", "B1"])
    theirs = h.submit(["B2", "B3"], seller=other)
    assert len(h.dispatcher.messages) == 2  # one in flight for each seller
    h.drain()

    with session_scope() as s:
        assert get_job(s, seller.id, mine).id == mine
        with pytest.raises(JobNotFound):
            get_job(s, seller.id, theirs)
        with pytest.raises(JobNotFound):
            get_job(s, other.id, mine)
    with pytest.raises(JobNotFound):
        orchestration.pause(other.id, mine)
    assert {c.seller_account_id for c in h.checks(mine)} == {seller.id}
    assert {c.seller_account_id for c in h.checks(theirs)} == {other.id}
    assert h.job(mine).status == h.job(theirs).status == JobStatus.COMPLETED


def test_check_keys_include_the_seller_so_identical_asins_do_not_collide(make_harness, seller):
    with session_scope() as s:
        other = SellerAccount(id=uuid.uuid4(), name="O", api_key_hash="c" * 64, api_key_prefix="o")
        s.add(other)
    h = make_harness(simple_client(n=1))
    a = h.submit(["Oddy"])
    b = h.submit(["Oddy"], seller=other)
    h.drain()
    assert h.checks(a)[0].check_key != h.checks(b)[0].check_key


def test_progress_counts_reflect_results(make_harness):
    from app.amazon.mock import demo_client

    h = make_harness(demo_client())
    job = h.submit(["eSSL", "Havells", "Oddy"])
    h.drain()
    p = job_progress_of(job)
    assert (p.brands_total, p.brands_completed, p.percent_complete) == (3, 3, 100.0)
    assert p.verified_listable == 4 and p.restricted == 2
    assert p.checks_total == p.checks_done == 6
