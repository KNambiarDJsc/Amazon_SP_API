import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.enums import (
    BrandMatch,
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    JobStatus,
    ObservationOutcome,
    ObservationSource,
)
from app.models import (
    AuditEvent,
    CatalogCandidate,
    CheckObservation,
    EligibilityCheck,
    Job,
    JobBrand,
    SellerAccount,
)


def make_job(session, seller, **kw):
    job = Job(
        id=uuid.uuid4(),
        seller_account_id=seller.id,
        status=JobStatus.QUEUED,
        data_source=DataSource.MOCK,
        settings={},
        ingestion_report={},
        **kw,
    )
    session.add(job)
    session.commit()
    return job


def make_brand(session, job, name="eSSL", position=0):
    brand = JobBrand(
        id=uuid.uuid4(),
        job_id=job.id,
        seller_account_id=job.seller_account_id,
        position=position,
        original_name=name,
        display_name=name,
        dedupe_key=name.casefold(),
    )
    session.add(brand)
    session.commit()
    return brand


def make_candidate(session, brand, asin="B08PDL14VR"):
    cand = CatalogCandidate(
        id=uuid.uuid4(),
        job_brand_id=brand.id,
        seller_account_id=brand.seller_account_id,
        asin=asin,
        marketplace_id="A21TJRUUN4KGV",
        brand_match=BrandMatch.EXACT,
    )
    session.add(cand)
    session.commit()
    return cand


def make_check(session, cand, brand, key="k1", **kw):
    check = EligibilityCheck(
        id=uuid.uuid4(),
        seller_account_id=brand.seller_account_id,
        job_id=brand.job_id,
        job_brand_id=brand.id,
        candidate_id=cand.id,
        asin=cand.asin,
        marketplace_id=cand.marketplace_id,
        condition="new_new",
        verification_context="sp_api",
        check_key=key,
        **kw,
    )
    session.add(check)
    session.commit()
    return check


def test_round_trip_with_defaults_and_timezone_aware_datetimes(session, seller):
    job = make_job(session, seller)
    brand = make_brand(session, job)
    session.expire_all()
    loaded = session.get(JobBrand, brand.id)
    assert loaded.status == BrandStatus.PENDING and loaded.attempts == 0
    assert loaded.created_at.tzinfo is not None
    assert session.get(Job, job.id).created_at.utcoffset().total_seconds() == 0


def test_naive_datetimes_are_rejected(session, seller):
    job = make_job(session, seller)
    brand = make_brand(session, job)
    brand.started_at = datetime(2026, 1, 1)
    with pytest.raises((ValueError, StatementError)):
        session.commit()
    session.rollback()


def test_job_brand_cannot_point_into_another_sellers_job(session, seller):
    other = SellerAccount(
        id=uuid.uuid4(), name="Other", api_key_hash="f" * 64, api_key_prefix="oth"
    )
    session.add(other)
    session.commit()
    job = make_job(session, seller)
    session.add(
        JobBrand(
            id=uuid.uuid4(),
            job_id=job.id,
            seller_account_id=other.id,
            position=0,
            original_name="x",
            display_name="x",
            dedupe_key="x",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_check_cannot_reference_a_candidate_of_another_seller(session, seller):
    other = SellerAccount(
        id=uuid.uuid4(), name="Other", api_key_hash="e" * 64, api_key_prefix="oth"
    )
    session.add(other)
    session.commit()
    brand = make_brand(session, make_job(session, seller))
    cand = make_candidate(session, brand)
    other_brand = make_brand(session, make_job(session, other), name="Other brand")
    session.add(
        EligibilityCheck(
            id=uuid.uuid4(),
            seller_account_id=other.id,
            job_id=other_brand.job_id,
            job_brand_id=other_brand.id,
            candidate_id=cand.id,  # seller A's candidate
            asin=cand.asin,
            marketplace_id="A21TJRUUN4KGV",
            condition="new_new",
            verification_context="sp_api",
            check_key="x",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_duplicate_brand_in_a_job_is_rejected_but_allowed_in_another_job(session, seller):
    job1, job2 = make_job(session, seller), make_job(session, seller)
    make_brand(session, job1, "eSSL")
    make_brand(session, job2, "eSSL")  # a different job may check the same brand
    session.add(
        JobBrand(
            id=uuid.uuid4(),
            job_id=job1.id,
            seller_account_id=seller.id,
            position=1,
            original_name="ESSL",
            display_name="ESSL",
            dedupe_key="essl",
        )
    )
    session.add(
        JobBrand(
            id=uuid.uuid4(),
            job_id=job1.id,
            seller_account_id=seller.id,
            position=2,
            original_name="essl",
            display_name="essl",
            dedupe_key="essl",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_check_key_is_unique_per_job(session, seller):
    brand = make_brand(session, make_job(session, seller))
    cand = make_candidate(session, brand)
    make_check(session, cand, brand, key="same")
    with pytest.raises(IntegrityError):
        make_check(session, cand, brand, key="same")
    session.rollback()


def test_a_check_is_done_if_and_only_if_it_has_a_status(session, seller):
    brand = make_brand(session, make_job(session, seller))
    cand = make_candidate(session, brand)
    with pytest.raises(IntegrityError):
        make_check(session, cand, brand, key="a", state=CheckState.DONE)  # DONE without status
    session.rollback()
    with pytest.raises(IntegrityError):
        make_check(
            session,
            cand,
            brand,
            key="b",
            state=CheckState.PENDING,
            status=EligibilityStatus.VERIFIED_LISTABLE,
        )  # status without DONE
    session.rollback()
    make_check(
        session, cand, brand, key="c", state=CheckState.DONE, status=EligibilityStatus.NOT_ELIGIBLE
    )


def test_unknown_enum_strings_are_rejected(session, seller):
    job = make_job(session, seller)
    brand = make_brand(session, job)
    brand.brand_status = "VERIFIED_UNGATED_FOREVER"
    with pytest.raises((LookupError, StatementError, ValueError)):
        session.commit()
    session.rollback()


def test_observation_and_audit_round_trip_json(session, seller):
    job = make_job(session, seller)
    brand = make_brand(session, job)
    cand = make_candidate(session, brand)
    check = make_check(session, cand, brand)
    obs = CheckObservation(
        check_id=check.id,
        seller_account_id=seller.id,
        source=ObservationSource.SP_API_RESTRICTIONS,
        outcome=ObservationOutcome.OK,
        http_status=200,
        context={"asin": cand.asin},
        signals={"restrictions_present": True, "entries": []},
        raw={"restrictions": []},
        observed_at=datetime(2026, 10, 21, tzinfo=UTC),
    )
    session.add(obs)
    session.add(
        AuditEvent(seller_account_id=seller.id, job_id=job.id, event_type="X", payload={"a": 1})
    )
    session.commit()
    session.expire_all()
    got = session.scalars(select(CheckObservation)).one()
    assert got.raw == {"restrictions": []} and got.signals["restrictions_present"] is True
    assert got.id >= 1
    assert session.scalars(select(AuditEvent)).one().payload == {"a": 1}


def test_database_connection_is_usable(session):
    assert session.execute(text("select 1")).scalar() == 1
