"""Token-bucket rate limiting, keyed per seller and operation.

Amazon's usage plans are per operation and per authorised seller, so adding workers must
not raise a seller's call rate. `reserve` takes a token in advance and says how long the
caller has to wait before using it; if that wait is longer than the caller will tolerate,
nothing is consumed and the caller defers the work instead of blocking a worker.
"""

import threading
import time
from collections.abc import Callable
from typing import Protocol

# Lua keeps read-refill-take atomic across worker processes. Replies are strings because
# Redis truncates Lua numbers to integers.
_RESERVE_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens < 1 then
  wait = (1 - tokens) / rate
end
local ttl = math.ceil(((burst + max_wait * rate) / rate + 1) * 1000)
if wait > max_wait then
  redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
  redis.call('PEXPIRE', key, ttl)
  return '-1'
end
redis.call('HSET', key, 'tokens', tostring(tokens - 1), 'ts', tostring(now))
redis.call('PEXPIRE', key, ttl)
return tostring(wait)
"""


class RateLimiter(Protocol):
    def reserve(self, key: str, *, rate: float, burst: int, max_wait: float) -> float | None:
        """Take one token. Returns seconds to wait before using it, or None if the wait would
        exceed `max_wait` (in which case no token is consumed)."""
        ...


class InMemoryRateLimiter:
    """Correct for a single process. Use RedisRateLimiter when several workers share a seller."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_seen)

    def reserve(self, key: str, *, rate: float, burst: int, max_wait: float) -> float | None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        with self._lock:
            now = self._clock()
            tokens, seen = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + max(0.0, now - seen) * rate)
            wait = 0.0 if tokens >= 1 else (1 - tokens) / rate
            if wait > max_wait:
                self._buckets[key] = (tokens, now)
                return None
            self._buckets[key] = (tokens - 1, now)
            return wait


class RedisRateLimiter:
    def __init__(self, client, prefix: str = "ratelimit:"):
        self._script = client.register_script(_RESERVE_LUA)
        self._prefix = prefix

    def reserve(self, key: str, *, rate: float, burst: int, max_wait: float) -> float | None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        reply = self._script(keys=[self._prefix + key], args=[rate, burst, max_wait])
        wait = float(reply.decode() if isinstance(reply, bytes) else reply)
        return None if wait < 0 else wait
