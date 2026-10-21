"""Login with Amazon (LWA): exchange a seller's refresh token for short-lived access tokens."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from app.amazon.errors import (
    AmazonAuthError,
    AmazonCredentialsRevoked,
    AmazonMalformedResponse,
    AmazonTransientError,
)
from app.domain.observations import utcnow
from app.observability import LWA_REFRESHES

log = logging.getLogger(__name__)

# Renew slightly early so a token never expires between being handed out and being used.
EXPIRY_SKEW = timedelta(seconds=60)

# LWA error codes that mean the credentials themselves are no good. Retrying cannot help.
_CREDENTIAL_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope"}


def _lwa_error(response: httpx.Response) -> tuple[str, str]:
    """Only the documented `error` / `error_description` fields; never echo the request."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    return str(body.get("error", ""))[:60], str(body.get("error_description", ""))[:200]


def exchange(http: httpx.Client, token_url: str, form: dict[str, str]) -> dict:
    """POST to the LWA token endpoint and return the parsed JSON, or raise a typed error."""
    try:
        response = http.post(token_url, data=form, headers={"Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise AmazonTransientError("LWA token request timed out") from exc
    except httpx.TransportError as exc:
        raise AmazonTransientError(f"LWA token request failed: {type(exc).__name__}") from exc

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError as exc:
            raise AmazonMalformedResponse("LWA token response was not JSON") from exc
        if not isinstance(body, dict):
            raise AmazonMalformedResponse("LWA token response was not an object")
        return body

    error, description = _lwa_error(response)
    if response.status_code in (400, 401) and (error in _CREDENTIAL_ERRORS or not error):
        message = (
            f"Amazon rejected the credentials ({error or response.status_code}): {description}"
        )
        if error == "invalid_grant":  # the seller's token, not our application's secret
            raise AmazonCredentialsRevoked(message.strip())
        raise AmazonAuthError(message.strip())
    if response.status_code == 429 or response.status_code >= 500:
        raise AmazonTransientError(f"LWA token endpoint returned HTTP {response.status_code}")
    raise AmazonAuthError(f"LWA token request failed (HTTP {response.status_code}): {error}")


class LwaTokenProvider:
    """Caches one seller's access token and refreshes it when needed. Thread-safe."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        clock: Callable[[], datetime] = utcnow,
        on_revoked: Callable[[str], None] | None = None,
    ):
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._clock = clock
        self._on_revoked = on_revoked
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires: datetime | None = None

    def get_access_token(self, *, force: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if (
                not force
                and self._token is not None
                and self._expires is not None
                and now < self._expires - EXPIRY_SKEW
            ):
                return self._token
            try:
                body = exchange(
                    self._http,
                    self._token_url,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                )
            except AmazonAuthError as exc:
                LWA_REFRESHES.labels("rejected").inc()
                self._token = self._expires = None
                if isinstance(exc, AmazonCredentialsRevoked) and self._on_revoked is not None:
                    self._on_revoked(str(exc))
                raise
            LWA_REFRESHES.labels("ok").inc()
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise AmazonMalformedResponse("LWA response had no access_token")
            try:
                lifetime = int(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                lifetime = 3600
            self._token = token
            self._expires = now + timedelta(seconds=lifetime)
            return token


def authorization_code_exchange(
    http: httpx.Client,
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    """Website authorisation: exchange the short-lived `spapi_oauth_code` for a refresh token."""
    body = exchange(
        http,
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AmazonMalformedResponse("LWA response had no refresh_token")
    return refresh
