"""Shared enumerations. Values equal names so they are stable in the database and in JSON."""

from enum import StrEnum

MARKETPLACE_ID_IN = "A21TJRUUN4KGV"  # Amazon India


class EligibilityStatus(StrEnum):
    VERIFIED_LISTABLE = "VERIFIED_LISTABLE"
    BRAND_APPROVAL_REQUIRED = "BRAND_APPROVAL_REQUIRED"
    OTHER_APPROVAL_REQUIRED = "OTHER_APPROVAL_REQUIRED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    NO_PRODUCT_FOUND = "NO_PRODUCT_FOUND"
    NEEDS_MANUAL_CHECK = "NEEDS_MANUAL_CHECK"
    MIXED_RESULTS = "MIXED_RESULTS"


# Statuses that mean "Amazon says this seller cannot list this ASIN right now".
BLOCKING_STATUSES = frozenset(
    {
        EligibilityStatus.BRAND_APPROVAL_REQUIRED,
        EligibilityStatus.OTHER_APPROVAL_REQUIRED,
        EligibilityStatus.NOT_ELIGIBLE,
    }
)


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class BrandStatus(StrEnum):
    """Execution state of one brand-check unit (not its eligibility outcome)."""

    PENDING = "PENDING"  # waiting for a free slot in the seller's concurrency window
    QUEUED = "QUEUED"  # a task message has been sent; the database row stays the source of truth
    IN_PROGRESS = "IN_PROGRESS"  # leased by a worker
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # retries exhausted: dead-lettered into the manual-review path
    CANCELLED = "CANCELLED"


class CheckState(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"


class ObservationSource(StrEnum):
    SP_API_RESTRICTIONS = "SP_API_RESTRICTIONS"
    BROWSER = "BROWSER"
    MANUAL = "MANUAL"


class ObservationOutcome(StrEnum):
    """Whether the evidence source produced a usable answer at all."""

    OK = "OK"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    HTTP_ERROR = "HTTP_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PAGE_UNAVAILABLE = "PAGE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"


class BrandMatch(StrEnum):
    EXACT = "EXACT"  # same text ignoring case and whitespace
    NORMALIZED = "NORMALIZED"  # same after also dropping punctuation ("e-SSL" == "eSSL")
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"  # catalog item carries no brand attribute


class DataSource(StrEnum):
    LIVE = "LIVE"
    MOCK = "MOCK"


class ExportStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    READY = "READY"
    FAILED = "FAILED"
