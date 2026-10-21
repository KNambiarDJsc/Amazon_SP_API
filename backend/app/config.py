from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import MARKETPLACE_ID_IN


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost:5432/eligibility"
    redis_url: str = "redis://localhost:6379/0"

    # "mock" serves offline fixtures and every job is flagged as demo data; "live" calls SP-API.
    amazon_mode: str = Field(default="mock", pattern="^(mock|live)$")
    marketplace_id: str = MARKETPLACE_ID_IN

    # ---- live SP-API ----
    # Amazon India is served by the Europe endpoint. Point this at a sandbox endpoint to test.
    spapi_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"
    lwa_token_url: str = "https://api.amazon.com/auth/o2/token"
    lwa_client_id: str | None = None
    lwa_client_secret: SecretStr | None = None
    # Restriction messages are classified by their wording ("...approval to list in this
    # brand"), so reasons and catalog summaries are always requested in English.
    spapi_locale: str = "en_IN"
    spapi_timeout_seconds: float = 20.0
    # Fernet keys, comma-separated. The first encrypts; every one decrypts (key rotation).
    credentials_encryption_keys: SecretStr | None = None

    # Website authorisation workflow (Seller Central consent page). Optional for a private app,
    # whose refresh token can instead be registered with `python -m app.cli connect-seller`.
    spapi_application_id: str | None = None
    spapi_redirect_uri: str | None = None  # must exactly match a URI registered on the app
    spapi_app_is_draft: bool = False  # draft apps need version=beta on the consent URL
    seller_central_url: str = "https://sellercentral.amazon.in"
    oauth_state_ttl_seconds: int = 600  # Amazon expects the whole flow to finish in 10 minutes

    # Documented usage plans: searchCatalogItems 2 rps (burst 2), getListingsRestrictions 5 rps
    # (burst 10), per seller and application. Amazon can change them; keep these at or below.
    rate_search_catalog_per_sec: float = 2.0
    rate_search_catalog_burst: int = 2
    rate_restrictions_per_sec: float = 5.0
    rate_restrictions_burst: int = 10
    max_rate_wait_seconds: float = 3.0  # longer waits defer the brand instead of blocking a worker

    # "redis" shares one bucket per seller across worker processes; "memory" is for a single
    # process (local runs, tests) and would let N workers exceed a seller's rate.
    rate_limiter: str = Field(default="redis", pattern="^(redis|memory)$")

    # Run the scheduler and worker inside the API process (single-machine mode, no Redis queue).
    inline_worker: bool = False

    seller_max_concurrent_brands: int = 2

    # A dispatched brand must be picked up within this window or it returns to the pool;
    # this is what recovers work whose queue message was lost.
    queue_lease_seconds: int = 120
    scheduler_interval_seconds: float = 2.0
    max_dispatch_per_tick: int = 100

    brand_lease_seconds: int = 300
    max_brand_attempts: int = 5
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 300.0

    max_upload_bytes: int = 5 * 1024 * 1024
    max_brands_per_job: int = 5000
    max_brand_length: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
