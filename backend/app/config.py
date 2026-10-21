from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import MARKETPLACE_ID_IN


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    environment: str = "development"  # "production" disables API docs and hardens defaults
    enable_api_docs: bool = True
    log_json: bool = False
    log_level: str = "INFO"

    # --- API protection. Limits are per API key (or per client IP when unauthenticated).
    api_rate_limit_enabled: bool = True
    allowed_hosts: str = "*"  # comma-separated Host values accepted (set your domain in production)
    api_rate_per_sec: float = 20.0
    api_rate_burst: int = 60
    api_anon_rate_per_sec: float = 5.0
    api_anon_rate_burst: int = 20
    auth_failure_burst: int = 10  # failed authentications per IP before they are throttled
    auth_failure_refill_seconds: float = 6.0
    max_json_body_bytes: int = 1_048_576
    metrics_token: SecretStr = SecretStr("")  # if set, /metrics requires this bearer token
    otel_exporter_otlp_endpoint: str = ""  # set to enable tracing

    database_url: str = "postgresql+psycopg://postgres@localhost:5432/eligibility"
    redis_url: str = "redis://localhost:6379/0"

    # "mock" serves offline fixtures and every job is flagged as demo data; "live" calls SP-API.
    amazon_mode: str = Field(default="mock", pattern="^(mock|live)$")
    marketplace_id: str = MARKETPLACE_ID_IN

    # --- Amazon SP-API (live mode). India (A21TJRUUN4KGV) is served by the EU endpoint.
    sp_api_base_url: str = (
        "https://sellingpartnerapi-eu.amazon.com"  # sandbox: sandbox.sellingpartnerapi-eu...
    )
    lwa_token_url: str = "https://api.amazon.com/auth/o2/token"
    lwa_client_id: str = ""
    lwa_client_secret: SecretStr = SecretStr("")
    # Only needed for the public/multi-seller website authorisation flow.
    sp_api_application_id: str = ""
    oauth_redirect_uri: str = ""
    seller_central_url: str = "https://sellercentral.amazon.in"
    sp_api_draft_app: bool = False  # adds version=beta to the consent URL for apps in draft state
    # Comma-separated Fernet keys, newest first. Older keys stay listed to decrypt until re-saved.
    credential_encryption_keys: SecretStr = SecretStr("")
    reason_locale: str = "en_US"  # message language; the classifier reads English reason text
    http_timeout_seconds: float = 20.0
    oauth_state_ttl_seconds: int = 900

    # --- Optional browser verification: OFF by default; read docs/BROWSER_VERIFICATION.md first.
    browser_automation_enabled: bool = False
    # Who operates this agent; sent in the User-Agent so Amazon can identify the operator.
    browser_agent_contact: str = ""
    browser_executable_path: str = ""  # empty: Playwright's own Chromium
    browser_headless: bool = True
    browser_launch_args: str = ""  # comma-separated extra Chromium flags (e.g. --no-sandbox in CI)
    # The page that hosts the "search your catalogue" box. Calibrate against your live account.
    browser_listing_url: str = "https://sellercentral.amazon.in/product-search"
    browser_timeout_ms: int = 15_000
    browser_max_attempts: int = 2
    browser_min_interval_seconds: float = 4.0  # pause between ASINs: this is a person's pace
    browser_screenshots: bool = True
    evidence_dir: str = "var/evidence"

    # SP-API usage plans differ per operation and seller; these are conservative defaults.
    # Verify against the current usage plan of the account before raising them.
    # Amazon's own pages disagree on searchCatalogItems (2/s vs 5/s); stay conservative.
    rate_search_catalog_per_sec: float = 2.0
    rate_search_catalog_burst: int = 2
    rate_restrictions_per_sec: float = 5.0
    rate_restrictions_burst: int = 10
    max_rate_wait_seconds: float = 3.0  # longer waits defer the brand instead of blocking a worker

    seller_max_concurrent_brands: int = 2  # in-flight brand tasks per seller, across all their jobs

    brand_lease_seconds: int = 300
    queue_stale_seconds: int = (
        900  # a QUEUED brand older than this is presumed to have lost its message
    )
    max_brand_attempts: int = 5  # consecutive failures with no progress before dead-lettering
    max_check_attempts: int = 3  # transient failures on one ASIN before it goes to manual review
    max_throttle_deferrals: int = 30
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 300.0

    # Large reports are built by a worker instead of inside an HTTP request.
    export_dir: str = "var/exports"
    export_sync_max_checks: int = 10_000
    export_ttl_hours: int = 24
    export_stale_minutes: int = 30  # a RUNNING export older than this lost its worker

    max_upload_bytes: int = 5 * 1024 * 1024
    max_brands_per_job: int = 5000
    max_brand_length: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()
