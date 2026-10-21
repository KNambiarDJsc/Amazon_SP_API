from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app import __version__
from app.api.authorization import router as authorization_router
from app.api.routes import router
from app.config import get_settings
from app.db import get_engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = None
    if settings.inline_worker:
        from app.workers.inline import InlineEngine
        from app.workers.runtime import build_runtime

        engine = InlineEngine(
            build_runtime(settings, limiter=_in_memory_limiter()),
            settings.scheduler_interval_seconds,
        )
        engine.start()
    try:
        yield
    finally:
        if engine is not None:
            engine.stop()


def _in_memory_limiter():
    from app.services.ratelimit import InMemoryRateLimiter

    return InMemoryRateLimiter()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Amazon India Seller Eligibility Checker", version=__version__, lifespan=lifespan
    )
    app.include_router(router, prefix="/api/v1")
    app.include_router(authorization_router, prefix="/api/v1")

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "amazon_mode": get_settings().amazon_mode}

    return app


app = create_app()
