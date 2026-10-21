"""Typed failures from the Amazon boundary; the orchestrator decides what each one means."""


class AmazonError(Exception):
    """Base class."""


class AmazonRateLimited(AmazonError):
    def __init__(self, retry_after: float | None = None, detail: str = "rate limited"):
        super().__init__(detail)
        self.retry_after = retry_after


class AmazonAuthError(AmazonError):
    """Credentials or authorisation are missing, expired or revoked. Retrying cannot help."""


class AmazonTransientError(AmazonError):
    """Network trouble, timeouts, 5xx. Safe to retry with backoff."""


class AmazonRequestError(AmazonError):
    """The request itself was rejected (4xx other than auth/throttle). Retrying cannot help."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AmazonMalformedResponse(AmazonError):
    """The body was not the shape the API documents."""
