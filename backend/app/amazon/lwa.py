"""Login with Amazon (LWA): exchanging an authorisation code for a refresh token, and a refresh
token for the one-hour access token that every SP-API call carries.

Secrets never appear in exceptions or logs. Access tokens live only in process memory.
"""

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonTimeout,
    AmazonTransientError,
)

# Refresh this long before Amazon's stated expiry, so a token never dies mid-request.
EXPIRY_MARGIN_SECONDS = 60.0


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float  # on the client's monotonic clock


@dataclass(frozen=True)
class TokenGrant:
    refresh_token: str
    access_token: str
    expires_in: int


class LwaClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_url: str,
        http: httpx.Client,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._http = http
        self._monotonic = monotonic

    def refresh(self, refresh_token: str) -> AccessToken:
        body = self._post({"grant_type": "refresh_token", "refresh_token": refresh_token})
        access, expires_in = _access_token_fields(body)
        return AccessToken(access, self._monotonic() + expires_in)

    def exchange_code(self, code: str, redirect_uri: str) -> TokenGrant:
        """Website workflow: the spapi_oauth_code is single-use and valid for five minutes."""
        body = self._post(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )
        access, expires_in = _access_token_fields(body)
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AmazonMalformedResponse("LWA response has no refresh_token")
        return TokenGrant(refresh_token=refresh, access_token=access, expires_in=expires_in)

    def _post(self, form: dict[str, str]) -> Mapping[str, Any]:
        grant = form["grant_type"]
        form = {**form, "client_id": self._client_id, "client_secret": self._client_secret}
        try:
            response = self._http.post(
                self._token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
        except httpx.TimeoutException as exc:
            raise AmazonTimeout("LWA token request timed out") from exc
        except httpx.TransportError as exc:
            raise AmazonTransientError(f"LWA token request failed: {type(exc).__name__}") from exc

        status = response.status_code
        if status == 429:
            raise AmazonRateLimited(detail="LWA token endpoint throttled the request")
        if status >= 500:
            raise AmazonTransientError(f"LWA token endpoint returned HTTP {status}")
        try:
            body = response.json()
        except ValueError:
            body = None
        if status >= 400:
            # invalid_grant (revoked/expired token or used code), invalid_client (wrong app
            # credentials), unauthorized_client... none of them can be fixed by retrying.
            error = body.get("error") if isinstance(body, Mapping) else None
            description = body.get("error_description") if isinstance(body, Mapping) else None
            raise AmazonAuthError(
                f"LWA rejected the {grant} grant (HTTP {status}"
                + (f", {error}" if error else "")
                + (f": {description}" if description else "")
                + ")"
            )
        if not isinstance(body, Mapping):
            raise AmazonMalformedResponse("LWA token response is not a JSON object")
        return body


def _access_token_fields(body: Mapping[str, Any]) -> tuple[str, int]:
    access = body.get("access_token")
    expires_in = body.get("expires_in")
    if not isinstance(access, str) or not access:
        raise AmazonMalformedResponse("LWA response has no access_token")
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise AmazonMalformedResponse("LWA response has no usable expires_in")
    return access, expires_in


class TokenCache:
    """Access tokens per refresh token, reused until shortly before they expire.

    Thread-safe, with one lock per refresh token: threads of one seller wait for a single
    refresh instead of stampeding LWA, and a slow refresh for one seller never blocks another.
    """

    def __init__(
        self,
        lwa: LwaClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        margin: float = EXPIRY_MARGIN_SECONDS,
    ):
        self._lwa = lwa
        self._monotonic = monotonic
        self._margin = margin
        self._tokens: dict[str, AccessToken] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @staticmethod
    def _key(refresh_token: str) -> str:
        return hashlib.sha256(refresh_token.encode()).hexdigest()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, refresh_token: str) -> str:
        key = self._key(refresh_token)
        with self._lock(key):
            token = self._tokens.get(key)
            if token is None or self._monotonic() >= token.expires_at - self._margin:
                token = self._lwa.refresh(refresh_token)
                self._tokens[key] = token
            return token.value

    def invalidate(self, refresh_token: str, value: str) -> None:
        """Drop a token Amazon refused, unless another thread already replaced it."""
        key = self._key(refresh_token)
        with self._lock(key):
            current = self._tokens.get(key)
            if current is not None and current.value == value:
                del self._tokens[key]
