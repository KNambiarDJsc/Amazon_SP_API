"""Live Selling Partner API client for Amazon India (EU endpoint).

Authentication is one LWA access token per request in `x-amz-access-token`; AWS SigV4 has not been
required since October 2023. Failures are mapped onto the typed errors the orchestrator knows how
to handle; this class never retries by itself (the processor owns retry policy) except for one
forced token refresh when Amazon answers 401/403, since the cached token may simply have expired.
"""

import logging
import platform
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.amazon.lwa import LwaTokenProvider
from app.config import Settings
from app.db import session_scope
from app.enums import DataSource
from app.models import SellerAccount
from app.services import credentials

log = logging.getLogger(__name__)

USER_AGENT = f"AmazonSellerEligibilityChecker/0.1 (Language=Python/{platform.python_version()})"
CATALOG_PATH = "/catalog/2022-04-01/items"
RESTRICTIONS_PATH = "/listings/2021-08-01/restrictions"
MAX_RETRY_AFTER = 60.0


def _api_error_text(response: httpx.Response) -> str:
    """The documented `errors[].code/message`, trimmed. Response bodies carry no secrets."""
    try:
        errors = response.json().get("errors")
    except (ValueError, AttributeError):
        return response.text[:200]
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        return f"{first.get('code', '')}: {first.get('message', '')}"[:300]
    return response.text[:200]


def _retry_after(response: httpx.Response) -> float | None:
    """Amazon sends the allowed rate in x-amzn-RateLimit-Limit (requests/second); honour a
    standard Retry-After if one is present."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), MAX_RETRY_AFTER)
        except ValueError:
            pass
    limit = response.headers.get("x-amzn-ratelimit-limit")
    if limit:
        try:
            rate = float(limit)
            if rate > 0:
                return min(1.0 / rate, MAX_RETRY_AFTER)
        except ValueError:
            pass
    return None


class SpApiClient:
    data_source = DataSource.LIVE

    def __init__(
        self,
        *,
        http: httpx.Client,
        base_url: str,
        tokens: LwaTokenProvider,
        reason_locale: str = "en_US",
    ):
        self._http = http
        self._base = base_url.rstrip("/")
        self._tokens = tokens
        self._locale = reason_locale

    # ---- operations --------------------------------------------------------------------------

    def search_catalog_items(
        self,
        *,
        keywords: str,
        marketplace_id: str,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "keywords": keywords,
            "marketplaceIds": marketplace_id,
            "includedData": "summaries,productTypes",
            "pageSize": min(page_size, 20),
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get(CATALOG_PATH, params)

    def get_listings_restrictions(
        self, *, asin: str, seller_id: str, marketplace_id: str, condition: str
    ) -> Mapping[str, Any]:
        return self._get(
            RESTRICTIONS_PATH,
            {
                "asin": asin,
                "sellerId": seller_id,
                "marketplaceIds": marketplace_id,
                "conditionType": condition,
                "reasonLocale": self._locale,
            },
        )

    # ---- transport ---------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> Mapping[str, Any]:
        response: httpx.Response | None = None
        for attempt in (0, 1):
            token = self._tokens.get_access_token(force=attempt == 1)
            try:
                response = self._http.get(
                    self._base + path,
                    params=params,
                    headers={
                        "x-amz-access-token": token,
                        "user-agent": USER_AGENT,
                        "accept": "application/json",
                    },
                )
            except httpx.TimeoutException as exc:
                raise AmazonTransientError(f"timeout calling {path}") from exc
            except httpx.TransportError as exc:
                raise AmazonTransientError(f"{type(exc).__name__} calling {path}") from exc
            if response.status_code in (401, 403) and attempt == 0:
                continue  # the cached access token may have expired; refresh once and retry
            break
        assert response is not None
        return self._interpret(response, path)

    @staticmethod
    def _interpret(response: httpx.Response, path: str) -> Mapping[str, Any]:
        status = response.status_code
        if status == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise AmazonMalformedResponse(f"{path} returned a non-JSON body") from exc
            if not isinstance(body, dict):
                raise AmazonMalformedResponse(
                    f"{path} returned {type(body).__name__}, not an object"
                )
            return body
        if status in (401, 403):
            raise AmazonAuthError(
                f"Amazon denied access ({status}): {_api_error_text(response)}. "
                "Check that the seller authorised this application and that it has the "
                "Product Listing role."
            )
        if status == 429:
            raise AmazonRateLimited(_retry_after(response), f"HTTP 429 from {path}")
        if status >= 500:
            raise AmazonTransientError(f"HTTP {status} from {path}")
        raise AmazonRequestError(status, _api_error_text(response))


# ---- per-seller client construction ---------------------------------------------------------


class LiveClientFactory:
    """Builds (and caches, per seller) a client from that seller's stored credentials.

    The token cache survives across brands in a worker process. If the seller reconnects with a new
    refresh token the credential's `updated_at` changes and the cached client is rebuilt.
    """

    def __init__(self, cfg: Settings, http: httpx.Client | None = None):
        self._cfg = cfg
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(cfg.http_timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._cipher = credentials.CredentialCipher.from_setting(
            cfg.credential_encryption_keys.get_secret_value()
        )
        self._cache: dict[uuid.UUID, tuple[Any, SpApiClient]] = {}
        self._lock = threading.Lock()

    def __call__(self, seller: SellerAccount) -> SpApiClient:
        try:
            with session_scope() as session:
                refresh_token, row = credentials.load_refresh_token(
                    session, self._cipher, seller.id
                )
                version = row.updated_at
        except credentials.CredentialError as exc:
            raise AmazonAuthError(str(exc)) from exc

        with self._lock:
            cached = self._cache.get(seller.id)
            if cached is not None and cached[0] == version:
                return cached[1]
            client = SpApiClient(
                http=self._http,
                base_url=self._cfg.sp_api_base_url,
                tokens=LwaTokenProvider(
                    self._http,
                    token_url=self._cfg.lwa_token_url,
                    client_id=self._cfg.lwa_client_id,
                    client_secret=self._cfg.lwa_client_secret.get_secret_value(),
                    refresh_token=refresh_token,
                    on_revoked=lambda reason, sid=seller.id: self._revoke(sid, reason),
                ),
                reason_locale=self._cfg.reason_locale,
            )
            self._cache[seller.id] = (version, client)
            return client

    @staticmethod
    def _revoke(seller_id: uuid.UUID, reason: str) -> None:
        # Mark the stored credential unusable so the dashboard can say "reconnect".
        try:
            with session_scope() as session:
                credentials.mark_revoked(session, seller_id, reason)
        except Exception:
            log.exception("could not mark credential revoked for seller %s", seller_id)


def validate_live_config(cfg: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("LWA_CLIENT_ID", cfg.lwa_client_id),
            ("LWA_CLIENT_SECRET", cfg.lwa_client_secret.get_secret_value()),
            ("CREDENTIAL_ENCRYPTION_KEYS", cfg.credential_encryption_keys.get_secret_value()),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("AMAZON_MODE=live requires: " + ", ".join(missing))


ClientFactory = Callable[[SellerAccount], SpApiClient]
