"""API contracts. These are the public request/response shapes; routes in app.api build on them."""

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.observations import ApprovalLink
from app.domain.summary import BrandSummary
from app.enums import (
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

# conditionType values accepted by getListingsRestrictions.
Condition = Literal[
    "new_new",
    "new_open_box",
    "new_oem",
    "refurbished_refurbished",
    "used_like_new",
    "used_very_good",
    "used_good",
    "used_acceptable",
    "collectible_like_new",
    "collectible_very_good",
    "collectible_good",
    "collectible_acceptable",
    "club_club",
]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- input ---------------------------------------------------------------------------------


class JobSettings(BaseModel):
    conditions: list[Condition] = Field(default=["new_new"], min_length=1, max_length=4)
    max_asins_per_brand: int = Field(default=5, ge=1, le=10)
    min_sample_size: int = Field(default=3, ge=1, le=10)
    max_catalog_pages: int = Field(default=3, ge=1, le=5)

    @field_validator("conditions")
    @classmethod
    def _unique_conditions(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("conditions must be unique")
        return v

    @model_validator(mode="after")
    def _sample_fits(self) -> "JobSettings":
        if self.min_sample_size > self.max_asins_per_brand:
            raise ValueError("min_sample_size cannot exceed max_asins_per_brand")
        return self


class JobCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    brands: list[str] = Field(min_length=1)
    settings: JobSettings = Field(default_factory=JobSettings)


# ---- ingestion report ----------------------------------------------------------------------


class DuplicateRow(BaseModel):
    row: int
    original_name: str
    duplicate_of_row: int


class InvalidRow(BaseModel):
    row: int
    original_name: str
    reason: str


class IngestionReport(BaseModel):
    total_rows: int = 0
    accepted: int = 0
    empty_rows: list[int] = Field(default_factory=list)
    duplicates: list[DuplicateRow] = Field(default_factory=list)
    invalid: list[InvalidRow] = Field(default_factory=list)


# ---- output --------------------------------------------------------------------------------


class JobProgress(BaseModel):
    brands_total: int = 0
    brands_pending: int = 0
    brands_in_progress: int = 0
    brands_completed: int = 0
    brands_failed: int = 0
    brands_cancelled: int = 0
    checks_total: int = 0
    checks_done: int = 0
    verified_listable: int = 0
    restricted: int = 0  # brand approval + other approval + not eligible
    needs_manual_check: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    percent_complete: float = 0.0


class JobOut(ORM):
    id: uuid.UUID
    name: str | None
    status: JobStatus
    data_source: DataSource
    pause_reason: str | None
    source_filename: str | None
    settings: JobSettings
    ingestion_report: IngestionReport
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: JobProgress
    notice: str | None = None  # set for MOCK jobs so demo data is never mistaken for live data


class BrandOut(ORM):
    id: uuid.UUID
    position: int
    original_name: str
    display_name: str
    status: BrandStatus
    attempts: int
    last_error: str | None
    brand_status: EligibilityStatus | None
    brand_summary: BrandSummary | None
    started_at: datetime | None
    finished_at: datetime | None


class CandidateOut(ORM):
    id: uuid.UUID
    asin: str
    title: str | None
    brand_attribute: str | None
    category_name: str | None
    product_type: str | None
    brand_match: BrandMatch
    rejection_reason: str | None
    rank: int | None
    selected: bool


class CheckOut(ORM):
    id: uuid.UUID
    job_brand_id: uuid.UUID
    brand: str
    asin: str
    title: str | None
    marketplace_id: str
    condition: str
    state: CheckState
    status: EligibilityStatus | None
    rule_id: str | None
    rationale: str | None
    reason_codes: list[str]
    messages: list[str]
    approval_links: list[ApprovalLink]
    verification_methods: list[str]
    checked_at: datetime | None


class ObservationOut(ORM):
    id: int
    source: ObservationSource
    outcome: ObservationOutcome
    http_status: int | None
    error_detail: str | None
    context: dict[str, Any]
    signals: dict[str, Any] | None
    raw: dict[str, Any] | None
    evidence_ref: str | None
    observed_at: datetime


class CheckDetailOut(CheckOut):
    rules_version: str | None
    candidate: CandidateOut
    observations: list[ObservationOut]


class AuditEventOut(ORM):
    id: int
    event_type: str
    job_brand_id: uuid.UUID | None
    check_id: uuid.UUID | None
    payload: dict[str, Any]
    created_at: datetime


class AuthorizationOut(BaseModel):
    """The state of a seller's Amazon grant. Never carries the token itself."""

    connected: bool
    status: AuthorizationStatus | None = None
    method: AuthorizationMethod | None = None
    selling_partner_id: str | None = None
    store_name: str | None = None
    authorized_at: datetime | None = None
    verified_at: datetime | None = None
    # Amazon refresh tokens last 365 days; the seller must grant access again before this.
    reauthorize_by: datetime | None = None
    last_error: str | None = None


class AuthorizationStart(BaseModel):
    authorization_url: str
    expires_at: datetime


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorOut(BaseModel):
    detail: str
