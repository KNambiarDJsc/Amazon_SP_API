import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models import Base, SellerAccount

# Rate limiting is covered by its own tests; shared test processes must not trip it.
os.environ.setdefault("API_RATE_LIMIT_ENABLED", "false")

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite://")
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")


def load_fixture(relative: str):
    return json.loads((FIXTURES / relative).read_text())


@pytest.fixture
def engine():
    eng = app_db.make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    app_db.configure(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def seller(session) -> SellerAccount:
    account = SellerAccount(
        id=uuid.uuid4(), name="Test Seller", api_key_hash=uuid.uuid4().hex * 2, api_key_prefix="tst"
    )
    session.add(account)
    session.commit()
    return account


@pytest.fixture
def now():
    return datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def make_harness(seller):
    from harness import Harness

    def factory(client, **overrides):
        return Harness(seller, client, **overrides)

    return factory
