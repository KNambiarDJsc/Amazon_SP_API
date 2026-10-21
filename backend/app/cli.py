"""Operator commands:  python -m app.cli <command>

demo            run the offline demo end to end and write an Excel report (no setup needed)
init-db         create tables (SQLite dev databases; use `alembic upgrade head` for Postgres)
create-seller   create a seller account and print its API key (shown once)
drain           run every open job to completion in this process, without Redis
"""

import argparse
import sys
from pathlib import Path

from app.amazon.factory import LiveModeUnavailable, data_source_for, ensure_available
from app.amazon.mock import demo_client
from app.auth import create_seller_account
from app.config import get_settings
from app.db import configure, make_engine, session_scope
from app.domain.observations import utcnow
from app.enums import MARKETPLACE_ID_IN
from app.models import Base, Job, JobBrand
from app.schemas import JobSettings
from app.services.ingestion import rows_from_list
from app.services.jobs import compute_progress, create_job
from app.services.orchestrator import Runtime
from app.services.ratelimit import InMemoryRateLimiter
from app.services.reports import build_workbook
from app.services.tick import run_until_idle
from app.workers.runtime import build_runtime

DEMO_BRANDS = ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]


def cmd_init_db(_args: argparse.Namespace) -> int:
    configure(make_engine())
    from app.db import get_engine

    Base.metadata.create_all(get_engine())
    print("tables created")
    return 0


def cmd_create_seller(args: argparse.Namespace) -> int:
    with session_scope() as session:
        account, key = create_seller_account(
            session,
            name=args.name,
            amazon_seller_id=args.amazon_seller_id,
            marketplace_id=args.marketplace_id,
        )
        print(f"seller id : {account.id}")
        print(f"api key   : {key}")
        print("Store this key now. It is not stored and cannot be shown again.")
    return 0


def cmd_drain(_args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        ensure_available(settings)
    except LiveModeUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    runs = run_until_idle(build_runtime(settings, limiter=InMemoryRateLimiter()))
    print(f"ran {runs} brand(s)")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Everything runs against fixtures in a throwaway in-memory database."""
    engine = make_engine(args.database_url or "sqlite://")
    configure(engine)
    Base.metadata.create_all(engine)
    settings = get_settings()

    with session_scope() as session:
        seller, _key = create_seller_account(session, name="Demo Seller")
        job = create_job(
            session,
            seller=seller,
            rows=rows_from_list(args.brands or DEMO_BRANDS),
            settings=JobSettings(),
            data_source=data_source_for(settings.model_copy(update={"amazon_mode": "mock"})),
            name="Offline demo",
            now=utcnow(),
        )
        job_id = job.id

    runtime = Runtime(client=demo_client(), limiter=InMemoryRateLimiter(), settings=settings)
    runs = run_until_idle(runtime)

    with session_scope() as session:
        progress = compute_progress(session, job_id)
        brands = (
            session.query(JobBrand).filter(JobBrand.job_id == job_id).order_by(JobBrand.position)
        )
        print(f"DEMO DATA (offline fixtures, not Amazon). {runs} brand run(s).\n")
        print(f"{'Brand':<12} {'Result':<26} Checked")
        for brand in brands:
            summary = brand.brand_summary or {}
            result = brand.brand_status.value if brand.brand_status else "-"
            print(
                f"{brand.display_name:<12} {result:<26} {summary.get('asins_checked', 0)} ASIN(s)"
            )
        print(
            f"\nlistable ASINs: {progress.verified_listable}   restricted: {progress.restricted}"
            f"   manual review: {progress.needs_manual_check}"
        )
        data = build_workbook(session, session.get(Job, job_id), generated=utcnow())

    out = Path(args.out)
    out.write_bytes(data)
    print(f"\nreport written to {out.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the offline demo and write an Excel report")
    demo.add_argument("--out", default="demo-report.xlsx")
    demo.add_argument("--database-url", help="defaults to a throwaway in-memory SQLite database")
    demo.add_argument("brands", nargs="*", help=f"defaults to {', '.join(DEMO_BRANDS)}")
    demo.set_defaults(func=cmd_demo)

    init = sub.add_parser("init-db", help="create tables (dev databases)")
    init.set_defaults(func=cmd_init_db)

    seller = sub.add_parser("create-seller", help="create a seller account and API key")
    seller.add_argument("name")
    seller.add_argument("--amazon-seller-id")
    seller.add_argument("--marketplace-id", default=MARKETPLACE_ID_IN)
    seller.set_defaults(func=cmd_create_seller)

    drain = sub.add_parser("drain", help="run open jobs to completion in this process")
    drain.set_defaults(func=cmd_drain)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
