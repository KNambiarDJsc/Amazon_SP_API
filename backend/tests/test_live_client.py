import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fake_spapi import (
    CLIENT_ID,
    CLIENT_SECRET,
    REFRESH_TOKEN,
    FakeSellingPartnerApi,
)

from app.amazon.errors import (
    AmazonAuthError,
    AmazonCredentialsRevoked,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.amazon.live import USER_AGENT, SpApiClient
from app.amazon.lwa import LwaTokenProvider
from app.amazon.mock import (
    NO_RESTRICTIONS,
    MockAmazonClient,
    brand_gated,
    catalog_item,
    demo_client,
)
from app.amazon.parsing import parse_catalog_page, parse_restrictions_observation
from app.domain.classification import classify_observation
from app.domain.observations import ObservationContext
from app.enums import MARKETPLACE_ID_IN
from app.enums import EligibilityStatus as S

BASE = "https://sellingpartnerapi-eu.amazon.com"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


def make(
    fake=None,
    *,
    clock=None,
    refresh_token=REFRESH_TOKEN,
    client_secret=CLIENT_SECRET,
    on_revoked=None,
):
    fake = fake or FakeSellingPartnerApi(demo_client())
    http = fake.client()
    tokens = LwaTokenProvider(
        http,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=client_secret,
        refresh_token=refresh_token,
        clock=clock or Clock(),
        on_revoked=on_revoked,
    )
    return fake, SpApiClient(http=http, base_url=BASE, tokens=tokens), tokens


def restrictions(client, asin="B08Y9C5H38", condition="new_new"):
    return client.get_listings_restrictions(
        asin=asin, seller_id="A1SELLER12345", marketplace_id=MARKETPLACE_ID_IN, condition=condition
    )


# ---- request construction (the documented contract) ------------------------------------------


def test_catalog_search_request_shape():
    fake, client, _ = make()
    client.search_catalog_items(keywords="eSSL", marketplace_id=MARKETPLACE_ID_IN)
    (req,) = fake.sp_api_requests("/catalog")
    assert req.method == "GET" and str(req.url).startswith(f"{BASE}/catalog/2022-04-01/items?")
    params = dict(req.url.params)
    assert params == {
        "keywords": "eSSL",
        "marketplaceIds": "A21TJRUUN4KGV",
        "includedData": "summaries,productTypes",
        "pageSize": "20",
    }


def test_catalog_pagination_token_and_page_size_cap():
    fake, client, _ = make()
    client.search_catalog_items(
        keywords="x", marketplace_id=MARKETPLACE_ID_IN, page_token="page:20", page_size=500
    )
    params = dict(fake.sp_api_requests("/catalog")[0].url.params)
    assert params["pageToken"] == "page:20" and params["pageSize"] == "20"  # Amazon's maximum


def test_restrictions_request_shape():
    fake, client, _ = make()
    restrictions(client, condition="used_good")
    (req,) = fake.sp_api_requests("/listings")
    assert req.url.path == "/listings/2021-08-01/restrictions"
    assert dict(req.url.params) == {
        "asin": "B08Y9C5H38",
        "sellerId": "A1SELLER12345",
        "marketplaceIds": "A21TJRUUN4KGV",
        "conditionType": "used_good",
        "reasonLocale": "en_US",
    }


def test_authentication_is_one_lwa_token_header_and_no_sigv4():
    fake, client, _ = make()
    restrictions(client)
    (req,) = fake.sp_api_requests()
    assert req.headers["x-amz-access-token"].startswith("Atza|")
    assert (
        req.headers["user-agent"] == USER_AGENT and "AmazonSellerEligibilityChecker" in USER_AGENT
    )
    assert "authorization" not in req.headers and "x-amz-date" not in req.headers
    assert fake.protocol_violations == []


def test_lwa_request_is_a_form_encoded_refresh_token_grant():
    fake, client, _ = make()
    restrictions(client)
    assert fake.lwa_forms == [
        {
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
    ]
    assert fake.protocol_violations == []


# ---- results go through the same parser and rules engine as the mock -------------------------


def test_live_http_path_yields_the_same_classification_as_the_offline_mock():
    fake, client, _ = make()
    page = parse_catalog_page(
        client.search_catalog_items(keywords="eSSL", marketplace_id=MARKETPLACE_ID_IN),
        MARKETPLACE_ID_IN,
    )
    assert [i.asin for i in page.items][:2] == ["B08PDL14VR", "B0BXM5CGRM"]
    assert page.items[0].brand == "eSSL" and page.total_results == 5

    def classify(asin):
        obs = parse_restrictions_observation(
            restrictions(client, asin),
            context=ObservationContext(
                asin=asin,
                marketplace_id=MARKETPLACE_ID_IN,
                condition="new_new",
                seller_ref="A1SELLER12345",
            ),
        )
        return classify_observation(obs).status

    assert classify("B08PDL14VR") == S.VERIFIED_LISTABLE
    assert classify("B08Y9C5H38") == S.BRAND_APPROVAL_REQUIRED
    assert classify("B0MOCKTP01") == S.OTHER_APPROVAL_REQUIRED
    assert classify("B0NEVERSEEN") == S.NEEDS_MANUAL_CHECK  # ASIN_NOT_FOUND over real HTTP


# ---- token lifecycle -------------------------------------------------------------------------


def test_access_token_is_cached_then_renewed_before_it_expires():
    clock = Clock()
    fake, client, _ = make(clock=clock)
    for _ in range(5):
        restrictions(client)
    assert fake.lwa_calls == 1  # one token served five requests

    clock.t += timedelta(seconds=3600 - 30)  # inside the 60s safety margin
    restrictions(client)
    assert fake.lwa_calls == 2
    restrictions(client)
    assert fake.lwa_calls == 2


def test_an_expired_token_is_refreshed_once_and_the_request_retried():
    fake, client, _ = make()
    restrictions(client)
    fake.expire_access_tokens()  # Amazon expires it earlier than our clock expected
    assert restrictions(client)["restrictions"]  # transparently succeeds
    assert fake.lwa_calls == 2
    assert len(fake.sp_api_requests()) == 3  # ok, denied, retried


def test_persistent_403_gives_up_after_one_refresh_and_says_what_to_check():
    fake, client, _ = make()
    fake.deny_all_sp_api = True  # e.g. missing Product Listing role
    with pytest.raises(AmazonAuthError) as info:
        restrictions(client)
    assert "Product Listing role" in str(info.value) and "403" in str(info.value)
    assert fake.lwa_calls == 2 and len(fake.sp_api_requests()) == 2  # no retry loop


def test_concurrent_cold_callers_trigger_exactly_one_token_refresh():
    fake, _, tokens = make()
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(tokens.get_access_token())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert fake.lwa_calls == 1 and len(set(results)) == 1


# ---- LWA failure modes -----------------------------------------------------------------------


def test_revoked_refresh_token_is_reported_as_revoked_and_notifies():
    seen = []
    fake, _, tokens = make(on_revoked=seen.append)
    fake.revoke_refresh_tokens()
    with pytest.raises(AmazonCredentialsRevoked) as info:
        tokens.get_access_token()
    assert "invalid_grant" in str(info.value) and len(seen) == 1


def test_wrong_application_secret_is_an_auth_error_but_does_not_revoke_the_seller():
    seen = []
    _, _, tokens = make(client_secret="wrong", on_revoked=seen.append)
    with pytest.raises(AmazonAuthError) as info:
        tokens.get_access_token()
    assert not isinstance(info.value, AmazonCredentialsRevoked)
    assert "invalid_client" in str(info.value) and seen == []  # not the seller's fault


@pytest.mark.parametrize(
    ("handler", "exc"),
    [
        (lambda r: httpx.Response(500), AmazonTransientError),
        (lambda r: httpx.Response(429), AmazonTransientError),
        (lambda r: httpx.Response(200, text="<html>"), AmazonMalformedResponse),
        (lambda r: httpx.Response(200, json=[1]), AmazonMalformedResponse),
        (lambda r: httpx.Response(200, json={"token_type": "bearer"}), AmazonMalformedResponse),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("dns")), AmazonTransientError),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), AmazonTransientError),
    ],
)
def test_lwa_transport_and_shape_failures_map_to_typed_errors(handler, exc):
    tokens = LwaTokenProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
    )
    with pytest.raises(exc):
        tokens.get_access_token()


def test_secrets_never_appear_in_error_messages():
    for failing in ("revoked", "bad_secret", "denied"):
        fake, client, tokens = make(
            client_secret="wrong" if failing == "bad_secret" else CLIENT_SECRET
        )
        if failing == "revoked":
            fake.revoke_refresh_tokens()
        if failing == "denied":
            fake.deny_all_sp_api = True
        with pytest.raises(Exception) as info:
            restrictions(client)
        text = f"{info.value!r} {info.value}"
        for secret in (REFRESH_TOKEN, CLIENT_SECRET, "wrong", "Atza|"):
            assert secret not in text, (failing, secret)


# ---- HTTP status mapping ---------------------------------------------------------------------


def client_returning(response):
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: (
                httpx.Response(200, json={"access_token": "Atza|x", "expires_in": 3600})
                if req.url.host == "api.amazon.com"
                else response(req)
            )
        )
    )
    tokens = LwaTokenProvider(
        http,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        refresh_token=REFRESH_TOKEN,
    )
    return SpApiClient(http=http, base_url=BASE, tokens=tokens)


@pytest.mark.parametrize("status", [400, 404, 405, 413, 415])
def test_client_errors_are_request_errors_with_amazons_message(status):
    client = client_returning(
        lambda r: httpx.Response(
            status, json={"errors": [{"code": "InvalidInput", "message": "ASIN is malformed"}]}
        )
    )
    with pytest.raises(AmazonRequestError) as info:
        restrictions(client)
    assert info.value.status == status and "InvalidInput: ASIN is malformed" in info.value.detail


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_transient(status):
    with pytest.raises(AmazonTransientError):
        restrictions(client_returning(lambda r: httpx.Response(status)))


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"x-amzn-RateLimit-Limit": "0.5"}, 2.0),  # 0.5 req/s -> wait 2s
        ({"x-amzn-RateLimit-Limit": "5.0"}, 0.2),
        ({"Retry-After": "7"}, 7.0),
        ({"Retry-After": "9999"}, 60.0),  # capped
        ({}, None),
        ({"x-amzn-RateLimit-Limit": "garbage"}, None),
    ],
)
def test_429_becomes_rate_limited_with_a_retry_hint(headers, expected):
    client = client_returning(lambda r: httpx.Response(429, headers=headers, json={"errors": []}))
    with pytest.raises(AmazonRateLimited) as info:
        restrictions(client)
    assert info.value.retry_after == (pytest.approx(expected) if expected else None)


@pytest.mark.parametrize(
    "response",
    [
        lambda r: httpx.Response(200, text="<html>captive portal</html>"),
        lambda r: httpx.Response(200, json=["not", "an", "object"]),
        lambda r: httpx.Response(200, content=b""),
    ],
)
def test_success_status_with_a_bad_body_is_malformed_never_trusted(response):
    with pytest.raises(AmazonMalformedResponse):
        restrictions(client_returning(response))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.ConnectError("c"),
        httpx.RemoteProtocolError("p"),
    ],
)
def test_network_failures_are_transient(error):
    def boom(_req):
        raise error

    with pytest.raises(AmazonTransientError):
        restrictions(client_returning(boom))


# ---- scenarios from the offline mock, now over HTTP ------------------------------------------


def test_scripted_429_then_success_over_http():
    backend = MockAmazonClient(
        catalog={"Oddy": [catalog_item("B0HTTP0001", "x", "Oddy")]},
        restrictions={"B0HTTP0001": [AmazonRateLimited(retry_after=2), NO_RESTRICTIONS]},
    )
    _, client, _ = make(FakeSellingPartnerApi(backend))
    with pytest.raises(AmazonRateLimited) as info:
        restrictions(client, "B0HTTP0001")
    assert info.value.retry_after == pytest.approx(2.0)
    assert restrictions(client, "B0HTTP0001") == {"restrictions": []}


def test_gated_scenario_round_trips_with_message_and_link():
    backend = MockAmazonClient(restrictions={"B0GATED001": brand_gated("B0GATED001")})
    _, client, _ = make(FakeSellingPartnerApi(backend))
    reason = restrictions(client, "B0GATED001")["restrictions"][0]["reasons"][0]
    assert reason["reasonCode"] == "APPROVAL_REQUIRED"
    assert reason["links"][0]["title"] == "Apply to sell"
