"""Seller authorisation: connecting a seller account to its Amazon account, keeping the grant
encrypted, and noticing when it stops working.

Two ways in, both ending in connect_seller():

    website workflow   start_oauth -> Seller Central consent page -> callback (complete_oauth):
                       consume the state -> exchange the code with LWA -> verify -> store
    private app        refresh token generated in Seller Central (CLI connect-seller):
                       verify -> store

The seller never gives this application a password. Network calls (code exchange,
verification) run outside database transactions; nothing is stored unless Amazon accepted the
grant and the account sells on the seller account's marketplace.
"""

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.amazon.errors import (
    AmazonAuthError,
    AmazonError,
    AmazonMalformedResponse,
    AmazonRateLimited,
    AmazonRequestError,
    AmazonTransientError,
)
from app.amazon.factory import LiveServices
from app.amazon.live import SpApiClient
from app.config import Settings
from app.crypto import CredentialDecryptError
from app.db import session_scope
from app.enums import AuthorizationMethod, AuthorizationStatus
from app.models import OAuthState, SellerAccount, SellerAuthorization
from app.schemas import AuthorizationOut
from app.services.audit import audit
from app.services.jobs import pause_seller_jobs

# Amazon asks sellers to reauthorise once a year; refresh tokens stop working after 365 days.
REFRESH_TOKEN_LIFETIME = timedelta(days=365)
_SELLING_PARTNER_ID = re.compile(r"^[A-Z0-9]{6,40}$")
# Any keyword works: the probe only proves the app holds the Product Listing role.
_PROBE_KEYWORDS = "notebook"
_MAX_ERROR = 500

SessionFactory = Callable[[], AbstractContextManager[Session]]


class AuthorizationError(Exception):
    """The grant cannot be accepted or used; the message is safe to show the seller."""


@dataclass(frozen=True)
class Verification:
    status: AuthorizationStatus  # ACTIVE, or PENDING_VERIFICATION when Amazon was unreachable
    store_name: str | None = None
    detail: str | None = None


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def get_grant(session: Session, seller_account_id: uuid.UUID) -> SellerAuthorization | None:
    return session.scalar(
        select(SellerAuthorization).where(
            SellerAuthorization.seller_account_id == seller_account_id
        )
    )


def is_active(session: Session, seller_account_id: uuid.UUID) -> bool:
    grant = get_grant(session, seller_account_id)
    return grant is not None and grant.status == AuthorizationStatus.ACTIVE


def authorization_out(grant: SellerAuthorization | None) -> AuthorizationOut:
    if grant is None:
        return AuthorizationOut(connected=False)
    return AuthorizationOut(
        connected=grant.status != AuthorizationStatus.REVOKED,
        status=grant.status,
        method=grant.method,
        selling_partner_id=grant.selling_partner_id,
        store_name=grant.store_name,
        authorized_at=grant.authorized_at,
        verified_at=grant.verified_at,
        reauthorize_by=grant.authorized_at + REFRESH_TOKEN_LIFETIME,
        last_error=grant.last_error,
    )


# ---- website workflow ----------------------------------------------------------------------


def start_oauth(
    session: Session, seller: SellerAccount, *, settings: Settings, now: datetime
) -> tuple[str, datetime]:
    """Returns the Seller Central consent URL to send the seller to, and when it expires."""
    if not (settings.spapi_application_id and settings.spapi_redirect_uri):
        raise AuthorizationError(
            "the Seller Central consent flow is not configured "
            "(set SPAPI_APPLICATION_ID and SPAPI_REDIRECT_URI)"
        )
    session.execute(delete(OAuthState).where(OAuthState.expires_at < now - timedelta(days=1)))
    state = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=settings.oauth_state_ttl_seconds)
    session.add(
        OAuthState(
            seller_account_id=seller.id,
            state_hash=hash_state(state),
            created_at=now,
            expires_at=expires_at,
        )
    )
    params = {
        "application_id": settings.spapi_application_id,
        "state": state,
        "redirect_uri": settings.spapi_redirect_uri,
    }
    if settings.spapi_app_is_draft:
        params["version"] = "beta"
    audit(session, seller_account_id=seller.id, event_type="authorization_started")
    url = f"{settings.seller_central_url.rstrip('/')}/apps/authorize/consent?{urlencode(params)}"
    return url, expires_at


def consume_state(session: Session, state: str, *, now: datetime) -> uuid.UUID:
    """Single use: the row is locked, checked and marked used in one transaction."""
    row = session.scalar(
        select(OAuthState).where(OAuthState.state_hash == hash_state(state)).with_for_update()
    )
    if row is None or row.consumed_at is not None or now >= row.expires_at:
        raise AuthorizationError(
            "this authorisation link is invalid, already used or expired; start again"
        )
    row.consumed_at = now
    return row.seller_account_id


def complete_oauth(
    *,
    state: str,
    code: str,
    selling_partner_id: str,
    live: LiveServices,
    now: datetime,
    session: SessionFactory = session_scope,
) -> SellerAuthorization:
    # The state is spent in its own transaction, before anything else can fail, so a callback
    # URL can never be replayed, whatever happens next.
    with session() as s:
        seller_account_id = consume_state(s, state, now=now)
    try:
        grant = live.lwa.exchange_code(code, live.settings.spapi_redirect_uri)
    except AmazonError as exc:
        raise AuthorizationError(f"Amazon did not accept the authorisation: {exc}") from exc
    return connect_with_refresh_token(
        seller_account_id=seller_account_id,
        selling_partner_id=selling_partner_id,
        refresh_token=grant.refresh_token,
        method=AuthorizationMethod.OAUTH,
        live=live,
        now=now,
        session=session,
    )


# ---- connecting ----------------------------------------------------------------------------


def verify_access(client: SpApiClient, marketplace_id: str) -> Verification:
    """Prove the grant works for what jobs will do: the account sells on this marketplace
    (Sellers API) and the app holds the Product Listing role (one catalog search)."""
    try:
        participations = client.get_marketplace_participations()
    except AmazonAuthError as exc:
        raise AuthorizationError(
            f"Amazon refused the grant ({exc}). Check the app's credentials and that it has the "
            "Selling Partner Insights role."
        ) from exc
    except (AmazonTransientError, AmazonRateLimited, AmazonMalformedResponse) as exc:
        return Verification(AuthorizationStatus.PENDING_VERIFICATION, detail=_short(exc))
    except AmazonRequestError as exc:
        raise AuthorizationError(f"Amazon rejected the verification request: {exc}") from exc

    entry = next(
        (
            p
            for p in participations.get("payload") or []
            if isinstance(p, dict) and (p.get("marketplace") or {}).get("id") == marketplace_id
        ),
        None,
    )
    if entry is None or not (entry.get("participation") or {}).get("isParticipating"):
        raise AuthorizationError(
            f"this Amazon account does not sell in marketplace {marketplace_id}"
        )
    store_name = entry.get("storeName") if isinstance(entry.get("storeName"), str) else None

    try:
        client.search_catalog_items(
            keywords=_PROBE_KEYWORDS, marketplace_id=marketplace_id, page_size=1
        )
    except AmazonAuthError as exc:
        raise AuthorizationError(
            f"the app cannot search the catalog ({exc}); it needs the Product Listing role"
        ) from exc
    except (AmazonTransientError, AmazonRateLimited, AmazonMalformedResponse) as exc:
        return Verification(AuthorizationStatus.PENDING_VERIFICATION, store_name, _short(exc))
    except AmazonRequestError as exc:
        raise AuthorizationError(f"Amazon rejected the catalog check: {exc}") from exc
    return Verification(AuthorizationStatus.ACTIVE, store_name)


def connect_seller(
    session: Session,
    *,
    seller_account_id: uuid.UUID,
    selling_partner_id: str,
    refresh_token: str,
    method: AuthorizationMethod,
    verification: Verification,
    live: LiveServices,
    now: datetime,
) -> SellerAuthorization:
    seller = session.scalar(
        select(SellerAccount).where(SellerAccount.id == seller_account_id).with_for_update()
    )
    if seller is None or not seller.is_active:
        raise AuthorizationError("seller account not found")
    if seller.amazon_seller_id and seller.amazon_seller_id != selling_partner_id:
        # Existing results describe what that Amazon account may list; mixing in another
        # account's answers under the same seller would make them meaningless.
        raise AuthorizationError(
            f"this seller account is linked to Amazon seller {seller.amazon_seller_id} and "
            f"cannot be switched to {selling_partner_id}; create a new seller account instead"
        )

    grant = get_grant(session, seller.id)
    if grant is None:
        grant = SellerAuthorization(seller_account_id=seller.id)
        session.add(grant)
    grant.selling_partner_id = selling_partner_id
    grant.method = method
    grant.status = verification.status
    grant.refresh_token_encrypted = live.box.seal(seller.id, refresh_token)
    grant.store_name = verification.store_name
    grant.authorized_at = now
    grant.verified_at = now if verification.status == AuthorizationStatus.ACTIVE else None
    grant.last_error = verification.detail
    grant.updated_at = now
    seller.amazon_seller_id = selling_partner_id
    audit(
        session,
        seller_account_id=seller.id,
        event_type="seller_authorized",
        method=method.value,
        selling_partner_id=selling_partner_id,
        status=verification.status.value,
    )
    session.flush()
    return grant


def connect_with_refresh_token(
    *,
    seller_account_id: uuid.UUID,
    selling_partner_id: str,
    refresh_token: str,
    method: AuthorizationMethod,
    live: LiveServices,
    now: datetime,
    session: SessionFactory = session_scope,
) -> SellerAuthorization:
    selling_partner_id = selling_partner_id.strip()
    refresh_token = refresh_token.strip()
    if not _SELLING_PARTNER_ID.match(selling_partner_id):
        raise AuthorizationError("selling partner ID must be 6-40 uppercase letters and digits")
    if not refresh_token:
        raise AuthorizationError("refresh token is empty")
    with session() as s:
        seller = s.get(SellerAccount, seller_account_id)
        if seller is None or not seller.is_active:
            raise AuthorizationError("seller account not found")
        marketplace_id = seller.marketplace_id
    verification = verify_access(live.client(refresh_token), marketplace_id)
    with session() as s:
        return connect_seller(
            s,
            seller_account_id=seller_account_id,
            selling_partner_id=selling_partner_id,
            refresh_token=refresh_token,
            method=method,
            verification=verification,
            live=live,
            now=now,
        )


def reverify(
    seller_account_id: uuid.UUID,
    *,
    live: LiveServices,
    now: datetime,
    session: SessionFactory = session_scope,
) -> SellerAuthorization:
    """Run the connection check again on the stored grant (after Amazon was unreachable, or
    once a role was added). A grant Amazon now refuses is marked for reauthorisation."""
    with session() as s:
        grant = get_grant(s, seller_account_id)
        if grant is None or not grant.refresh_token_encrypted:
            raise AuthorizationError("seller account is not connected to Amazon")
        try:
            refresh_token = live.box.open(seller_account_id, grant.refresh_token_encrypted)
        except CredentialDecryptError as exc:
            raise AuthorizationError(f"{exc}; connect the account again") from exc
        marketplace_id = s.get(SellerAccount, seller_account_id).marketplace_id

    try:
        verification = verify_access(live.client(refresh_token), marketplace_id)
    except AuthorizationError as exc:
        with session() as s:
            mark_needs_reauthorization(s, seller_account_id, detail=str(exc), now=now)
        raise

    with session() as s:
        grant = get_grant(s, seller_account_id)
        if grant.status != AuthorizationStatus.REVOKED:  # disconnected while we were checking
            grant.status = verification.status
            grant.store_name = verification.store_name or grant.store_name
            grant.last_error = verification.detail
            if verification.status == AuthorizationStatus.ACTIVE:
                grant.verified_at = now
            grant.updated_at = now
        audit(
            s,
            seller_account_id=seller_account_id,
            event_type="authorization_verified",
            status=grant.status.value,
        )
        return grant


# ---- losing access -------------------------------------------------------------------------


def mark_needs_reauthorization(
    session: Session, seller_account_id: uuid.UUID, *, detail: str, now: datetime
) -> None:
    """Amazon refused a working grant. A disconnected grant stays disconnected."""
    grant = get_grant(session, seller_account_id)
    if grant is None or grant.status == AuthorizationStatus.REVOKED:
        return
    grant.status = AuthorizationStatus.NEEDS_REAUTHORIZATION
    grant.last_error = detail[:_MAX_ERROR]
    grant.updated_at = now


def disconnect(session: Session, seller_account_id: uuid.UUID, *, now: datetime) -> None:
    """Erase the stored token and stop the seller's jobs. Revoking the app inside Seller Central
    is separate, and only the seller can do it there."""
    grant = get_grant(session, seller_account_id)
    if grant is None or grant.status == AuthorizationStatus.REVOKED:
        return
    grant.status = AuthorizationStatus.REVOKED
    grant.refresh_token_encrypted = None
    grant.updated_at = now
    pause_seller_jobs(
        session,
        seller_account_id,
        reason="Amazon account disconnected; connect it again, then resume",
        now=now,
    )
    audit(session, seller_account_id=seller_account_id, event_type="seller_disconnected")


def _short(exc: Exception) -> str:
    return f"could not verify yet: {exc}"[:_MAX_ERROR]
