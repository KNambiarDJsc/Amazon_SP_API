from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.config import get_settings


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo on read)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if make_url(url).get_backend_name() == "sqlite":
        # In-memory SQLite is per-connection; share one connection so tests see one database.
        in_memory = ":memory:" in url or url.endswith("://")
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if in_memory else None,
        )

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            # SQLite ignores foreign keys unless asked; the isolation guarantees depend on them.
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True)


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


def configure(engine: Engine) -> None:
    """Point the process-wide factory at a specific engine (tests, CLI)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
