"""Offline Amazon client that returns raw SP-API-shaped JSON bodies.

Used by tests and by AMAZON_MODE=mock demos. Results are fixtures, never real Amazon
data; jobs created against it are flagged DataSource.MOCK and exports say so.

Behaviours for a restrictions lookup can be: a response body (returned), an exception
(raised), a list of those (consumed one per call, the last one repeating), or a callable
taking the call number. An ASIN with no configured behaviour answers ASIN_NOT_FOUND,
never "no restrictions", so a missing fixture can not masquerade as permission to sell.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from app.amazon.errors import AmazonError
from app.enums import MARKETPLACE_ID_IN, DataSource

Behaviour = Any  # Mapping | AmazonError | Sequence[Behaviour] | Callable[[int], Behaviour]

APPROVAL_LINK = {
    "resource": "https://sellercentral.amazon.in/hz/approvalrequest/restrictions/approve?asin={asin}",
    "verb": "GET",
    "title": "Apply to sell",
    "type": "text/html",
}


# ---- body builders -------------------------------------------------------------------------


def catalog_item(
    asin: str,
    title: str,
    brand: str | None,
    *,
    category: str = "Electronics",
    category_id: str = "976419031",
    product_type: str = "PRODUCT",
    marketplace_id: str = MARKETPLACE_ID_IN,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "marketplaceId": marketplace_id,
        "itemName": title,
        "browseClassification": {"displayName": category, "classificationId": category_id},
    }
    if brand is not None:
        summary["brand"] = brand
    return {
        "asin": asin,
        "summaries": [summary],
        "productTypes": [{"marketplaceId": marketplace_id, "productType": product_type}],
    }


def reason(code: str, message: str, *, asin: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"reasonCode": code, "message": message}
    if code == "APPROVAL_REQUIRED" and asin:
        out["links"] = [{**APPROVAL_LINK, "resource": APPROVAL_LINK["resource"].format(asin=asin)}]
    return out


def restrictions_body(
    *reasons: dict[str, Any],
    condition: str = "new_new",
    marketplace_id: str = MARKETPLACE_ID_IN,
) -> dict[str, Any]:
    if not reasons:
        return {"restrictions": []}
    return {
        "restrictions": [
            {
                "marketplaceId": marketplace_id,
                "conditionType": condition,
                "reasons": list(reasons),
            }
        ]
    }


NO_RESTRICTIONS: dict[str, Any] = {"restrictions": []}


def brand_gated(asin: str, condition: str = "new_new") -> dict[str, Any]:
    return restrictions_body(
        reason("APPROVAL_REQUIRED", "You need approval to list in this brand.", asin=asin),
        condition=condition,
    )


def category_gated(asin: str, condition: str = "new_new") -> dict[str, Any]:
    return restrictions_body(
        reason("APPROVAL_REQUIRED", "You need approval to list in this category.", asin=asin),
        condition=condition,
    )


# ---- the client ----------------------------------------------------------------------------


class MockAmazonClient:
    data_source = DataSource.MOCK

    def __init__(
        self,
        catalog: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        restrictions: Mapping[Any, Behaviour] | None = None,
    ):
        # keywords (casefolded) -> raw catalog items returned for that search, in order
        self.catalog = {k.casefold(): list(v) for k, v in (catalog or {}).items()}
        # asin or (asin, condition) -> behaviour
        self.restrictions = dict(restrictions or {})
        self.catalog_behaviour: Behaviour = None  # set to an error/list to script failures
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._counts: defaultdict[Any, int] = defaultdict(int)

    # -- helpers
    def calls_to(self, operation: str) -> list[dict[str, Any]]:
        return [args for op, args in self.calls if op == operation]

    def _resolve(self, behaviour: Behaviour, key: Any) -> Any:
        n = self._counts[key]
        self._counts[key] += 1
        if callable(behaviour) and not isinstance(behaviour, (Mapping, AmazonError)):
            behaviour = behaviour(n)
        if isinstance(behaviour, list):
            behaviour = behaviour[min(n, len(behaviour) - 1)]
        if isinstance(behaviour, AmazonError):
            raise behaviour
        return behaviour

    # -- SP-API operations
    def search_catalog_items(
        self,
        *,
        keywords: str,
        marketplace_id: str,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> Mapping[str, Any]:
        self.calls.append(("searchCatalogItems", {"keywords": keywords, "page_token": page_token}))
        if self.catalog_behaviour is not None:
            scripted = self._resolve(self.catalog_behaviour, "catalog")
            if scripted is not None:
                return scripted
        items = self.catalog.get(keywords.casefold(), [])
        start = int(page_token.split(":")[1]) if page_token else 0
        page = items[start : start + page_size]
        body: dict[str, Any] = {
            "numberOfResults": len(items),
            "pagination": {},
            "items": page,
        }
        if start + page_size < len(items):
            body["pagination"]["nextToken"] = f"page:{start + page_size}"
        return body

    def get_listings_restrictions(
        self,
        *,
        asin: str,
        seller_id: str,
        marketplace_id: str,
        condition: str,
    ) -> Mapping[str, Any]:
        self.calls.append(
            (
                "getListingsRestrictions",
                {"asin": asin, "seller_id": seller_id, "condition": condition},
            )
        )
        key = (asin, condition) if (asin, condition) in self.restrictions else asin
        if key not in self.restrictions:
            return restrictions_body(
                reason("ASIN_NOT_FOUND", f"ASIN {asin} was not found in marketplace."),
                condition=condition,
                marketplace_id=marketplace_id,
            )
        return self._resolve(self.restrictions[key], (asin, condition))


def demo_client() -> MockAmazonClient:
    """Small illustrative dataset, loosely modelled on the supplied Seller Central screenshots.

    ASINs and outcomes here are fixtures chosen to exercise the pipeline. They are not
    real eligibility results for any seller.
    """
    essl = [
        catalog_item("B08PDL14VR", "ESSL IDENTIX K30 PRO BIOMETRIC Machine", "eSSL"),
        catalog_item(
            "B0BXM5CGRM", "eSSL K21 Pro RFID + Fingerprint Time & Attendance System", "eSSL"
        ),
        catalog_item("B084FZXB48", "eSSL K90 Pro Biometric Time Attendance Machine", "eSSL"),
        catalog_item(
            "B0B63WV6CW", "eSSL MB20 Face Recognition Biometric Attendance Machine", "eSSL"
        ),
        # Title mentions the brand but the product belongs to someone else: must be rejected.
        catalog_item("B0MOCKWR01", "Wall mount bracket compatible with eSSL K30", "Generic Mounts"),
    ]
    havells = [
        catalog_item(
            "B08Y9C5H38",
            "Havells 1000 Watts Air Care Volumizer Hair Dryer | Pre-Styling Half Brush",
            "Havells",
            category="Beauty",
            category_id="1355016031",
        ),
        catalog_item("B0MOCKHV01", "Havells Hair Straightener", "Havells", category="Beauty"),
    ]
    kangaro = [
        catalog_item("B0MOCKKG01", "Kangaro Stapler HP-45", "Kangaro", category="Office Products"),
        catalog_item(
            "B0MOCKKG02", "Kangaro Paper Punch DP-600", "Kangaro", category="Office Products"
        ),
        catalog_item("B0MOCKKG03", "Kangaro Staple Pins 24/6", "KANGARO", category="Industrial"),
    ]
    taparia = [
        catalog_item(
            "B0MOCKTP01", "Taparia Screwdriver Set", "Taparia", category="Tools & Home Improvement"
        ),
        catalog_item(
            "B0MOCKTP02",
            "Taparia Combination Pliers",
            "Taparia",
            category="Tools & Home Improvement",
        ),
        catalog_item(
            "B0MOCKTP03", "Taparia Spanner Set", "taparia", category="Tools & Home Improvement"
        ),
    ]
    restrictions: dict[Any, Behaviour] = {
        "B08PDL14VR": NO_RESTRICTIONS,
        "B0BXM5CGRM": NO_RESTRICTIONS,
        "B084FZXB48": NO_RESTRICTIONS,
        "B0B63WV6CW": NO_RESTRICTIONS,
        "B08Y9C5H38": brand_gated("B08Y9C5H38"),
        "B0MOCKHV01": brand_gated("B0MOCKHV01"),
        "B0MOCKKG01": NO_RESTRICTIONS,
        "B0MOCKKG02": NO_RESTRICTIONS,
        "B0MOCKKG03": brand_gated("B0MOCKKG03"),
        "B0MOCKTP01": category_gated("B0MOCKTP01"),
        "B0MOCKTP02": category_gated("B0MOCKTP02"),
        "B0MOCKTP03": category_gated("B0MOCKTP03"),
    }
    return MockAmazonClient(
        catalog={"eSSL": essl, "Havells": havells, "Kangaro": kangaro, "Taparia": taparia},
        restrictions=restrictions,
    )


__all__ = [
    "MockAmazonClient",
    "demo_client",
    "catalog_item",
    "reason",
    "restrictions_body",
    "brand_gated",
    "category_gated",
    "NO_RESTRICTIONS",
]
