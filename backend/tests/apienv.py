"""Shared HTTP test environment: the real app with an in-memory dispatcher and a worker."""

from fastapi.testclient import TestClient

from app.amazon.mock import demo_client
from app.api.deps import dispatcher_dep, settings_dep
from app.api.main import create_app
from app.config import Settings
from app.db import session_scope
from app.services.accounts import create_seller_account
from app.services.dispatch import InlineDispatcher
from app.services.processor import BrandProcessor
from app.services.rate_limit import InMemoryTokenBucket


class Env:
    def __init__(self, client=None):
        self.cfg = Settings(
            database_url="sqlite://",
            amazon_mode="mock",
            seller_max_concurrent_brands=2,
            rate_restrictions_per_sec=1000,
            rate_restrictions_burst=1000,
            rate_search_catalog_per_sec=1000,
            rate_search_catalog_burst=1000,
            max_upload_bytes=64 * 1024,
        )
        self.amazon = client or demo_client()
        self.dispatcher = InlineDispatcher()
        app = create_app()
        app.dependency_overrides[dispatcher_dep] = lambda: self.dispatcher
        app.dependency_overrides[settings_dep] = lambda: self.cfg
        self.http = TestClient(app)
        self.processor = BrandProcessor(
            client_factory=lambda _s: self.amazon,
            limiter=InMemoryTokenBucket(),
            dispatcher=self.dispatcher,
            settings=self.cfg,
        )

    def new_seller(self, name="Seller A"):
        with session_scope() as s:
            account, key = create_seller_account(s, name=name)
            return account.id, {"Authorization": f"Bearer {key}"}

    def drain(self):
        while (m := self.dispatcher.pop()) is not None:
            self.processor.process(m[0])
