"""Prepare a throwaway database for the dashboard's end-to-end tests.

Drops and recreates the target PostgreSQL database, migrates it, empties the Redis database, and
creates two seller accounts whose API keys are written to frontend/e2e/.state/keys.json.

  DATABASE_URL=postgresql+psycopg://... REDIS_URL=redis://... python scripts/e2e_prepare.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import redis  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

STATE = ROOT / "frontend" / "e2e" / ".state"


def recreate_database(url) -> None:
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"),
            {"d": url.database},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}"'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


def main() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if "e2e" not in (url.database or ""):
        sys.exit(f"Refusing to drop database {url.database!r}: its name must contain 'e2e'.")
    recreate_database(url)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend", check=True
    )
    redis.Redis.from_url(os.environ["REDIS_URL"]).flushdb()

    from app.db import session_scope
    from app.services.accounts import create_seller_account

    keys = {}
    with session_scope() as session:
        for label in ("a", "b"):
            _, key = create_seller_account(session, name=f"E2E Seller {label.upper()}")
            keys[label] = key
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "keys.json").write_text(json.dumps(keys))
    print("e2e database ready")


if __name__ == "__main__":
    main()
