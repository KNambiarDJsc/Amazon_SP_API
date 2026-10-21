import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def record(
    session: Session,
    *,
    seller_account_id: uuid.UUID,
    event_type: str,
    job_id: uuid.UUID | None = None,
    job_brand_id: uuid.UUID | None = None,
    check_id: uuid.UUID | None = None,
    **payload: Any,
) -> None:
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
