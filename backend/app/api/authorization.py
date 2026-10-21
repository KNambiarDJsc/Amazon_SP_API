"""Connecting a seller account to its Amazon account (SP-API authorisation).

The seller signs in to Seller Central themselves and approves the app there; this application
only ever sees the resulting grant, never a password.
"""

import html
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from app.amazon.factory import LiveModeUnavailable, LiveServices, live_config_problems
from app.api.deps import current_seller
from app.config import get_settings
from app.db import session_scope
from app.domain.observations import utcnow
from app.enums import AuthorizationStatus
from app.models import SellerAccount
from app.schemas import AuthorizationOut, AuthorizationStart
from app.services.authorization import (
    AuthorizationError,
    authorization_out,
    complete_oauth,
    disconnect,
    get_grant,
    reverify,
    start_oauth,
)

router = APIRouter()

# The callback URL carries a one-time code: keep it out of caches, referrers and frames.
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    ),
}


@lru_cache
def _build_live_services() -> LiveServices:
    return LiveServices(get_settings())


def optional_live_services() -> LiveServices | None:
    try:
        return _build_live_services()
    except LiveModeUnavailable:
        return None


def live_services(
    live: LiveServices | None = Depends(optional_live_services),
) -> LiveServices:
    if live is None:
        problems = "; ".join(live_config_problems(get_settings()))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"Amazon access is not configured: {problems}"
        )
    return live


@router.get("/seller/authorization", response_model=AuthorizationOut)
def get_authorization(seller: SellerAccount = Depends(current_seller)):
    with session_scope() as session:
        return authorization_out(get_grant(session, seller.id))


@router.post("/seller/authorization/start", response_model=AuthorizationStart)
def start_authorization(
    seller: SellerAccount = Depends(current_seller),
    live: LiveServices = Depends(live_services),
):
    """Returns the Seller Central consent URL. Open it in the seller's browser; Amazon sends
    the seller back to SPAPI_REDIRECT_URI (this API's /amazon/callback) when they approve."""
    try:
        with session_scope() as session:
            url, expires_at = start_oauth(session, seller, settings=live.settings, now=utcnow())
    except AuthorizationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return AuthorizationStart(authorization_url=url, expires_at=expires_at)


@router.post("/seller/authorization/verify", response_model=AuthorizationOut)
def verify_authorization(
    seller: SellerAccount = Depends(current_seller),
    live: LiveServices = Depends(live_services),
):
    try:
        grant = reverify(seller.id, live=live, now=utcnow())
    except AuthorizationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return authorization_out(grant)


@router.delete("/seller/authorization", response_model=AuthorizationOut)
def disconnect_authorization(seller: SellerAccount = Depends(current_seller)):
    """Erases the stored grant and pauses the seller's jobs. To revoke the app on Amazon's
    side as well, remove it under Seller Central > Apps and Services > Manage Your Apps."""
    with session_scope() as session:
        disconnect(session, seller.id, now=utcnow())
        return authorization_out(get_grant(session, seller.id))


@router.get("/amazon/callback", response_class=HTMLResponse, include_in_schema=False)
def amazon_callback(
    state: str = Query("", max_length=200),
    spapi_oauth_code: str = Query("", max_length=1000),
    selling_partner_id: str = Query("", max_length=60),
    live: LiveServices | None = Depends(optional_live_services),
):
    """Where Seller Central sends the seller after they approve the app. Unauthenticated by
    necessity; the single-use state is what ties the request to the seller who started it."""
    if not (state and spapi_oauth_code and selling_partner_id):
        return _page(
            "Authorisation incomplete",
            "Amazon did not return an authorisation. Start again from the application.",
            status.HTTP_400_BAD_REQUEST,
        )
    if live is None:
        return _page(
            "Not available",
            "Amazon access is not configured on this server.",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        grant = complete_oauth(
            state=state,
            code=spapi_oauth_code,
            selling_partner_id=selling_partner_id,
            live=live,
            now=utcnow(),
        )
    except AuthorizationError as exc:
        return _page("Authorisation failed", str(exc), status.HTTP_400_BAD_REQUEST)
    if grant.status == AuthorizationStatus.ACTIVE:
        return _page("Amazon account connected", "You can close this window.", status.HTTP_200_OK)
    return _page(
        "Amazon account saved, not yet verified",
        "Amazon could not be reached to confirm access. Use 'verify' in the application "
        "before starting a job.",
        status.HTTP_202_ACCEPTED,
    )


def _page(title: str, message: str, code: int) -> HTMLResponse:
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:4rem auto;"
        "padding:0 1rem;color:#1a1a1a;background:#fff}h1{font-size:1.25rem}</style>"
        f"</head><body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>"
    )
    return HTMLResponse(body, status_code=code, headers=_PAGE_HEADERS)
