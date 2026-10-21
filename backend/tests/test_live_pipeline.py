"""A complete job over the live code path: stored encrypted credentials -> LWA -> SP-API HTTP
(fake Amazon) -> parser -> rules engine -> checkpoints -> export."""

import pytest
from fake_spapi import CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, FakeSellingPartnerApi

from app.amazon.errors import AmazonRateLimited, AmazonRequestError
from app.amazon.live import LiveClientFactory
from app.amazon.mock import NO_RESTRICTIONS, MockAmazonClient, catalog_item, demo_client
from app.db import session_scope
from app.enums import BrandStatus, DataSource, JobStatus
from app.enums import EligibilityStatus as S
from app.models import SellerCredential
from app.services import credentials, orchestration
from app.services.credentials import CredentialCipher, generate_key

KEY = generate_key()
SELLER_ID = "A1SELLER12345"


@pytest.fixture
def live(seller, make_harness):
    """Returns a function building (fake_amazon, harness) wired through the live client."""

    def build(backend=None, *, connected=True, **overrides):
        fake = FakeSellingPartnerApi(backend or demo_client())
        h = make_harness(
            MockAmazonClient(),
            amazon_mode="live",
            lwa_client_id=CLIENT_ID,
            lwa_client_secret=CLIENT_SECRET,
            credential_encryption_keys=KEY,
            **overrides,
        )
        factory = LiveClientFactory(h.cfg, http=fake.client())
        h.processor.client_factory = factory
        with session_scope() as s:
            s.get(type(seller), seller.id).amazon_seller_id = SELLER_ID
            if connected:
                credentials.save_refresh_token(
                    s, CredentialCipher([KEY]), seller.id, REFRESH_TOKEN, authorization_type="SELF"
                )
        return fake, h

    return build


def submit_live(h, brands, **kw):
    return h.submit(brands, data_source=DataSource.LIVE, **kw)


def test_live_run_matches_the_offline_mock_result_for_result(live, make_harness):
    brands = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]
    fake, h = live()
    job = submit_live(h, brands)
    h.drain()

    offline = make_harness(demo_client())
    ref = offline.submit(brands)
    offline.drain()

    assert h.statuses(job) == offline.statuses(ref)
    assert {n: b.brand_status for n, b in h.brands(job).items()} == {
        n: b.brand_status for n, b in offline.brands(ref).items()
    }
    assert h.job(job).status == JobStatus.COMPLETED and h.job(job).data_source == DataSource.LIVE
    assert fake.protocol_violations == []
    assert fake.lwa_calls == 1  # one cached access token for the whole job


def test_live_jobs_are_not_labelled_as_demo_data(live):
    import io

    from openpyxl import load_workbook

    from app.models import Job
    from app.services.reporting import build_workbook

    _, h = live()
    job = submit_live(h, ["Havells"])
    h.drain()
    with session_scope() as s:
        wb = load_workbook(io.BytesIO(build_workbook(s, s.get(Job, job))))
    values = {c.value for c in wb["Restricted Products"]["P"][1:]}
    assert values == {"LIVE"} and wb.properties.subject is None


def test_access_token_expiring_mid_job_is_transparent(live):
    fake, h = live()
    job = submit_live(h, ["eSSL", "Havells"])
    h.processor.process(h.dispatcher.pop()[0])
    fake.expire_access_tokens()
    h.drain()
    assert h.job(job).status == JobStatus.COMPLETED
    assert not [s for s in h.statuses(job).values() if s == "NEEDS_MANUAL_CHECK"]
    assert fake.lwa_calls == 2


def test_amazon_throttling_over_http_defers_and_recovers(live):
    backend = MockAmazonClient(
        catalog={
            "Oddy": [
                catalog_item("B0LIVE0001", "x", "Oddy"),
                catalog_item("B0LIVE0002", "y", "Oddy"),
            ]
        },
        restrictions={
            "B0LIVE0001": NO_RESTRICTIONS,
            "B0LIVE0002": [AmazonRateLimited(retry_after=12), NO_RESTRICTIONS],
        },
    )
    fake, h = live(backend)
    job = submit_live(h, ["Oddy"])
    h.drain()
    assert h.brand(job, "Oddy").status == BrandStatus.COMPLETED
    # Amazon said 'limit 1/12 per second'; that hint (12s) outranks our own 5s base backoff.
    assert max(h.delays) == pytest.approx(12.0)
    assert set(h.statuses(job).values()) == {"VERIFIED_LISTABLE"}


def test_revoked_token_pauses_the_job_flags_the_credential_and_resumes_after_reconnect(
    live, seller
):
    fake, h = live()
    job = submit_live(h, ["eSSL", "Havells", "Kangaro"])
    h.processor.process(h.dispatcher.pop()[0])  # a first brand succeeds
    fake.revoke_refresh_tokens()
    fake.expire_access_tokens()  # so the next call must go back to LWA
    h.drain()

    j = h.job(job)
    assert j.status == JobStatus.PAUSED and j.pause_reason.startswith("AUTH_REQUIRED")
    assert "invalid_grant" in j.pause_reason
    with session_scope() as s:
        row = s.get(SellerCredential, seller.id)
        assert row.status == "REVOKED" and "invalid_grant" in row.last_error
    assert all(c.status is not None for c in h.checks(job))  # nothing half-recorded as a result
    assert not [o for o in h.observations(job) if o.outcome.value == "AUTH_ERROR"]

    # The seller reconnects with a fresh token; the factory notices the change and rebuilds.
    new_token = "Atzr|brand-new-refresh-token-0123456789"
    fake.valid_refresh_tokens.add(new_token)
    with session_scope() as s:
        credentials.save_refresh_token(
            s, CredentialCipher([KEY]), seller.id, new_token, authorization_type="OAUTH"
        )
    orchestration.resume(h.dispatcher, h.cfg, seller.id, job)
    h.drain()
    assert h.job(job).status == JobStatus.COMPLETED
    assert {n: b.status for n, b in h.brands(job).items()} == {
        "eSSL": BrandStatus.COMPLETED,
        "Havells": BrandStatus.COMPLETED,
        "Kangaro": BrandStatus.COMPLETED,
    }
    assert h.brand(job, "Kangaro").brand_status == S.MIXED_RESULTS


def test_missing_product_listing_role_pauses_with_actionable_guidance(live):
    fake, h = live()
    fake.deny_all_sp_api = True
    job = submit_live(h, ["eSSL"])
    h.drain()
    assert h.job(job).status == JobStatus.PAUSED
    assert "Product Listing role" in h.job(job).pause_reason
    assert h.observations(job) == []


def test_an_unconnected_seller_pauses_without_calling_amazon(live):
    fake, h = live(connected=False)
    job = submit_live(h, ["eSSL"])
    h.drain()
    assert h.job(job).status == JobStatus.PAUSED and "not connected" in h.job(job).pause_reason
    assert fake.requests == []


def test_credentials_that_cannot_be_decrypted_pause_the_job(live):
    fake, h = live()
    h.cfg.credential_encryption_keys = type(h.cfg.credential_encryption_keys)(generate_key())
    h.processor.client_factory = LiveClientFactory(
        h.cfg, http=fake.client()
    )  # operator lost the key
    job = submit_live(h, ["eSSL"])
    h.drain()
    assert h.job(job).status == JobStatus.PAUSED and "decrypted" in h.job(job).pause_reason
    assert fake.requests == []


def test_a_rejected_catalog_search_is_not_retried_for_that_brand_only(live):
    backend = demo_client()
    backend.catalog_behaviour = [AmazonRequestError(400, "InvalidInput: keywords too long"), None]
    fake, h = live(backend)
    job = submit_live(h, ["Oddy", "Havells"])
    h.drain()
    oddy = h.brand(job, "Oddy")
    assert oddy.status == BrandStatus.FAILED and oddy.attempts == 0  # failed at once, no retry loop
    assert "Catalog search rejected (HTTP 400)" in oddy.last_error
    assert oddy.brand_status == S.NEEDS_MANUAL_CHECK  # not NO_PRODUCT_FOUND: the search never ran
    assert h.brand(job, "Havells").brand_status == S.BRAND_APPROVAL_REQUIRED  # others unaffected
    assert len(backend.calls_to("searchCatalogItems")) == 2
    assert h.job(job).status == JobStatus.COMPLETED


def test_live_mode_refuses_to_start_without_credentials_configuration(make_harness):
    from app.amazon.live import validate_live_config
    from app.config import Settings

    with pytest.raises(
        RuntimeError, match="LWA_CLIENT_ID, LWA_CLIENT_SECRET, CREDENTIAL_ENCRYPTION_KEYS"
    ):
        validate_live_config(Settings(database_url="x", amazon_mode="live"))
    validate_live_config(
        Settings(
            database_url="x",
            lwa_client_id="a",
            lwa_client_secret="b",
            credential_encryption_keys=KEY,
        )
    )
