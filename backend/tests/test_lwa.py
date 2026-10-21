import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fake_amazon import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, REFRESH_TOKEN, FakeAmazon

from app.amazon import errors as err
from app.amazon.lwa import LwaClient, TokenCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_lwa(fake: FakeAmazon, clock=None, secret=CLIENT_SECRET) -> LwaClient:
    return LwaClient(
        client_id=CLIENT_ID,
        client_secret=secret,
        token_url="https://api.amazon.com/auth/o2/token",
        http=fake.http(),
        monotonic=clock or Clock(),
    )


def test_refresh_posts_the_documented_form_and_returns_an_expiring_token():
    fake, clock = FakeAmazon(), Clock()
    token = make_lwa(fake, clock).refresh(REFRESH_TOKEN)

    request = fake.lwa_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.amazon.com/auth/o2/token"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    assert "scope" not in form  # LWA: never send scope with a refresh_token grant
    assert token.value in fake.access_tokens
    assert token.expires_at == clock.t + 3600


def test_exchange_code_returns_the_refresh_token():
    grant = make_lwa(FakeAmazon()).exchange_code("auth-code-1", REDIRECT_URI)
    assert grant.refresh_token == REFRESH_TOKEN and grant.expires_in == 3600


def test_an_authorisation_code_works_once():
    lwa = make_lwa(FakeAmazon())
    lwa.exchange_code("auth-code-1", REDIRECT_URI)
    with pytest.raises(err.AmazonAuthError, match="invalid_grant"):
        lwa.exchange_code("auth-code-1", REDIRECT_URI)


def test_revoked_refresh_token_is_an_auth_error_and_the_secret_is_not_in_the_message():
    fake = FakeAmazon()
    fake.revoke()
    with pytest.raises(err.AmazonAuthError) as caught:
        make_lwa(fake).refresh(REFRESH_TOKEN)
    assert "invalid_grant" in str(caught.value)
    assert REFRESH_TOKEN not in str(caught.value) and CLIENT_SECRET not in str(caught.value)


def test_wrong_app_credentials_are_an_auth_error():
    with pytest.raises(err.AmazonAuthError, match="invalid_client"):
        make_lwa(FakeAmazon(), secret="wrong").refresh(REFRESH_TOKEN)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(500), err.AmazonTransientError),
        (httpx.Response(503, text="<html>"), err.AmazonTransientError),
        (httpx.Response(429), err.AmazonRateLimited),
        (httpx.Response(200, text="not json"), err.AmazonMalformedResponse),
        (httpx.Response(200, json={"token_type": "bearer"}), err.AmazonMalformedResponse),
        (
            httpx.Response(200, json={"access_token": "x", "expires_in": 0}),
            err.AmazonMalformedResponse,
        ),
    ],
)
def test_token_endpoint_failures_map_to_typed_errors(response, error):
    fake = FakeAmazon()
    fake.script("lwa", response)
    with pytest.raises(error):
        make_lwa(fake).refresh(REFRESH_TOKEN)


def test_network_failures_are_transient():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    fake = FakeAmazon()
    fake.script("lwa", timeout, refused)
    lwa = make_lwa(fake)
    with pytest.raises(err.AmazonTimeout):
        lwa.refresh(REFRESH_TOKEN)
    with pytest.raises(err.AmazonTransientError):
        lwa.refresh(REFRESH_TOKEN)


def test_cache_reuses_a_token_until_shortly_before_expiry():
    fake, clock = FakeAmazon(), Clock()
    cache = TokenCache(make_lwa(fake, clock), monotonic=clock)

    first = cache.get(REFRESH_TOKEN)
    clock.t += 3600 - 61
    assert cache.get(REFRESH_TOKEN) == first
    assert len(fake.lwa_requests()) == 1

    clock.t += 2  # inside the one-minute safety margin
    assert cache.get(REFRESH_TOKEN) != first
    assert len(fake.lwa_requests()) == 2


def test_invalidate_only_drops_the_token_that_was_refused():
    fake, clock = FakeAmazon(), Clock()
    cache = TokenCache(make_lwa(fake, clock), monotonic=clock)
    first = cache.get(REFRESH_TOKEN)
    cache.invalidate(REFRESH_TOKEN, "some-other-token")
    assert cache.get(REFRESH_TOKEN) == first
    cache.invalidate(REFRESH_TOKEN, first)
    assert cache.get(REFRESH_TOKEN) != first


def test_concurrent_callers_share_one_refresh():
    fake = FakeAmazon()
    gate = threading.Event()

    def slow_refresh(request):
        gate.wait(5)
        return fake._issue(refresh_token=None)

    fake.script("lwa", slow_refresh)
    cache = TokenCache(make_lwa(fake, time.monotonic), monotonic=time.monotonic)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get(REFRESH_TOKEN))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(fake.lwa_requests()) == 1
    assert len(set(results)) == 1 and len(results) == 8
