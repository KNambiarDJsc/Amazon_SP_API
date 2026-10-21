from typing import Any

from pydantic import BaseModel, Field


class CatalogItem(BaseModel):
    asin: str
    title: str | None = None
    brand: str | None = None
    marketplace_id: str
    category_name: str | None = None
    category_id: str | None = None
    product_type: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CatalogPage(BaseModel):
    items: list[CatalogItem] = Field(default_factory=list)
    next_token: str | None = None
    total_results: int | None = None
