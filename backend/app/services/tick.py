"""One scheduler pass, and a loop that runs the whole pipeline in-process (no Redis needed)."""

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from app.enums import BrandStatus, JobStatus
from app.models import Job, JobBrand
from app.services.jobs import maybe_complete_job
from app.services.orchestrator import Runtime, run_brand
from app.services.scheduling import (
    Dispatch,
    dispatch_for_seller,
    reclaim_expired,
    sellers_with_ready_brands,
)

_IDLE_POLL_SECONDS = 5.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class TickReport:
    reclaimed: int = 0
    dispatched: int = 0
    jobs_completed: int = 0


def tick(rt: Runtime, enqueue: Callable[[Dispatch], None]) -> TickReport:
    """Recover expired leases, complete finished jobs, and hand ready brands to workers.

    A dispatch is enqueued only after its lease has committed, so a worker can never receive
    a message for a lease the database does not yet show. If `enqueue` fails the lease just
    expires and the brand is dispatched again.
    """
    report = TickReport()
    now = rt.clock()

    with rt.session() as session:
        report.reclaimed = reclaim_expired(session, now=now, settings=rt.settings, rng=rt.rng)
        for job in session.scalars(select(Job).where(Job.status == JobStatus.RUNNING)):
            report.jobs_completed += maybe_complete_job(session, job, now=now)

    with rt.session() as session:
        sellers = sellers_with_ready_brands(session, now)

    budget = rt.settings.max_dispatch_per_tick
    for seller_id in sellers:
        if budget <= 0:
            break
        with rt.session() as session:
            dispatches = dispatch_for_seller(
                session, seller_id, now=now, settings=rt.settings, budget=budget
            )
        budget -= len(dispatches)
        report.dispatched += len(dispatches)
        for dispatch in dispatches:
            enqueue(dispatch)
    return report


def run_until_idle(rt: Runtime, *, worker_id: str | None = None, max_passes: int = 100_000) -> int:
    """Drive every open job to a stopping point in this process: the single-machine mode used
    by the CLI, the demo and tests. Returns the number of brand runs performed.

    It stops when nothing is runnable. Brands waiting out a backoff are waited for (through
    `rt.sleep`), but a paused job is left alone.
    """
    worker = worker_id or default_worker_id()
    runs = 0

    def run_now(dispatch: Dispatch) -> None:
        nonlocal runs
        runs += 1
        run_brand(dispatch.brand_id, dispatch.token, worker, rt)

    for _ in range(max_passes):
        report = tick(rt, run_now)
        if report.dispatched:
            continue

        now = rt.clock()
        with rt.session() as session:
            waiting = session.scalar(
                select(func.min(JobBrand.next_attempt_at))
                .join(Job, Job.id == JobBrand.job_id)
                .where(
                    JobBrand.status == BrandStatus.PENDING,
                    JobBrand.next_attempt_at.is_not(None),
                    Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
            )
            in_flight = session.scalar(
                select(func.count())
                .select_from(JobBrand)
                .where(JobBrand.status == BrandStatus.IN_PROGRESS)
            )
        if waiting is None and not in_flight:
            return runs
        horizon = waiting - now if waiting is not None else timedelta(seconds=_IDLE_POLL_SECONDS)
        rt.sleep(max(0.0, min(horizon.total_seconds(), _IDLE_POLL_SECONDS)))
    raise RuntimeError("run_until_idle exceeded max_passes; a brand is not making progress")
