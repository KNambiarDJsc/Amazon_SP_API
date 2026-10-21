import os
import time

import pytest

from app.services.rate_limit import InMemoryTokenBucket, RedisTokenBucket


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_burst_is_immediate_then_requests_are_spaced_by_rate():
    clock = Clock()
    limiter = InMemoryTokenBucket(clock)
    kw = dict(rate=2.0, burst=2, max_wait=10)
    assert [limiter.acquire("k", **kw).wait for _ in range(2)] == [0, 0]
    third = limiter.acquire("k", **kw)
    assert third.granted and third.wait == pytest.approx(0.5)  # reserved
    fourth = limiter.acquire("k", **kw)
    assert fourth.granted and fourth.wait == pytest.approx(1.0)  # queued behind the third


def test_a_wait_longer_than_max_wait_is_refused_and_consumes_nothing():
    clock = Clock()
    limiter = InMemoryTokenBucket(clock)
    kw = dict(rate=1.0, burst=1, max_wait=0.5)
    assert limiter.acquire("k", **kw).granted
    refused = limiter.acquire("k", **kw)
    assert not refused.granted and refused.wait == pytest.approx(1.0)
    clock.t += 0.6  # 0.6 tokens refilled; a refusal did not push the bucket further into debt
    again = limiter.acquire("k", **kw)
    assert again.granted and again.wait == pytest.approx(0.4)


def test_tokens_refill_up_to_burst_only():
    clock = Clock()
    limiter = InMemoryTokenBucket(clock)
    kw = dict(rate=1.0, burst=3, max_wait=0)
    for _ in range(3):
        assert limiter.acquire("k", **kw).granted
    clock.t += 1000
    assert [limiter.acquire("k", **kw).granted for _ in range(4)] == [True, True, True, False]


def test_keys_are_independent_per_seller_and_operation():
    limiter = InMemoryTokenBucket(Clock())
    kw = dict(rate=1.0, burst=1, max_wait=0)
    assert limiter.acquire("seller-a:search", **kw).granted
    assert not limiter.acquire("seller-a:search", **kw).granted
    assert limiter.acquire("seller-b:search", **kw).granted
    assert limiter.acquire("seller-a:restrictions", **kw).granted


@pytest.mark.skipif(not os.environ.get("REDIS_TEST_URL"), reason="needs a Redis server")
def test_redis_bucket_matches_in_memory_semantics():
    import redis

    client = redis.Redis.from_url(os.environ["REDIS_TEST_URL"])
    key = f"test:{time.time_ns()}"
    limiter = RedisTokenBucket(client, prefix="rltest:")
    kw = dict(rate=1.0, burst=3, max_wait=1.5)
    assert [limiter.acquire(key, **kw).wait for _ in range(3)] == [0, 0, 0]
    reserved = limiter.acquire(key, **kw)
    assert reserved.granted and 0.8 < reserved.wait <= 1.0
    refused = limiter.acquire(key, **kw)  # would have to wait ~2s, beyond max_wait=1.5
    assert not refused.granted and 1.5 < refused.wait <= 2.0
    other = limiter.acquire(key + ":other", **kw)
    assert other.granted and other.wait == 0
    client.delete("rltest:" + key, "rltest:" + key + ":other")
