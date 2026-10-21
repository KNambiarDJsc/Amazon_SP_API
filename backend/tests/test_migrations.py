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
