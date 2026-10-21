"""Verifies the Alembic migration matches the models. Needs PostgreSQL (TEST_DATABASE_URL)."""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from conftest import IS_POSTGRES, TEST_DATABASE_URL
from sqlalchemy import inspect

from alembic import command
from app import db as app_db
from app.models import Base

pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="migration check runs against PostgreSQL")

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    return cfg


@pytest.fixture
def clean_db():
    engine = app_db.make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    yield engine
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    engine.dispose()


def test_upgrade_matches_models_and_downgrade_removes_everything(clean_db):
    cfg = alembic_config()
    command.upgrade(cfg, "head")

    with clean_db.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == [], f"migration and models have drifted: {diff}"

    command.downgrade(cfg, "base")
    remaining = set(inspect(clean_db).get_table_names()) - {"alembic_version"}
    assert remaining == set()


def test_the_order_key_backfill_gives_existing_rows_the_same_order_new_rows_get(clean_db):
    """A data migration is only as good as its backfill: build rows under 0004, upgrade, compare."""
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import text

    from app.domain.conditions import check_seq

    cfg = alembic_config()
    command.upgrade(cfg, "0004")
    seller, job = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    expected: dict[str, int] = {}
    with clean_db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO seller_accounts (id, name, marketplace_id, api_key_hash, api_key_prefix, is_active, created_at) VALUES (:i, 'n', 'M', :h, 'p', true, :n)"
            ),
            dict(i=seller, h="h" * 64, n=now),
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, seller_account_id, status, data_source, settings, ingestion_report, created_at, updated_at) VALUES (:j, :s, 'COMPLETED', 'MOCK', '{}', '{}', :n, :n)"
            ),
            dict(j=job, s=seller, n=now),
        )
        for position, rank, condition in [
            (0, 1, "new_new"),
            (0, 2, "used_good"),
            (7, 3, "new_new"),
            (7, 3, "club_club"),
        ]:
            brand, cand, check = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO job_brands (id, job_id, seller_account_id, position, original_name, display_name, dedupe_key, status, attempts, checkpoint, created_at) VALUES (:b, :j, :s, :p, 'x', 'x', :d, 'COMPLETED', 0, '{}', :n)"
                ),
                dict(b=brand, j=job, s=seller, p=position, d=str(brand), n=now),
            )
            conn.execute(
                text(
                    "INSERT INTO catalog_candidates (id, job_brand_id, seller_account_id, asin, marketplace_id, brand_match, rank, selected, discovered_at) VALUES (:c, :b, :s, 'B000000000', 'M', 'EXACT', :r, true, :n)"
                ),
                dict(c=cand, b=brand, s=seller, r=rank, n=now),
            )
            conn.execute(
                text(
                    "INSERT INTO eligibility_checks (id, seller_account_id, job_id, job_brand_id, candidate_id, asin, marketplace_id, condition, verification_context, check_key, state, reason_codes, messages, approval_links, verification_methods, attempts, created_at) VALUES (:k, :s, :j, :b, :c, 'B000000000', 'M', :cond, 'v', :key, 'PENDING', '[]', '[]', '[]', '[]', 0, :n)"
                ),
                dict(
                    k=check, s=seller, j=job, b=brand, c=cand, cond=condition, key=str(check), n=now
                ),
            )
            expected[str(check)] = check_seq(position, rank, condition)

    command.upgrade(cfg, "head")
    with clean_db.connect() as conn:
        got = {
            str(r[0]): r[1] for r in conn.execute(text("SELECT id, seq FROM eligibility_checks"))
        }
    assert got == expected  # the SQL backfill and the Python rule agree for every row
    assert sorted(got.values()) == [
        check_seq(0, 1, "new_new"),
        check_seq(0, 2, "used_good"),
        check_seq(7, 3, "new_new"),
        check_seq(7, 3, "club_club"),
    ]
    command.downgrade(cfg, "0004")  # and it reverses cleanly
    with clean_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eligibility_checks")).scalar() == 4
