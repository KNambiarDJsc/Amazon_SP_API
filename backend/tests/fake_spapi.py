"""A fake of Amazon's HTTP surface (LWA + the two SP-API operations) at the httpx transport.

It encodes the documented contract the live client must satisfy and rejects anything else:
  * LWA: form-encoded POST; refresh_token and authorization_code grants; invalid_grant /
    invalid_client errors.
  * SP-API: `x-amz-access-token` required (no SigV4 / Authorization header); required query
    parameters enforced; documented error body shape; 429 carries x-amzn-RateLimit-Limit.
Behaviour for the two operations is delegated to a MockAmazonClient so every scenario already
defined for the offline mock (approval required, ambiguous, flaky, ...) runs through real HTTP.
"""

import json
from urllib.parse import parse_qs

import httpx

from app.amazon.errors import (
    AmazonAuthError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.amazon.mock import MockAmazonClient

REFRESH_TOKEN = "Atzr|valid-refresh-token-0123456789abcdef"
CLIENT_ID = "amzn1.application-oa2-client.test"
CLIENT_SECRET = "client-secret-do-not-leak-0123456789"
AUTH_CODE = "ANxxvalidAuthCode"
REDIRECT_URI = "https://app.example.test/api/v1/amazon/callback"


def _json(status, body, headers=None):
    return httpx.Response(status, json=body, headers=headers)


class FakeSellingPartnerApi:
    def __init__(self, backend: MockAmazonClient | None = None):
        self.backend = backend or MockAmazonClient()
        self.valid_refresh_tokens = {REFRESH_TOKEN}
        self.valid_access_tokens: set[str] = set()
        self.access_lifetime = 3600
        self.lwa_calls = 0
        self.lwa_forms: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.protocol_violations: list[str] = []
        self.deny_all_sp_api = False  # e.g. the app lacks the Product Listing role
        self._n = 0

    # ---- helpers for tests ---------------------------------------------------------------
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def expire_access_tokens(self) -> None:
        self.valid_access_tokens.clear()

    def revoke_refresh_tokens(self) -> None:
        self.valid_refresh_tokens.clear()

    def sp_api_requests(self, path_prefix: str = "") -> list[httpx.Request]:
        return [
            r for r in self.requests if r.url.host != "api.amazon.com" and path_prefix in r.url.path
        ]

    # ---- transport ------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.amazon.com" and request.url.path == "/auth/o2/token":
            return self._lwa(request)
        if request.url.host != "sellingpartnerapi-eu.amazon.com":
            self.protocol_violations.append(f"unexpected host {request.url.host}")
            return _json(500, {"errors": [{"code": "Fake", "message": "unexpected host"}]})
        return self._sp_api(request)

    def _lwa(self, request: httpx.Request) -> httpx.Response:
        self.lwa_calls += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.lwa_forms.append(form)
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/x-www-form-urlencoded"
        ):
            self.protocol_violations.append("LWA body must be form-encoded")
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return _json(
                401,
                {"error": "invalid_client", "error_description": "Client authentication failed"},
            )
        grant = form.get("grant_type")
        if grant == "refresh_token":
            if form.get("refresh_token") not in self.valid_refresh_tokens:
                return _json(
                    400, {"error": "invalid_grant", "error_description": "Invalid refresh token"}
                )
            return _json(
                200,
                {
                    "access_token": self._issue(),
                    "token_type": "bearer",
                    "expires_in": self.access_lifetime,
                },
            )
        if grant == "authorization_code":
            if form.get("code") != AUTH_CODE or form.get("redirect_uri") != REDIRECT_URI:
                return _json(400, {"error": "invalid_grant", "error_description": "bad code"})
            return _json(
                200,
                {
                    "access_token": self._issue(),
                    "refresh_token": REFRESH_TOKEN,
                    "token_type": "bearer",
                    "expires_in": self.access_lifetime,
                },
            )
        return _json(400, {"error": "unsupported_grant_type"})

    def _issue(self) -> str:
        self._n += 1
        token = f"Atza|access-token-{self._n}"
        self.valid_access_tokens.add(token)
        return token

    def _sp_api(self, request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers or "x-amz-date" in request.headers:
            self.protocol_violations.append("SigV4/Authorization header sent; no longer required")
        if request.method != "GET":
            return _json(405, {"errors": [{"code": "MethodNotAllowed", "message": request.method}]})
        token = request.headers.get("x-amz-access-token")
        if self.deny_all_sp_api or token not in self.valid_access_tokens:
            return _json(
                403,
                {
                    "errors": [
                        {
                            "code": "Unauthorized",
                            "message": "Access to requested resource is denied.",
                        }
                    ]
                },
            )
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}

        def need(*names):
            missing = [n for n in names if not q.get(n)]
            return (
                _json(
                    400,
                    {
                        "errors": [
                            {"code": "InvalidInput", "message": f"missing {','.join(missing)}"}
                        ]
                    },
                )
                if missing
                else None
            )

        try:
            if request.url.path == "/catalog/2022-04-01/items":
                if bad := need("keywords", "marketplaceIds"):
                    return bad
                if int(q.get("pageSize", 10)) > 20 or "," in q["marketplaceIds"]:
                    return _json(
                        400,
                        {
                            "errors": [
                                {"code": "InvalidInput", "message": "pageSize/marketplaceIds"}
                            ]
                        },
                    )
                body = self.backend.search_catalog_items(
                    keywords=q["keywords"],
                    marketplace_id=q["marketplaceIds"],
                    page_token=q.get("pageToken"),
                    page_size=int(q.get("pageSize", 10)),
                )
                return _json(200, dict(body))
            if request.url.path == "/listings/2021-08-01/restrictions":
                if bad := need("asin", "sellerId", "marketplaceIds"):
                    return bad
                body = self.backend.get_listings_restrictions(
                    asin=q["asin"],
                    seller_id=q["sellerId"],
                    marketplace_id=q["marketplaceIds"],
                    condition=q.get("conditionType", ""),
                )
                return _json(200, dict(body))
        except AmazonRateLimited as exc:
            headers = (
                {"x-amzn-RateLimit-Limit": str(1 / exc.retry_after)} if exc.retry_after else {}
            )
            return _json(
                429, {"errors": [{"code": "QuotaExceeded", "message": "throttled"}]}, headers
            )
        except AmazonAuthError:
            return _json(403, {"errors": [{"code": "Unauthorized", "message": "denied"}]})
        except AmazonTransientError:
            return _json(503, {"errors": [{"code": "ServiceUnavailable", "message": "try later"}]})
        except AmazonRequestError as exc:
            return _json(exc.status, {"errors": [{"code": "InvalidInput", "message": exc.detail}]})
        except AmazonMalformedResponse:
            return httpx.Response(200, text="<html>gateway splash</html>")
        return _json(404, {"errors": [{"code": "NotFound", "message": request.url.path}]})


def json_body(request: httpx.Request):
    return json.loads(request.content)
