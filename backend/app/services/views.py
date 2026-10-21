"""Read models for the API: queries that return API schemas."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.conditions import label_for
from app.enums import BrandStatus, CheckState, DataSource, EligibilityStatus
from app.models import (
    AuditEvent,
    CatalogCandidate,
    CheckObservation,
    EligibilityCheck,
    Job,
    JobBrand,
)
from app.schemas import (
    BrandOut,
    CandidateOut,
    CheckDetailOut,
    CheckOut,
    IngestionReport,
    JobOut,
    JobSettings,
    ObservationOut,
    Page,
    ReviewItem,
)
from app.services.jobs import JobNotFound, job_progress

MOCK_NOTICE = "Demo data from offline fixtures. These are not live Amazon results."


def job_out(session: Session, job: Job) -> JobOut:
    return JobOut(
        id=job.id,
        name=job.name,
        status=job.status,
        data_source=job.data_source,
        pause_reason=job.pause_reason,
        source_filename=job.source_filename,
        settings=JobSettings.model_validate(job.settings),
        ingestion_report=IngestionReport.model_validate(job.ingestion_report),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress=job_progress(session, job.id),
        notice=MOCK_NOTICE if job.data_source == DataSource.MOCK else None,
    )


def list_jobs(session: Session, seller_id: uuid.UUID, limit: int, offset: int) -> Page[JobOut]:
    where = Job.seller_account_id == seller_id
    total = session.scalar(select(func.count()).select_from(Job).where(where))
    jobs = session.scalars(
        select(Job).where(where).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return Page(items=[job_out(session, j) for j in jobs], total=total, limit=limit, offset=offset)


def list_brands(
    session: Session,
    job_id: uuid.UUID,
    *,
    status: BrandStatus | None,
    brand_status: EligibilityStatus | None,
    q: str | None,
    limit: int,
    offset: int,
) -> Page[BrandOut]:
    query = select(JobBrand).where(JobBrand.job_id == job_id)
    if status:
        query = query.where(JobBrand.status == status)
    if brand_status:
        query = query.where(JobBrand.brand_status == brand_status)
    if q:
        query = query.where(JobBrand.display_name.icontains(q, autoescape=True))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(JobBrand.position).limit(limit).offset(offset)).all()
    return Page(
        items=[BrandOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )


def _check_out(check: EligibilityCheck, brand: JobBrand, title: str | None) -> CheckOut:
    return CheckOut(
        id=check.id,
        job_brand_id=check.job_brand_id,
        brand=brand.display_name,
        asin=check.asin,
        title=title,
        marketplace_id=check.marketplace_id,
        condition=check.condition,
        state=check.state,
        status=check.status,
        rule_id=check.rule_id,
        rationale=check.rationale,
        reason_codes=check.reason_codes,
        messages=check.messages,
        approval_links=check.approval_links,
        verification_methods=check.verification_methods,
        checked_at=check.checked_at,
    )


def list_checks(
    session: Session,
    job_id: uuid.UUID,
    *,
    status: EligibilityStatus | None,
    state: CheckState | None,
    brand_id: uuid.UUID | None,
    q: str | None,
    limit: int,
    offset: int,
) -> Page[CheckOut]:
    """Two steps, so cost does not grow with the size of the job: choose the page of ids from the
    checks table alone (an index scan in display order), then load details for just those rows.
    Joins are only used inside the text search, as subqueries."""
    where = [EligibilityCheck.job_id == job_id]
    if status:
        where.append(EligibilityCheck.status == status)
    if state:
        where.append(EligibilityCheck.state == state)
    if brand_id:
        where.append(EligibilityCheck.job_brand_id == brand_id)
    if q:
        by_brand = select(JobBrand.id).where(
            JobBrand.job_id == job_id, JobBrand.display_name.icontains(q, autoescape=True)
        )
        by_title = (
            select(CatalogCandidate.id)
            .join(JobBrand, JobBrand.id == CatalogCandidate.job_brand_id)
            .where(JobBrand.job_id == job_id, CatalogCandidate.title.icontains(q, autoescape=True))
        )
        where.append(
            EligibilityCheck.asin.icontains(q, autoescape=True)
            | EligibilityCheck.job_brand_id.in_(by_brand)
            | EligibilityCheck.candidate_id.in_(by_title)
        )

    total = session.scalar(select(func.count()).select_from(EligibilityCheck).where(*where))
    ids = session.scalars(
        select(EligibilityCheck.id)
        .where(*where)
        .order_by(EligibilityCheck.seq)
        .limit(limit)
        .offset(offset)
    ).all()
    rows = (
        session.execute(
            select(EligibilityCheck, JobBrand, CatalogCandidate.title)
            .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
            .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
            .where(EligibilityCheck.id.in_(ids))
            .order_by(EligibilityCheck.seq)
        ).all()
        if ids
        else []
    )
    return Page(
        items=[_check_out(c, b, t) for c, b, t in rows], total=total, limit=limit, offset=offset
    )


def check_detail(session: Session, job_id: uuid.UUID, check_id: uuid.UUID) -> CheckDetailOut:
    row = session.execute(
        select(EligibilityCheck, JobBrand, CatalogCandidate)
        .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
        .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
        .where(EligibilityCheck.id == check_id, EligibilityCheck.job_id == job_id)
    ).first()
    if row is None:
        raise JobNotFound(str(check_id))
    check, brand, candidate = row
    observations = session.scalars(
        select(CheckObservation)
        .where(CheckObservation.check_id == check.id)
        .order_by(CheckObservation.observed_at, CheckObservation.id)
    ).all()
    base = _check_out(check, brand, candidate.title)
    return CheckDetailOut(
        **base.model_dump(),
        rules_version=check.rules_version,
        candidate=CandidateOut.model_validate(candidate),
        observations=[ObservationOut.model_validate(o) for o in observations],
    )


def list_candidates(session: Session, job_id: uuid.UUID, brand_id: uuid.UUID) -> list[CandidateOut]:
    brand = session.scalar(
        select(JobBrand).where(JobBrand.id == brand_id, JobBrand.job_id == job_id)
    )
    if brand is None:
        raise JobNotFound(str(brand_id))
    rows = session.scalars(
        select(CatalogCandidate)
        .where(CatalogCandidate.job_brand_id == brand_id)
        .order_by(CatalogCandidate.rank.is_(None), CatalogCandidate.rank, CatalogCandidate.asin)
    ).all()
    return [CandidateOut.model_validate(r) for r in rows]


def list_events(session: Session, job_id: uuid.UUID, limit: int) -> list[dict]:
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.job_id == job_id)
        .order_by(AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "job_brand_id": r.job_brand_id,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def review_queue(
    session: Session, job_id: uuid.UUID, seller_central_url: str, limit: int, offset: int
) -> Page[ReviewItem]:
    """Everything a person still has to look at: ASIN checks the machine could not settle, then
    brands that found nothing, failed, or were too thinly sampled. Paginated in SQL."""
    check_where = (
        EligibilityCheck.job_id == job_id,
        EligibilityCheck.state == CheckState.DONE,
        EligibilityCheck.status == EligibilityStatus.NEEDS_MANUAL_CHECK,
    )
    brand_where = (
        JobBrand.job_id == job_id,
        JobBrand.brand_status.in_(
            [EligibilityStatus.NO_PRODUCT_FOUND, EligibilityStatus.NEEDS_MANUAL_CHECK]
        ),
    )
    n_checks = session.scalar(
        select(func.count()).select_from(EligibilityCheck).where(*check_where)
    )
    n_brands = session.scalar(select(func.count()).select_from(JobBrand).where(*brand_where))

    items: list[ReviewItem] = []
    if offset < n_checks:
        rows = session.execute(
            select(EligibilityCheck, JobBrand, CatalogCandidate.title)
            .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
            .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
            .where(*check_where)
            .order_by(EligibilityCheck.seq)
            .limit(limit)
            .offset(offset)
        ).all()
        for check, brand, title in rows:
            label = label_for(check.condition)
            items.append(
                ReviewItem(
                    kind="CHECK",
                    brand_id=brand.id,
                    brand=brand.display_name,
                    check_id=check.id,
                    asin=check.asin,
                    title=title,
                    condition=check.condition,
                    status=check.status,
                    rule_id=check.rule_id,
                    rationale=check.rationale,
                    actions=["record_evidence", "recheck"],
                    how_to=[
                        f"In Seller Central, open the product panel for ASIN {check.asin}.",
                        f"Choose the condition '{label}' in the dropdown. Do not click Sell, "
                        "Apply to sell or Copy listing.",
                        "Record exactly what is on screen: the ASIN shown, the condition selected, "
                        "the primary button's label and whether it is enabled, and the text of "
                        "every notice.",
                    ],
                    seller_central_url=seller_central_url,
                )
            )

    room = limit - len(items)
    if room > 0:
        brands = session.scalars(
            select(JobBrand)
            .where(*brand_where)
            .order_by(JobBrand.position)
            .limit(room)
            .offset(max(0, offset - n_checks))
        ).all()
        for brand in brands:
            summary = brand.brand_summary or {}
            items.append(
                ReviewItem(
                    kind="BRAND",
                    brand_id=brand.id,
                    brand=brand.display_name,
                    status=brand.brand_status,
                    rule_id=summary.get("rule_id"),
                    rationale=(summary.get("rationale") or "")
                    + (f" Last error: {brand.last_error}" if brand.last_error else ""),
                    actions=["retry_brand"],
                    how_to=[
                        "Check the brand's spelling as Amazon writes it, then retry.",
                        "Use 'rediscover' to search the catalog again from the start.",
                    ],
                )
            )
    return Page(items=items, total=n_checks + n_brands, limit=limit, offset=offset)
