import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import authenticate
from app.db import session_scope
from app.models import Job, SellerAccount
from app.services.jobs import get_job

_bearer = HTTPBearer(auto_error=False, description="Seller API key")


def current_seller(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> SellerAccount:
    unauthorised = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorised
    with session_scope() as session:
        account = authenticate(session, credentials.credentials)
    if account is None:
        raise unauthorised
    return account


def require_job(session, seller: SellerAccount, job_id: uuid.UUID) -> Job:
    """A job of another seller is reported as missing, never as forbidden."""
    job = get_job(session, seller.id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job
