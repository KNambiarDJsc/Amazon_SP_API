"""Wires the processor to a Dramatiq broker. Everything takes the broker explicitly so tests can
use an in-memory StubBroker while production uses Redis."""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import Middleware

from app.amazon.client import AmazonClient
from app.amazon.mock import demo_client
from app.config import Settings, get_settings
from app.models import SellerAccount
from app.services.dispatch import recover
from app.services.processor import BrandProcessor, ClientFactory
from app.services.rate_limit import RateLimiter, RedisTokenBucket

log = logging.getLogger(__name__)

QUEUE = "brand_checks"
TASK_TIME_LIMIT_MS = 15 * 60 * 1000


class DramatiqDispatcher:
    def __init__(self, actor: dramatiq.Actor):
        self._actor = actor

    def enqueue_brand(self, brand_id: uuid.UUID, delay_seconds: float = 0.0) -> None:
        options = {"delay": int(delay_seconds * 1000)} if delay_seconds > 0 else {}
        self._actor.send_with_options(args=(str(brand_id),), **options)


class SweeperMiddleware(Middleware):
    """Runs crash recovery periodically inside each worker process. Recovery is idempotent and
    serialised per seller in the database, so several sweepers running at once are harmless."""

    def __init__(self, run: Callable[[], object], interval_seconds: float = 60.0):
        self._run = run
        self._interval = interval_seconds
        self._stop = threading.Event()

    def after_process_boot(self, broker):
        threading.Thread(target=self._loop, name="recovery-sweeper", daemon=True).start()

    def before_process_stop(self, broker):
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run()
            except Exception:
                log.exception("recovery sweep failed")
            self._stop.wait(self._interval)


@dataclass
class Runtime:
    broker: dramatiq.Broker
    actor: dramatiq.Actor
    dispatcher: DramatiqDispatcher
    processor: BrandProcessor
    settings: Settings


def default_client_factory(cfg: Settings) -> ClientFactory:
    if cfg.amazon_mode == "mock":
        client: AmazonClient = demo_client()
        return lambda _seller: client
    raise RuntimeError(
        "AMAZON_MODE=live needs the SP-API client, which is part of Phase 3. "
        "Use AMAZON_MODE=mock for offline demo data."
    )


def build_runtime(
    broker: dramatiq.Broker,
    cfg: Settings | None = None,
    *,
    client_factory: Callable[[SellerAccount], AmazonClient] | None = None,
    limiter: RateLimiter | None = None,
) -> Runtime:
    cfg = cfg or get_settings()
    holder: dict[str, BrandProcessor] = {}

    def process_brand(brand_id: str) -> None:
        holder["processor"].process(uuid.UUID(brand_id))

    actor = dramatiq.actor(
        process_brand,
        broker=broker,
        actor_name="process_brand",
        queue_name=QUEUE,
        max_retries=0,  # retries are bounded and audited in the database, not by the broker
        time_limit=TASK_TIME_LIMIT_MS,
    )
    dispatcher = DramatiqDispatcher(actor)
    if limiter is None:
        import redis

        limiter = RedisTokenBucket(redis.Redis.from_url(cfg.redis_url))
    processor = BrandProcessor(
        client_factory=client_factory or default_client_factory(cfg),
        limiter=limiter,
        dispatcher=dispatcher,
        settings=cfg,
    )
    holder["processor"] = processor
    return Runtime(broker, actor, dispatcher, processor, cfg)


_runtime: Runtime | None = None
_lock = threading.Lock()


def get_runtime() -> Runtime:
    """Process-wide runtime on a Redis broker (API and worker processes)."""
    global _runtime
    with _lock:
        if _runtime is None:
            cfg = get_settings()
            broker = RedisBroker(url=cfg.redis_url)
            dramatiq.set_broker(broker)
            _runtime = build_runtime(broker, cfg)
            broker.add_middleware(
                SweeperMiddleware(lambda: recover(_runtime.dispatcher, cfg), interval_seconds=60)
            )
        return _runtime
