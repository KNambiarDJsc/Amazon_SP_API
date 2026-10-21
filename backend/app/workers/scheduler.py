"""Scheduler process:  python -m app.workers.scheduler

Every pass it reclaims expired leases, completes finished jobs and hands ready brands to the
queue. It holds no state of its own, so it can be restarted at any time, and running a second
copy is safe (dispatch is serialised per seller in the database).
"""

import logging
import signal
import threading
from collections.abc import Callable

from app.config import get_settings
from app.services.orchestrator import Runtime
from app.services.scheduling import Dispatch
from app.services.tick import tick
from app.workers.runtime import get_runtime

log = logging.getLogger(__name__)
_ERROR_PAUSE_SECONDS = 5.0


def run_forever(
    runtime: Runtime,
    enqueue: Callable[[Dispatch], None],
    stop: threading.Event,
    interval: float,
) -> None:
    while not stop.is_set():
        pause = interval
        try:
            report = tick(runtime, enqueue)
            if report.dispatched or report.reclaimed or report.jobs_completed:
                log.info(
                    "tick: dispatched=%d reclaimed=%d completed=%d",
                    report.dispatched,
                    report.reclaimed,
                    report.jobs_completed,
                )
        except Exception:
            log.exception("scheduler pass failed; will retry")
            pause = max(interval, _ERROR_PAUSE_SECONDS)
        stop.wait(pause)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.workers.tasks import enqueue

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("scheduler started")
    run_forever(get_runtime(), enqueue, stop, get_settings().scheduler_interval_seconds)
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
