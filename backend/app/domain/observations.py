"""Raw evidence, exactly as observed. Nothing in here interprets or derives a status."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.enums import ObservationOutcome, ObservationSource


def utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalLink(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

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


class BrowserSignals(BaseModel):
    """What was on the Seller Central screen, whether read by the browser adapter or typed in by
    a human reviewer. Observations only: the verdict is derived from these by the rules engine."""

    signed_in: bool = True
    challenge_detected: bool = False  # CAPTCHA / MFA / robot check: a human must take over
    page_loaded: bool = True
    product_unavailable: bool = False
    page_asin: str | None = None
    page_brand: str | None = None  # the panel often shows none; the catalog attribute covers it
    selected_condition: str | None = None  # normalised conditionType, e.g. "new_new"
    notices: list[str] = Field(default_factory=list)  # every notice/alert visible in the panel
    action_label: str | None = None  # "Sell this product" | "Apply to sell"
    action_enabled: bool | None = None


class ObservationContext(BaseModel):
    """The question that was asked; used to confirm an answer is about the same thing."""

    asin: str
    marketplace_id: str
    condition: str
    seller_ref: str
    expected_brand: str | None = None
    # True when the catalog's own brand attribute for this ASIN already matched the searched brand.
    brand_validated: bool = False


class Observation(BaseModel):
    source: ObservationSource
    outcome: ObservationOutcome = ObservationOutcome.OK
    context: ObservationContext
    sp_api: SpApiSignals | None = None
    browser: BrowserSignals | None = None
    http_status: int | None = None
    error_detail: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    evidence_ref: str | None = None
    raw: dict[str, Any] | None = None
