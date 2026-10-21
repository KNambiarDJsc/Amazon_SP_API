"""Per-seller, per-operation token buckets.

A caller asks for one token and is told how long to wait. If the wait would exceed
`max_wait`, nothing is consumed and the caller should defer its work instead of tying up
a worker. Tokens may go negative: a granted request reserves future capacity, so
concurrent workers queue up behind each other instead of all firing at once.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RateDecision:
    granted: bool
    wait: float  # seconds to sleep if granted; seconds until a token is free if not


class RateLimiter(Protocol):
    def acquire(self, key: str, *, rate: float, burst: int, max_wait: float) -> RateDecision: ...


class InMemoryTokenBucket:
    """Single-process limiter (tests, local development)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def acquire(self, key: str, *, rate: float, burst: int, max_wait: float) -> RateDecision:
        now = self._clock()
        tokens, stamp = self._state.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + (now - stamp) * rate)
        wait = 0.0 if tokens >= 1 else (1 - tokens) / rate
        if wait > max_wait:
            self._state[key] = (tokens, now)
            return RateDecision(False, wait)
        self._state[key] = (tokens - 1, now)
        return RateDecision(True, wait)


# Runs atomically inside Redis and uses the Redis server clock, so worker clock skew is irrelevant.
_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1e6
local rate, burst, max_wait = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens, stamp = tonumber(data[1]), tonumber(data[2])
if tokens == nil then tokens = burst; stamp = now end
tokens = math.min(burst, tokens + (now - stamp) * rate)
local wait = 0
if tokens < 1 then wait = (1 - tokens) / rate end
local granted = 1
if wait > max_wait then granted = 0 else tokens = tokens - 1 end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil((burst / rate + max_wait) * 2000) + 60000)
return {granted, tostring(wait)}
"""


class RedisTokenBucket:
    """Limiter shared by every worker process."""

    def __init__(self, redis_client, prefix: str = "ratelimit:"):
        self._redis = redis_client
        self._prefix = prefix
        self._script = redis_client.register_script(_LUA)

    def acquire(self, key: str, *, rate: float, burst: int, max_wait: float) -> RateDecision:
        granted, wait = self._script(keys=[self._prefix + key], args=[rate, burst, max_wait])
        return RateDecision(bool(int(granted)), float(wait))
