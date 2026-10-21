"""Pure-ASGI middlewares that protect the API. Pure ASGI (not BaseHTTPMiddleware) so they never
buffer bodies or break streaming responses."""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import log_context
from app.services.rate_limit import RateDecision, RateLimiter

log = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key == name:
            return value.decode("latin-1")
    return None


async def _reply(
    send: Send, status: int, detail: str, headers: dict[str, str] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Defensive response headers, and a request id on every response and log line."""

    def __init__(self, app: ASGIApp, *, docs_paths: tuple[str, ...] = ()):
        self.app = app
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        is_docs = scope["path"].startswith(self.docs_paths) if self.docs_paths else False

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message["headers"]]
                names = {k for k, _ in headers}
                extra = {
                    b"x-request-id": request_id.encode(),
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=()",
                    b"cross-origin-resource-policy": b"same-origin",
                }
                if not is_docs:
                    extra[b"content-security-policy"] = (
                        b"default-src 'none'; frame-ancestors 'none'"
                    )
                if scope["path"].startswith("/api/") and b"cache-control" not in names:
                    extra[b"cache-control"] = b"no-store"  # results are private to the seller
                headers += [(k, v) for k, v in extra.items() if k not in names]
                message = {**message, "headers": headers}
            await send(message)

        with log_context(request_id=request_id):
            await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before the application reads them, whether the size is
    declared (Content-Length) or discovered while streaming (chunked)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        upload_paths: tuple[str, ...] = (),
        upload_limit: int = 0,
    ):
        self.app = app
        self.default_limit = default_limit
        self.upload_paths = upload_paths
        self.upload_limit = upload_limit

    def _limit_for(self, path: str) -> int:
        return self.upload_limit if path in self.upload_paths else self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope["path"])
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _reply(send, 413, f"Request body larger than {limit} bytes.")
            return

        seen = 0
        rejected = False

        async def limited_receive() -> Message:
            nonlocal seen, rejected
            message = await receive()
            if message["type"] == "http.request" and not rejected:
                seen += len(message.get("body", b""))
                if seen > limit:
                    rejected = True
                    await _reply(send, 413, f"Request body larger than {limit} bytes.")
            if rejected:
                return {"type": "http.disconnect"}  # the app stops reading; our 413 stands
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:  # once we have answered, nothing the app sends may follow
                await send(message)

        await self.app(scope, limited_receive, guarded_send)


def client_key(scope: Scope) -> tuple[str, bool]:
    """(rate-limit key, authenticated?) for a request: a hash of the bearer token when one is
    presented (the token itself is never stored or logged), otherwise the client address."""
    auth = _header(scope, b"authorization") or ""
    if auth.lower().startswith("bearer ") and len(auth) > 14:
        return "key:" + hashlib.sha256(auth[7:].encode()).hexdigest()[:20], True
    client = scope.get("client")
    return "ip:" + (client[0] if client else "unknown"), False


def safe_acquire(limiter: RateLimiter, key: str, *, rate: float, burst: int, max_wait: float):
    """Ask the limiter, but never let a limiter outage break a request: if it cannot answer, the
    request is allowed. Availability beats throttling."""
    try:
        return limiter.acquire(key, rate=rate, burst=burst, max_wait=max_wait)
    except Exception:
        log.warning("rate limiter unavailable; allowing the request", exc_info=True)
        return RateDecision(True, 0.0)


class RateLimitMiddleware:
    """Per-key (or per-IP) request throttling. Fails open if the limiter backend is unavailable:
    losing rate limiting is better than losing the service."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: Callable[[], RateLimiter],
        authed: tuple[float, int],
        anon: tuple[float, int],
        exempt: tuple[str, ...] = ("/healthz", "/readyz"),
    ):
        self.app = app
        self.limiter = limiter
        self.authed = authed
        self.anon = anon
        self.exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt:
            await self.app(scope, receive, send)
            return
        key, authenticated = client_key(scope)
        rate, burst = self.authed if authenticated else self.anon
        decision = safe_acquire(self.limiter(), f"api:{key}", rate=rate, burst=burst, max_wait=0.0)
        if not decision.granted:
            retry = max(1, int(decision.wait + 0.999))
            await _reply(send, 429, "Too many requests.", {"Retry-After": str(retry)})
            return
        await self.app(scope, receive, send)


Handler = Callable[[Scope, Receive, Send], Awaitable[None]]
