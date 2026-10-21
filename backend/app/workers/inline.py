"""Single-machine mode: the scheduler and worker run on a thread inside the API process, so a
local prototype needs no Redis. Brands run one at a time; use the Dramatiq worker for parallelism.
"""

import logging
import threading

from app.services.orchestrator import Runtime, run_brand
from app.services.scheduling import Dispatch
from app.services.tick import default_worker_id
from app.workers.scheduler import run_forever

log = logging.getLogger(__name__)


class InlineEngine:
    def __init__(self, runtime: Runtime, interval: float):
        self._runtime = runtime
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="inline-engine", daemon=True)
        self._worker_id = default_worker_id()

    def _execute(self, dispatch: Dispatch) -> None:
        run_brand(dispatch.brand_id, dispatch.token, self._worker_id, self._runtime)

    def _run(self) -> None:
        run_forever(self._runtime, self._execute, self._stop, self._interval)

    def start(self) -> None:
        log.info("inline engine started")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
