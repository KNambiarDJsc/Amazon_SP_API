"""Operator commands:  python -m app.cli <command>"""

import argparse
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.db import session_scope


def create_seller(args: argparse.Namespace) -> int:
    from app.services.accounts import create_seller_account

    with session_scope() as session:
        account, key = create_seller_account(
            session, name=args.name, marketplace_id=args.marketplace
        )
        account_id = account.id
    print(f"Seller account: {account_id}")
    print(f"API key (shown once, store it safely): {key}")
    return 0


def run_recovery(args: argparse.Namespace) -> int:
    from app.services.dispatch import recover
    from app.workers.runtime import get_runtime

    runtime = get_runtime()
    while True:
        stats = recover(runtime.dispatcher, get_settings())
        print(stats)
        if not args.loop:
            return 0
        time.sleep(args.loop)


def demo(args: argparse.Namespace) -> int:
    """Run the offline demo dataset end to end in this process (no Redis, no worker)."""
    from app.amazon.mock import demo_client
    from app.enums import DataSource
    from app.models import Job
    from app.schemas import JobSettings
    from app.services import orchestration
    from app.services.accounts import create_seller_account
    from app.services.dispatch import InlineDispatcher
    from app.services.ingestion import ingest, rows_from_list
    from app.services.processor import BrandProcessor
    from app.services.rate_limit import InMemoryTokenBucket
    from app.services.reporting import build_workbook

    cfg = get_settings()
    client = demo_client()
    dispatcher = InlineDispatcher()
    processor = BrandProcessor(
        client_factory=lambda _s: client,
        limiter=InMemoryTokenBucket(),
        dispatcher=dispatcher,
        settings=cfg,
    )
    with session_scope() as session:
        account, _ = create_seller_account(session, name="Demo seller")
        seller_id = account.id
    brands = args.brands or ["eSSL", "Havells", "Kangaro", "Taparia", "Oddy"]
    job_id = orchestration.submit_job(
        dispatcher,
        cfg,
        seller_id,
        ingestion=ingest(
            rows_from_list(brands),
            max_brands=cfg.max_brands_per_job,
            max_length=cfg.max_brand_length,
        ),
        name="Offline demo",
        settings=JobSettings(),
        data_source=DataSource.MOCK,
    )
    while (message := dispatcher.pop()) is not None:
        processor.process(message[0])
    with session_scope() as session:
        Path(args.out).write_bytes(build_workbook(session, session.get(Job, job_id)))
    print(f"Wrote {args.out} (DEMO DATA from offline fixtures, not live Amazon results)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-seller", help="create a seller account and print its API key once")
    p.add_argument("--name", required=True)
    p.add_argument("--marketplace", default="A21TJRUUN4KGV")
    p.set_defaults(func=create_seller)

    p = sub.add_parser("recover", help="repair interrupted work and refill dispatch windows")
    p.add_argument("--loop", type=float, metavar="SECONDS", help="repeat every N seconds")
    p.set_defaults(func=run_recovery)

    p = sub.add_parser("demo", help="run the offline demo dataset and write an Excel report")
    p.add_argument("--out", default="demo-report.xlsx")
    p.add_argument("brands", nargs="*")
    p.set_defaults(func=demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
