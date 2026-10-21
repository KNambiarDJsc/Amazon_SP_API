"""Typed failures from the Amazon boundary; the orchestrator decides what each one means."""


class AmazonError(Exception):
    """Base class. `request_id` is Amazon's x-amzn-RequestId, when the failure came from a
    response, so the evidence can point at the exact call."""

    request_id: str | None = None


class AmazonRateLimited(AmazonError):
    def __init__(self, retry_after: float | None = None, detail: str = "rate limited"):
        super().__init__(detail)
        self.retry_after = retry_after


class AmazonAuthError(AmazonError):
    """Credentials or authorisation are missing, expired or revoked. Retrying cannot help."""


class SellerNotAuthorized(AmazonAuthError):
    """No usable grant is stored for the seller (never connected, disconnected, or the stored
    token cannot be decrypted), so no request is sent to Amazon at all."""


class AmazonTransientError(AmazonError):
    """Network trouble, timeouts, 5xx. Safe to retry with backoff."""


class AmazonTimeout(AmazonTransientError):
    """No answer in time. Recorded as TIMEOUT evidence rather than a generic HTTP error."""


class AmazonRequestError(AmazonError):
    """The request itself was rejected (4xx other than auth/throttle). Retrying cannot help."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AmazonMalformedResponse(AmazonError):
    """The body was not the shape the API documents."""
