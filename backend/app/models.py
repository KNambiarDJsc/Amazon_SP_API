"""Persistent state. Every tenant-owned row carries seller_account_id, and child tables
reference their parent through a composite (id, seller_account_id) foreign key, so the
database itself refuses a row that points into another seller's job."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import UTCDateTime
from app.domain.observations import utcnow
from app.enums import (
    MARKETPLACE_ID_IN,
    AuthorizationMethod,
    AuthorizationStatus,
    BrandMatch,
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    JobStatus,
    ObservationOutcome,
    ObservationSource,
)

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def enum_col(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class SellerAccount(Base):
    __tablename__ = "seller_accounts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    marketplace_id: Mapped[str] = mapped_column(String(20), default=MARKETPLACE_ID_IN)
    # Amazon's merchant token (seller ID); filled in during SP-API authorisation.
    amazon_seller_id: Mapped[str | None] = mapped_column(String(40))
    # SHA-256 of a random high-entropy API key; the key itself is shown once and never stored.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    api_key_prefix: Mapped[str] = mapped_column(String(12))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SellerAuthorization(Base):
    """A seller's grant to this application: which Amazon account granted it, and the LWA
    refresh token, encrypted (app.crypto). The token is never returned by the API; it is only
    decrypted inside a worker, at the moment an access token is needed."""

    __tablename__ = "seller_authorizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    seller_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seller_accounts.id"), unique=True
    )
    selling_partner_id: Mapped[str] = mapped_column(String(40))
    method: Mapped[AuthorizationMethod] = mapped_column(
        enum_col(AuthorizationMethod, "authorization_method")
    )
    status: Mapped[AuthorizationStatus] = mapped_column(
        enum_col(AuthorizationStatus, "authorization_status")
    )
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)  # erased on disconnect
    store_name: Mapped[str | None] = mapped_column(String(200))
    authorized_at: Mapped[datetime] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class OAuthState(Base):
    """One pending consent-page round trip. Only a hash of the state is stored; it is
    single-use, short-lived, and bound to the seller who started it."""

    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = _uuid_pk()
    seller_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seller_accounts.id"), index=True
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("id", "seller_account_id", name="uq_jobs_id_seller"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    seller_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seller_accounts.id"), index=True
    )
    name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[JobStatus] = mapped_column(enum_col(JobStatus, "job_status"), index=True)
    data_source: Mapped[DataSource] = mapped_column(enum_col(DataSource, "data_source"))
    pause_reason: Mapped[str | None] = mapped_column(String(200))
    source_filename: Mapped[str | None] = mapped_column(String(300))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    ingestion_report: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class JobBrand(Base):
    """The unit of scheduling: one brand within one job, with its own checkpoint."""

    __tablename__ = "job_brands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "seller_account_id"],
            ["jobs.id", "jobs.seller_account_id"],
            name="fk_job_brands_job_seller",
        ),
        UniqueConstraint("id", "seller_account_id", name="uq_job_brands_id_seller"),
        UniqueConstraint("job_id", "dedupe_key", name="uq_job_brands_job_dedupe"),
        Index("ix_job_brands_job_status", "job_id", "status"),
        Index("ix_job_brands_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    seller_account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    position: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(500))  # exactly as supplied
    display_name: Mapped[str] = mapped_column(String(300))  # whitespace-normalised, case kept
    dedupe_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[BrandStatus] = mapped_column(
        enum_col(BrandStatus, "brand_status"), default=BrandStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Derived brand-level result; the status here describes the sampled ASINs only.
    brand_status: Mapped[EligibilityStatus | None] = mapped_column(
        enum_col(EligibilityStatus, "brand_eligibility_status")
    )
    brand_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CatalogCandidate(Base):
    """Every product the catalog search returned, kept even when rejected."""

    __tablename__ = "catalog_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_brand_id", "seller_account_id"],
            ["job_brands.id", "job_brands.seller_account_id"],
            name="fk_candidates_brand_seller",
        ),
        UniqueConstraint("id", "seller_account_id", name="uq_candidates_id_seller"),
        UniqueConstraint("job_brand_id", "asin", name="uq_candidates_brand_asin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_brand_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    seller_account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    asin: Mapped[str] = mapped_column(String(20))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(Text)
    brand_attribute: Mapped[str | None] = mapped_column(String(300))
    category_name: Mapped[str | None] = mapped_column(String(300))
    category_id: Mapped[str | None] = mapped_column(String(50))
    product_type: Mapped[str | None] = mapped_column(String(100))
    brand_match: Mapped[BrandMatch] = mapped_column(enum_col(BrandMatch, "brand_match"))
    rejection_reason: Mapped[str | None] = mapped_column(String(200))
    rank: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EligibilityCheck(Base):
    """One ASIN + condition + verification context. `status` is derived, never observed."""

    __tablename__ = "eligibility_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_brand_id", "seller_account_id"],
            ["job_brands.id", "job_brands.seller_account_id"],
            name="fk_checks_brand_seller",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "seller_account_id"],
            ["catalog_candidates.id", "catalog_candidates.seller_account_id"],
            name="fk_checks_candidate_seller",
        ),
        ForeignKeyConstraint(
            ["job_id", "seller_account_id"],
            ["jobs.id", "jobs.seller_account_id"],
            name="fk_checks_job_seller",
        ),
        UniqueConstraint("id", "seller_account_id", name="uq_checks_id_seller"),
        UniqueConstraint("job_id", "check_key", name="uq_checks_job_key"),
        Index("ix_checks_job_status", "job_id", "status"),
        Index("ix_checks_job_brand", "job_brand_id"),
        CheckConstraint("(state = 'DONE') = (status IS NOT NULL)", name="done_iff_status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    seller_account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_brand_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    asin: Mapped[str] = mapped_column(String(20))
    marketplace_id: Mapped[str] = mapped_column(String(20))
    condition: Mapped[str] = mapped_column(String(40))
    verification_context: Mapped[str] = mapped_column(String(100))
    check_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[CheckState] = mapped_column(
        enum_col(CheckState, "check_state"), default=CheckState.PENDING
    )
    status: Mapped[EligibilityStatus | None] = mapped_column(
        enum_col(EligibilityStatus, "check_eligibility_status")
    )
    rule_id: Mapped[str | None] = mapped_column(String(80))
    rationale: Mapped[str | None] = mapped_column(Text)
    rules_version: Mapped[str | None] = mapped_column(String(20))
    reason_codes: Mapped[list[str]] = mapped_column(JSONType, default=list)
    messages: Mapped[list[str]] = mapped_column(JSONType, default=list)
    approval_links: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    verification_methods: Mapped[list[str]] = mapped_column(JSONType, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CheckObservation(Base):
    """Append-only evidence. Never updated; a re-check adds a new row."""

    __tablename__ = "check_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["check_id", "seller_account_id"],
            ["eligibility_checks.id", "eligibility_checks.seller_account_id"],
            name="fk_observations_check_seller",
        ),
        Index("ix_observations_check", "check_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    check_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    seller_account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[ObservationSource] = mapped_column(enum_col(ObservationSource, "obs_source"))
    outcome: Mapped[ObservationOutcome] = mapped_column(enum_col(ObservationOutcome, "obs_outcome"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_detail: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType)
    signals: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    evidence_ref: Mapped[str | None] = mapped_column(String(300))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "seller_account_id"],
            ["jobs.id", "jobs.seller_account_id"],
            name="fk_audit_job_seller",
        ),
        Index("ix_audit_job_created", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    seller_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller_accounts.id"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    job_brand_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    check_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
