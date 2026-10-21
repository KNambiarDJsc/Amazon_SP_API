"""Production worker entry point:  dramatiq app.workers.tasks --queues brand_checks

Importing this module connects to Redis, so only workers and the scheduler import it.
"""

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.config import get_settings
from app.services.scheduling import Dispatch
from app.workers.actors import build_actor
from app.workers.runtime import get_runtime

broker = RedisBroker(url=get_settings().redis_url)
dramatiq.set_broker(broker)
check_brand = build_actor(broker, get_runtime)


def enqueue(dispatch: Dispatch) -> None:
    check_brand.send(str(dispatch.brand_id), dispatch.token)
