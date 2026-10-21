"""Persisting evidence and deriving a check's status from it.

There is exactly one path from evidence to status: observations are appended, then the
rules engine (app.domain.classification) is run over all of a check's observations.
Nothing in the orchestrator assigns a status directly.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.classification import Classification, classify_check
from app.domain.observations import Observation, ObservationContext, SpApiSignals
from app.enums import CheckState
from app.models import CheckObservation, EligibilityCheck


def store_observation(
    session: Session, check: EligibilityCheck, obs: Observation
) -> CheckObservation:
    row = CheckObservation(
        check_id=check.id,
        seller_account_id=check.seller_account_id,
        source=obs.source,
        outcome=obs.outcome,
        http_status=obs.http_status,
        error_detail=obs.error_detail,
        context=obs.context.model_dump(mode="json"),
        signals=obs.sp_api.model_dump(mode="json") if obs.sp_api is not None else None,
        raw=obs.raw,
        evidence_ref=obs.evidence_ref,
        observed_at=obs.observed_at,
    )
    session.add(row)
    session.flush()
    return row


def load_observations(session: Session, check_id) -> list[Observation]:
    rows = session.scalars(
        select(CheckObservation)
        .where(CheckObservation.check_id == check_id)
        .order_by(CheckObservation.observed_at, CheckObservation.id)
    ).all()
    return [
        Observation(
            source=r.source,
            outcome=r.outcome,
            context=ObservationContext.model_validate(r.context),
            sp_api=SpApiSignals.model_validate(r.signals) if r.signals is not None else None,
            http_status=r.http_status,
            error_detail=r.error_detail,
            observed_at=r.observed_at,
            evidence_ref=r.evidence_ref,
            raw=r.raw,
        )
        for r in rows
    ]


def apply_classification(check: EligibilityCheck, result: Classification, now: datetime) -> None:
    check.state = CheckState.DONE
    check.status = result.status
    check.rule_id = result.rule_id
    check.rationale = result.rationale
    check.rules_version = result.rules_version
    check.reason_codes = list(result.reason_codes)
    check.messages = list(result.messages)
    check.approval_links = [link.model_dump(mode="json") for link in result.approval_links]
    check.verification_methods = [m.value for m in result.verification_methods]
    check.checked_at = now


def reset_check(check: EligibilityCheck) -> None:
    """Return a finished check to PENDING so it is asked again. Its observations stay."""
    check.state = CheckState.PENDING
    check.status = None
    check.rule_id = None
    check.rationale = None
    check.rules_version = None
    check.reason_codes = []
    check.messages = []
    check.approval_links = []
    check.verification_methods = []
    check.checked_at = None


def classify_and_store(session: Session, check: EligibilityCheck, now: datetime) -> Classification:
    result = classify_check(load_observations(session, check.id))
    apply_classification(check, result, now)
    return result
