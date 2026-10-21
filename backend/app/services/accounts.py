import hashlib
import secrets
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import MARKETPLACE_ID_IN, JobStatus
from app.models import Job, SellerAccount
from app.services import audit, jobs

KEY_PREFIX = "spk_"


def hash_api_key(key: str) -> str:
    # The key is 256 bits of randomness, so a fast hash is sufficient (no password stretching).
    return hashlib.sha256(key.encode()).hexdigest()


def create_seller_account(
    session: Session, *, name: str, marketplace_id: str = MARKETPLACE_ID_IN
) -> tuple[SellerAccount, str]:
    """Returns the account and the plaintext API key. The key is shown once and never stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    account = SellerAccount(
        name=name,
        marketplace_id=marketplace_id,
        api_key_hash=hash_api_key(key),
        api_key_prefix=key[:8],
    )
    session.add(account)
    session.flush()
    return account, key


def authenticate(session: Session, api_key: str) -> SellerAccount | None:
    if not api_key.startswith(KEY_PREFIX):
        return None
    account = session.scalar(
        select(SellerAccount).where(SellerAccount.api_key_hash == hash_api_key(api_key))
    )
    return account if account is not None and account.is_active else None


class AccountNotFound(Exception):
    pass


def _get(session: Session, account_id: uuid.UUID) -> SellerAccount:
    account = session.get(SellerAccount, account_id)
    if account is None:
        raise AccountNotFound(str(account_id))
    return account


def list_accounts(session: Session) -> Sequence[SellerAccount]:
    return session.scalars(select(SellerAccount).order_by(SellerAccount.created_at)).all()


def rotate_api_key(session: Session, account_id: uuid.UUID) -> tuple[SellerAccount, str]:
    """Issue a new API key and invalidate the old one at once. Returns the key (shown once)."""
    account = _get(session, account_id)
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    previous_prefix = account.api_key_prefix
    account.api_key_hash = hash_api_key(key)
    account.api_key_prefix = key[:8]
    audit.record(
        session,
        seller_account_id=account.id,
        event_type="API_KEY_ROTATED",
        previous_prefix=previous_prefix,
        new_prefix=account.api_key_prefix,
    )
    return account, key


def deactivate(session: Session, account_id: uuid.UUID) -> int:
    """Cut the seller's API access and pause their running jobs (resumable after reactivation).
    Returns how many jobs were paused."""
    account = _get(session, account_id)
    account.is_active = False
    running = session.scalars(
        select(Job).where(Job.seller_account_id == account.id, Job.status == JobStatus.RUNNING)
    ).all()
    for job in running:
        jobs.pause_job(session, job, "ACCOUNT_DEACTIVATED")
    audit.record(
        session,
        seller_account_id=account.id,
        event_type="SELLER_DEACTIVATED",
        jobs_paused=len(running),
    )
    return len(running)


def activate(session: Session, account_id: uuid.UUID) -> None:
    """Restore API access. Paused jobs stay paused until the seller resumes them."""
    account = _get(session, account_id)
    account.is_active = True
    audit.record(session, seller_account_id=account.id, event_type="SELLER_ACTIVATED")
