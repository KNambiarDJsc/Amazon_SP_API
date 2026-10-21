"""The whole pipeline in live mode: real scheduler, orchestrator, live SP-API client and LWA
token handling, talking HTTP to a fake Amazon. Only Amazon's servers and the clock are fake."""

import random

import httpx
import pytest
from fake_amazon import (
    REFRESH_TOKEN,
    SELLING_PARTNER_ID,
    FakeAmazon,
    live_settings,
    sp_error,
)
from support import (
    brands_of,
    checks_of,
    job_of,
    make_runtime,
    observations_of,
    submit,
)

from app.amazon.factory import LiveClientProvider, LiveServices
from app.amazon.live import RESTRICTIONS_PATH
from app.amazon.mock import NO_RESTRICTIONS, brand_gated, demo_client
from app.crypto import generate_key
from app.db import session_scope
from app.enums import (
    AuthorizationMethod,
    AuthorizationStatus,
    CheckState,
    DataSource,
    JobStatus,
    ObservationOutcome,
)
from app.enums import EligibilityStatus as S
from app.models import Job
from app.services.authorization import connect_with_refresh_token, get_grant
from app.services.jobs import JobStateError, resume_job
from app.services.orchestrator import Runtime
from app.services.ratelimit import InMemoryRateLimiter
from app.services.tick import run_until_idle

DEMO = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]


@pytest.fixture
def fake():
    return FakeAmazon()


def services_for(fake, clock, **settings) -> LiveServices:
    return LiveServices(live_settings(**settings), http=fake.http(), monotonic=clock.monotonic)


def live_runtime(services: LiveServices, clock) -> Runtime:
    return Runtime(
        clients=LiveClientProvider(services),
        limiter=InMemoryRateLimiter(clock.monotonic),
        settings=services.settings,
        clock=clock.now,
        sleep=clock.sleep,
        rng=random.Random(7),
    )


def connect(seller, services, clock, refresh_token=REFRESH_TOKEN):
    return connect_with_refresh_token(
        seller_account_id=seller.id,
        selling_partner_id=SELLING_PARTNER_ID,
        refresh_token=refresh_token,
        method=AuthorizationMethod.SELF_AUTHORIZED,
        live=services,
        now=clock.now(),
    )


def grant_status(seller_id):
    with session_scope() as s:
        return get_grant(s, seller_id).status


def test_demo_brands_over_http_reach_the_same_verdicts_with_request_ids_as_evidence(
    engine, seller, clock, fake
):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    job_id = submit(seller, DEMO, clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.COMPLETED
    brands = brands_of(job_id)
    assert {name: b.brand_status for name, b in brands.items()} == {
        "eSSL": S.VERIFIED_LISTABLE,
        "Havells": S.BRAND_APPROVAL_REQUIRED,
        "Kangaro": S.MIXED_RESULTS,
        "Taparia": S.OTHER_APPROVAL_REQUIRED,
        "Oddy": S.NO_PRODUCT_FOUND,
    }
    observations = observations_of(job_id)
    assert observations and all(
        o.evidence_ref.startswith("x-amzn-RequestId:req-") for o in observations
    )
    assert brands["eSSL"].checkpoint["catalog_evidence"][0].startswith("x-amzn-RequestId:")
    # every restrictions call was made for the connected seller, never a placeholder
    sellers = {r.url.params["sellerId"] for r in fake.sp_requests(RESTRICTIONS_PATH)}
    assert sellers == {SELLING_PARTNER_ID}


def test_one_access_token_serves_the_whole_job(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    submit(seller, DEMO, clock, data_source=DataSource.LIVE)
    lwa_before = len(fake.lwa_requests())

    run_until_idle(live_runtime(services, clock))
    assert len(fake.lwa_requests()) - lwa_before <= 1


def test_an_access_token_expiring_mid_job_is_invisible(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    fake.backing.restrictions["B08Y9C5H38"] = lambda n: (
        fake.expire_access_tokens(),
        brand_gated("B08Y9C5H38"),
    )[1]
    job_id = submit(seller, ["Havells", "eSSL"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.COMPLETED
    assert brands_of(job_id)["eSSL"].brand_status == S.VERIFIED_LISTABLE
    assert all(o.outcome == ObservationOutcome.OK for o in observations_of(job_id))


def test_unconnected_seller_never_reaches_amazon(engine, seller, clock, fake):
    services = services_for(fake, clock)
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert fake.requests == []


def test_seller_id_without_a_stored_grant_never_reaches_amazon(engine, seller, clock, fake):
    with session_scope() as s:
        s.get(type(seller), seller.id).amazon_seller_id = SELLING_PARTNER_ID
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services_for(fake, clock), clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert "authorisation" in job_of(job_id).pause_reason
    assert fake.requests == []


def test_a_grant_revoked_mid_job_pauses_until_the_seller_reconnects(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    # The seller removes the app in Seller Central right after this answer is served.
    fake.backing.restrictions["B08PDL14VR"] = lambda n: (fake.revoke(), NO_RESTRICTIONS)[1]
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    rt = live_runtime(services, clock)

    run_until_idle(rt)

    job = job_of(job_id)
    assert job.status == JobStatus.PAUSED and "reauthorise" in job.pause_reason
    assert grant_status(seller.id) == AuthorizationStatus.NEEDS_REAUTHORIZATION
    checks = checks_of(job_id)
    assert checks[("B08PDL14VR", "new_new")].status == S.VERIFIED_LISTABLE  # answered before
    refused = [o for o in observations_of(job_id) if o.outcome == ObservationOutcome.AUTH_ERROR]
    assert refused and "invalid_grant" in refused[0].error_detail
    pending = [c for c in checks.values() if c.state == CheckState.PENDING]
    assert pending  # the refused check stays open; it is not guessed at

    with session_scope() as s, pytest.raises(JobStateError, match="connect"):
        resume_job(s, s.get(Job, job_id), now=clock.now())

    fake.refresh_tokens.add("Atzr|reauthorised")
    connect(seller, services, clock, refresh_token="Atzr|reauthorised")
    with session_scope() as s:
        resume_job(s, s.get(Job, job_id), now=clock.now())
    run_until_idle(rt)

    assert job_of(job_id).status == JobStatus.COMPLETED
    assert brands_of(job_id)["eSSL"].brand_status == S.VERIFIED_LISTABLE
    assert all(c.state == CheckState.DONE for c in checks_of(job_id).values())


def test_credentials_that_no_longer_decrypt_pause_instead_of_failing_open(
    engine, seller, clock, fake
):
    connect(seller, services_for(fake, clock), clock)
    rotated_wrongly = services_for(fake, clock, credentials_encryption_keys=generate_key())
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    lwa_before = len(fake.lwa_requests())

    run_until_idle(live_runtime(rotated_wrongly, clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert grant_status(seller.id) == AuthorizationStatus.NEEDS_REAUTHORIZATION
    assert len(fake.lwa_requests()) == lwa_before and fake.sp_requests(RESTRICTIONS_PATH) == []


def test_amazon_throttling_over_http_defers_and_then_completes(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    fake.script(
        "getListingsRestrictions",
        sp_error(429, "QuotaExceeded", "You exceeded your quota for the requested resource."),
    )
    job_id = submit(seller, ["Havells"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.COMPLETED
    assert brands_of(job_id)["Havells"].brand_status == S.BRAND_APPROVAL_REQUIRED
    assert brands_of(job_id)["Havells"].attempts == 1  # the throttled attempt was refunded


def test_a_server_error_is_recorded_with_its_request_id_and_retried(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    fake.script(
        "getListingsRestrictions", sp_error(503, "ServiceUnavailable", "Service unavailable")
    )
    job_id = submit(seller, ["Havells"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    failed = [o for o in observations_of(job_id) if o.outcome == ObservationOutcome.HTTP_ERROR]
    assert failed[0].evidence_ref == "x-amzn-RequestId:req-error-503"
    assert brands_of(job_id)["Havells"].brand_status == S.BRAND_APPROVAL_REQUIRED


def test_a_rejected_check_is_manual_review_with_amazons_reason(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    fake.script(
        "getListingsRestrictions",
        lambda request: sp_error(400, "InvalidInput", "Invalid input"),
    )
    job_id = submit(
        seller,
        ["Havells"],
        clock,
        data_source=DataSource.LIVE,
        max_asins_per_brand=1,
        min_sample_size=1,
    )

    run_until_idle(live_runtime(services, clock))

    (check,) = checks_of(job_id).values()
    assert check.status == S.NEEDS_MANUAL_CHECK
    assert "InvalidInput" in check.rationale


def test_live_workers_will_not_run_a_demo_job(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    requests_before = len(fake.requests)
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.MOCK)

    run_until_idle(live_runtime(services, clock))

    job = job_of(job_id)
    assert job.status == JobStatus.PAUSED
    assert "MOCK data but the workers run LIVE" in job.pause_reason
    assert len(fake.requests) == requests_before


def test_demo_workers_will_not_run_a_live_job(engine, seller, clock):
    client = demo_client()
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)

    run_until_idle(make_runtime(client, clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert "LIVE data but the workers run MOCK" in job_of(job_id).pause_reason
    assert client.calls == []  # fixtures never answered a live job


def test_connection_failures_are_retried_like_any_transient_error(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)

    def reset(request):
        raise httpx.ConnectError("connection reset", request=request)

    fake.script("getListingsRestrictions", reset)
    job_id = submit(seller, ["Havells"], clock, data_source=DataSource.LIVE)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.COMPLETED
    assert brands_of(job_id)["Havells"].brand_status == S.BRAND_APPROVAL_REQUIRED


def test_an_unverified_grant_is_never_used(engine, seller, clock, fake):
    services = services_for(fake, clock)
    fake.script("getMarketplaceParticipations", sp_error(503, "ServiceUnavailable", "later"))
    connect(seller, services, clock)  # stored, but PENDING_VERIFICATION
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    requests_before = len(fake.requests)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert len(fake.requests) == requests_before


def test_a_grant_for_a_different_amazon_seller_is_never_used(engine, seller, clock, fake):
    services = services_for(fake, clock)
    connect(seller, services, clock)
    with session_scope() as s:  # an inconsistent row, e.g. after a bad manual edit
        s.get(type(seller), seller.id).amazon_seller_id = "A9SOMEONEELSE"
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    requests_before = len(fake.requests)

    run_until_idle(live_runtime(services, clock))

    assert job_of(job_id).status == JobStatus.PAUSED
    assert len(fake.requests) == requests_before
