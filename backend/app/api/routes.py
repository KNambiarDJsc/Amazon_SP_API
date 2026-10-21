"""HTTP routes. Thin by design: validate, call a service, shape the response."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import ValidationError

from app.amazon.factory import LiveModeUnavailable, data_source_for, ensure_available
from app.api.deps import current_seller, require_job
from app.config import get_settings
from app.db import session_scope
from app.domain.observations import utcnow
from app.enums import BrandStatus, CheckState, EligibilityStatus
from app.models import SellerAccount
from app.schemas import (
    AuditEventOut,
    BrandOut,
    CheckDetailOut,
    CheckOut,
    JobCreateRequest,
    JobOut,
    JobSettings,
    Page,
)
from app.services import queries
from app.services.ingestion import (
    IngestionError,
    ingest_brands,
    rows_from_list,
    rows_from_upload,
)
from app.services.jobs import (
    JobStateError,
    build_job_out,
    cancel_job,
    create_job,
    pause_job,
    resume_job,
    retry_failed_brands,
)
from app.services.reports import build_workbook, report_filename

router = APIRouter()

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_READ_CHUNK = 64 * 1024

Limit = Query(50, ge=1, le=200)
Offset = Query(0, ge=0)


def _live_guard() -> None:
    try:
        ensure_available(get_settings())
    except LiveModeUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


def _create(seller: SellerAccount, rows, settings: JobSettings, name, filename) -> JobOut:
    _live_guard()
    try:
        with session_scope() as session:
            job = create_job(
                session,
                seller=seller,
                rows=rows,
                settings=settings,
                data_source=data_source_for(get_settings()),
                name=name,
                source_filename=filename,
                now=utcnow(),
            )
            return build_job_out(session, job)
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job_from_list(body: JobCreateRequest, seller: SellerAccount = Depends(current_seller)):
    try:
        rows = rows_from_list(body.brands)
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _create(seller, rows, body.settings, body.name, None)


@router.post("/jobs/upload", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job_from_upload(
    file: UploadFile,
    name: str | None = Form(default=None, max_length=200),
    settings: str | None = Form(default=None, description="JobSettings as a JSON object"),
    seller: SellerAccount = Depends(current_seller),
):
    limit = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"file exceeds {limit} bytes")

    try:
        job_settings = JobSettings.model_validate_json(settings) if settings else JobSettings()
    except ValidationError as exc:
        raise HTTPException(422, f"invalid settings: {exc}") from exc
    try:
        rows = rows_from_upload(file.filename or "", bytes(data))
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _create(seller, rows, job_settings, name, (file.filename or "")[:300] or None)


@router.post("/jobs/preview", response_model=dict)
def preview_brands(body: JobCreateRequest, seller: SellerAccount = Depends(current_seller)):
    """What would be accepted, without creating anything: lets a client fix duplicates and
    invalid rows before committing to a job."""
    try:
        result = ingest_brands(rows_from_list(body.brands))
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "report": result.report.model_dump(mode="json"),
        "brands": [b.display_name for b in result.accepted],
    }


@router.get("/jobs", response_model=Page[JobOut])
def list_jobs(
    limit: int = Limit, offset: int = Offset, seller: SellerAccount = Depends(current_seller)
):
    with session_scope() as session:
        jobs, total = queries.list_jobs(session, seller.id, limit=limit, offset=offset)
        return Page[JobOut](
            items=[build_job_out(session, j) for j in jobs], total=total, limit=limit, offset=offset
        )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job_status(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    with session_scope() as session:
        return build_job_out(session, require_job(session, seller, job_id))


def _control(job_id: uuid.UUID, seller: SellerAccount, action) -> JobOut:
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        try:
            action(session, job)
        except JobStateError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return build_job_out(session, job)


@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    return _control(
        job_id, seller, lambda s, j: pause_job(s, j, reason="Paused by user", now=utcnow())
    )


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    return _control(job_id, seller, lambda s, j: resume_job(s, j, now=utcnow()))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    return _control(job_id, seller, lambda s, j: cancel_job(s, j, now=utcnow()))


@router.post("/jobs/{job_id}/retry-failed", response_model=JobOut)
def retry_failed(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    return _control(job_id, seller, lambda s, j: retry_failed_brands(s, j, now=utcnow()))


@router.get("/jobs/{job_id}/brands", response_model=Page[BrandOut])
def list_brands(
    job_id: uuid.UUID,
    run_status: BrandStatus | None = Query(None, alias="status"),
    result: EligibilityStatus | None = None,
    q: str | None = Query(None, max_length=200),
    limit: int = Limit,
    offset: int = Offset,
    seller: SellerAccount = Depends(current_seller),
):
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        items, total = queries.list_brands(
            session, job, status=run_status, result=result, q=q, limit=limit, offset=offset
        )
        return Page[BrandOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/jobs/{job_id}/checks", response_model=Page[CheckOut])
def list_checks(
    job_id: uuid.UUID,
    result: EligibilityStatus | None = Query(None, alias="status"),
    state: CheckState | None = None,
    brand_id: uuid.UUID | None = None,
    q: str | None = Query(None, max_length=200),
    limit: int = Limit,
    offset: int = Offset,
    seller: SellerAccount = Depends(current_seller),
):
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        items, total = queries.list_checks(
            session,
            job,
            status=result,
            state=state,
            brand_id=brand_id,
            q=q,
            limit=limit,
            offset=offset,
        )
        return Page[CheckOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/jobs/{job_id}/checks/{check_id}", response_model=CheckDetailOut)
def get_check(
    job_id: uuid.UUID, check_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)
):
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        detail = queries.get_check_detail(session, job, check_id)
        if detail is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "check not found")
        return detail


@router.get("/jobs/{job_id}/audit", response_model=list[AuditEventOut])
def get_audit(
    job_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Offset,
    seller: SellerAccount = Depends(current_seller),
):
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        return [
            AuditEventOut.model_validate(e)
            for e in queries.list_audit(session, job, limit=limit, offset=offset)
        ]


@router.get("/jobs/{job_id}/export.xlsx")
def export_workbook(job_id: uuid.UUID, seller: SellerAccount = Depends(current_seller)):
    with session_scope() as session:
        job = require_job(session, seller, job_id)
        data = build_workbook(session, job, generated=utcnow())
        filename = report_filename(job)
    return Response(
        content=data,
        media_type=XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
