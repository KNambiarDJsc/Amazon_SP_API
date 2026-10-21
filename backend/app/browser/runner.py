"""Runs browser verification for one job's unresolved checks.

Never autonomous in the sense that matters: it is started on purpose, by an operator, for one
seller and one job; it works at a human pace, one page at a time; it records evidence (a
screenshot of the product panel only) and never presses a control that lists, offers or applies;
and it stops the moment it meets a sign-in, CAPTCHA or verification prompt.
"""

import hashlib
import logging
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select, text

from app.browser.policy import BrowserNotPermitted, agent_token, ensure_permitted
from app.browser.session_store import load_state, save_state
from app.config import Settings
from app.db import get_engine, session_scope
from app.domain.classification import classify_observation
from app.domain.observations import BrowserSignals, utcnow
from app.enums import CheckState, EligibilityStatus, ObservationOutcome, ObservationSource
from app.models import CatalogCandidate, CheckObservation, EligibilityCheck, JobBrand, SellerAccount
from app.services import audit, review
from app.services.credentials import CredentialCipher
from app.services.evidence import observation_from_row
from app.services.jobs import get_job

log = logging.getLogger(__name__)

MAX_BROWSER_OBSERVATIONS_PER_CHECK = 3


class BrowserBusy(RuntimeError):
    """Another browser session for this seller is already running."""


@dataclass
class RunStats:
    considered: int = 0
    resolved: int = 0
    unresolved: int = 0
    failed: int = 0
    stopped: str | None = None  # "signed_out" | "challenge" when a person must take over


_local_locks: dict[uuid.UUID, threading.Lock] = {}


@contextmanager
def seller_browser_lock(seller_id: uuid.UUID) -> Iterator[None]:
    """At most one browser session per seller across processes (PostgreSQL advisory lock held on
    a dedicated connection) and across threads (SQLite / tests)."""
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        key = (seller_id.int ^ 0xB0B0) % (2**63)
        with engine.connect() as conn:
            if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar():
                raise BrowserBusy("A browser session for this seller is already running.")
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
    else:
        lock = _local_locks.setdefault(seller_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise BrowserBusy("A browser session for this seller is already running.")
        try:
            yield
        finally:
            lock.release()


def _targets(session, job_id: uuid.UUID, limit: int) -> list[tuple[uuid.UUID, str, str]]:
    """Unresolved ASIN checks worth another look. A check is skipped once the browser has already
    given a usable answer for it (looking again at the same page cannot change it) or has been
    tried too many times."""
    candidates = session.execute(
        select(EligibilityCheck)
        .join(JobBrand, JobBrand.id == EligibilityCheck.job_brand_id)
        .join(CatalogCandidate, CatalogCandidate.id == EligibilityCheck.candidate_id)
        .where(
            EligibilityCheck.job_id == job_id,
            EligibilityCheck.state == CheckState.DONE,
            EligibilityCheck.status == EligibilityStatus.NEEDS_MANUAL_CHECK,
        )
        .order_by(JobBrand.position, CatalogCandidate.rank, EligibilityCheck.condition)
    ).scalars()

    targets = []
    for check in candidates:
        tried = session.scalars(
            select(CheckObservation)
            .where(
                CheckObservation.check_id == check.id,
                CheckObservation.source == ObservationSource.BROWSER,
            )
            .order_by(CheckObservation.observed_at, CheckObservation.id)
        ).all()
        if len(tried) >= MAX_BROWSER_OBSERVATIONS_PER_CHECK:
            continue
        if tried and not classify_observation(observation_from_row(tried[-1])).evidence_missing:
            continue
        targets.append((check.id, check.asin, check.condition))
        if len(targets) >= limit:
            break
    return targets


def _store_screenshot(cfg: Settings, job_id, check_id, png: bytes) -> tuple[str, str]:
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    relative = Path(str(job_id)) / str(check_id) / f"{stamp}.png"
    path = Path(cfg.evidence_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return f"file:{relative.as_posix()}", hashlib.sha256(png).hexdigest()


def run_browser_verification(
    *,
    seller_id: uuid.UUID,
    job_id: uuid.UUID,
    cfg: Settings,
    cipher: CredentialCipher,
    factory,
    limit: int = 50,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> RunStats:
    from app.browser.safe_page import SafePage
    from app.browser.verifier import HumanRequired, SellerCentralVerifier

    stats = RunStats()
    with session_scope() as session:
        seller = session.get(SellerAccount, seller_id)
        get_job(session, seller_id, job_id)
        ensure_permitted(cfg, seller)
        state = load_state(session, cipher, seller_id)
        if state is None:
            raise BrowserNotPermitted(
                "No saved Seller Central session. Sign in yourself with: "
                "python -m app.cli browser-login --seller-account <id>"
            )
        targets = _targets(session, job_id, limit)
        audit.record(
            session,
            seller_account_id=seller_id,
            job_id=job_id,
            event_type="BROWSER_RUN_STARTED",
            targets=len(targets),
            agent=agent_token(cfg),
        )
    if not targets:
        return stats

    observer = f"browser-agent:{agent_token(cfg)}"
    final_state = None
    with seller_browser_lock(seller_id), factory.open(state) as handle:
        page = SafePage(
            handle.page,
            allowed_host=urlparse(cfg.browser_listing_url).hostname,
            timeout_ms=cfg.browser_timeout_ms,
        )
        verifier = SellerCentralVerifier(
            page,
            listing_url=cfg.browser_listing_url,
            max_attempts=cfg.browser_max_attempts,
            screenshots=cfg.browser_screenshots,
        )
        for index, (check_id, asin, condition) in enumerate(targets):
            if index:  # a person's pace, with jitter; never a tight loop
                sleep(cfg.browser_min_interval_seconds * (0.75 + 0.5 * jitter()))
            stats.considered += 1
            try:
                result = verifier.verify(asin, condition)
            except HumanRequired as human:
                stats.stopped = human.kind
                _record_stop(cfg, job_id, check_id, seller_id, observer, human.kind)
                break
            _record_result(cfg, job_id, check_id, observer, result, stats)
        else:
            final_state = handle.state()  # keep the session fresh only after a clean run
    if final_state is not None:
        with session_scope() as session:
            save_state(session, cipher, seller_id, final_state)
    return stats


def _record_result(cfg, job_id, check_id, observer, result, stats: RunStats) -> None:
    extra = {"agent": agent_token(cfg), "url_path": result.url_path}
    evidence_ref = None
    if result.screenshot:
        evidence_ref, digest = _store_screenshot(cfg, job_id, check_id, result.screenshot)
        extra["screenshot_sha256"] = digest
    with session_scope() as session:
        check = session.get(EligibilityCheck, check_id)
        _, new = review.record_screen_evidence(
            session,
            check,
            source=ObservationSource.BROWSER,
            observer=observer,
            signals=result.signals,
            outcome=result.outcome,
            error_detail=result.error_detail,
            evidence_ref=evidence_ref,
            extra_raw=extra,
        )
    if result.outcome != ObservationOutcome.OK:
        stats.failed += 1
    elif new == EligibilityStatus.NEEDS_MANUAL_CHECK:
        stats.unresolved += 1
    else:
        stats.resolved += 1


def _record_stop(cfg, job_id, check_id, seller_id, observer, kind: str) -> None:
    with session_scope() as session:
        check = session.get(EligibilityCheck, check_id)
        if kind == "signed_out":
            review.record_screen_evidence(
                session,
                check,
                source=ObservationSource.BROWSER,
                observer=observer,
                signals=None,
                outcome=ObservationOutcome.SESSION_EXPIRED,
                error_detail="Seller Central asked to sign in",
                extra_raw={"agent": agent_token(cfg)},
            )
        else:
            review.record_screen_evidence(
                session,
                check,
                source=ObservationSource.BROWSER,
                observer=observer,
                signals=BrowserSignals(challenge_detected=True),
                extra_raw={"agent": agent_token(cfg)},
            )
        audit.record(
            session,
            seller_account_id=seller_id,
            job_id=job_id,
            event_type="BROWSER_SESSION_STOPPED",
            reason=kind,
            detail="A person must complete the prompt; the agent did not interact with it.",
        )


def login_interactively(
    *,
    seller_id: uuid.UUID,
    cfg: Settings,
    cipher: CredentialCipher,
    factory,
    wait_for_person: Callable[[object], None],
) -> None:
    """A *person* signs in (password, MFA, CAPTCHA) in a visible browser. This function opens the
    page and later saves the resulting session; it never types credentials or touches a form."""
    with session_scope() as session:
        seller = session.get(SellerAccount, seller_id)
        ensure_permitted(cfg, seller)
        state = load_state(session, cipher, seller_id)
    with factory.open(state) as handle:
        handle.page.goto(cfg.browser_listing_url)
        wait_for_person(handle)
        new_state = handle.state()
    with session_scope() as session:
        save_state(session, cipher, seller_id, new_state)
        audit.record(session, seller_account_id=seller_id, event_type="BROWSER_SESSION_SAVED")
