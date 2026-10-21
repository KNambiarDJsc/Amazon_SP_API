import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import amazon
from app.api.deps import build_limiter
from app.api.http_metrics import MetricsMiddleware
from app.api.routes import health, router
from app.api.security import BodySizeLimitMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from app.config import Settings, get_settings
from app.logging_config import setup_logging
from app.observability import render_metrics
from app.services.rate_limit import RateLimiter
from app.telemetry import setup_tracing

DOC_PATHS = ("/docs", "/openapi.json")
UPLOAD_PATH = "/api/v1/jobs/upload"


def create_app(cfg: Settings | None = None, *, limiter: RateLimiter | None = None) -> FastAPI:
    cfg = cfg or get_settings()
    production = cfg.environment == "production"
    docs = cfg.enable_api_docs and not production

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        setup_logging(cfg.log_level, json_logs=cfg.log_json)
        yield

    app = FastAPI(
        title="Amazon India Seller Eligibility Checker",
        version="0.1.0",
        description=(
            "ASIN-level, evidence-backed listing eligibility for an authorised seller account. "
            "A brand-level result describes only the ASINs that were actually checked."
        ),
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    app.state.limiter = limiter or build_limiter(cfg)
    app.state.settings = cfg
    app.include_router(router)
    app.include_router(amazon.router)
    app.include_router(health)

    @app.get("/metrics", include_in_schema=False)
    def metrics(authorization: str | None = Header(default=None)):
        token = cfg.metrics_token.get_secret_value()
        if token:
            supplied = (authorization or "").removeprefix("Bearer ").strip()
            if not secrets.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(401, "Invalid metrics token.")
        body, content_type = render_metrics()
        return Response(body, media_type=content_type)

    # add_middleware wraps: the last one added is the outermost.
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=cfg.max_json_body_bytes,
        upload_paths=(UPLOAD_PATH,),
        upload_limit=cfg.max_upload_bytes + 64 * 1024,  # multipart framing overhead
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=lambda: app.state.limiter,
        authed=(cfg.api_rate_per_sec, cfg.api_rate_burst),
        anon=(cfg.api_anon_rate_per_sec, cfg.api_anon_rate_burst),
        exempt=("/healthz", "/readyz", "/metrics"),
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, docs_paths=DOC_PATHS if docs else ())
    hosts = [h.strip() for h in cfg.allowed_hosts.split(",") if h.strip()]
    if hosts and hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    setup_tracing(app, endpoint=cfg.otel_exporter_otlp_endpoint)
    return app


app = create_app()
