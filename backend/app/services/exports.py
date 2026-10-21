"""Excel exports built by a worker for large jobs, with a short-lived file and a status record.

Small jobs are still exported inline by the API. The file is produced from one consistent
snapshot of committed rows (REPEATABLE READ on PostgreSQL) so a job that is still running cannot
yield a report whose sheets disagree with each other.
"""

import logging
import os
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update

from app.config import Settings
from app.db import session_scope
from app.domain.observations import utcnow
from app.enums import ExportStatus
from app.models import EligibilityCheck, Export, Job
from app.services import audit
from app.services.reporting import build_workbook

log = logging.getLogger(__name__)
ACTIVE = (ExportStatus.QUEUED, ExportStatus.RUNNING)


def count_checks(session, job_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count()).select_from(EligibilityCheck).where(EligibilityCheck.job_id == job_id)
    )


def request_export(session, seller_id: uuid.UUID, job: Job) -> tuple[Export, bool]:
    """Create a QUEUED export, or return the one already in flight for this job.
    Returns (export, created)."""
    existing = session.scalar(
        select(Export)
        .where(Export.job_id == job.id, Export.status.in_(ACTIVE))
        .order_by(Export.requested_at.desc())
    )
    if existing is not None:
        return existing, False
    export = Export(seller_account_id=seller_id, job_id=job.id, status=ExportStatus.QUEUED)
    session.add(export)
    session.flush()
    audit.record(
        session,
        seller_account_id=seller_id,
        job_id=job.id,
        event_type="EXPORT_REQUESTED",
        export_id=str(export.id),
    )
    return export, True


def export_path(cfg: Settings, export: Export) -> Path:
    return Path(cfg.export_dir) / export.file_name


def run_export(export_id: uuid.UUID, cfg: Settings) -> None:
    """Worker entry point. Idempotent: only a QUEUED export is claimed."""
    with session_scope() as session:
        claimed = session.execute(
            update(Export)
            .where(Export.id == export_id, Export.status == ExportStatus.QUEUED)
            .values(status=ExportStatus.RUNNING, started_at=utcnow())
        )
        if claimed.rowcount != 1:
            return
        export = session.get(Export, export_id)
        job_id, seller_id = export.job_id, export.seller_account_id

    try:
        with session_scope() as session:
            if session.get_bind().dialect.name == "postgresql":
                # One snapshot for every query the report makes (must be set before the first).
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            job = session.get(Job, job_id)
            checks = count_checks(session, job_id)
            data = build_workbook(session, job)
        name = f"{export_id}.xlsx"
        directory = Path(cfg.export_dir)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{name}.part"
        temporary.write_bytes(data)
        os.replace(temporary, directory / name)  # a half-written file is never downloadable
        now = utcnow()
        with session_scope() as session:
            export = session.get(Export, export_id)
            export.status = ExportStatus.READY
            export.file_name = name
            export.size_bytes = len(data)
            export.checks_count = checks
            export.finished_at = now
            export.expires_at = now + timedelta(hours=cfg.export_ttl_hours)
            audit.record(
                session,
                seller_account_id=seller_id,
                job_id=job_id,
                event_type="EXPORT_READY",
                export_id=str(export_id),
                size_bytes=len(data),
                checks=checks,
            )
    except Exception as exc:
        log.exception("export %s failed", export_id)
        with session_scope() as session:
            export = session.get(Export, export_id)
            export.status = ExportStatus.FAILED
            export.finished_at = utcnow()
            export.error = f"{type(exc).__name__}: {exc}"[:300]


def purge(cfg: Settings) -> dict[str, int]:
    """Delete expired files and records; fail exports whose worker disappeared."""
    now = utcnow()
    stats = {"purged": 0, "failed_stale": 0}
    with session_scope() as session:
        stale = session.scalars(
            select(Export).where(
                Export.status == ExportStatus.RUNNING,
                Export.started_at < now - timedelta(minutes=cfg.export_stale_minutes),
            )
        ).all()
        for export in stale:
            export.status = ExportStatus.FAILED
            export.finished_at = now
            export.error = "The worker building this export stopped; request it again."
            stats["failed_stale"] += 1

        expired = session.scalars(
            select(Export).where(
                (Export.expires_at < now)
                | (
                    (Export.status == ExportStatus.FAILED)
                    & (Export.finished_at < now - timedelta(hours=cfg.export_ttl_hours))
                )
            )
        ).all()
        for export in expired:
            if export.file_name:
                (Path(cfg.export_dir) / export.file_name).unlink(missing_ok=True)
            session.delete(export)
            stats["purged"] += 1
    return stats
