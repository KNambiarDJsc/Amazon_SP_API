from collections.abc import Mapping
from typing import Any, Protocol

from app.enums import DataSource


class AmazonClient(Protocol):
    """The two SP-API operations the platform needs, returning raw response bodies.

    Implementations raise the typed errors in app.amazon.errors for non-2xx outcomes.
    Parsing lives in app.amazon.parsing so mocks and the live client share it.
    """

    data_source: DataSource

    def search_catalog_items(
        self,
        *,
        keywords: str,
        marketplace_id: str,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> Mapping[str, Any]: ...

    def get_listings_restrictions(
        self,
        *,
        asin: str,
        seller_id: str,
        marketplace_id: str,
        condition: str,
    ) -> Mapping[str, Any]: ...
