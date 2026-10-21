from functools import lru_cache

from app.amazon.factory import get_amazon_client
from app.config import Settings, get_settings
from app.services.orchestrator import Runtime
from app.services.ratelimit import InMemoryRateLimiter, RateLimiter, RedisRateLimiter


def build_runtime(
    settings: Settings | None = None, *, limiter: RateLimiter | None = None
) -> Runtime:
    settings = settings or get_settings()
    if limiter is None:
        if settings.rate_limiter == "memory":
            limiter = InMemoryRateLimiter()
        else:
            import redis

            limiter = RedisRateLimiter(redis.Redis.from_url(settings.redis_url))
    return Runtime(client=get_amazon_client(settings), limiter=limiter, settings=settings)


@lru_cache
def get_runtime() -> Runtime:
    """One runtime per worker process, so every thread shares the same Amazon client and the
    same view of the rate limiter."""
    return build_runtime()
