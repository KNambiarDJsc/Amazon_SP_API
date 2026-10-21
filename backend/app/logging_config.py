"""Structured logging with request/job context, and redaction of anything secret-shaped."""

import contextvars
import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "log_context", default=None
)

# Secrets that must never reach a log line, even by accident (an exception message, a URL).
_REDACTIONS = [
    (re.compile(r"spk_[A-Za-z0-9_\-]{8,}"), "spk_[REDACTED]"),  # this app's API keys
    (re.compile(r"Atz[ar]\|[A-Za-z0-9_\-|.=]+"), "Atz?|[REDACTED]"),  # LWA access/refresh tokens
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-.=]{8,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(client_secret|refresh_token|access_token|password|secret)\b(\s*[=:]\s*)[^\s,;&'\"}]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(x-amz-access-token['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            # Format now so the traceback text can be redacted too.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    _CONTEXT_KEYS = ("request_id", "job_id", "brand_id", "seller")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in self._CONTEXT_KEYS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


@contextmanager
def log_context(**values: str) -> Iterator[None]:
    """Attach request/job identifiers to every log line emitted inside the block."""
    token = _context.set({**(_context.get() or {}), **{k: str(v) for k, v in values.items()}})
    try:
        yield
    finally:
        _context.reset(token)


def setup_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # their URLs can carry query strings
