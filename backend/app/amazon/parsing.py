"""Turn raw SP-API bodies into typed domain objects. Shared by the live client and the mock."""

from collections.abc import Mapping
from typing import Any

from app.amazon.errors import AmazonMalformedResponse
from app.amazon.types import CatalogItem, CatalogPage
from app.domain.observations import (
    ApprovalLink,
    Observation,
    ObservationContext,
    RestrictionEntry,
    RestrictionReason,
    SpApiSignals,
)
from app.enums import ObservationOutcome, ObservationSource


def _pick_by_marketplace(entries: Any, marketplace_id: str) -> Mapping[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("marketplaceId") == marketplace_id:
            return entry
    return None


def parse_catalog_page(body: Mapping[str, Any], marketplace_id: str) -> CatalogPage:
    """Parse a Catalog Items 2022-04-01 searchCatalogItems response."""
    items_raw = body.get("items")
    if not isinstance(items_raw, list):
        raise AmazonMalformedResponse("searchCatalogItems response has no 'items' list")

    items: list[CatalogItem] = []
    for raw in items_raw:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("asin"), str):
            continue  # an item without an ASIN cannot be checked; it is dropped, not guessed at
        summary = _pick_by_marketplace(raw.get("summaries"), marketplace_id) or {}
        browse = summary.get("browseClassification")
        browse = browse if isinstance(browse, Mapping) else {}
        product_type_entry = _pick_by_marketplace(raw.get("productTypes"), marketplace_id) or {}
        items.append(
            CatalogItem(
                asin=raw["asin"].strip().upper(),
                title=summary.get("itemName"),
                brand=summary.get("brand"),
                marketplace_id=marketplace_id,
                category_name=browse.get("displayName"),
                category_id=browse.get("classificationId"),
                product_type=product_type_entry.get("productType"),
                raw=dict(raw),
            )
        )

    pagination = body.get("pagination")
    next_token = pagination.get("nextToken") if isinstance(pagination, Mapping) else None
    total = body.get("numberOfResults")
    return CatalogPage(
        items=items,
        next_token=next_token if isinstance(next_token, str) and next_token else None,
        total_results=total if isinstance(total, int) else None,
    )


def _parse_links(raw_links: Any) -> list[ApprovalLink]:
    if not isinstance(raw_links, list):
        return []
    return [
        ApprovalLink(
            resource=link.get("resource"),
            verb=link.get("verb"),
            title=link.get("title"),
            type=link.get("type"),
        )
        for link in raw_links
        if isinstance(link, Mapping)
    ]


def parse_restrictions_observation(
    body: Mapping[str, Any] | None,
    *,
    context: ObservationContext,
    http_status: int = 200,
    evidence_ref: str | None = None,
) -> Observation:
    """Parse a getListingsRestrictions (2021-08-01) body into an Observation.

    A body whose `restrictions` is not a list yields restrictions_present=False so the
    rules engine treats it as missing data instead of "no restrictions".
    """
    signals = SpApiSignals(restrictions_present=False)
    raw = body if isinstance(body, Mapping) else None
    restrictions = raw.get("restrictions") if raw is not None else None

    if isinstance(restrictions, list):
        entries: list[RestrictionEntry] = []
        for entry in restrictions:
            if not isinstance(entry, Mapping):
                return Observation(
                    source=ObservationSource.SP_API_RESTRICTIONS,
                    outcome=ObservationOutcome.MALFORMED_RESPONSE,
                    context=context,
                    http_status=http_status,
                    error_detail="restriction entry is not an object",
                    raw=dict(raw) if raw is not None else None,
                    evidence_ref=evidence_ref,
                )
            reasons_raw = entry.get("reasons")
            reasons = [
                RestrictionReason(
                    code=r.get("reasonCode"),
                    message=r.get("message"),
                    links=_parse_links(r.get("links")),
                )
                for r in (reasons_raw if isinstance(reasons_raw, list) else [])
                if isinstance(r, Mapping)
            ]
            entries.append(
                RestrictionEntry(
                    marketplace_id=entry.get("marketplaceId"),
                    condition_type=entry.get("conditionType"),
                    reasons=reasons,
                )
            )
        signals = SpApiSignals(restrictions_present=True, entries=entries)

    return Observation(
        source=ObservationSource.SP_API_RESTRICTIONS,
        outcome=ObservationOutcome.OK,
        context=context,
        sp_api=signals,
        http_status=http_status,
        raw=dict(raw) if raw is not None else None,
        evidence_ref=evidence_ref,
    )


def failure_observation(
    *,
    outcome: ObservationOutcome,
    context: ObservationContext,
    http_status: int | None = None,
    detail: str | None = None,
    source: ObservationSource = ObservationSource.SP_API_RESTRICTIONS,
    evidence_ref: str | None = None,
) -> Observation:
    return Observation(
        source=source,
        outcome=outcome,
        context=context,
        http_status=http_status,
        error_detail=detail,
        evidence_ref=evidence_ref,
    )
