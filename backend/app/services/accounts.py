import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import MARKETPLACE_ID_IN
from app.models import SellerAccount

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
