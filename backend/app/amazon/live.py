"""The live SP-API client: HTTPS calls to Amazon carrying the seller's LWA access token.

There is no AWS SigV4 signing. SP-API stopped requiring it on 2023-10-02 and authorises on the
x-amz-access-token header alone. There are no retries here either, except one: a 401/403 is
tried once more with a freshly minted access token, because a cached token can expire in
flight. Every other outcome is mapped to the typed errors in app.amazon.errors, and the
orchestrator decides what each one means.
"""

import logging
import platform
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app import __version__
from app.amazon.errors import (
    AmazonAuthError,
    AmazonError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTimeout,
    AmazonTransientError,
)
from app.amazon.lwa import TokenCache
from app.enums import DataSource

log = logging.getLogger(__name__)

USER_AGENT = (
    f"AmazonIndiaEligibilityChecker/{__version__} (Language=Python/{platform.python_version()})"
)
CATALOG_PATH = "/catalog/2022-04-01/items"
RESTRICTIONS_PATH = "/listings/2021-08-01/restrictions"
PARTICIPATIONS_PATH = "/sellers/v1/marketplaceParticipations"
MAX_CATALOG_PAGE_SIZE = 20
_MAX_DETAIL = 300


class AmazonBody(dict):
    """A response body that remembers which request produced it."""

    def __init__(
        self,
        body: Mapping[str, Any],
        *,
        request_id: str | None,
        http_status: int,
        rate_limit: float | None = None,
    ):
        super().__init__(body)
        self.request_id = request_id
        self.http_status = http_status
        self.rate_limit = rate_limit  # x-amzn-RateLimit-Limit, when Amazon chose to send it


def keyword_query(brand: str) -> str:
    """searchCatalogItems splits `keywords` on commas, so "Tom, Dick & Harry" would become
    three separate searches. Commas become spaces."""
    return " ".join(brand.replace(",", " ").split())


class SpApiClient:
    """One seller's view of SP-API. Thread-safe: share it between a seller's workers."""

    data_source = DataSource.LIVE

    def __init__(
        self,
        *,
        endpoint: str,
        refresh_token: str,
        tokens: TokenCache,
        http: httpx.Client,
        locale: str = "en_IN",
    ):
        self._endpoint = endpoint.rstrip("/")
        self._refresh_token = refresh_token
        self._tokens = tokens
        self._http = http
        self._locale = locale
        self._reported_limits: set[str] = set()

    def __repr__(self) -> str:  # never show the refresh token
        return f"SpApiClient(endpoint={self._endpoint!r})"

    # ---- operations ----------------------------------------------------------------------

    def search_catalog_items(
        self,
        *,
        keywords: str,
        marketplace_id: str,
        page_token: str | None = None,
        page_size: int = MAX_CATALOG_PAGE_SIZE,
    ) -> AmazonBody:
        params: dict[str, Any] = {
            "marketplaceIds": marketplace_id,
            "keywords": keyword_query(keywords),
            "includedData": "summaries,productTypes",
            "pageSize": min(page_size, MAX_CATALOG_PAGE_SIZE),
            "locale": self._locale,
        }
        if page_token:
            params["pageToken"] = page_token  # passed back exactly as Amazon returned it
        return self._get("searchCatalogItems", CATALOG_PATH, params)

    def get_listings_restrictions(
        self,
        *,
        asin: str,
        seller_id: str,
        marketplace_id: str,
        condition: str,
    ) -> AmazonBody:
        return self._get(
            "getListingsRestrictions",
            RESTRICTIONS_PATH,
            {
                "asin": asin,
                "sellerId": seller_id,
                "marketplaceIds": marketplace_id,
                "conditionType": condition,
                "reasonLocale": self._locale,
            },
        )

    def get_marketplace_participations(self) -> AmazonBody:
        return self._get("getMarketplaceParticipations", PARTICIPATIONS_PATH, {})

    # ---- transport -----------------------------------------------------------------------

    def _get(self, operation: str, path: str, params: dict[str, Any]) -> AmazonBody:
        for attempt in (1, 2):
            token = self._tokens.get(self._refresh_token)
            response = self._send(operation, path, params, token)
            if response.status_code in (401, 403) and attempt == 1:
                self._tokens.invalidate(self._refresh_token, token)
                continue
            return self._interpret(operation, response)
        raise AssertionError("unreachable")

    def _send(self, operation: str, path: str, params: dict[str, Any], token: str):
        headers = {
            "x-amz-access-token": token,
            "x-amz-date": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            "user-agent": USER_AGENT,
            "accept": "application/json",
        }
        try:
            return self._http.get(self._endpoint + path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AmazonTimeout(f"{operation} timed out") from exc
        except httpx.TransportError as exc:
            raise AmazonTransientError(f"{operation} failed: {type(exc).__name__}") from exc

    def _interpret(self, operation: str, response: httpx.Response) -> AmazonBody:
        request_id = response.headers.get("x-amzn-RequestId")
        status = response.status_code
        if 200 <= status < 300:
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, Mapping):
                raise _tag(
                    AmazonMalformedResponse(f"{operation} response is not a JSON object"),
                    request_id,
                )
            limit = _float_header(response, "x-amzn-RateLimit-Limit")
            if limit is not None and operation not in self._reported_limits:
                self._reported_limits.add(operation)
                log.info("Amazon reports a rate limit of %s rps for %s", limit, operation)
            return AmazonBody(body, request_id=request_id, http_status=status, rate_limit=limit)

        detail = _error_detail(response)
        exc: AmazonError
        if status == 429:
            exc = AmazonRateLimited(
                retry_after=_float_header(response, "Retry-After"),
                detail=f"{operation} throttled: {detail}",
            )
        elif status in (401, 403):
            exc = AmazonAuthError(f"{operation} refused (HTTP {status}): {detail}")
        elif status >= 500:
            exc = AmazonTransientError(f"{operation} failed (HTTP {status}): {detail}")
        else:
            exc = AmazonRequestError(status, f"{operation}: {detail}")
        raise _tag(exc, request_id)


def _tag(exc: AmazonError, request_id: str | None) -> AmazonError:
    exc.request_id = request_id
    return exc


def _float_header(response: httpx.Response, name: str) -> float | None:
    try:
        return float(response.headers[name])
    except (KeyError, ValueError):
        return None


def _error_detail(response: httpx.Response) -> str:
    """SP-API errors arrive as {"errors": [{"code", "message", "details"}]}."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "no error body"
    errors = body.get("errors") if isinstance(body, Mapping) else None
    if isinstance(errors, list) and errors:
        parts = []
        for error in errors:
            if isinstance(error, Mapping):
                text = ": ".join(
                    str(error[k]) for k in ("code", "message") if error.get(k) not in (None, "")
                )
                parts.append(text or "unspecified error")
        if parts:
            return "; ".join(parts)[:_MAX_DETAIL]
    return response.reason_phrase or "unrecognised error body"
