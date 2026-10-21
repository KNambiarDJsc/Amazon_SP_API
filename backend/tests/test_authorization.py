"""Seller authorisation: consent flow, private-app tokens, verification, losing access."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fake_amazon import (
    REDIRECT_URI,
    REFRESH_TOKEN,
    SELLING_PARTNER_ID,
    FakeAmazon,
    live_settings,
    sp_error,
)
from sqlalchemy import inspect, select
from support import FakeClock, job_of, submit

from app.amazon.factory import LiveServices
from app.db import session_scope
from app.enums import AuthorizationMethod, AuthorizationStatus, DataSource, JobStatus
from app.models import OAuthState, SellerAccount, SellerAuthorization
from app.services.authorization import (
    AuthorizationError,
    complete_oauth,
    connect_with_refresh_token,
    consume_state,
    disconnect,
    get_grant,
    is_active,
    mark_needs_reauthorization,
    reverify,
    start_oauth,
)


@pytest.fixture
def fake():
    return FakeAmazon()


@pytest.fixture
def live(fake):
    return LiveServices(live_settings(), http=fake.http())


def grant_of(seller_id) -> SellerAuthorization | None:
    with session_scope() as s:
        return get_grant(s, seller_id)


def start(seller, clock: FakeClock, **settings) -> str:
    with session_scope() as s:
        url, _ = start_oauth(s, seller, settings=live_settings(**settings), now=clock.now())
    return parse_qs(urlparse(url).query)["state"][0]


def connect(seller, live, clock, **overrides):
    values = dict(
        seller_account_id=seller.id,
        selling_partner_id=SELLING_PARTNER_ID,
        refresh_token=REFRESH_TOKEN,
        method=AuthorizationMethod.SELF_AUTHORIZED,
        live=live,
        now=clock.now(),
    )
    values.update(overrides)
    return connect_with_refresh_token(**values)


# ---- website workflow ----------------------------------------------------------------------


def test_consent_url_targets_seller_central_india_with_a_fresh_state(engine, seller, clock):
    with session_scope() as s:
        url, expires_at = start_oauth(s, seller, settings=live_settings(), now=clock.now())
    parsed = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://sellercentral.amazon.in/apps/authorize/consent"
    )
    assert query["application_id"] == "amzn1.sp.solution.test"
    assert query["redirect_uri"] == REDIRECT_URI
    assert "version" not in query
    assert len(query["state"]) >= 40
    assert expires_at == clock.now() + timedelta(minutes=10)
    with session_scope() as s:
        stored = s.scalars(select(OAuthState)).all()
    assert [row.state_hash for row in stored] != [query["state"]]  # only the hash is kept


def test_draft_apps_get_version_beta(engine, seller, clock):
    with session_scope() as s:
        url, _ = start_oauth(
            s, seller, settings=live_settings(spapi_app_is_draft=True), now=clock.now()
        )
    assert parse_qs(urlparse(url).query)["version"] == ["beta"]


def test_consent_flow_needs_an_application_id_and_redirect_uri(engine, seller, clock):
    with session_scope() as s, pytest.raises(AuthorizationError, match="not configured"):
        start_oauth(s, seller, settings=live_settings(spapi_redirect_uri=None), now=clock.now())


def test_each_state_is_unique(engine, seller, clock):
    assert start(seller, clock) != start(seller, clock)


def test_complete_oauth_stores_an_encrypted_verified_grant(engine, seller, clock, fake, live):
    state = start(seller, clock)
    grant = complete_oauth(
        state=state,
        code="auth-code-1",
        selling_partner_id=SELLING_PARTNER_ID,
        live=live,
        now=clock.now(),
    )
    assert grant.status == AuthorizationStatus.ACTIVE
    assert grant.method == AuthorizationMethod.OAUTH
    assert grant.store_name == "Test Store"

    with session_scope() as s:
        row = get_grant(s, seller.id)
        account = s.get(SellerAccount, seller.id)
        # Nothing in the stored row or the account contains the token in clear text.
        for obj in (row, account):
            for column in inspect(obj).mapper.column_attrs:
                assert REFRESH_TOKEN not in str(getattr(obj, column.key))
        assert live.box.open(seller.id, row.refresh_token_encrypted) == REFRESH_TOKEN
        assert account.amazon_seller_id == SELLING_PARTNER_ID
        assert is_active(s, seller.id)


def test_a_state_works_once(engine, seller, clock, live):
    state = start(seller, clock)
    with session_scope() as s:
        consume_state(s, state, now=clock.now())
    with session_scope() as s, pytest.raises(AuthorizationError, match="already used or expired"):
        consume_state(s, state, now=clock.now())


def test_a_state_is_spent_even_when_the_code_exchange_fails(engine, seller, clock, live):
    state = start(seller, clock)
    with pytest.raises(AuthorizationError, match="did not accept"):
        complete_oauth(
            state=state,
            code="wrong-code",
            selling_partner_id=SELLING_PARTNER_ID,
            live=live,
            now=clock.now(),
        )
    with pytest.raises(AuthorizationError, match="already used or expired"):
        complete_oauth(
            state=state,
            code="auth-code-1",
            selling_partner_id=SELLING_PARTNER_ID,
            live=live,
            now=clock.now(),
        )
    assert grant_of(seller.id) is None


def test_an_expired_state_is_refused(engine, seller, clock, live):
    state = start(seller, clock)
    clock.advance(600)
    with session_scope() as s, pytest.raises(AuthorizationError):
        consume_state(s, state, now=clock.now())


def test_an_unknown_state_is_refused(engine, seller, clock):
    start(seller, clock)
    with session_scope() as s, pytest.raises(AuthorizationError):
        consume_state(s, "forged-state", now=clock.now())


def test_a_state_connects_the_seller_who_started_it(engine, seller, clock, live):
    with session_scope() as s:
        other = SellerAccount(name="Other", api_key_hash="b" * 64, api_key_prefix="o")
        s.add(other)
    state = start(seller, clock)
    complete_oauth(
        state=state,
        code="auth-code-1",
        selling_partner_id=SELLING_PARTNER_ID,
        live=live,
        now=clock.now(),
    )
    assert grant_of(seller.id) is not None
    assert grant_of(other.id) is None


# ---- connecting and verification -----------------------------------------------------------


def test_private_app_token_is_verified_and_stored(engine, seller, clock, fake, live):
    grant = connect(seller, live, clock)
    assert grant.status == AuthorizationStatus.ACTIVE
    assert grant.method == AuthorizationMethod.SELF_AUTHORIZED
    assert grant.verified_at == clock.now()
    # verification asked Amazon both questions: marketplace participation and catalog access
    assert {r.url.path for r in fake.sp_requests()} == {
        "/sellers/v1/marketplaceParticipations",
        "/catalog/2022-04-01/items",
    }


def test_a_token_amazon_rejects_is_never_stored(engine, seller, clock, fake, live):
    fake.revoke()
    with pytest.raises(AuthorizationError, match="invalid_grant"):
        connect(seller, live, clock)
    assert grant_of(seller.id) is None


def test_an_account_that_does_not_sell_in_india_is_refused(engine, seller, clock, fake, live):
    fake.participations[0]["participation"]["isParticipating"] = False
    with pytest.raises(AuthorizationError, match="does not sell in marketplace A21TJRUUN4KGV"):
        connect(seller, live, clock)
    assert grant_of(seller.id) is None


def test_an_account_without_india_at_all_is_refused(engine, seller, clock, fake, live):
    fake.participations[0]["marketplace"]["id"] = "A1F83G8C2ARO7P"  # UK
    with pytest.raises(AuthorizationError, match="does not sell"):
        connect(seller, live, clock)


def test_missing_product_listing_role_is_caught_at_connect_time(engine, seller, clock, fake, live):
    denied = sp_error(403, "Unauthorized", "Access to requested resource is denied.")
    fake.script("searchCatalogItems", denied, denied)
    with pytest.raises(AuthorizationError, match="Product Listing role"):
        connect(seller, live, clock)
    assert grant_of(seller.id) is None


def test_amazon_unreachable_during_verification_stores_an_unverified_grant(
    engine, seller, clock, fake, live
):
    fake.script("getMarketplaceParticipations", sp_error(503, "ServiceUnavailable", "try later"))
    grant = connect(seller, live, clock)
    assert grant.status == AuthorizationStatus.PENDING_VERIFICATION
    assert "could not verify yet" in grant.last_error
    with session_scope() as s:
        assert not is_active(s, seller.id)

    assert reverify(seller.id, live=live, now=clock.now()).status == AuthorizationStatus.ACTIVE


@pytest.mark.parametrize("spid", ["", "a2lowercase", "A2 SPACE", "A2;DROP", "X" * 41])
def test_selling_partner_id_is_validated(engine, seller, clock, live, spid):
    with pytest.raises(AuthorizationError, match="selling partner ID"):
        connect(seller, live, clock, selling_partner_id=spid)


def test_a_seller_account_cannot_be_switched_to_another_amazon_account(engine, seller, clock, live):
    connect(seller, live, clock)
    with pytest.raises(AuthorizationError, match="cannot be switched"):
        connect(seller, live, clock, selling_partner_id="A9DIFFERENT01")
    assert grant_of(seller.id).selling_partner_id == SELLING_PARTNER_ID


def test_reconnecting_the_same_account_replaces_the_token(engine, seller, clock, fake, live):
    connect(seller, live, clock)
    fake.refresh_tokens.add("Atzr|second")
    clock.advance(60)
    connect(seller, live, clock, refresh_token="Atzr|second")
    with session_scope() as s:
        rows = s.scalars(select(SellerAuthorization)).all()
        assert len(rows) == 1
        assert live.box.open(seller.id, rows[0].refresh_token_encrypted) == "Atzr|second"
        assert rows[0].authorized_at == clock.now()


# ---- losing access -------------------------------------------------------------------------


def test_reverify_marks_a_revoked_grant_for_reauthorisation(engine, seller, clock, fake, live):
    connect(seller, live, clock)
    fake.revoke()
    with pytest.raises(AuthorizationError):
        reverify(seller.id, live=live, now=clock.now())
    grant = grant_of(seller.id)
    assert grant.status == AuthorizationStatus.NEEDS_REAUTHORIZATION
    assert "invalid_grant" in grant.last_error


def test_disconnect_erases_the_token_and_pauses_jobs(engine, seller, clock, live):
    connect(seller, live, clock)
    job_id = submit(seller, ["eSSL"], clock, data_source=DataSource.LIVE)
    with session_scope() as s:
        disconnect(s, seller.id, now=clock.now())
    grant = grant_of(seller.id)
    assert grant.status == AuthorizationStatus.REVOKED
    assert grant.refresh_token_encrypted is None
    assert job_of(job_id).status == JobStatus.PAUSED
    with pytest.raises(AuthorizationError, match="not connected"):
        reverify(seller.id, live=live, now=clock.now())


def test_a_disconnected_grant_is_not_revived_by_a_later_failure(engine, seller, clock, live):
    connect(seller, live, clock)
    with session_scope() as s:
        disconnect(s, seller.id, now=clock.now())
        mark_needs_reauthorization(s, seller.id, detail="403", now=clock.now())
    assert grant_of(seller.id).status == AuthorizationStatus.REVOKED
