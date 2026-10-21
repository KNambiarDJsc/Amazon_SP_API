from app.amazon.client import AmazonClient
from app.amazon.mock import demo_client
from app.config import Settings
from app.enums import DataSource


class LiveModeUnavailable(RuntimeError):
    """AMAZON_MODE=live was requested but no live client exists yet."""


def data_source_for(settings: Settings) -> DataSource:
    return DataSource.LIVE if settings.amazon_mode == "live" else DataSource.MOCK


def ensure_available(settings: Settings) -> None:
    if settings.amazon_mode != "mock":
        raise LiveModeUnavailable(
            "AMAZON_MODE=live needs the SP-API client and seller authorisation (Phase 3); "
            "it is not available yet"
        )


def get_amazon_client(settings: Settings) -> AmazonClient:
    """Mock mode serves the offline demo dataset. Live mode must never fall back to it: a job
    that silently ran on fixtures would present invented eligibility as a seller's real one."""
    ensure_available(settings)
    return demo_client()
