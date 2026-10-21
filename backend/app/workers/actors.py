"""The Dramatiq actor, built against an explicit broker so tests can use a stub broker.

Dramatiq's own retries are switched off (max_retries=0). Retry, backoff, attempt counting and
dead-lettering live in the database where they are visible, audited and survive a Redis
flush; a second retry system on top would only multiply attempts.
"""

import logging
import uuid
from collections.abc import Callable

import dramatiq

from app.services.orchestrator import Runtime, run_brand
from app.services.tick import default_worker_id

log = logging.getLogger(__name__)

QUEUE_NAME = "brand_checks"
ACTOR_NAME = "check_brand"
TIME_LIMIT_MS = 30 * 60 * 1000


def build_actor(broker: dramatiq.Broker, runtime: Callable[[], Runtime]) -> dramatiq.Actor:
    @dramatiq.actor(
        broker=broker,
        actor_name=ACTOR_NAME,
        queue_name=QUEUE_NAME,
        max_retries=0,
        time_limit=TIME_LIMIT_MS,
    )
    def check_brand(brand_id: str, token: str) -> None:
        outcome = run_brand(uuid.UUID(brand_id), token, default_worker_id(), runtime())
        log.info("brand %s finished: %s", brand_id, outcome)

    return check_brand
