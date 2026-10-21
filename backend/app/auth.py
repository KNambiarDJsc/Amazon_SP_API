"""Seller API keys.

A key is high-entropy random, so a plain SHA-256 is enough to store it (no password stretching
needed) and the lookup can be an indexed equality on the hash. The key itself is shown once,
at creation, and never stored.
"""

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import MARKETPLACE_ID_IN
from app.models import SellerAccount

_KEY_PREFIX = "spk_"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_seller_account(
    session: Session,
    *,
    name: str,
    amazon_seller_id: str | None = None,
    marketplace_id: str = MARKETPLACE_ID_IN,
) -> tuple[SellerAccount, str]:
    """Returns the account and its API key. This is the only time the key is available."""
    key = _KEY_PREFIX + secrets.token_urlsafe(32)
    account = SellerAccount(
        name=name,
        amazon_seller_id=amazon_seller_id,
        marketplace_id=marketplace_id,
        api_key_hash=hash_api_key(key),
        api_key_prefix=key[:12],
    )
    session.add(account)
    session.flush()
    return account, key


def authenticate(session: Session, key: str) -> SellerAccount | None:
    account = session.scalar(
        select(SellerAccount).where(SellerAccount.api_key_hash == hash_api_key(key))
    )
    return account if account is not None and account.is_active else None
