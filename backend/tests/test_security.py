import json
import logging

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import observability
from app.api.deps import dispatcher_dep, settings_dep
from app.api.main import create_app
from app.config import Settings
from app.db import session_scope
from app.logging_config import JsonFormatter, RedactingFilter, log_context, redact
from app.services.accounts import create_seller_account
from app.services.dispatch import InlineDispatcher
from app.services.rate_limit import InMemoryTokenBucket


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_app(engine, clock=None, **overrides):
    cfg = Settings(database_url="sqlite://", amazon_mode="mock", **overrides)
    limiter = InMemoryTokenBucket(clock) if clock else None
    app = create_app(cfg, limiter=limiter)
    app.dependency_overrides[settings_dep] = lambda: cfg
    app.dependency_overrides[dispatcher_dep] = lambda: InlineDispatcher()
    return TestClient(app), cfg


def seller_headers(name="S"):
    with session_scope() as s:
        _, key = create_seller_account(s, name=name)
    return {"Authorization": f"Bearer {key}"}


# ---- response headers ------------------------------------------------------------------------


def test_every_response_carries_defensive_headers(engine):
    client, _ = make_app(engine)
    r = client.get("/api/v1/jobs", headers=seller_headers())
    h = r.headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
    assert h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert "camera=()" in h["permissions-policy"]
    # errors carry them too
    assert client.get("/api/v1/jobs").headers["x-content-type-options"] == "nosniff"


def test_request_ids_are_generated_echoed_and_never_taken_blindly(engine):
    client, _ = make_app(engine)
    generated = client.get("/healthz").headers["x-request-id"]
    assert len(generated) == 32
    assert (
        client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"}).headers["x-request-id"]
        == "trace-abc-12345"
    )
    for hostile in ("short", "has space in it", "x" * 100, "a<script>alert(1)</script>bbb"):
        got = client.get("/healthz", headers={"X-Request-ID": hostile}).headers["x-request-id"]
        assert got != hostile and len(got) == 32


def test_api_docs_exist_in_development_and_vanish_in_production(engine):
    dev, _ = make_app(engine)
    assert dev.get("/docs").status_code == 200 and dev.get("/openapi.json").status_code == 200
    assert "content-security-policy" not in dev.get("/docs").headers  # swagger needs its scripts
    prod, _ = make_app(engine, environment="production", api_rate_limit_enabled=False)
    assert prod.get("/docs").status_code == 404 and prod.get("/openapi.json").status_code == 404
    assert prod.get("/redoc").status_code == 404


def test_disallowed_host_headers_are_refused(engine):
    client, _ = make_app(engine, allowed_hosts="app.example.test")
    assert client.get("/healthz", headers={"Host": "app.example.test"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400


# ---- request size ----------------------------------------------------------------------------


def test_oversized_json_is_refused_by_declared_length(engine):
    client, cfg = make_app(engine, max_json_body_bytes=2000)
    big = json.dumps({"brands": ["x" * 50] * 100})
    r = client.post(
        "/api/v1/jobs",
        content=big,
        headers={**seller_headers(), "Content-Type": "application/json"},
    )
    assert r.status_code == 413 and "larger than 2000" in r.json()["detail"]


def test_oversized_chunked_bodies_are_refused_while_streaming(engine):
    client, _ = make_app(engine, max_json_body_bytes=2000)

    def chunks():
        for _ in range(50):  # no Content-Length: the size is only discovered while reading
            yield b'{"brands": ["' + b"x" * 100 + b'"]}'

    r = client.post(
        "/api/v1/jobs",
        content=chunks(),
        headers={**seller_headers(), "Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_normal_requests_and_gets_are_unaffected_by_the_size_limit(engine):
    client, _ = make_app(engine, max_json_body_bytes=2000)
    h = seller_headers()
    assert client.post("/api/v1/jobs", json={"brands": ["eSSL"]}, headers=h).status_code == 201
    assert client.get("/api/v1/jobs", headers=h).status_code == 200


def test_uploads_have_their_own_larger_limit(engine):
    client, _ = make_app(engine, max_json_body_bytes=500, max_upload_bytes=4000)
    h = seller_headers()
    ok = client.post(
        "/api/v1/jobs/upload", headers=h, files={"file": ("b.csv", b"Brand\n" + b"eSSL\n" * 200)}
    )  # ~1 KB > JSON limit
    assert ok.status_code == 201
    huge = client.post(
        "/api/v1/jobs/upload", headers=h, files={"file": ("b.csv", b"x" * (4000 + 70_000))}
    )
    assert huge.status_code == 413


# ---- rate limiting ---------------------------------------------------------------------------


def test_requests_are_throttled_per_api_key_and_recover_with_time(engine):
    clock = Clock()
    client, _ = make_app(engine, clock=clock, api_rate_per_sec=1, api_rate_burst=3)
    a, b = seller_headers("A"), seller_headers("B")
    assert [client.get("/api/v1/jobs", headers=a).status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get("/api/v1/jobs", headers=a)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "1"
    assert blocked.json() == {"detail": "Too many requests."}
    assert "x-request-id" in blocked.headers  # even a refusal is traceable
    assert client.get("/api/v1/jobs", headers=b).status_code == 200  # another key is unaffected
    clock.t += 1.1
    assert client.get("/api/v1/jobs", headers=a).status_code == 200  # refilled


def test_unauthenticated_requests_are_throttled_by_address(engine):
    client, _ = make_app(engine, clock=Clock(), api_anon_rate_per_sec=1, api_anon_rate_burst=2)
    codes = [client.get("/api/v1/amazon/callback").status_code for _ in range(4)]
    assert 429 in codes and codes[0] != 429


def test_health_and_metrics_probes_are_never_throttled(engine):
    client, _ = make_app(engine, clock=Clock(), api_anon_rate_per_sec=1, api_anon_rate_burst=1)
    assert all(client.get(p).status_code == 200 for p in ["/healthz"] * 10 + ["/metrics"] * 5)


def test_a_broken_limiter_backend_fails_open(engine):
    class Broken:
        def acquire(self, *a, **k):
            raise ConnectionError("redis down")

    cfg = Settings(database_url="sqlite://", amazon_mode="mock")
    client = TestClient(create_app(cfg, limiter=Broken()))
    assert client.get("/healthz").status_code == 200
    assert (
        client.get("/api/v1/jobs").status_code == 401
    )  # served (and rejected for auth), not a 500


def test_guessing_api_keys_is_throttled_but_real_users_are_not(engine):
    clock = Clock()
    client, _ = make_app(engine, clock=clock, auth_failure_burst=3, auth_failure_refill_seconds=60)
    good = seller_headers()
    wrong = {"Authorization": "Bearer spk_" + "x" * 43}
    codes = [client.get("/api/v1/jobs", headers=wrong).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [
        429,
        429,
    ]  # then the guesses are throttled
    assert client.get("/api/v1/jobs", headers=wrong).headers["retry-after"]
    # a valid key from the same address is untouched, however many guesses came before
    assert [client.get("/api/v1/jobs", headers=good).status_code for _ in range(5)] == [200] * 5
    clock.t += 61  # one token refills
    assert client.get("/api/v1/jobs", headers=wrong).status_code == 401


# ---- secrets stay out of logs and reprs ------------------------------------------------------


SECRETS = {
    "api key": "spk_AbCdEf0123456789_-AbCdEf0123456789",
    "access token": "Atza|IwEBIAbCdEf0123456789xyz",
    "refresh token": "Atzr|IwEBIAbCdEf0123456789xyz",
}


@pytest.mark.parametrize(
    "text",
    [
        "auth failed for Bearer spk_AbCdEf0123456789_-AbCdEf0123456789",
        "token Atza|IwEBIAbCdEf0123456789xyz expired",
        "refresh Atzr|IwEBIAbCdEf0123456789xyz",
        "POST failed client_secret=hunter2hunter2 grant_type=refresh_token",
        "payload {'refresh_token': 'Atzr|IwEBIAbCdEf0123456789xyz'}",
        "headers {'x-amz-access-token': 'Atza|IwEBIAbCdEf0123456789xyz'}",
        "password: correct-horse-battery",
        "Authorization: Bearer abcdefghijklmnop12345",
    ],
)
def test_secret_shaped_text_is_redacted(text):
    cleaned = redact(text)
    for fragment in ("spk_AbCdEf", "IwEBIAbCdEf", "hunter2", "correct-horse", "abcdefghijklmnop"):
        assert fragment not in cleaned
    assert "[REDACTED]" in cleaned


def test_ordinary_text_is_left_alone():
    text = "Brand eSSL checked 4 ASINs: B08PDL14VR is VERIFIED_LISTABLE (job 7f3a)"
    assert redact(text) == text


def test_log_records_and_tracebacks_are_redacted_before_formatting():
    logger = logging.getLogger("test.redaction")
    stream = []
    handler = logging.Handler()
    handler.emit = lambda record: stream.append(JsonFormatter().format(record))
    handler.addFilter(RedactingFilter())
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        raise RuntimeError(f"upstream said {SECRETS['refresh token']} is invalid")
    except RuntimeError:
        logger.error("call failed with key %s", SECRETS["api key"], exc_info=True)
    line = stream[0]
    assert "spk_AbCdEf" not in line and "IwEBIAbCdEf" not in line
    assert json.loads(line)["level"] == "ERROR"


def test_json_logs_carry_request_and_job_context_only_inside_the_block():
    handler = logging.Handler()
    out = []
    handler.emit = lambda record: out.append(json.loads(JsonFormatter().format(record)))
    from app.logging_config import ContextFilter

    handler.addFilter(ContextFilter())
    logger = logging.getLogger("test.context")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    with log_context(request_id="req-1"):
        with log_context(job_id="job-9", brand_id="b-3"):
            logger.info("inside")
        logger.info("outer")
    logger.info("outside")
    assert (out[0]["request_id"], out[0]["job_id"], out[0]["brand_id"]) == ("req-1", "job-9", "b-3")
    assert out[1]["request_id"] == "req-1" and "job_id" not in out[1]
    assert "request_id" not in out[2]


def test_settings_never_reveal_secrets_when_printed_or_dumped():
    cfg = Settings(
        database_url="x",
        lwa_client_secret="client-secret-value-1234",
        credential_encryption_keys="key-material-abcdef",
        metrics_token="metrics-token-value",
    )
    for rendering in (
        repr(cfg),
        str(cfg),
        cfg.model_dump_json(),
        json.dumps(cfg.model_dump(), default=str),
    ):
        for secret in ("client-secret-value", "key-material", "metrics-token-value"):
            assert secret not in rendering


# ---- metrics ---------------------------------------------------------------------------------


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def scrape(client, **headers):
    observability._db_collector._cached = None  # make the DB gauges fresh
    r = client.get("/metrics", headers=headers)
    return r


def test_metrics_cover_http_amazon_brand_and_state(engine, make_harness):
    from apienv import Env

    env = Env()
    _, auth = env.new_seller()
    before = {
        "ok": sample(
            "eligibility_amazon_requests_total", operation="getListingsRestrictions", outcome="ok"
        ),
        "done": sample("eligibility_brand_tasks_total", result="completed"),
        "gated": sample("eligibility_checks_classified_total", status="BRAND_APPROVAL_REQUIRED"),
        "http": sample(
            "eligibility_http_requests_total", method="POST", route="/api/v1/jobs", status="201"
        ),
    }
    job = env.http.post("/api/v1/jobs", json={"brands": ["eSSL", "Havells"]}, headers=auth).json()[
        "id"
    ]
    env.drain()
    env.http.get(f"/api/v1/jobs/{job}", headers=auth)

    assert (
        sample(
            "eligibility_amazon_requests_total", operation="getListingsRestrictions", outcome="ok"
        )
        - before["ok"]
        == 6
    )  # 4 eSSL ASINs + 2 Havells ASINs
    assert sample("eligibility_brand_tasks_total", result="completed") - before["done"] == 2
    assert (
        sample("eligibility_checks_classified_total", status="BRAND_APPROVAL_REQUIRED")
        - before["gated"]
        == 2
    )
    assert (
        sample("eligibility_http_requests_total", method="POST", route="/api/v1/jobs", status="201")
        - before["http"]
        == 1
    )
    assert (
        sample(
            "eligibility_http_requests_total", method="GET", route="/api/v1/jobs/{id}", status="200"
        )
        >= 1
    )  # the job id collapsed: bounded label cardinality

    body = scrape(env.http).text
    assert 'eligibility_jobs{status="COMPLETED"} 1.0' in body
    assert 'eligibility_brands{status="COMPLETED"} 2.0' in body
    assert 'eligibility_checks{status="VERIFIED_LISTABLE"} 4.0' in body
    assert "eligibility_expired_leases 0.0" in body
    assert "eligibility_oldest_queued_brand_age_seconds 0.0" in body
    assert "eligibility_amazon_request_seconds_bucket" in body


def test_amazon_failures_are_counted_by_outcome(engine, make_harness):
    from app.amazon.errors import AmazonRateLimited, AmazonRequestError, AmazonTransientError
    from app.amazon.mock import NO_RESTRICTIONS, MockAmazonClient, catalog_item

    items = [catalog_item(f"B0METRIC0{i}", "x", "Oddy", category_id="c") for i in (1, 2, 3)]
    client = MockAmazonClient(
        catalog={"Oddy": items},
        restrictions={
            "B0METRIC01": [AmazonRateLimited(retry_after=1), NO_RESTRICTIONS],
            "B0METRIC02": AmazonRequestError(400, "bad"),
            "B0METRIC03": [AmazonTransientError("boom"), NO_RESTRICTIONS],
        },
    )

    def count(outcome):
        return sample(
            "eligibility_amazon_requests_total",
            operation="getListingsRestrictions",
            outcome=outcome,
        )

    before = {o: count(o) for o in ("rate_limited", "request_error", "transient", "ok")}
    deferred = sample("eligibility_brand_tasks_total", result="deferred")
    h = make_harness(client)
    h.submit(["Oddy"])
    h.drain()
    assert count("rate_limited") - before["rate_limited"] == 1
    assert count("request_error") - before["request_error"] == 1
    assert count("transient") - before["transient"] == 1
    assert count("ok") - before["ok"] == 2  # the two that eventually succeeded
    assert sample("eligibility_brand_tasks_total", result="deferred") - deferred == 2


def test_route_labels_keep_cardinality_bounded():
    uid = "7f3a1b2c-1111-2222-3333-444455556666"
    assert observability.route_label(f"/api/v1/jobs/{uid}/checks/{uid}") == (
        "/api/v1/jobs/{id}/checks/{id}"
    )
    assert observability.route_label("/wp-login.php") == "other"
    assert observability.route_label("/anything/random/" + uid) == "other"


def test_metrics_can_require_a_bearer_token(engine):
    client, _ = make_app(engine, metrics_token="s3cret-metrics-token")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"})
    assert ok.status_code == 200 and "eligibility_http_requests_total" in ok.text
    assert "/metrics" not in client.get("/openapi.json").text  # not advertised


# ---- tracing ---------------------------------------------------------------------------------


def test_tracing_records_request_spans_when_enabled(engine):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.telemetry import setup_tracing

    exporter = InMemorySpanExporter()
    cfg = Settings(database_url="sqlite://", amazon_mode="mock")
    app = create_app(cfg)
    try:
        assert setup_tracing(app, exporter=exporter) is True
        client = TestClient(app)
        h = seller_headers()
        client.get("/healthz")
        client.get("/api/v1/jobs", headers=h)
        spans = exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert any("GET /api/v1/jobs" in n for n in names)
        assert not any("healthz" in n for n in names)  # probes are not traced
        import sqlalchemy

        if tuple(int(x) for x in sqlalchemy.__version__.split(".")[:2]) < (2, 1):
            assert any(s.attributes.get("db.system") for s in spans)  # DB work as child spans
        assert all("spk_" not in str(dict(s.attributes)) for s in spans)  # no key in span data
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        SQLAlchemyInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()


def test_tracing_is_off_unless_configured(engine):
    from app.telemetry import setup_tracing

    assert setup_tracing(create_app(Settings(database_url="sqlite://"))) is False
