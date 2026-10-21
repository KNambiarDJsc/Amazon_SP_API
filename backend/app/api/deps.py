from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.models import SellerAccount
from app.services.accounts import authenticate
from app.services.dispatch import Dispatcher

bearer = HTTPBearer(auto_error=False, description="Per-seller API key")


def settings_dep() -> Settings:
    return get_settings()


def db_dep() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def dispatcher_dep(request: Request) -> Dispatcher:
    """Redis-backed in production; tests replace this dependency with an in-memory dispatcher."""
    from app.workers.runtime import get_runtime

    return get_runtime().dispatcher


class AllowAll:
    """Rate limiting switched off."""

    def acquire(self, key, *, rate, burst, max_wait):
        from app.services.rate_limit import RateDecision

        return RateDecision(True, 0.0)


class FailOpenLimiter:
    """Wraps a shared limiter so a Redis outage degrades to "no limiting", not to an outage."""

    def __init__(self, inner):
        self._inner = inner

    def acquire(self, key, *, rate, burst, max_wait):
        from app.services.rate_limit import RateDecision

        try:
            return self._inner.acquire(key, rate=rate, burst=burst, max_wait=max_wait)
        except Exception:
            return RateDecision(True, 0.0)


def build_limiter(cfg: Settings):
    from app.services.rate_limit import InMemoryTokenBucket, RedisTokenBucket

    if not cfg.api_rate_limit_enabled:
        return AllowAll()
    if cfg.environment == "production":
        import redis

        return FailOpenLimiter(RedisTokenBucket(redis.Redis.from_url(cfg.redis_url), prefix="api:"))
    return InMemoryTokenBucket()


def current_seller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(db_dep)],
    cfg: Annotated[Settings, Depends(settings_dep)],
) -> SellerAccount:
    account = authenticate(db, credentials.credentials) if credentials else None
    if account is None:
        # Slow down anyone guessing keys: failures spend tokens from a small per-IP bucket;
        # successful requests never touch it, so legitimate users are unaffected.
        ip = request.client.host if request.client else "unknown"
        from app.api.security import safe_acquire

        decision = safe_acquire(
            request.app.state.limiter,
            f"authfail:{ip}",
            rate=1.0 / cfg.auth_failure_refill_seconds,
            burst=cfg.auth_failure_burst,
            max_wait=0.0,
        )
        if not decision.granted:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed authentication attempts.",
                headers={"Retry-After": str(max(1, int(decision.wait + 0.999)))},
            )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return account


_http_client = None


def http_dep():
    """Shared outbound HTTP client (LWA calls). Tests override this with a mock transport."""
    import httpx

    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=httpx.Timeout(get_settings().http_timeout_seconds, connect=5.0)
        )
    return _http_client


def cipher_dep(cfg: Annotated[Settings, Depends(settings_dep)]):
    from app.services.credentials import CredentialCipher, CredentialError

    try:
        return CredentialCipher.from_setting(cfg.credential_encryption_keys.get_secret_value())
    except CredentialError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
