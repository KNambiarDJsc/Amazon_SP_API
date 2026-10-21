"""Encryption at rest for seller credentials (LWA refresh tokens).

Fernet (AES-128-CBC with HMAC-SHA256) behind MultiFernet, so keys can be rotated: the first
key encrypts and every listed key decrypts. The sealed plaintext names the seller account it
belongs to, so a ciphertext copied onto another seller's row fails to open rather than lending
that seller someone else's Amazon access.
"""

import json
import uuid
from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import Settings


class CredentialKeyError(RuntimeError):
    """No usable encryption key is configured."""


class CredentialDecryptError(RuntimeError):
    """The stored credential cannot be opened with the configured keys for this seller."""


def generate_key() -> str:
    return Fernet.generate_key().decode()


def parse_keys(raw: str | None) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


class CredentialBox:
    def __init__(self, keys: Sequence[str]):
        if not keys:
            raise CredentialKeyError("CREDENTIALS_ENCRYPTION_KEYS is not set")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except ValueError as exc:
            raise CredentialKeyError(
                "CREDENTIALS_ENCRYPTION_KEYS must be comma-separated Fernet keys "
                "(python -m app.cli generate-key)"
            ) from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> "CredentialBox":
        secret = settings.credentials_encryption_keys
        return cls(parse_keys(secret.get_secret_value() if secret else None))

    def seal(self, seller_account_id: uuid.UUID, secret: str) -> str:
        payload = json.dumps({"seller_account_id": str(seller_account_id), "secret": secret})
        return self._fernet.encrypt(payload.encode()).decode()

    def open(self, seller_account_id: uuid.UUID, sealed: str) -> str:
        try:
            payload = json.loads(self._fernet.decrypt(sealed.encode()))
        except (InvalidToken, ValueError) as exc:
            raise CredentialDecryptError(
                "stored credential could not be decrypted with the configured keys"
            ) from exc
        if payload.get("seller_account_id") != str(seller_account_id):
            raise CredentialDecryptError("stored credential belongs to a different seller account")
        return payload["secret"]

    def rotate(self, sealed: str) -> str:
        """Re-encrypt under the first (newest) key."""
        try:
            return self._fernet.rotate(sealed.encode()).decode()
        except InvalidToken as exc:
            raise CredentialDecryptError("stored credential could not be decrypted") from exc
