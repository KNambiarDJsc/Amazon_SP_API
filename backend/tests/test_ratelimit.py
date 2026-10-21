import os
import uuid

import pytest

from app.services.ratelimit import InMemoryRateLimiter, RedisRateLimiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limiter():
    clock = Clock()
    return InMemoryRateLimiter(clock), clock


KW = dict(rate=2.0, burst=3, max_wait=10.0)


def test_burst_is_free_then_calls_are_spaced_by_rate():
    rl, _ = limiter()
    waits = [rl.reserve("k", **KW) for _ in range(6)]
    assert waits[:3] == [0.0, 0.0, 0.0]
    assert waits[3:] == pytest.approx([0.5, 1.0, 1.5])  # one token every 1/rate seconds


def test_tokens_refill_with_time_but_never_beyond_burst():
    rl, clock = limiter()
    for _ in range(3):
        rl.reserve("k", **KW)
    clock.t += 1.0  # two tokens back
    assert rl.reserve("k", **KW) == 0.0
    assert rl.reserve("k", **KW) == 0.0
    assert rl.reserve("k", **KW) > 0
    clock.t += 3600  # a long idle period still only buys `burst` free calls
    assert [rl.reserve("k", **KW) for _ in range(4)][-1] > 0


def test_refusal_consumes_nothing():
    rl, clock = limiter()
    tight = dict(rate=1.0, burst=1, max_wait=0.5)
    assert rl.reserve("k", **tight) == 0.0
    assert rl.reserve("k", **tight) is None  # would need to wait 1s
    assert rl.reserve("k", **tight) is None  # still 1s: the refusal did not dig a deeper hole
    clock.t += 1.0
    assert rl.reserve("k", **tight) == 0.0


def test_keys_are_independent_so_sellers_cannot_starve_each_other():
    rl, _ = limiter()
    for _ in range(3):
        rl.reserve("seller-a:restrictions", **KW)
    assert rl.reserve("seller-a:restrictions", **KW) > 0
    assert rl.reserve("seller-b:restrictions", **KW) == 0.0
    assert rl.reserve("seller-a:catalog", **KW) == 0.0


@pytest.mark.parametrize("rate,burst", [(0, 1), (-1, 1), (1, 0)])
def test_invalid_configuration_is_rejected(rate, burst):
    rl, _ = limiter()
    with pytest.raises(ValueError):
        rl.reserve("k", rate=rate, burst=burst, max_wait=1)


# ---- real Redis (opt in: TEST_REDIS_URL=redis://localhost:6379/15) --------------------------

REDIS_URL = os.environ.get("TEST_REDIS_URL")


@pytest.mark.skipif(not REDIS_URL, reason="set TEST_REDIS_URL to exercise the Lua limiter")
def test_redis_limiter_matches_in_memory_semantics():
    import redis

    client = redis.Redis.from_url(REDIS_URL)
    rl = RedisRateLimiter(client, prefix=f"test:{uuid.uuid4()}:")
    tight = dict(rate=1.0, burst=2, max_wait=0.4)
    assert rl.reserve("k", **tight) == 0.0
    assert rl.reserve("k", **tight) == 0.0
    assert rl.reserve("k", **tight) is None  # would wait ~1s, over max_wait
    assert rl.reserve("other", **tight) == 0.0
    patient = dict(rate=1.0, burst=2, max_wait=5.0)
    assert 0.5 < rl.reserve("k", **patient) <= 1.0
