"""A stand-in for Amazon's servers: LWA and SP-API answering real HTTP requests through
httpx.MockTransport.

Catalog and restrictions answers come from a MockAmazonClient's fixtures (raised AmazonErrors
become the matching HTTP status and SP-API error body), so the live client, its token
handling and the orchestrator are exercised with no credentials and no network. Response
shapes follow the published API models (Catalog Items 2022-04-01, Listings Restrictions
2021-08-01, Sellers v1, LWA token endpoint).
"""

import itertools
import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
from support import make_settings

from app.amazon import errors as err
from app.amazon.live import CATALOG_PATH, PARTICIPATIONS_PATH, RESTRICTIONS_PATH
from app.amazon.mock import MockAmazonClient, demo_client
from app.crypto import generate_key
from app.enums import MARKETPLACE_ID_IN

CLIENT_ID = "amzn1.application-oa2-client.test"
CLIENT_SECRET = "test-client-secret"
REFRESH_TOKEN = "Atzr|test-refresh-token"
SELLING_PARTNER_ID = "A2TESTSELLER01"
REDIRECT_URI = "https://eligibility.example/api/v1/amazon/callback"
ENCRYPTION_KEY = generate_key()

Script = httpx.Response | Callable[[httpx.Request], httpx.Response]


def live_settings(**overrides):
    values = dict(
        amazon_mode="live",
        lwa_client_id=CLIENT_ID,
        lwa_client_secret=CLIENT_SECRET,
        credentials_encryption_keys=ENCRYPTION_KEY,
        spapi_application_id="amzn1.sp.solution.test",
        spapi_redirect_uri=REDIRECT_URI,
    )
    values.update(overrides)
    return make_settings(**values)


def sp_error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"errors": [{"code": code, "message": message, "details": ""}]},
        headers={"x-amzn-RequestId": f"req-error-{status}"},
    )


class FakeAmazon:
    def __init__(self, backing: MockAmazonClient | None = None):
        self.backing = backing or demo_client()
        self.refresh_tokens = {REFRESH_TOKEN}
        self.codes = {"auth-code-1": REFRESH_TOKEN}
        self.access_tokens: set[str] = set()
        self.participations = [
            {
                "marketplace": {
                    "id": MARKETPLACE_ID_IN,
                    "countryCode": "IN",
                    "name": "Amazon.in",
                    "defaultCurrencyCode": "INR",
                    "defaultLanguageCode": "en_IN",
                    "domainName": "www.amazon.in",
                },
                "participation": {"isParticipating": True, "hasSuspendedListings": False},
                "storeName": "Test Store",
            }
        ]
        # operation -> responses served before the normal behaviour (consumed in order)
        self.scripts: dict[str, list[Script]] = {}
        self.requests: list[httpx.Request] = []
        self._ids = itertools.count(1)

    # ---- helpers for tests ---------------------------------------------------------------

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def script(self, operation: str, *responses: Script) -> None:
        self.scripts.setdefault(operation, []).extend(responses)

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def revoke(self) -> None:
        self.refresh_tokens.clear()
        self.access_tokens.clear()

    def sp_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.url.host != "api.amazon.com" and (path is None or r.url.path == path)
        ]

    def lwa_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "api.amazon.com"]

    # ---- the servers ---------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.amazon.com":
            return self._lwa(request)
        return self._sp_api(request)

    def _lwa(self, request: httpx.Request) -> httpx.Response:
        if scripted := self._next_script("lwa"):
            return scripted(request) if callable(scripted) else scripted
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
            return httpx.Response(
                401, json={"error": "invalid_client", "error_description": "Client auth failed"}
            )
        grant = form.get("grant_type")
        if grant == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
            return self._issue(refresh_token=None)
        if grant == "authorization_code":
            refresh = self.codes.pop(form.get("code"), None)
            if refresh is not None and form.get("redirect_uri") == REDIRECT_URI:
                return self._issue(refresh_token=refresh)
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "The request has an invalid grant parameter",
            },
        )

    def _issue(self, refresh_token: str | None) -> httpx.Response:
        token = f"Atza|access-{next(self._ids)}"
        self.access_tokens.add(token)
        body = {"access_token": token, "token_type": "bearer", "expires_in": 3600}
        if refresh_token:
            body["refresh_token"] = refresh_token
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store"})

    def _sp_api(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-amz-access-token") not in self.access_tokens:
            return sp_error(403, "Unauthorized", "The access token you provided has expired.")
        path = request.url.path
        operation = {
            CATALOG_PATH: "searchCatalogItems",
            RESTRICTIONS_PATH: "getListingsRestrictions",
            PARTICIPATIONS_PATH: "getMarketplaceParticipations",
        }.get(path)
        if operation is None:
            return sp_error(404, "NotFound", "Resource not found")
        if scripted := self._next_script(operation):
            return scripted(request) if callable(scripted) else scripted

        params = request.url.params
        try:
            if operation == "searchCatalogItems":
                body = self.backing.search_catalog_items(
                    keywords=params["keywords"],
                    marketplace_id=params["marketplaceIds"],
                    page_token=params.get("pageToken"),
                    page_size=int(params.get("pageSize", 10)),
                )
                body = {**body, "refinements": {"brands": [], "classifications": []}}
            elif operation == "getListingsRestrictions":
                body = self.backing.get_listings_restrictions(
                    asin=params["asin"],
                    seller_id=params["sellerId"],
                    marketplace_id=params["marketplaceIds"],
                    condition=params["conditionType"],
                )
            else:
                body = {"payload": self.participations}
        except err.AmazonRateLimited:
            return sp_error(429, "QuotaExceeded", "You exceeded your quota.")
        except err.AmazonAuthError:
            return sp_error(403, "Unauthorized", "Access to requested resource is denied.")
        except err.AmazonRequestError as exc:
            return sp_error(exc.status, "InvalidInput", exc.detail)
        except err.AmazonTransientError:
            return sp_error(503, "ServiceUnavailable", "Service temporarily unavailable.")
        return httpx.Response(
            200,
            content=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "x-amzn-RequestId": f"req-{next(self._ids)}",
                "x-amzn-RateLimit-Limit": "5.0",
            },
        )

    def _next_script(self, operation: str) -> Script | None:
        queue = self.scripts.get(operation)
        return queue.pop(0) if queue else None
