"""Which Amazon client a job talks to.

Mock mode hands every seller the offline demo client. Live mode builds a client per seller
from that seller's own stored grant, and refuses (rather than falling back to fixtures) when
the application is not configured or the seller is not authorised: a job that silently ran on
fixtures would present invented eligibility as a seller's real one.
"""

import time
from collections.abc import Callable
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.amazon.client import AmazonClient
from app.amazon.errors import SellerNotAuthorized
from app.amazon.live import SpApiClient
from app.amazon.lwa import LwaClient, TokenCache
from app.amazon.mock import demo_client
from app.config import Settings
from app.crypto import CredentialBox, CredentialDecryptError, CredentialKeyError
from app.enums import AuthorizationStatus, DataSource
from app.models import SellerAccount, SellerAuthorization


class LiveModeUnavailable(RuntimeError):
    """AMAZON_MODE=live was requested but the application is not configured for it."""


def data_source_for(settings: Settings) -> DataSource:
    return DataSource.LIVE if settings.amazon_mode == "live" else DataSource.MOCK


def live_config_problems(settings: Settings) -> list[str]:
    problems = []
    if not settings.lwa_client_id:
        problems.append("LWA_CLIENT_ID is not set")
    if not settings.lwa_client_secret or not settings.lwa_client_secret.get_secret_value():
        problems.append("LWA_CLIENT_SECRET is not set")
    try:
        CredentialBox.from_settings(settings)
    except CredentialKeyError as exc:
        problems.append(str(exc))
    return problems


def ensure_available(settings: Settings) -> None:
    if settings.amazon_mode != "live":
        return
    problems = live_config_problems(settings)
    if problems:
        raise LiveModeUnavailable("AMAZON_MODE=live is not configured: " + "; ".join(problems))


class LiveServices:
    """The process-wide pieces of the live integration: one HTTP connection pool, one LWA
    client, one access-token cache and the credential box. Thread-safe."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.Client | None = None,
        box: CredentialBox | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        ensure_available(settings.model_copy(update={"amazon_mode": "live"}))
        self.settings = settings
        self.http = http or httpx.Client(timeout=httpx.Timeout(settings.spapi_timeout_seconds))
        self.box = box or CredentialBox.from_settings(settings)
        self.lwa = LwaClient(
            client_id=settings.lwa_client_id,
            client_secret=settings.lwa_client_secret.get_secret_value(),
            token_url=settings.lwa_token_url,
            http=self.http,
            monotonic=monotonic,
        )
        self.tokens = TokenCache(self.lwa, monotonic=monotonic)

    def client(self, refresh_token: str) -> SpApiClient:
        return SpApiClient(
            endpoint=self.settings.spapi_endpoint,
            refresh_token=refresh_token,
            tokens=self.tokens,
            http=self.http,
            locale=self.settings.spapi_locale,
        )


class ClientProvider(Protocol):
    data_source: DataSource

    def for_seller(self, session: Session, seller: SellerAccount) -> AmazonClient: ...


class SharedClientProvider:
    """One client for every seller: the offline demo, and tests."""

    def __init__(self, client: AmazonClient):
        self.client = client

    @property
    def data_source(self) -> DataSource:
        return self.client.data_source

    def for_seller(self, session: Session, seller: SellerAccount) -> AmazonClient:
        return self.client


class LiveClientProvider:
    data_source = DataSource.LIVE

    def __init__(self, services: LiveServices):
        self.services = services

    def for_seller(self, session: Session, seller: SellerAccount) -> AmazonClient:
        grant = session.scalar(
            select(SellerAuthorization).where(SellerAuthorization.seller_account_id == seller.id)
        )
        if (
            grant is None
            or grant.status != AuthorizationStatus.ACTIVE
            or not grant.refresh_token_encrypted
        ):
            raise SellerNotAuthorized(
                "seller account has no active Amazon authorisation; connect it and resume"
            )
        if grant.selling_partner_id != seller.amazon_seller_id:
            raise SellerNotAuthorized("stored grant is for a different Amazon seller")
        try:
            refresh_token = self.services.box.open(seller.id, grant.refresh_token_encrypted)
        except CredentialDecryptError as exc:
            raise SellerNotAuthorized(str(exc)) from exc
        return self.services.client(refresh_token)


def get_client_provider(settings: Settings) -> ClientProvider:
    ensure_available(settings)
    if settings.amazon_mode == "live":
        return LiveClientProvider(LiveServices(settings))
    return SharedClientProvider(demo_client())
