"""Connecting a seller account to Amazon.

Two ways, both ending with an encrypted refresh token and the seller's merchant id:

* Website authorisation (public / multi-seller applications): the seller approves the app on
  Seller Central and is redirected back with a short-lived code. The seller never types a
  password or token into this application.
* Self-authorisation (a *private* application the seller owns): the seller generates a refresh
  token for their own app in the Solution Provider Portal and supplies it once.
"""

import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.amazon.errors import AmazonAuthError, AmazonError
from app.amazon.lwa import LwaTokenProvider, authorization_code_exchange
from app.config import Settings
from app.domain.observations import utcnow
from app.models import OAuthState, SellerAccount, SellerCredential
from app.services import audit, credentials

log = logging.getLogger(__name__)

SELLER_ID_RE = re.compile(r"^[A-Z0-9]{8,20}$")


class OAuthError(Exception):
    """A problem the caller can be told about. The message never contains secrets."""


class NotConfigured(OAuthError):
    pass


def _require_app_config(cfg: Settings) -> None:
    if not (cfg.sp_api_application_id and cfg.oauth_redirect_uri):
        raise NotConfigured(
            "Website authorisation is not configured (SP_API_APPLICATION_ID, OAUTH_REDIRECT_URI). "
            "For a private application use self-authorisation instead."
        )


def authorization_url(cfg: Settings, state: str) -> str:
    params = {"application_id": cfg.sp_api_application_id, "state": state}
    if cfg.sp_api_draft_app:
        params["version"] = "beta"
    return f"{cfg.seller_central_url.rstrip('/')}/apps/authorize/consent?{urlencode(params)}"


def begin_authorization(session: Session, cfg: Settings, seller_id: uuid.UUID) -> str:
    _require_app_config(cfg)
    state = secrets.token_urlsafe(32)
    session.add(
        OAuthState(
            state=state,
            seller_account_id=seller_id,
            expires_at=utcnow() + timedelta(seconds=cfg.oauth_state_ttl_seconds),
        )
    )
    session.flush()
    return authorization_url(cfg, state)


def consume_state(session: Session, state: str) -> uuid.UUID:
    """Atomically use up a state. Unknown, expired and already-used states are
    indistinguishable to the caller."""
    now = utcnow()
    row = session.get(OAuthState, state)
    claimed = session.execute(
        update(OAuthState)
        .where(OAuthState.state == state, OAuthState.used_at.is_(None), OAuthState.expires_at > now)
        .values(used_at=now)
    )
    if row is None or claimed.rowcount != 1:
        raise OAuthError(
            "This authorisation request is invalid or has expired. Please start again."
        )
    return row.seller_account_id


def _check_seller_id(value: str) -> str:
    value = value.strip().upper()
    if not SELLER_ID_RE.match(value):
        raise OAuthError("The selling partner (merchant) ID looks malformed.")
    return value


def _store(
    session: Session,
    cipher: credentials.CredentialCipher,
    seller: SellerAccount,
    amazon_seller_id: str,
    refresh_token: str,
    authorization_type: str,
) -> None:
    previous = seller.amazon_seller_id
    credentials.save_refresh_token(
        session, cipher, seller.id, refresh_token, authorization_type=authorization_type
    )
    session.get(SellerCredential, seller.id).last_verified_at = utcnow()
    seller.amazon_seller_id = amazon_seller_id
    audit.record(
        session,
        seller_account_id=seller.id,
        event_type="SELLER_CONNECTED",
        authorization_type=authorization_type,
        amazon_seller_id=amazon_seller_id,
        replaced_seller_id=previous if previous and previous != amazon_seller_id else None,
    )


def complete_authorization(
    session: Session,
    cfg: Settings,
    cipher: credentials.CredentialCipher,
    http: httpx.Client,
    *,
    state: str,
    code: str,
    selling_partner_id: str,
) -> uuid.UUID:
    _require_app_config(cfg)
    seller_id = consume_state(session, state)
    session.commit()  # the state is spent even if the exchange below fails: no replays
    partner_id = _check_seller_id(selling_partner_id)
    try:
        refresh_token = authorization_code_exchange(
            http,
            token_url=cfg.lwa_token_url,
            client_id=cfg.lwa_client_id,
            client_secret=cfg.lwa_client_secret.get_secret_value(),
            code=code,
            redirect_uri=cfg.oauth_redirect_uri,
        )
    except AmazonError as exc:
        log.warning("authorisation code exchange failed for seller %s: %s", seller_id, exc)
        raise OAuthError(
            "Amazon did not accept the authorisation. Please try connecting again."
        ) from exc
    seller = session.get(SellerAccount, seller_id)
    _store(session, cipher, seller, partner_id, refresh_token, "OAUTH")
    return seller_id


def connect_with_refresh_token(
    session: Session,
    cfg: Settings,
    cipher: credentials.CredentialCipher,
    http: httpx.Client,
    seller: SellerAccount,
    *,
    selling_partner_id: str,
    refresh_token: str,
) -> None:
    """Self-authorisation. The token is proven valid with Amazon before anything is stored."""
    partner_id = _check_seller_id(selling_partner_id)
    provider = LwaTokenProvider(
        http,
        token_url=cfg.lwa_token_url,
        client_id=cfg.lwa_client_id,
        client_secret=cfg.lwa_client_secret.get_secret_value(),
        refresh_token=refresh_token,
    )
    try:
        provider.get_access_token()
    except AmazonAuthError as exc:
        raise OAuthError(f"Amazon did not accept this refresh token. {exc}") from exc
    except AmazonError as exc:
        raise OAuthError("Could not reach Amazon to verify the token. Please try again.") from exc
    _store(session, cipher, seller, partner_id, refresh_token, "SELF")


def verify_connection(
    session: Session,
    cfg: Settings,
    cipher: credentials.CredentialCipher,
    http: httpx.Client,
    seller: SellerAccount,
) -> None:
    """Force a token refresh to prove the stored credential still works."""
    try:
        token, row = credentials.load_refresh_token(session, cipher, seller.id)
    except credentials.CredentialError as exc:
        raise OAuthError(str(exc)) from exc
    provider = LwaTokenProvider(
        http,
        token_url=cfg.lwa_token_url,
        client_id=cfg.lwa_client_id,
        client_secret=cfg.lwa_client_secret.get_secret_value(),
        refresh_token=token,
    )
    try:
        provider.get_access_token()
    except AmazonAuthError as exc:
        credentials.mark_revoked(session, seller.id, str(exc))
        raise OAuthError(f"Amazon rejected the stored credential. {exc}") from exc
    except AmazonError as exc:
        raise OAuthError("Could not reach Amazon to verify the credential.") from exc
    row.last_verified_at = utcnow()
    row.last_error = None
