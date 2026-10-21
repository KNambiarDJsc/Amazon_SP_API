"""Encrypted storage for seller SP-API refresh tokens.

* Fernet (AES-128-CBC + HMAC-SHA256, authenticated) with MultiFernet for key rotation: the first
  key encrypts, every listed key can decrypt.
* The ciphertext embeds the seller id and it is verified on decryption, so a ciphertext copied into
  another seller's row is refused rather than silently used.
* Plaintext tokens never appear in logs, exceptions, repr output or API responses.
"""

import json
import uuid
from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.orm import Session

from app.domain.observations import utcnow
from app.models import SellerCredential

ACTIVE = "ACTIVE"
REVOKED = "REVOKED"


class CredentialError(Exception):
    """Stored credentials are missing, unreadable, or do not belong to this seller."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


class CredentialCipher:
    def __init__(self, keys: Sequence[str]):
        keys = [k.strip() for k in keys if k.strip()]
        if not keys:
            raise CredentialError(
                "CREDENTIAL_ENCRYPTION_KEYS is not set. Generate one with: "
                "python -m app.cli generate-encryption-key"
            )
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise CredentialError("CREDENTIAL_ENCRYPTION_KEYS contains an invalid key.") from exc

    @classmethod
    def from_setting(cls, value: str) -> "CredentialCipher":
        return cls(value.split(","))

    def encrypt(self, seller_id: uuid.UUID, refresh_token: str) -> bytes:
        payload = json.dumps({"v": 1, "seller": str(seller_id), "token": refresh_token})
        return self._fernet.encrypt(payload.encode())

    def decrypt(self, seller_id: uuid.UUID, blob: bytes) -> str:
        try:
            payload = json.loads(self._fernet.decrypt(bytes(blob)))
        except (InvalidToken, ValueError) as exc:
            raise CredentialError("Stored credential could not be decrypted.") from exc
        if payload.get("seller") != str(seller_id) or not isinstance(payload.get("token"), str):
            raise CredentialError("Stored credential does not belong to this seller.")
        return payload["token"]

    def rotate(self, blob: bytes) -> bytes:
        """Re-encrypt under the newest key."""
        return self._fernet.rotate(bytes(blob))


def save_refresh_token(
    session: Session,
    cipher: CredentialCipher,
    seller_id: uuid.UUID,
    refresh_token: str,
    *,
    authorization_type: str,
) -> SellerCredential:
    blob = cipher.encrypt(seller_id, refresh_token)
    row = session.get(SellerCredential, seller_id)
    if row is None:
        row = SellerCredential(seller_account_id=seller_id)
        session.add(row)
    row.authorization_type = authorization_type
    row.refresh_token_encrypted = blob
    row.status = ACTIVE
    row.last_error = None
    row.updated_at = utcnow()
    session.flush()
    return row


def load_refresh_token(
    session: Session, cipher: CredentialCipher, seller_id: uuid.UUID
) -> tuple[str, SellerCredential]:
    row = session.get(SellerCredential, seller_id)
    if row is None:
        raise CredentialError("This seller account is not connected to Amazon.")
    if row.status != ACTIVE:
        raise CredentialError(
            "Amazon authorisation was revoked or expired; reconnect the seller account."
        )
    return cipher.decrypt(seller_id, row.refresh_token_encrypted), row


def mark_revoked(session: Session, seller_id: uuid.UUID, reason: str) -> None:
    row = session.get(SellerCredential, seller_id)
    if row is not None:
        row.status = REVOKED
        row.last_error = reason[:300]
        row.updated_at = utcnow()


def delete_credential(session: Session, seller_id: uuid.UUID) -> bool:
    row = session.get(SellerCredential, seller_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
