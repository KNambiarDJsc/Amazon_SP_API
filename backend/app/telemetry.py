"""Optional distributed tracing (OpenTelemetry). Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set or
an exporter is injected. The packages live in the `telemetry` extra."""

import logging

log = logging.getLogger(__name__)


def setup_tracing(
    app, *, endpoint: str = "", exporter=None, service_name="eligibility-api"
) -> bool:
    """Instrument FastAPI, SQLAlchemy and httpx. Returns False if tracing is off or unavailable."""
    if not (endpoint or exporter):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        log.warning("tracing requested but the 'telemetry' extra is not installed")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    import sqlalchemy

    from app.db import get_engine

    if tuple(int(x) for x in sqlalchemy.__version__.split(".")[:2]) >= (2, 1):
        # The OpenTelemetry SQLAlchemy instrumentation declares support for < 2.1 only.
        log.warning(
            "SQLAlchemy %s is not yet supported by OpenTelemetry; no DB spans",
            sqlalchemy.__version__,
        )

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="healthz,readyz,metrics"
    )
    SQLAlchemyInstrumentor().instrument(engine=get_engine(), tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return True
