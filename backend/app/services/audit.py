import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def audit(
    session: Session,
    *,
    seller_account_id: uuid.UUID,
    event_type: str,
    job_id: uuid.UUID | None = None,
    job_brand_id: uuid.UUID | None = None,
    check_id: uuid.UUID | None = None,
    **payload: Any,
) -> None:
    """Append an audit event inside the caller's transaction, so the event commits or rolls
    back together with the change it describes. Payloads hold identifiers and reasons only,
    never credentials."""
    session.add(
        AuditEvent(
            seller_account_id=seller_account_id,
            job_id=job_id,
            job_brand_id=job_brand_id,
            check_id=check_id,
            event_type=event_type,
            payload=payload,
        )
    )
