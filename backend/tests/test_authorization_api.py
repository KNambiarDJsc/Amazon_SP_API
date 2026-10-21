from urllib.parse import parse_qs, urlparse

import pytest
from fake_amazon import (
    CLIENT_ID,
    CLIENT_SECRET,
    ENCRYPTION_KEY,
    REDIRECT_URI,
    REFRESH_TOKEN,
    SELLING_PARTNER_ID,
    FakeAmazon,
    live_settings,
)
from fastapi.testclient import TestClient

from app.amazon.factory import LiveServices
from app.api.authorization import _build_live_services, optional_live_services
from app.api.main import create_app
from app.auth import create_seller_account
from app.config import get_settings
from app.db import session_scope

CALLBACK = "/api/v1/amazon/callback"


def auth(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}"}


@pytest.fixture
def live_env(monkeypatch):
    """AMAZON_MODE=live with a complete configuration, as an operator would deploy it."""
    for name, value in {
        "AMAZON_MODE": "live",
        "LWA_CLIENT_ID": CLIENT_ID,
        "LWA_CLIENT_SECRET": CLIENT_SECRET,
        "CREDENTIALS_ENCRYPTION_KEYS": ENCRYPTION_KEY,
        "SPAPI_APPLICATION_ID": "amzn1.sp.solution.test",
        "SPAPI_REDIRECT_URI": REDIRECT_URI,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake():
    return FakeAmazon()


@pytest.fixture
def client(engine, fake):
    app = create_app()
    services = LiveServices(live_settings(), http=fake.http())
    app.dependency_overrides[optional_live_services] = lambda: services
    return TestClient(app)


@pytest.fixture
def key(engine) -> str:
    with session_scope() as s:
        _, k = create_seller_account(s, name="Acme Traders")
    return k


@pytest.fixture
def other_key(engine) -> str:
    with session_scope() as s:
        _, k = create_seller_account(s, name="Rival Co")
    return k


def start(client, key) -> str:
    response = client.post("/api/v1/seller/authorization/start", headers=auth(key))
    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    assert url.startswith("https://sellercentral.amazon.in/apps/authorize/consent?")
    return parse_qs(urlparse(url).query)["state"][0]


def callback(client, state, code="auth-code-1", spid=SELLING_PARTNER_ID):
    return client.get(
        CALLBACK,
        params={"state": state, "spapi_oauth_code": code, "selling_partner_id": spid},
    )


def connect(client, key):
    response = callback(client, start(client, key))
    assert response.status_code == 200, response.text
    return response


# ---- the consent round trip ----------------------------------------------------------------


def test_status_starts_disconnected(client, key):
    response = client.get("/api/v1/seller/authorization", headers=auth(key))
    assert response.json() == {
        "connected": False,
        "status": None,
        "method": None,
        "selling_partner_id": None,
        "store_name": None,
        "authorized_at": None,
        "verified_at": None,
        "reauthorize_by": None,
        "last_error": None,
    }


def test_consent_round_trip_connects_the_seller(client, key):
    response = connect(client, key)
    assert "Amazon account connected" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    status = client.get("/api/v1/seller/authorization", headers=auth(key))
    body = status.json()
    assert body["connected"] is True and body["status"] == "ACTIVE"
    assert body["method"] == "OAUTH" and body["selling_partner_id"] == SELLING_PARTNER_ID
    assert body["store_name"] == "Test Store" and body["reauthorize_by"]
    assert REFRESH_TOKEN not in status.text


def test_a_callback_url_cannot_be_replayed(client, key):
    state = start(client, key)
    assert callback(client, state).status_code == 200
    replay = callback(client, state)
    assert replay.status_code == 400
    assert "already used or expired" in replay.text


def test_a_forged_state_connects_nobody(client, key):
    start(client, key)
    response = callback(client, "forged-state-value")
    assert response.status_code == 400
    assert (
        client.get("/api/v1/seller/authorization", headers=auth(key)).json()["connected"] is False
    )


def test_callback_without_parameters_is_a_friendly_error(client):
    response = client.get(CALLBACK)
    assert response.status_code == 400
    assert "Start again" in response.text


def test_callback_never_reflects_its_input_as_markup(client, key):
    response = callback(client, start(client, key), spid="<script>alert(1)</script>")
    assert response.status_code == 400
    assert "<script>" not in response.text


def test_starting_requires_a_seller_key(client):
    assert client.post("/api/v1/seller/authorization/start").status_code == 401


def test_without_live_configuration_authorisation_is_unavailable(engine, key, monkeypatch):
    monkeypatch.delenv("LWA_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    _build_live_services.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.post("/api/v1/seller/authorization/start", headers=auth(key))
        page = client.get(
            CALLBACK,
            params={
                "state": "s",
                "spapi_oauth_code": "c",
                "selling_partner_id": SELLING_PARTNER_ID,
            },
        )
    finally:
        get_settings.cache_clear()
        _build_live_services.cache_clear()
    assert response.status_code == 503 and "LWA_CLIENT_ID" in response.json()["detail"]
    assert page.status_code == 503


# ---- isolation, verification, disconnecting ------------------------------------------------


def test_one_sellers_grant_is_invisible_to_another(client, key, other_key):
    connect(client, key)
    other = client.get("/api/v1/seller/authorization", headers=auth(other_key)).json()
    assert other["connected"] is False


def test_verify_reports_a_revoked_grant(client, key, fake):
    connect(client, key)
    fake.revoke()
    response = client.post("/api/v1/seller/authorization/verify", headers=auth(key))
    assert response.status_code == 409
    status = client.get("/api/v1/seller/authorization", headers=auth(key)).json()
    assert status["status"] == "NEEDS_REAUTHORIZATION"


def test_verify_succeeds_on_a_working_grant(client, key):
    connect(client, key)
    response = client.post("/api/v1/seller/authorization/verify", headers=auth(key))
    assert response.status_code == 200 and response.json()["status"] == "ACTIVE"


def test_disconnect(client, key):
    connect(client, key)
    response = client.delete("/api/v1/seller/authorization", headers=auth(key))
    assert response.status_code == 200
    assert response.json()["connected"] is False and response.json()["status"] == "REVOKED"


# ---- live jobs -----------------------------------------------------------------------------


def test_live_jobs_need_a_connected_seller(client, key, live_env):
    refused = client.post("/api/v1/jobs", json={"brands": ["eSSL"]}, headers=auth(key))
    assert refused.status_code == 409
    assert "not connected to Amazon" in refused.json()["detail"]

    connect(client, key)
    created = client.post("/api/v1/jobs", json={"brands": ["eSSL"]}, headers=auth(key))
    assert created.status_code == 201, created.text
    assert created.json()["data_source"] == "LIVE"
    assert created.json()["notice"] is None


def test_resuming_a_live_job_after_disconnect_is_refused(client, key, live_env):
    connect(client, key)
    job = client.post("/api/v1/jobs", json={"brands": ["eSSL"]}, headers=auth(key)).json()
    client.delete("/api/v1/seller/authorization", headers=auth(key))

    status = client.get(f"/api/v1/jobs/{job['id']}", headers=auth(key)).json()
    assert status["status"] == "PAUSED"
    resumed = client.post(f"/api/v1/jobs/{job['id']}/resume", headers=auth(key))
    assert resumed.status_code == 409


def test_result_pages_escape_their_text():
    from app.api.authorization import _page

    body = _page("<b>title</b>", "<script>alert(1)</script>", 400).body.decode()
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "<b>title</b>" not in body
