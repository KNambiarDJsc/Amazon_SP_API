"""Connecting a seller account to Amazon. Tokens are accepted but never returned."""

import html
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.orm import Session

from app.api.deps import cipher_dep, current_seller, db_dep, http_dep, settings_dep
from app.config import Settings
from app.models import SellerAccount, SellerCredential
from app.services import audit, credentials, oauth
from app.services.credentials import CredentialCipher

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/amazon", tags=["amazon"])

Seller = Annotated[SellerAccount, Depends(current_seller)]
Db = Annotated[Session, Depends(db_dep)]
Cfg = Annotated[Settings, Depends(settings_dep)]
Cipher = Annotated[CredentialCipher, Depends(cipher_dep)]
Http = Annotated[httpx.Client, Depends(http_dep)]


class ConnectRequest(BaseModel):
    seller_id: str = Field(min_length=8, max_length=20, description="Selling partner (merchant) ID")
    refresh_token: SecretStr = Field(min_length=20, max_length=4000)


class ConnectionStatus(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    connected: bool
    seller_id: str | None = None
    authorization_type: str | None = None
    status: str | None = None  # ACTIVE | REVOKED
    needs_reconnect: bool = False
    last_verified_at: str | None = None
    last_error: str | None = None


def _status(db: Session, seller: SellerAccount) -> ConnectionStatus:
    row = db.get(SellerCredential, seller.id)
    if row is None:
        return ConnectionStatus(connected=False, seller_id=seller.amazon_seller_id)
    return ConnectionStatus(
        connected=row.status == credentials.ACTIVE,
        seller_id=seller.amazon_seller_id,
        authorization_type=row.authorization_type,
        status=row.status,
        needs_reconnect=row.status != credentials.ACTIVE,
        last_verified_at=row.last_verified_at.isoformat() if row.last_verified_at else None,
        last_error=row.last_error,
    )


def _oauth_http_error(exc: oauth.OAuthError) -> HTTPException:
    code = 501 if isinstance(exc, oauth.NotConfigured) else 400
    return HTTPException(code, str(exc))


@router.get("/connection", response_model=ConnectionStatus)
def connection_status(seller: Seller, db: Db):
    return _status(db, seller)


@router.put("/connection", response_model=ConnectionStatus)
def connect_self_authorised(
    body: ConnectRequest, seller: Seller, db: Db, cfg: Cfg, cipher: Cipher, http: Http
):
    """Self-authorisation for a private application the seller owns."""
    try:
        oauth.connect_with_refresh_token(
            db,
            cfg,
            cipher,
            http,
            seller,
            selling_partner_id=body.seller_id,
            refresh_token=body.refresh_token.get_secret_value(),
        )
        db.commit()
    except oauth.OAuthError as exc:
        db.rollback()
        raise _oauth_http_error(exc) from None
    return _status(db, seller)


@router.post("/connection/verify", response_model=ConnectionStatus)
def verify_connection(seller: Seller, db: Db, cfg: Cfg, cipher: Cipher, http: Http):
    try:
        oauth.verify_connection(db, cfg, cipher, http, seller)
        db.commit()
    except oauth.OAuthError as exc:
        db.commit()  # keep a REVOKED mark if Amazon rejected the credential
        raise _oauth_http_error(exc) from None
    return _status(db, seller)


@router.delete("/connection", status_code=204)
def disconnect(seller: Seller, db: Db):
    if credentials.delete_credential(db, seller.id):
        audit.record(db, seller_account_id=seller.id, event_type="SELLER_DISCONNECTED")
        db.commit()
    return Response(status_code=204)


class AuthorizeResponse(BaseModel):
    authorization_url: str
    expires_in_seconds: int


@router.post("/authorize", response_model=AuthorizeResponse)
def start_authorization(seller: Seller, db: Db, cfg: Cfg):
    """Begin website authorisation: send the seller's browser to `authorization_url`."""
    try:
        url = oauth.begin_authorization(db, cfg, seller.id)
        db.commit()
    except oauth.OAuthError as exc:
        raise _oauth_http_error(exc) from None
    return AuthorizeResponse(authorization_url=url, expires_in_seconds=cfg.oauth_state_ttl_seconds)


_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.4rem">{title}</h1><p>{message}</p></body></html>"""


def _page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    # Static text only: nothing from the query string is ever reflected into the page.
    return HTMLResponse(
        _PAGE.format(title=html.escape(title), message=html.escape(message)),
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/callback", include_in_schema=False)
def authorization_callback(
    db: Db,
    cfg: Cfg,
    cipher: Cipher,
    http: Http,
    state: Annotated[str, Query(max_length=128)] = "",
    spapi_oauth_code: Annotated[str, Query(max_length=2000)] = "",
    selling_partner_id: Annotated[str, Query(max_length=64)] = "",
):
    """Where Amazon sends the seller's browser back. Unauthenticated by necessity: the single-use,
    expiring `state` issued by POST /authorize is what proves which seller this is."""
    if not (state and spapi_oauth_code and selling_partner_id):
        return _page("Authorisation incomplete", "Amazon did not return the expected details.", 400)
    try:
        oauth.complete_authorization(
            db,
            cfg,
            cipher,
            http,
            state=state,
            code=spapi_oauth_code,
            selling_partner_id=selling_partner_id,
        )
        db.commit()
    except oauth.OAuthError as exc:
        db.commit()  # persist the spent state
        return _page("Could not connect", str(exc), 400)
    return _page("Connected", "Your Amazon seller account is connected. You can close this tab.")
