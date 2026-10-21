import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import current_seller, db_dep, dispatcher_dep, settings_dep
from app.config import Settings
from app.db import session_scope
from app.domain.observations import BrowserSignals, utcnow
from app.enums import (
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    ExportStatus,
    ObservationSource,
)
from app.models import Export, SellerAccount, SellerCredential
from app.schemas import (
    BrandOut,
    CandidateOut,
    CheckDetailOut,
    CheckOut,
    ErrorOut,
    EventOut,
    ExportOut,
    JobCreateRequest,
    JobOut,
    JobSettings,
    ManualEvidenceIn,
    MeOut,
    Page,
    RetryBrandIn,
    ReviewItem,
)
from app.services import exports, orchestration, review, views
from app.services.dispatch import Dispatcher
from app.services.ingestion import (
    IngestionError,
    IngestionResult,
    ingest,
    rows_from_list,
    rows_from_upload,
)
from app.services.jobs import JobNotFound, JobStateError, get_job
from app.services.reporting import build_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

router = APIRouter(prefix="/api/v1")
NOT_FOUND = {404: {"model": ErrorOut}}

Seller = Annotated[SellerAccount, Depends(current_seller)]
Db = Annotated[Session, Depends(db_dep)]
Cfg = Annotated[Settings, Depends(settings_dep)]
Dispatch = Annotated[Dispatcher, Depends(dispatcher_dep)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


def _data_source(cfg: Settings) -> DataSource:
    return DataSource.MOCK if cfg.amazon_mode == "mock" else DataSource.LIVE


def _owned_job(db: Session, seller: SellerAccount, job_id: uuid.UUID):
    try:
        return get_job(db, seller.id, job_id)
    except JobNotFound:
        raise HTTPException(404, "Job not found.") from None


def _submit(
    cfg: Settings,
    dispatcher: Dispatcher,
    db: Session,
    seller: SellerAccount,
    ingestion: IngestionResult,
    *,
    name: str | None,
    settings: JobSettings,
    filename: str | None = None,
) -> JobOut:
    job_id = orchestration.submit_job(
        dispatcher,
        cfg,
        seller.id,
        ingestion=ingestion,
        name=name,
        settings=settings,
        data_source=_data_source(cfg),
        source_filename=filename,
    )
    db.expire_all()
    return views.job_out(db, _owned_job(db, seller, job_id))


@router.get("/me", response_model=MeOut)
def me(seller: Seller, db: Db, cfg: Cfg) -> MeOut:
    credential = db.get(SellerCredential, seller.id)
    return MeOut(
        name=seller.name,
        marketplace_id=seller.marketplace_id,
        amazon_mode=cfg.amazon_mode,
        connected_to_amazon=bool(credential and credential.status == "ACTIVE"),
    )


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    body: JobCreateRequest, seller: Seller, db: Db, cfg: Cfg, dispatcher: Dispatch
) -> JobOut:
    try:
        ingestion = ingest(
            rows_from_list(body.brands),
            max_brands=cfg.max_brands_per_job,
            max_length=cfg.max_brand_length,
        )
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from None
    return _submit(cfg, dispatcher, db, seller, ingestion, name=body.name, settings=body.settings)


@router.post("/jobs/upload", response_model=JobOut, status_code=201)
def create_job_from_upload(
    seller: Seller,
    db: Db,
    cfg: Cfg,
    dispatcher: Dispatch,
    file: Annotated[UploadFile, File(description=".xlsx or .csv with a Brand column")],
    name: Annotated[str | None, Form(max_length=200)] = None,
    settings: Annotated[str | None, Form(description="JobSettings as JSON")] = None,
) -> JobOut:
    try:
        job_settings = (
            JobSettings.model_validate(json.loads(settings)) if settings else JobSettings()
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, f"Invalid settings: {exc}") from None
    data = file.file.read(cfg.max_upload_bytes + 1)  # never buffer more than the limit allows
    try:
        ingestion = ingest(
            rows_from_upload(file.filename, data, max_bytes=cfg.max_upload_bytes),
            max_brands=cfg.max_brands_per_job,
            max_length=cfg.max_brand_length,
        )
    except IngestionError as exc:
        raise HTTPException(422, str(exc)) from None
    return _submit(
        cfg,
        dispatcher,
        db,
        seller,
        ingestion,
        name=name or file.filename,
        settings=job_settings,
        filename=file.filename,
    )


@router.get("/jobs", response_model=Page[JobOut])
def list_jobs(seller: Seller, db: Db, limit: Limit = 25, offset: Offset = 0):
    return views.list_jobs(db, seller.id, limit, offset)


@router.get("/jobs/{job_id}", response_model=JobOut, responses=NOT_FOUND)
def read_job(job_id: uuid.UUID, seller: Seller, db: Db):
    return views.job_out(db, _owned_job(db, seller, job_id))


def _transition(action, seller: SellerAccount, job_id: uuid.UUID, db: Session) -> JobOut:
    _owned_job(db, seller, job_id)
    try:
        action()
    except JobStateError as exc:
        raise HTTPException(409, str(exc)) from None
    db.expire_all()
    return views.job_out(db, _owned_job(db, seller, job_id))


@router.post("/jobs/{job_id}/pause", response_model=JobOut, responses=NOT_FOUND)
def pause_job(job_id: uuid.UUID, seller: Seller, db: Db):
    return _transition(lambda: orchestration.pause(seller.id, job_id), seller, job_id, db)


@router.post("/jobs/{job_id}/resume", response_model=JobOut, responses=NOT_FOUND)
def resume_job(job_id: uuid.UUID, seller: Seller, db: Db, cfg: Cfg, dispatcher: Dispatch):
    return _transition(
        lambda: orchestration.resume(dispatcher, cfg, seller.id, job_id), seller, job_id, db
    )


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, responses=NOT_FOUND)
def cancel_job(job_id: uuid.UUID, seller: Seller, db: Db):
    return _transition(lambda: orchestration.cancel(seller.id, job_id), seller, job_id, db)


@router.get("/jobs/{job_id}/brands", response_model=Page[BrandOut], responses=NOT_FOUND)
def list_brands(
    job_id: uuid.UUID,
    seller: Seller,
    db: Db,
    status: BrandStatus | None = None,
    brand_status: EligibilityStatus | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    _owned_job(db, seller, job_id)
    return views.list_brands(
        db, job_id, status=status, brand_status=brand_status, q=q, limit=limit, offset=offset
    )


@router.get(
    "/jobs/{job_id}/brands/{brand_id}/candidates",
    response_model=list[CandidateOut],
    responses=NOT_FOUND,
)
def list_candidates(job_id: uuid.UUID, brand_id: uuid.UUID, seller: Seller, db: Db):
    _owned_job(db, seller, job_id)
    try:
        return views.list_candidates(db, job_id, brand_id)
    except JobNotFound:
        raise HTTPException(404, "Brand not found.") from None


@router.get("/jobs/{job_id}/checks", response_model=Page[CheckOut], responses=NOT_FOUND)
def list_checks(
    job_id: uuid.UUID,
    seller: Seller,
    db: Db,
    status: Annotated[
        list[EligibilityStatus] | None,
        Query(description="Repeat to match several statuses (e.g. the 'restricted' group)"),
    ] = None,
    state: CheckState | None = None,
    brand_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    _owned_job(db, seller, job_id)
    return views.list_checks(
        db, job_id, statuses=status, state=state, brand_id=brand_id, q=q, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}/checks/{check_id}", response_model=CheckDetailOut, responses=NOT_FOUND)
def read_check(job_id: uuid.UUID, check_id: uuid.UUID, seller: Seller, db: Db):
    _owned_job(db, seller, job_id)
    try:
        return views.check_detail(db, job_id, check_id)
    except JobNotFound:
        raise HTTPException(404, "Check not found.") from None


@router.get("/jobs/{job_id}/review-queue", response_model=Page[ReviewItem], responses=NOT_FOUND)
def review_queue(
    job_id: uuid.UUID, seller: Seller, db: Db, cfg: Cfg, limit: Limit = 50, offset: Offset = 0
):
    _owned_job(db, seller, job_id)
    return views.review_queue(db, job_id, cfg.seller_central_url, limit, offset)


@router.post(
    "/jobs/{job_id}/checks/{check_id}/manual-evidence",
    response_model=CheckDetailOut,
    responses={**NOT_FOUND, 409: {"model": ErrorOut}},
)
def add_manual_evidence(
    job_id: uuid.UUID, check_id: uuid.UUID, body: ManualEvidenceIn, seller: Seller, db: Db
):
    """Record what you saw in Seller Central. The status is derived from it, never supplied."""
    _owned_job(db, seller, job_id)
    try:
        check = review.get_check(db, job_id, check_id)
        review.record_screen_evidence(
            db,
            check,
            source=ObservationSource.MANUAL,
            observer=f"api-key:{seller.api_key_prefix}",
            signals=BrowserSignals(
                page_asin=body.page_asin,
                page_brand=body.page_brand,
                selected_condition=body.selected_condition,
                notices=body.notices,
                action_label=body.action_label,
                action_enabled=body.action_enabled,
                product_unavailable=body.product_unavailable,
            ),
            note=body.note,
        )
        db.commit()
    except review.ReviewError as exc:
        db.rollback()
        raise HTTPException(404 if "not found" in str(exc) else 409, str(exc)) from None
    return views.check_detail(db, job_id, check_id)


@router.post(
    "/jobs/{job_id}/checks/{check_id}/recheck",
    response_model=CheckDetailOut,
    responses={**NOT_FOUND, 409: {"model": ErrorOut}},
)
def recheck(
    job_id: uuid.UUID,
    check_id: uuid.UUID,
    seller: Seller,
    db: Db,
    cfg: Cfg,
    dispatcher: Dispatch,
):
    """Ask Amazon's API about this ASIN again (earlier evidence is kept as history)."""
    _owned_job(db, seller, job_id)
    try:
        orchestration.recheck_check(dispatcher, cfg, seller.id, job_id, check_id)
    except JobNotFound:
        raise HTTPException(404, "Check not found.") from None
    except JobStateError as exc:
        raise HTTPException(409, str(exc)) from None
    db.expire_all()
    return views.check_detail(db, job_id, check_id)


@router.post(
    "/jobs/{job_id}/brands/{brand_id}/retry",
    response_model=JobOut,
    responses={**NOT_FOUND, 409: {"model": ErrorOut}},
)
def retry_brand(
    job_id: uuid.UUID,
    brand_id: uuid.UUID,
    seller: Seller,
    db: Db,
    cfg: Cfg,
    dispatcher: Dispatch,
    body: RetryBrandIn | None = None,
):
    _owned_job(db, seller, job_id)
    try:
        orchestration.retry_brand(
            dispatcher,
            cfg,
            seller.id,
            job_id,
            brand_id,
            rediscover=body.rediscover if body else False,
        )
    except JobNotFound:
        raise HTTPException(404, "Brand not found.") from None
    except JobStateError as exc:
        raise HTTPException(409, str(exc)) from None
    db.expire_all()
    return views.job_out(db, _owned_job(db, seller, job_id))


@router.get("/jobs/{job_id}/events", response_model=list[EventOut], responses=NOT_FOUND)
def read_events(job_id: uuid.UUID, seller: Seller, db: Db, limit: Limit = 100):
    _owned_job(db, seller, job_id)
    return views.list_events(db, job_id, limit)


def _snapshot_workbook(cfg: Settings, job_id: uuid.UUID) -> bytes:
    """Build the workbook from one consistent snapshot of committed rows."""
    from app.models import Job

    with session_scope() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return build_workbook(session, session.get(Job, job_id))


@router.get(
    "/jobs/{job_id}/export.xlsx",
    responses={**NOT_FOUND, 409: {"model": ErrorOut}, 200: {"content": {XLSX: {}}}},
)
def export_job(job_id: uuid.UUID, seller: Seller, db: Db, cfg: Cfg):
    """Download the report directly. Very large jobs must use POST /jobs/{id}/exports instead,
    because building the file would otherwise hold a request open for tens of seconds."""
    _owned_job(db, seller, job_id)
    size = exports.count_checks(db, job_id)
    if size > cfg.export_sync_max_checks:
        raise HTTPException(
            409,
            f"This job has {size} checks, too many to export in one request "
            f"(limit {cfg.export_sync_max_checks}). Use POST /api/v1/jobs/{job_id}/exports and "
            "download the file when it is ready.",
        )
    return Response(
        _snapshot_workbook(cfg, job_id),
        media_type=XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="eligibility-{str(job_id)[:8]}.xlsx"'
        },
    )


def _export_out(job_id: uuid.UUID, export: Export) -> ExportOut:
    out = ExportOut.model_validate(export)
    if export.status == ExportStatus.READY:
        out.download_url = f"/api/v1/jobs/{job_id}/exports/{export.id}/download"
    return out


@router.post(
    "/jobs/{job_id}/exports", response_model=ExportOut, status_code=202, responses=NOT_FOUND
)
def create_export(job_id: uuid.UUID, seller: Seller, db: Db, dispatcher: Dispatch):
    """Queue a report build; poll the returned export until it is READY, then download it."""
    _owned_job(db, seller, job_id)
    export_id, _ = orchestration.request_export(dispatcher, seller.id, job_id)
    db.expire_all()
    return _export_out(job_id, db.get(Export, export_id))


@router.get("/jobs/{job_id}/exports", response_model=list[ExportOut], responses=NOT_FOUND)
def list_exports(job_id: uuid.UUID, seller: Seller, db: Db):
    _owned_job(db, seller, job_id)
    rows = db.scalars(
        select(Export).where(Export.job_id == job_id).order_by(Export.requested_at.desc()).limit(20)
    ).all()
    return [_export_out(job_id, e) for e in rows]


def _owned_export(db: Session, seller: SellerAccount, job_id: uuid.UUID, export_id: uuid.UUID):
    _owned_job(db, seller, job_id)
    export = db.scalar(
        select(Export).where(
            Export.id == export_id, Export.job_id == job_id, Export.seller_account_id == seller.id
        )
    )
    if export is None:
        raise HTTPException(404, "Export not found.")
    return export


@router.get("/jobs/{job_id}/exports/{export_id}", response_model=ExportOut, responses=NOT_FOUND)
def read_export(job_id: uuid.UUID, export_id: uuid.UUID, seller: Seller, db: Db):
    return _export_out(job_id, _owned_export(db, seller, job_id, export_id))


@router.get(
    "/jobs/{job_id}/exports/{export_id}/download",
    responses={**NOT_FOUND, 409: {"model": ErrorOut}, 410: {"model": ErrorOut}},
)
def download_export(job_id: uuid.UUID, export_id: uuid.UUID, seller: Seller, db: Db, cfg: Cfg):
    export = _owned_export(db, seller, job_id, export_id)
    if export.status != ExportStatus.READY:
        raise HTTPException(409, f"The export is {export.status.value}, not ready to download.")
    path = exports.export_path(cfg, export)
    if (export.expires_at and export.expires_at < utcnow()) or not path.is_file():
        raise HTTPException(410, "This export has expired; request a new one.")
    return FileResponse(
        path,
        media_type=XLSX,
        filename=f"eligibility-{str(job_id)[:8]}.xlsx",
        headers={"Cache-Control": "no-store"},
    )


health = APIRouter()


@health.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@health.get("/readyz", include_in_schema=False)
def readyz(db: Db):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}
