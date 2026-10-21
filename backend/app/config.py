from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import MARKETPLACE_ID_IN


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost:5432/eligibility"
    redis_url: str = "redis://localhost:6379/0"

    # "mock" serves offline fixtures and every job is flagged as demo data; "live" calls SP-API.
    amazon_mode: str = Field(default="mock", pattern="^(mock|live)$")
    marketplace_id: str = MARKETPLACE_ID_IN

    # SP-API usage plans differ per operation and seller; these are conservative defaults.
    # Verify against the current usage plan of the account before raising them.
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
