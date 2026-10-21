"""Raw evidence, exactly as observed. Nothing in here interprets or derives a status."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.enums import ObservationOutcome, ObservationSource


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalLink(BaseModel):
    resource: str | None = None
    verb: str | None = None
    title: str | None = None
    type: str | None = None


class RestrictionReason(BaseModel):
    code: str | None = None
    message: str | None = None
    links: list[ApprovalLink] = Field(default_factory=list)


class RestrictionEntry(BaseModel):
    marketplace_id: str | None = None
    condition_type: str | None = None
    reasons: list[RestrictionReason] = Field(default_factory=list)


class SpApiSignals(BaseModel):
    """What getListingsRestrictions returned.

    `restrictions_present` is False when the body had no well-formed `restrictions` list;
    an absent list is missing data, which is different from an empty list (no restrictions).
    """

    restrictions_present: bool
    entries: list[RestrictionEntry] = Field(default_factory=list)


class ObservationContext(BaseModel):
    """The question that was asked; used to confirm an answer is about the same thing."""

    asin: str
    marketplace_id: str
    condition: str
    seller_ref: str
    expected_brand: str | None = None


class Observation(BaseModel):
    source: ObservationSource
    outcome: ObservationOutcome = ObservationOutcome.OK
    context: ObservationContext
    sp_api: SpApiSignals | None = None
    http_status: int | None = None
    error_detail: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    evidence_ref: str | None = None
    raw: dict[str, Any] | None = None
