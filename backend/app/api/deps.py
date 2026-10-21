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
