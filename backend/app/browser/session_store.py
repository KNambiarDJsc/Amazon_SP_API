"""A seller's Seller Central browser session, encrypted at rest and bound to that seller."""

import json
import uuid

from sqlalchemy.orm import Session

from app.domain.observations import utcnow
from app.models import BrowserSession
from app.services.credentials import CredentialCipher, CredentialError


def save_state(
    session: Session, cipher: CredentialCipher, seller_id: uuid.UUID, state: dict
) -> None:
    blob = cipher.encrypt(seller_id, json.dumps(state))
    row = session.get(BrowserSession, seller_id)
    if row is None:
        row = BrowserSession(seller_account_id=seller_id, state_encrypted=blob)
        session.add(row)
    row.state_encrypted = blob
    row.updated_at = utcnow()
    session.flush()


def load_state(session: Session, cipher: CredentialCipher, seller_id: uuid.UUID) -> dict | None:
    row = session.get(BrowserSession, seller_id)
    if row is None:
        return None
    try:
        state = json.loads(cipher.decrypt(seller_id, row.state_encrypted))
    except (CredentialError, ValueError):
        return None  # unreadable: treat as signed out; a person signs in again
    row.last_used_at = utcnow()
    return state


def delete_state(session: Session, seller_id: uuid.UUID) -> bool:
    row = session.get(BrowserSession, seller_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
