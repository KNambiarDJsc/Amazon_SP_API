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


def current_seller(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(db_dep)],
) -> SellerAccount:
    account = authenticate(db, credentials.credentials) if credentials else None
    if account is None:
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
