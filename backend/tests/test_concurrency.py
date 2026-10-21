"""Real races between threads. Needs PostgreSQL: SQLite serialises writers and proves nothing."""

import threading
import time

import pytest
from conftest import IS_POSTGRES
from sqlalchemy import func, select

from app.amazon.mock import NO_RESTRICTIONS, MockAmazonClient, catalog_item
from app.config import Settings
from app.db import session_scope
from app.enums import BrandStatus, DataSource
from app.models import JobBrand
from app.schemas import JobSettings
from app.services.dispatch import InlineDispatcher, dispatch_next
from app.services.ingestion import ingest, rows_from_list
from app.services.orchestration import submit_job
from app.services.processor import BrandProcessor
from app.services.rate_limit import InMemoryTokenBucket

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="concurrency tests need PostgreSQL")

CFG = Settings(
    database_url="x",
    seller_max_concurrent_brands=3,
    rate_restrictions_per_sec=10_000,
    rate_restrictions_burst=10_000,
    rate_search_catalog_per_sec=10_000,
    rate_search_catalog_burst=10_000,
)


def products(n):
    return [catalog_item(f"B0RACE{i:04d}", f"item {i}", "Oddy", category_id="c") for i in range(n)]


def run_threads(n, target):
    errors, barrier = [], threading.Barrier(n)

    def wrapped(i):
        try:
            barrier.wait()
            target(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors


def submit(seller, names, dispatcher, window=3):
    cfg = CFG.model_copy(update={"seller_max_concurrent_brands": window})
    ingestion = ingest(rows_from_list(names), max_brands=100, max_length=200)
    return cfg, submit_job(
        dispatcher,
        cfg,
        seller.id,
        ingestion=ingestion,
        name="race",
        settings=JobSettings(),
        data_source=DataSource.MOCK,
    )


def test_many_workers_receiving_the_same_message_process_the_brand_exactly_once(seller):
    slow = {f"B0RACE{i:04d}": (lambda _n: (time.sleep(0.05), NO_RESTRICTIONS)[1]) for i in range(3)}
    client = MockAmazonClient(catalog={"Oddy": products(3)}, restrictions=slow)
    dispatcher = InlineDispatcher()
    cfg, job = submit(seller, ["Oddy"], dispatcher)
    brand_id = dispatcher.pop()[0]

    def worker(i):
        BrandProcessor(
            client_factory=lambda _s: client,
            limiter=InMemoryTokenBucket(),
            dispatcher=InlineDispatcher(),
            settings=cfg,
            worker_id=f"worker-{i}",
        ).process(brand_id)

    run_threads(8, worker)

    calls = [c["asin"] for c in client.calls_to("getListingsRestrictions")]
    assert sorted(calls) == ["B0RACE0000", "B0RACE0001", "B0RACE0002"]  # each ASIN exactly once
    assert len(client.calls_to("searchCatalogItems")) == 1
    with session_scope() as s:
        assert s.get(JobBrand, brand_id).status == BrandStatus.COMPLETED


def test_concurrent_dispatchers_never_exceed_the_sellers_window(seller):
    names = [f"Brand{i}" for i in range(12)]
    dispatcher = InlineDispatcher()
    cfg, _job = submit(seller, names, dispatcher, window=3)
    sent_before = len(dispatcher.messages)
    assert sent_before == 3

    sent = []

    def hammer(_i):
        for _ in range(20):
            sent.extend(dispatch_next(InlineDispatcher(), seller.id, window=3))

    run_threads(8, hammer)

    assert sent == []  # the window was already full; nobody sneaked extra work in
    with session_scope() as s:
        queued = s.scalar(
            select(func.count()).select_from(JobBrand).where(JobBrand.status == BrandStatus.QUEUED)
        )
    assert queued == 3


def test_concurrent_dispatchers_fill_exactly_the_free_slots(seller):
    dispatcher = InlineDispatcher()
    cfg, job = submit(seller, [f"Brand{i}" for i in range(12)], dispatcher, window=3)
    with session_scope() as s:  # free the window as if three brands had finished
        from sqlalchemy import update

        s.execute(
            update(JobBrand)
            .where(JobBrand.status == BrandStatus.QUEUED)
            .values(status=BrandStatus.COMPLETED)
        )
    sent, lock = [], threading.Lock()

    def hammer(_i):
        got = dispatch_next(InlineDispatcher(), seller.id, window=3)
        with lock:
            sent.extend(got)

    run_threads(8, hammer)
    assert len(sent) == len(set(sent)) == 3  # three slots, filled once, no brand sent twice


def test_exactly_one_of_many_simultaneous_claims_wins(seller):
    """The claim itself, not just the end result: fencing would mask a broken claim."""
    dispatcher = InlineDispatcher()
    cfg, _job = submit(seller, ["Oddy"], dispatcher)
    brand_id = dispatcher.pop()[0]
    winners, lock = [], threading.Lock()

    def claim(i):
        processor = BrandProcessor(
            client_factory=lambda _s: None,
            limiter=InMemoryTokenBucket(),
            dispatcher=InlineDispatcher(),
            settings=cfg,
            worker_id=f"worker-{i}",
        )
        if processor._claim(brand_id) is not None:
            with lock:
                winners.append(i)

    run_threads(16, claim)
    assert len(winners) == 1
    with session_scope() as s:
        brand = s.get(JobBrand, brand_id)
        assert brand.status == BrandStatus.IN_PROGRESS
        assert brand.lease_owner == f"worker-{winners[0]}"


def test_two_brands_finishing_at_the_same_instant_still_complete_the_job(seller):
    """Each finisher's transaction can't see the other's uncommitted completion. Without
    serialising on the job row, both conclude "the other is still active" and the job is never
    marked COMPLETED."""
    from app.enums import JobStatus
    from app.models import Job
    from app.services.jobs import maybe_complete_job

    dispatcher = InlineDispatcher()
    cfg, job_id = submit(seller, ["Oddy", "Kangaro"], dispatcher)
    with session_scope() as s:
        brands = s.scalars(select(JobBrand).where(JobBrand.job_id == job_id)).all()
        ids = [b.id for b in brands]
        for b in brands:
            b.status = BrandStatus.IN_PROGRESS  # both are mid-flight
    both_have_written = threading.Barrier(2)

    def finish(i):
        with session_scope() as s:
            s.get(JobBrand, ids[i]).status = BrandStatus.COMPLETED
            s.flush()  # written but not committed
            both_have_written.wait()
            maybe_complete_job(s, job_id)

    run_threads(2, finish)
    with session_scope() as s:
        assert s.get(Job, job_id).status == JobStatus.COMPLETED
