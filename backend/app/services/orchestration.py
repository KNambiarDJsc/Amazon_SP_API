"""Entry points that combine a state change with the dispatch that must follow it.

Each commits its database change *before* anything is enqueued, so a worker that picks up
a message always sees the state it was sent for.
"""

import uuid

from app.config import Settings
from app.db import session_scope
from app.enums import DataSource
from app.models import SellerAccount
from app.schemas import JobSettings
from app.services import jobs
from app.services.dispatch import Dispatcher, dispatch_next, recover
from app.services.ingestion import IngestionResult


def submit_job(
    dispatcher: Dispatcher,
    cfg: Settings,
    seller_id: uuid.UUID,
    *,
    ingestion: IngestionResult,
    name: str | None,
    settings: JobSettings,
    data_source: DataSource,
    source_filename: str | None = None,
) -> uuid.UUID:
    with session_scope() as session:
        seller = session.get(SellerAccount, seller_id)
        job = jobs.create_job(
            session,
            seller,
            ingestion=ingestion,
            name=name,
            settings=settings,
            data_source=data_source,
            source_filename=source_filename,
        )
        job_id = job.id
    dispatch_next(dispatcher, seller_id, window=cfg.seller_max_concurrent_brands)
    return job_id


def pause(seller_id: uuid.UUID, job_id: uuid.UUID, reason: str = "USER_REQUESTED") -> None:
    with session_scope() as session:
        jobs.pause_job(session, jobs.get_job(session, seller_id, job_id), reason)


def resume(dispatcher: Dispatcher, cfg: Settings, seller_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with session_scope() as session:
        jobs.resume_job(session, jobs.get_job(session, seller_id, job_id))
    recover(dispatcher, cfg, seller_id=seller_id)  # also repairs anything interrupted meanwhile


def cancel(seller_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with session_scope() as session:
        jobs.cancel_job(session, jobs.get_job(session, seller_id, job_id))
