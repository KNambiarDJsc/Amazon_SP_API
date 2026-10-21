import httpx
import pytest
from fake_amazon import REFRESH_TOKEN, FakeAmazon, sp_error

from app.amazon import errors as err
from app.amazon.live import (
    CATALOG_PATH,
    PARTICIPATIONS_PATH,
    RESTRICTIONS_PATH,
    SpApiClient,
    keyword_query,
)
from app.amazon.lwa import LwaClient, TokenCache
from app.amazon.mock import catalog_item, demo_client
from app.amazon.parsing import parse_catalog_page
from app.enums import MARKETPLACE_ID_IN, DataSource

ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"


def make_client(fake: FakeAmazon) -> SpApiClient:
    http = fake.http()
    lwa = LwaClient(
        client_id="amzn1.application-oa2-client.test",
        client_secret="test-client-secret",
        token_url="https://api.amazon.com/auth/o2/token",
        http=http,
    )
    return SpApiClient(
        endpoint=ENDPOINT, refresh_token=REFRESH_TOKEN, tokens=TokenCache(lwa), http=http
    )


def restrictions(client, asin="B08Y9C5H38"):
    return client.get_listings_restrictions(
        asin=asin, seller_id="A2TESTSELLER01", marketplace_id=MARKETPLACE_ID_IN, condition="new_new"
    )


# ---- request shape -------------------------------------------------------------------------


def test_restrictions_request_matches_the_api_model():
    fake = FakeAmazon()
    client = make_client(fake)
    body = restrictions(client)

    request = fake.sp_requests(RESTRICTIONS_PATH)[0]
    assert request.method == "GET"
    assert request.url.host == "sellingpartnerapi-eu.amazon.com"  # India is in the EU region
    assert dict(request.url.params) == {
        "asin": "B08Y9C5H38",
        "sellerId": "A2TESTSELLER01",
        "marketplaceIds": MARKETPLACE_ID_IN,
        "conditionType": "new_new",
        "reasonLocale": "en_IN",
    }
    assert request.headers["x-amz-access-token"] in fake.access_tokens
    assert request.headers["user-agent"].startswith("AmazonIndiaEligibilityChecker/")
    assert "Language=Python" in request.headers["user-agent"]
    assert len(request.headers["x-amz-date"]) == 16  # 20261022T101500Z
    assert "authorization" not in request.headers  # no SigV4 since 2023-10-02
    assert REFRESH_TOKEN not in str(request.url) and not request.content
    assert body["restrictions"][0]["reasons"][0]["reasonCode"] == "APPROVAL_REQUIRED"
    assert body.request_id.startswith("req-") and body.http_status == 200
    assert body.rate_limit == 5.0


def test_catalog_request_asks_for_summaries_and_product_types_in_english():
    fake = FakeAmazon()
    make_client(fake).search_catalog_items(keywords="eSSL", marketplace_id=MARKETPLACE_ID_IN)
    params = fake.sp_requests(CATALOG_PATH)[0].url.params
    assert params["keywords"] == "eSSL"
    assert params["includedData"] == "summaries,productTypes"
    assert params["marketplaceIds"] == MARKETPLACE_ID_IN
    assert params["pageSize"] == "20" and params["locale"] == "en_IN"
    assert "pageToken" not in params


def test_catalog_pagination_passes_the_token_back_with_the_same_query():
    backing = demo_client()
    backing.catalog["many"] = [
        catalog_item(f"B0PAGE{n:04d}", f"Item {n}", "Many") for n in range(25)
    ]
    fake = FakeAmazon(backing)
    client = make_client(fake)

    first = parse_catalog_page(
        client.search_catalog_items(keywords="Many", marketplace_id=MARKETPLACE_ID_IN),
        MARKETPLACE_ID_IN,
    )
    second = parse_catalog_page(
        client.search_catalog_items(
            keywords="Many", marketplace_id=MARKETPLACE_ID_IN, page_token=first.next_token
        ),
        MARKETPLACE_ID_IN,
    )
    assert len(first.items) == 20 and len(second.items) == 5 and second.next_token is None
    params = fake.sp_requests(CATALOG_PATH)[1].url.params
    assert params["pageToken"] == first.next_token and params["keywords"] == "Many"


@pytest.mark.parametrize(
    ("brand", "query"),
    [("Tom, Dick & Harry", "Tom Dick & Harry"), ("  eSSL ", "eSSL"), ("A,B", "A B")],
)
def test_commas_never_split_a_brand_into_several_searches(brand, query):
    assert keyword_query(brand) == query


def test_page_size_is_capped_at_the_documented_maximum():
    fake = FakeAmazon()
    make_client(fake).search_catalog_items(
        keywords="eSSL", marketplace_id=MARKETPLACE_ID_IN, page_size=50
    )
    assert fake.sp_requests(CATALOG_PATH)[0].url.params["pageSize"] == "20"


def test_participations_call():
    fake = FakeAmazon()
    body = make_client(fake).get_marketplace_participations()
    assert fake.sp_requests(PARTICIPATIONS_PATH)
    assert body["payload"][0]["marketplace"]["id"] == MARKETPLACE_ID_IN


def test_client_is_live_and_never_prints_its_token():
    client = make_client(FakeAmazon())
    assert client.data_source == DataSource.LIVE
    assert REFRESH_TOKEN not in repr(client)


# ---- tokens --------------------------------------------------------------------------------


def test_access_token_is_reused_across_calls():
    fake = FakeAmazon()
    client = make_client(fake)
    restrictions(client)
    restrictions(client)
    assert len(fake.lwa_requests()) == 1


def test_a_token_that_expired_in_flight_is_refreshed_once_and_the_call_retried():
    fake = FakeAmazon()
    client = make_client(fake)
    restrictions(client)
    fake.expire_access_tokens()  # Amazon now refuses the cached token

    body = restrictions(client)
    assert body["restrictions"]
    assert len(fake.lwa_requests()) == 2
    first, retry = fake.sp_requests(RESTRICTIONS_PATH)[1:]
    assert first.headers["x-amz-access-token"] != retry.headers["x-amz-access-token"]


def test_a_persistent_403_is_an_auth_error_after_one_retry():
    fake = FakeAmazon()
    denied = sp_error(403, "Unauthorized", "Access to requested resource is denied.")
    fake.script("getListingsRestrictions", denied, denied, denied)
    with pytest.raises(err.AmazonAuthError, match="Access to requested resource is denied") as e:
        restrictions(make_client(fake))
    assert len(fake.sp_requests(RESTRICTIONS_PATH)) == 2
    assert e.value.request_id == "req-error-403"


def test_a_revoked_grant_surfaces_as_an_auth_error_from_lwa():
    fake = FakeAmazon()
    fake.revoke()
    with pytest.raises(err.AmazonAuthError, match="invalid_grant"):
        restrictions(make_client(fake))
    assert fake.sp_requests() == []  # nothing reached SP-API without a token


# ---- error mapping -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (sp_error(429, "QuotaExceeded", "You exceeded your quota"), err.AmazonRateLimited),
        (sp_error(400, "InvalidInput", "Invalid conditionType"), err.AmazonRequestError),
        (sp_error(404, "NotFound", "Resource not found"), err.AmazonRequestError),
        (
            sp_error(500, "InternalFailure", "We encountered an internal error"),
            err.AmazonTransientError,
        ),
        (
            sp_error(503, "ServiceUnavailable", "Service temporarily unavailable"),
            err.AmazonTransientError,
        ),
        (httpx.Response(502, text="<html>Bad Gateway</html>"), err.AmazonTransientError),
        (httpx.Response(200, text="<html>not json</html>"), err.AmazonMalformedResponse),
        (httpx.Response(200, json=["not", "an", "object"]), err.AmazonMalformedResponse),
    ],
)
def test_responses_map_to_typed_errors(response, error):
    fake = FakeAmazon()
    fake.script("getListingsRestrictions", response)
    with pytest.raises(error):
        restrictions(make_client(fake))


def test_request_error_keeps_amazons_code_message_and_request_id():
    fake = FakeAmazon()
    fake.script("getListingsRestrictions", sp_error(400, "InvalidInput", "Invalid ASIN"))
    with pytest.raises(err.AmazonRequestError) as caught:
        restrictions(make_client(fake))
    assert caught.value.status == 400
    assert "InvalidInput: Invalid ASIN" in caught.value.detail
    assert caught.value.request_id == "req-error-400"


def test_retry_after_is_honoured_when_amazon_sends_it():
    fake = FakeAmazon()
    fake.script("getListingsRestrictions", httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(err.AmazonRateLimited) as caught:
        restrictions(make_client(fake))
    assert caught.value.retry_after == 7.0


def test_timeouts_and_connection_failures_are_transient():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def reset(request):
        raise httpx.RemoteProtocolError("reset", request=request)

    fake = FakeAmazon()
    fake.script("getListingsRestrictions", timeout, reset)
    client = make_client(fake)
    with pytest.raises(err.AmazonTimeout):
        restrictions(client)
    with pytest.raises(err.AmazonTransientError):
        restrictions(client)
