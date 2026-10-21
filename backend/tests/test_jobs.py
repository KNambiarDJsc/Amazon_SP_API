import pytest
from support import brands_of, job_of, make_runtime, submit

from app.amazon.mock import demo_client
from app.db import session_scope
from app.enums import BrandStatus, DataSource, JobStatus
from app.models import Job, SellerAccount
from app.schemas import JobSettings
from app.services.ingestion import IngestionError, rows_from_list, rows_from_text
from app.services.jobs import (
    MOCK_NOTICE,
    JobStateError,
    build_job_out,
    cancel_job,
    create_job,
    get_job,
    maybe_complete_job,
    pause_job,
    resume_job,
)
from app.services.tick import run_until_idle


def load(job_id) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


# ---- creation ------------------------------------------------------------------------------


def test_create_job_stores_brands_report_and_settings(engine, seller, clock):
    with session_scope() as s:
        job = create_job(
            s,
            seller=seller,
            rows=rows_from_text("eSSL\n\n ESSL \nKangaro"),
            settings=JobSettings(conditions=["new_new", "used_good"], max_asins_per_brand=4),
            data_source=DataSource.MOCK,
            name="October sourcing",
            source_filename="brands.xlsx",
            now=clock.now(),
        )
        job_id = job.id

    job = load(job_id)
    assert job.status == JobStatus.QUEUED
    assert job.name == "October sourcing" and job.source_filename == "brands.xlsx"
    assert job.settings["conditions"] == ["new_new", "used_good"]
    assert job.ingestion_report["accepted"] == 2
    assert job.ingestion_report["empty_rows"] == [2]
    assert [d["row"] for d in job.ingestion_report["duplicates"]] == [3]
    brands = brands_of(job_id)
    assert list(brands) == ["eSSL", "Kangaro"]
    assert all(
        b.status == BrandStatus.PENDING and b.seller_account_id == seller.id
        for b in brands.values()
    )


def test_create_job_with_nothing_usable_is_rejected_and_writes_nothing(engine, seller, clock):
    with pytest.raises(IngestionError, match="no valid brand names"):
        with session_scope() as s:
            create_job(
                s,
                seller=seller,
                rows=rows_from_list(["", "  ", "---"]),
                settings=JobSettings(),
                data_source=DataSource.MOCK,
                now=clock.now(),
            )
    with session_scope() as s:
        assert s.query(Job).count() == 0


# ---- tenant isolation ----------------------------------------------------------------------


def test_a_job_is_invisible_to_another_seller(engine, seller, clock):
    intruder = SellerAccount(name="Intruder", api_key_hash="c" * 64, api_key_prefix="int")
    with session_scope() as s:
        s.add(intruder)
    job_id = submit(seller, ["eSSL"], clock)

    with session_scope() as s:
        assert get_job(s, seller.id, job_id) is not None
        assert get_job(s, intruder.id, job_id) is None


# ---- controls ------------------------------------------------------------------------------


def test_pause_and_resume_round_trip(engine, seller, clock):
    job_id = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        pause_job(s, s.get(Job, job_id), reason="lunch", now=clock.now())
    paused = load(job_id)
    assert paused.status == JobStatus.PAUSED and paused.pause_reason == "lunch"

    with session_scope() as s:
        resume_job(s, s.get(Job, job_id), now=clock.now())
    resumed = load(job_id)
    assert resumed.status == JobStatus.QUEUED  # never started, so it is queued, not running
    assert resumed.pause_reason is None


def test_controls_are_idempotent_so_a_retried_request_is_harmless(engine, seller, clock):
    job_id = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        job = s.get(Job, job_id)
        pause_job(s, job, reason="x", now=clock.now())
        pause_job(s, job, reason="y", now=clock.now())  # second pause keeps the first reason
        resume_job(s, job, now=clock.now())
        resume_job(s, job, now=clock.now())
        cancel_job(s, job, now=clock.now())
        cancel_job(s, job, now=clock.now())
    assert load(job_id).status == JobStatus.CANCELLED


def test_impossible_transitions_are_refused(engine, seller, clock):
    job_id = submit(seller, ["Havells"], clock)
    run_until_idle(make_runtime(demo_client(), clock))
    assert load(job_id).status == JobStatus.COMPLETED

    with session_scope() as s:
        job = s.get(Job, job_id)
        for action in (
            lambda: pause_job(s, job, reason="x", now=clock.now()),
            lambda: resume_job(s, job, now=clock.now()),
            lambda: cancel_job(s, job, now=clock.now()),
        ):
            with pytest.raises(JobStateError):
                action()


def test_a_cancelled_job_cannot_be_resumed_or_paused(engine, seller, clock):
    job_id = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        job = s.get(Job, job_id)
        cancel_job(s, job, now=clock.now())
        with pytest.raises(JobStateError):
            resume_job(s, job, now=clock.now())
        with pytest.raises(JobStateError):
            pause_job(s, job, reason="x", now=clock.now())


def test_cancel_marks_unstarted_brands_cancelled_immediately(engine, seller, clock):
    job_id = submit(seller, ["eSSL", "Havells"], clock)
    with session_scope() as s:
        cancel_job(s, s.get(Job, job_id), now=clock.now())
    assert {b.status for b in brands_of(job_id).values()} == {BrandStatus.CANCELLED}
    assert load(job_id).finished_at == clock.now()


def test_job_completes_only_when_no_brand_is_unfinished(engine, seller, clock):
    job_id = submit(seller, ["eSSL"], clock)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = JobStatus.RUNNING
        assert maybe_complete_job(s, job, now=clock.now()) is False  # its brand is still PENDING
        s.query(type(next(iter(brands_of(job_id).values())))).update(
            {"status": BrandStatus.COMPLETED}
        )
        assert maybe_complete_job(s, job, now=clock.now()) is True
    assert load(job_id).status == JobStatus.COMPLETED


# ---- presentation --------------------------------------------------------------------------


def test_mock_jobs_carry_a_demo_notice_and_live_jobs_do_not(engine, seller, clock):
    mock_id = submit(seller, ["eSSL"], clock, data_source=DataSource.MOCK)
    live_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    with session_scope() as s:
        assert build_job_out(s, s.get(Job, mock_id)).notice == MOCK_NOTICE
        assert build_job_out(s, s.get(Job, live_id)).notice is None
    assert "DEMO" in MOCK_NOTICE
    assert job_of(mock_id).data_source == DataSource.MOCK
