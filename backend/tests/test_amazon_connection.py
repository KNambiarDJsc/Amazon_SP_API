import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import IS_POSTGRES
from fake_spapi import (
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    REFRESH_TOKEN,
    FakeSellingPartnerApi,
)
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.deps import dispatcher_dep, http_dep, settings_dep
from app.api.main import create_app
from app.config import Settings
from app.db import session_scope
from app.domain.observations import utcnow
from app.models import AuditEvent, OAuthState, SellerAccount, SellerCredential
from app.services import oauth
from app.services.accounts import create_seller_account
from app.services.credentials import generate_key
from app.services.dispatch import InlineDispatcher

SELLER_ID = "A1SELLER12345"


class Env:
    def __init__(self, **overrides):
        base = dict(
            database_url="sqlite://",
            amazon_mode="live",
            lwa_client_id=CLIENT_ID,
            lwa_client_secret=CLIENT_SECRET,
            sp_api_application_id="amzn1.sp.solution.test-app",
            oauth_redirect_uri=REDIRECT_URI,
            credential_encryption_keys=generate_key(),
        )
        self.cfg = Settings(**{**base, **overrides})
        self.fake = FakeSellingPartnerApi()
        app = create_app()
        app.dependency_overrides[settings_dep] = lambda: self.cfg
        app.dependency_overrides[http_dep] = lambda: self.fake.client()
        app.dependency_overrides[dispatcher_dep] = lambda: InlineDispatcher()
        self.http = TestClient(app)

    def seller(self, name="Acme"):
        with session_scope() as s:
            account, key = create_seller_account(s, name=name)
            return account.id, {"Authorization": f"Bearer {key}"}


@pytest.fixture
def env(engine):
    return Env()


@pytest.fixture
def me(env):
    return env.seller()


def put(env, headers, **body):
    payload = {"seller_id": SELLER_ID, "refresh_token": REFRESH_TOKEN, **body}
    return env.http.put("/api/v1/amazon/connection", json=payload, headers=headers)


# ---- self-authorisation (private application) ------------------------------------------------


def test_self_authorisation_verifies_stores_encrypted_and_reports_status(env, me):
    seller_id, h = me
    assert env.http.get("/api/v1/amazon/connection", headers=h).json()["connected"] is False

    r = put(env, h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["connected"]
        and body["seller_id"] == SELLER_ID
        and body["authorization_type"] == "SELF"
    )
    assert body["status"] == "ACTIVE" and body["last_verified_at"] and not body["needs_reconnect"]

    # The token was proven valid with Amazon first ...
    assert env.fake.lwa_forms[0]["refresh_token"] == REFRESH_TOKEN
    # ... stored encrypted, never in plaintext ...
    with session_scope() as s:
        raw = bytes(s.scalars(select(SellerCredential.refresh_token_encrypted)).one())
        assert REFRESH_TOKEN.encode() not in raw
        assert s.get(SellerAccount, seller_id).amazon_seller_id == SELLER_ID
        assert [e.event_type for e in s.scalars(select(AuditEvent))] == ["SELLER_CONNECTED"]
    # ... and never returned or reflected anywhere.
    for response in (
        r,
        env.http.get("/api/v1/amazon/connection", headers=h),
        env.http.get("/api/v1/me", headers=h),
    ):
        assert REFRESH_TOKEN not in response.text and "Atzr" not in response.text
    assert env.http.get("/api/v1/me", headers=h).json()["connected_to_amazon"] is True


def test_a_token_amazon_rejects_is_not_stored(env, me):
    env.fake.revoke_refresh_tokens()
    r = put(env, me[1])
    assert r.status_code == 400 and "did not accept this refresh token" in r.json()["detail"]
    assert REFRESH_TOKEN not in r.text
    with session_scope() as s:
        assert s.scalars(select(SellerCredential)).all() == []
        assert s.get(SellerAccount, me[0]).amazon_seller_id is None


@pytest.mark.parametrize("seller_id", ["short", "has spaces here", "A1;DROP TABLE x", "a" * 40])
def test_malformed_merchant_ids_are_rejected(env, me, seller_id):
    assert put(env, me[1], seller_id=seller_id).status_code in (400, 422)


def test_short_token_is_422_and_missing_key_is_401(env, me):
    assert put(env, me[1], refresh_token="short").status_code == 422
    assert env.http.put("/api/v1/amazon/connection", json={}).status_code == 401
    assert env.http.get("/api/v1/amazon/connection").status_code == 401


def test_amazon_being_down_is_reported_not_stored(env, me):
    import httpx

    app = env.http.app
    app.dependency_overrides[http_dep] = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(503))
    )
    r = put(env, me[1])
    assert r.status_code == 400 and "Could not reach Amazon" in r.json()["detail"]


def test_missing_encryption_key_is_503_with_guidance(engine):
    env = Env(credential_encryption_keys="")
    _, h = env.seller()
    r = put(env, h)
    assert r.status_code == 503 and "generate-encryption-key" in r.json()["detail"]


def test_verify_detects_a_revoked_credential_and_flags_reconnect(env, me):
    _, h = me
    put(env, h)
    assert env.http.post("/api/v1/amazon/connection/verify", headers=h).status_code == 200
    env.fake.revoke_refresh_tokens()
    r = env.http.post("/api/v1/amazon/connection/verify", headers=h)
    assert r.status_code == 400 and "rejected the stored credential" in r.json()["detail"]
    status = env.http.get("/api/v1/amazon/connection", headers=h).json()
    assert (
        status["connected"] is False and status["needs_reconnect"] and status["status"] == "REVOKED"
    )
    assert "invalid_grant" in status["last_error"]
    assert env.http.get("/api/v1/me", headers=h).json()["connected_to_amazon"] is False


def test_verify_when_not_connected(env, me):
    r = env.http.post("/api/v1/amazon/connection/verify", headers=me[1])
    assert r.status_code == 400 and "not connected" in r.json()["detail"]


def test_disconnect_deletes_the_credential_and_is_idempotent(env, me):
    _, h = me
    put(env, h)
    assert env.http.delete("/api/v1/amazon/connection", headers=h).status_code == 204
    assert env.http.delete("/api/v1/amazon/connection", headers=h).status_code == 204
    assert env.http.get("/api/v1/amazon/connection", headers=h).json()["connected"] is False
    with session_scope() as s:
        assert s.scalars(select(SellerCredential)).all() == []
        assert [e.event_type for e in s.scalars(select(AuditEvent))] == [
            "SELLER_CONNECTED",
            "SELLER_DISCONNECTED",
        ]


def test_each_seller_has_their_own_connection(env):
    _, a = env.seller("A")
    _, b = env.seller("B")
    put(env, a)
    assert env.http.get("/api/v1/amazon/connection", headers=a).json()["connected"] is True
    assert env.http.get("/api/v1/amazon/connection", headers=b).json()["connected"] is False
    assert env.http.delete("/api/v1/amazon/connection", headers=b).status_code == 204
    assert env.http.get("/api/v1/amazon/connection", headers=a).json()["connected"] is True


# ---- website authorisation (public application) ----------------------------------------------


def begin(env, headers):
    r = env.http.post("/api/v1/amazon/authorize", headers=headers)
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["authorization_url"])
    return url, parse_qs(url.query)


def callback(env, **params):
    return env.http.get("/api/v1/amazon/callback", params=params)


def good_params(state):
    return {"state": state, "spapi_oauth_code": AUTH_CODE, "selling_partner_id": SELLER_ID}


def test_authorize_builds_the_seller_central_consent_url(env, me):
    url, q = begin(env, me[1])
    assert (
        f"{url.scheme}://{url.netloc}{url.path}"
        == "https://sellercentral.amazon.in/apps/authorize/consent"
    )
    assert q["application_id"] == ["amzn1.sp.solution.test-app"]
    assert len(q["state"][0]) >= 40 and "version" not in q  # high-entropy state; production app


def test_draft_applications_get_version_beta(engine):
    env = Env(sp_api_draft_app=True)
    _, h = env.seller()
    assert begin(env, h)[1]["version"] == ["beta"]


def test_each_authorize_call_gets_a_different_state(env, me):
    assert begin(env, me[1])[1]["state"] != begin(env, me[1])[1]["state"]


def test_authorize_needs_configuration_and_auth(engine):
    env = Env(sp_api_application_id="", oauth_redirect_uri="")
    _, h = env.seller()
    r = env.http.post("/api/v1/amazon/authorize", headers=h)
    assert r.status_code == 501 and "self-authorisation" in r.json()["detail"]
    assert env.http.post("/api/v1/amazon/authorize").status_code == 401


def test_full_callback_flow_connects_the_seller_who_started_it(env):
    seller_a, a = env.seller("A")
    seller_b, b = env.seller("B")
    _, q = begin(env, a)

    r = callback(env, **good_params(q["state"][0]))  # note: no API key, as from a real redirect
    assert r.status_code == 200 and "Connected" in r.text
    assert env.fake.lwa_forms[-1] == {
        "grant_type": "authorization_code",
        "code": AUTH_CODE,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    status_a = env.http.get("/api/v1/amazon/connection", headers=a).json()
    assert status_a["connected"] and status_a["authorization_type"] == "OAUTH"
    assert status_a["seller_id"] == SELLER_ID
    assert env.http.get("/api/v1/amazon/connection", headers=b).json()["connected"] is False
    with session_scope() as s:
        assert {c.seller_account_id for c in s.scalars(select(SellerCredential))} == {seller_a}


def test_a_state_can_only_be_used_once(env, me):
    _, q = begin(env, me[1])
    assert callback(env, **good_params(q["state"][0])).status_code == 200
    replay = callback(env, **good_params(q["state"][0]))
    assert replay.status_code == 400 and "invalid or has expired" in replay.text


def test_unknown_expired_and_used_states_are_indistinguishable(env, me):
    _, q = begin(env, me[1])
    state = q["state"][0]
    with session_scope() as s:
        s.get(OAuthState, state).expires_at = utcnow() - timedelta(seconds=1)
    expired = callback(env, **good_params(state))
    unknown = callback(env, **good_params("totally-made-up-state-value"))
    assert expired.status_code == unknown.status_code == 400
    assert expired.text == unknown.text  # no oracle for probing which states exist


def test_a_failed_code_exchange_spends_the_state_and_stores_nothing(env, me):
    _, q = begin(env, me[1])
    r = callback(env, **{**good_params(q["state"][0]), "spapi_oauth_code": "wrong-code"})
    assert r.status_code == 400 and "did not accept the authorisation" in r.text
    with session_scope() as s:
        assert s.scalars(select(SellerCredential)).all() == []
        assert s.get(OAuthState, q["state"][0]).used_at is not None
    # retrying with the right code but the spent state does not work either
    assert callback(env, **good_params(q["state"][0])).status_code == 400


@pytest.mark.parametrize("missing", ["state", "spapi_oauth_code", "selling_partner_id"])
def test_incomplete_callbacks_are_rejected_without_touching_state(env, me, missing):
    _, q = begin(env, me[1])
    params = good_params(q["state"][0])
    del params[missing]
    assert callback(env, **params).status_code == 400
    with session_scope() as s:
        assert s.get(OAuthState, q["state"][0]).used_at is None  # still usable


def test_a_malformed_partner_id_in_the_callback_is_rejected(env, me):
    _, q = begin(env, me[1])
    r = callback(env, **{**good_params(q["state"][0]), "selling_partner_id": "bad id!"})
    assert r.status_code == 400
    with session_scope() as s:
        assert s.scalars(select(SellerCredential)).all() == []


def test_callback_never_reflects_query_parameters(env, me):
    payload = "<script>alert(1)</script>"
    for params in (
        {"state": payload, "spapi_oauth_code": payload, "selling_partner_id": payload},
        {"state": "x" * 50, "spapi_oauth_code": payload, "selling_partner_id": "A1SELLER12345"},
    ):
        r = callback(env, **params)
        assert "<script>" not in r.text and "alert(1)" not in r.text
        assert (
            r.headers["cache-control"] == "no-store"
            and r.headers["referrer-policy"] == "no-referrer"
        )
        assert r.headers["content-type"].startswith("text/html")


def test_oversized_callback_parameters_are_refused(env):
    assert (
        callback(
            env, state="s" * 500, spapi_oauth_code="c", selling_partner_id="A1SELLER12345"
        ).status_code
        == 422
    )


@pytest.mark.skipif(not IS_POSTGRES, reason="needs PostgreSQL for real concurrency")
def test_two_simultaneous_callbacks_with_one_state_connect_exactly_once(env, me):
    _, q = begin(env, me[1])
    results, barrier = [], threading.Barrier(6)

    def hit():
        barrier.wait()
        results.append(callback(env, **good_params(q["state"][0])).status_code)

    threads = [threading.Thread(target=hit) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 400, 400, 400, 400, 400]


def test_oauth_module_reports_errors_without_secrets(env, me):
    _, q = begin(env, me[1])
    r = callback(env, **{**good_params(q["state"][0]), "spapi_oauth_code": "wrong-code"})
    for secret in (CLIENT_SECRET, REFRESH_TOKEN, "wrong-code"):
        assert secret not in r.text
    assert oauth.SELLER_ID_RE.match(SELLER_ID)
    with session_scope() as s:
        assert s.execute(text("select count(*) from oauth_states")).scalar() == 1
