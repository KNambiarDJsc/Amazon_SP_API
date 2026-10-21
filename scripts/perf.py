"""Performance measurements. Needs PostgreSQL (DATABASE_URL) and, for `pipeline`, Redis.

  python scripts/perf.py reads      # API read path and export against 50,000 seeded checks
  python scripts/perf.py pipeline   # end-to-end throughput through real workers

Numbers depend on the machine; what matters is their shape and where the time goes.
"""

import argparse
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime

sys.path.insert(0, "backend")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, insert, text  # noqa: E402

from app import db as app_db  # noqa: E402
from app.api.deps import dispatcher_dep, settings_dep  # noqa: E402
from app.api.main import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.enums import (  # noqa: E402
    BrandMatch,
    BrandStatus,
    CheckState,
    DataSource,
    EligibilityStatus,
    JobStatus,
)
from app.models import (  # noqa: E402
    Base,
    CatalogCandidate,
    CheckObservation,
    EligibilityCheck,
    Job,
    JobBrand,
    SellerAccount,
)
from app.services.accounts import hash_api_key  # noqa: E402
from app.services.dispatch import InlineDispatcher  # noqa: E402

STATUSES = [
    EligibilityStatus.VERIFIED_LISTABLE,
    EligibilityStatus.BRAND_APPROVAL_REQUIRED,
    EligibilityStatus.OTHER_APPROVAL_REQUIRED,
    EligibilityStatus.NEEDS_MANUAL_CHECK,
]


def seed(engine, brands: int, per_brand: int):
    now = datetime.now(UTC)
    seller_id, job_id = uuid.uuid4(), uuid.uuid4()
    key = "spk_perf_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            insert(SellerAccount),
            [
                dict(
                    id=seller_id,
                    name="perf",
                    marketplace_id="A21TJRUUN4KGV",
                    api_key_hash=hash_api_key(key),
                    api_key_prefix=key[:8],
                    is_active=True,
                    created_at=now,
                )
            ],
        )
        conn.execute(
            insert(Job),
            [
                dict(
                    id=job_id,
                    seller_account_id=seller_id,
                    name="perf",
                    status=JobStatus.COMPLETED,
                    data_source=DataSource.MOCK,
                    settings={},
                    ingestion_report={},
                    created_at=now,
                    updated_at=now,
                )
            ],
        )
        brand_rows, cand_rows, check_rows, obs_rows = [], [], [], []
        for b in range(brands):
            bid = uuid.uuid4()
            brand_rows.append(
                dict(
                    id=bid,
                    job_id=job_id,
                    seller_account_id=seller_id,
                    position=b,
                    original_name=f"Brand {b}",
                    display_name=f"Brand {b}",
                    dedupe_key=f"brand {b}",
                    status=BrandStatus.COMPLETED,
                    attempts=0,
                    checkpoint={},
                    brand_status=EligibilityStatus.MIXED_RESULTS,
                    created_at=now,
                )
            )
            for a in range(per_brand):
                cid, kid = uuid.uuid4(), uuid.uuid4()
                asin = f"B0{b:05d}{a:03d}"[:10]
                status = STATUSES[(b + a) % len(STATUSES)]
                cand_rows.append(
                    dict(
                        id=cid,
                        job_brand_id=bid,
                        seller_account_id=seller_id,
                        asin=asin,
                        marketplace_id="A21TJRUUN4KGV",
                        title=f"Brand {b} product {a} with a long title",
                        brand_attribute=f"Brand {b}",
                        category_name="Tools",
                        category_id="c",
                        brand_match=BrandMatch.EXACT,
                        rank=a + 1,
                        selected=True,
                        discovered_at=now,
                    )
                )
                check_rows.append(
                    dict(
                        id=kid,
                        seller_account_id=seller_id,
                        job_id=job_id,
                        job_brand_id=bid,
                        candidate_id=cid,
                        asin=asin,
                        marketplace_id="A21TJRUUN4KGV",
                        condition="new_new",
                        seq=b * 100_000 + (a + 1) * 100,
                        verification_context="sp_api",
                        check_key=uuid.uuid4().hex + uuid.uuid4().hex,
                        state=CheckState.DONE,
                        status=status,
                        rule_id="R",
                        rationale="seeded",
                        rules_version="x",
                        reason_codes=[],
                        messages=[],
                        approval_links=[],
                        verification_methods=["SP_API_RESTRICTIONS"],
                        attempts=0,
                        checked_at=now,
                        created_at=now,
                    )
                )
                obs_rows.append(
                    dict(
                        check_id=kid,
                        seller_account_id=seller_id,
                        source="SP_API_RESTRICTIONS",
                        outcome="OK",
                        http_status=200,
                        context={},
                        signals={"restrictions_present": True},
                        raw={"restrictions": []},
                        evidence_ref="db:x",
                        observed_at=now,
                    )
                )
        for table, rows in (
            (JobBrand, brand_rows),
            (CatalogCandidate, cand_rows),
            (EligibilityCheck, check_rows),
            (CheckObservation, obs_rows),
        ):
            for i in range(0, len(rows), 5000):
                conn.execute(insert(table), rows[i : i + 5000])
        conn.execute(text("ANALYZE"))
    return seller_id, job_id, {"Authorization": f"Bearer {key}"}


def timed(fn, n=25):
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return statistics.median(samples), samples[int(len(samples) * 0.95) - 1]


def count_statements(engine, fn):
    n = 0

    def bump(*_a, **_k):
        nonlocal n
        n += 1

    event.listen(engine, "before_cursor_execute", bump)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", bump)
    return n


def reads(args):
    engine = app_db.make_engine(args.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    app_db.configure(engine)
    t = time.perf_counter()
    _, job_id, auth = seed(engine, args.brands, args.per_brand)
    total = args.brands * args.per_brand
    print(
        f"seeded {total} checks, {total} observations, {args.brands} brands "
        f"in {time.perf_counter() - t:.1f}s\n"
    )

    cfg = Settings(database_url=args.database_url, amazon_mode="mock", api_rate_limit_enabled=False)
    app = create_app(cfg)
    app.dependency_overrides[settings_dep] = lambda: cfg
    app.dependency_overrides[dispatcher_dep] = lambda: InlineDispatcher()
    client = TestClient(app)
    base = f"/api/v1/jobs/{job_id}"
    print(f"{'request':58} {'p50 ms':>8} {'p95 ms':>8} {'SQL stmts':>10}")
    cases = [
        ("GET job (progress aggregates over all checks)", lambda: client.get(base, headers=auth)),
        ("GET checks limit=50", lambda: client.get(base + "/checks?limit=50", headers=auth)),
        (
            "GET checks limit=50 offset=40000",
            lambda: client.get(base + "/checks?limit=50&offset=40000", headers=auth),
        ),
        (
            "GET checks status=VERIFIED_LISTABLE limit=50",
            lambda: client.get(base + "/checks?status=VERIFIED_LISTABLE&limit=50", headers=auth),
        ),
        (
            "GET checks q=B0000 (substring search)",
            lambda: client.get(base + "/checks?q=B0000&limit=50", headers=auth),
        ),
        (
            "GET checks q=product 7 (title search)",
            lambda: client.get(base + "/checks?q=product%207&limit=50", headers=auth),
        ),
        ("GET brands limit=50", lambda: client.get(base + "/brands?limit=50", headers=auth)),
        ("GET review-queue", lambda: client.get(base + "/review-queue?limit=50", headers=auth)),
        ("GET /metrics (DB gauges, uncached)", lambda: client.get("/metrics")),
    ]
    from app import observability

    for name, call in cases:
        observability._db_collector._cached = None
        stmts = count_statements(engine, call)
        p50, p95 = timed(
            lambda c=call: (setattr(observability._db_collector, "_cached", None), c())
        )
        print(f"{name:58} {p50:8.1f} {p95:8.1f} {stmts:10d}")

    t = time.perf_counter()
    r = client.get(base + "/export.xlsx", headers=auth)
    secs = time.perf_counter() - t
    stmts = count_statements(engine, lambda: client.get(base + "/export.xlsx", headers=auth))
    print(
        f"\nexport.xlsx: {len(r.content) / 1e6:.1f} MB in {secs:.1f}s, {stmts} SQL statements "
        f"for {total} checks (+{total} audit rows)"
    )

    print("\nEXPLAIN of the hottest query shapes:")
    with engine.connect() as conn:
        for label, sql in {
            "checks page": f"EXPLAIN SELECT c.id FROM eligibility_checks c JOIN job_brands b ON b.id=c.job_brand_id JOIN catalog_candidates k ON k.id=c.candidate_id WHERE c.job_id='{job_id}' ORDER BY b.position, k.rank, c.condition LIMIT 50",
            "status filter": f"EXPLAIN SELECT c.id FROM eligibility_checks c WHERE c.job_id='{job_id}' AND c.status='VERIFIED_LISTABLE'",
            "substring search": f"EXPLAIN SELECT c.id FROM eligibility_checks c WHERE c.job_id='{job_id}' AND c.asin ILIKE '%B0000%'",
        }.items():
            plan = [r[0] for r in conn.execute(text(sql))]
            print(f"  [{label}] " + " | ".join(p.strip() for p in plan[:4]))


def pipeline(args):
    import redis
    from dramatiq import Worker
    from dramatiq.brokers.redis import RedisBroker

    from app.amazon.mock import NO_RESTRICTIONS, MockAmazonClient, catalog_item
    from app.schemas import JobSettings
    from app.services.ingestion import ingest, rows_from_list
    from app.services.orchestration import submit_job
    from app.services.rate_limit import RedisTokenBucket
    from app.workers.runtime import build_runtime

    engine = app_db.make_engine(args.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    app_db.configure(engine)
    names = [f"Brand{i}" for i in range(args.brands)]
    catalog = {
        n: [
            catalog_item(f"B0{i:05d}{a:03d}"[:10], f"{n} item {a}", n, category_id=f"c{a % 2}")
            for a in range(args.per_brand)
        ]
        for i, n in enumerate(names)
    }
    restrictions = {it["asin"]: NO_RESTRICTIONS for items in catalog.values() for it in items}

    class SlowAmazon(MockAmazonClient):
        def search_catalog_items(self, **kw):
            time.sleep(args.latency)
            return super().search_catalog_items(**kw)

        def get_listings_restrictions(self, **kw):
            time.sleep(args.latency)
            return super().get_listings_restrictions(**kw)

    amazon = SlowAmazon(catalog=catalog, restrictions=restrictions)
    cfg = Settings(
        database_url=args.database_url,
        seller_max_concurrent_brands=args.window,
        rate_search_catalog_per_sec=args.search_rate,
        rate_search_catalog_burst=max(2, int(args.search_rate)),
        rate_restrictions_per_sec=args.check_rate,
        rate_restrictions_burst=max(2, int(args.check_rate)),
    )
    r = redis.Redis.from_url(args.redis_url)
    prefix = f"perf-{uuid.uuid4().hex[:6]}"
    broker = RedisBroker(url=args.redis_url, namespace=prefix)
    runtime = build_runtime(
        broker,
        cfg,
        client_factory=lambda _s: amazon,
        limiter=RedisTokenBucket(r, prefix=f"{prefix}:rl:"),
    )
    with app_db.session_scope() as s:
        seller = SellerAccount(name="perf", api_key_hash=uuid.uuid4().hex * 2, api_key_prefix="p")
        s.add(seller)
        s.flush()
        seller_id = seller.id
    worker = Worker(broker, worker_threads=args.threads, worker_timeout=100)
    worker.start()
    start = time.perf_counter()
    job_id = submit_job(
        runtime.dispatcher,
        cfg,
        seller_id,
        ingestion=ingest(rows_from_list(names), max_brands=10**6, max_length=200),
        name="perf",
        settings=JobSettings(),
        data_source=DataSource.MOCK,
    )
    stmts = [0]
    event.listen(
        engine, "before_cursor_execute", lambda *a, **k: stmts.__setitem__(0, stmts[0] + 1)
    )
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        with app_db.session_scope() as s:
            if s.get(Job, job_id).status == JobStatus.COMPLETED:
                break
        time.sleep(0.2)
    elapsed = time.perf_counter() - start
    worker.stop()
    broker.flush_all()
    for k in r.scan_iter(f"{prefix}*"):
        r.delete(k)
    with app_db.session_scope() as s:
        done = s.get(Job, job_id).status == JobStatus.COMPLETED
    calls = len(amazon.calls)
    searches = len(amazon.calls_to("searchCatalogItems"))
    checks = len(amazon.calls_to("getListingsRestrictions"))
    ideal = max(searches / args.search_rate, checks / args.check_rate)
    print(
        f"brands={args.brands} asins/brand={args.per_brand} amazon latency={args.latency * 1000:.0f}ms "
        f"window={args.window} threads={args.threads}"
    )
    print(
        f"  complete={done} elapsed={elapsed:.1f}s  amazon calls={calls} ({searches} search, {checks} restrictions)"
    )
    print(f"  throughput: {args.brands / elapsed:.1f} brands/s, {checks / elapsed:.1f} checks/s")
    print(
        f"  rate-limit floor (search {args.search_rate}/s, checks {args.check_rate}/s): {ideal:.1f}s "
        f"-> efficiency {100 * ideal / elapsed:.0f}%"
    )
    print(
        f"  SQL statements: {stmts[0]} ({stmts[0] / args.brands:.0f} per brand, "
        f"{stmts[0] / max(checks, 1):.1f} per check)"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["reads", "pipeline"])
    p.add_argument(
        "--database-url", default="postgresql+psycopg://postgres@localhost:5433/eligibility_perf"
    )
    p.add_argument("--redis-url", default="redis://localhost:6380/4")
    p.add_argument("--brands", type=int, default=1000)
    p.add_argument("--per-brand", type=int, default=50)
    p.add_argument("--latency", type=float, default=0.0)
    p.add_argument("--window", type=int, default=8)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--search-rate", type=float, default=10_000)
    p.add_argument("--check-rate", type=float, default=10_000)
    p.add_argument("--timeout", type=float, default=300)
    a = p.parse_args()
    {"reads": reads, "pipeline": pipeline}[a.scenario](a)
