"""Bridge between stored evidence rows and the rules engine."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.classification import Classification, classify_check
from app.domain.observations import (
    BrowserSignals,
    Observation,
    ObservationContext,
    SpApiSignals,
    utcnow,
)
from app.enums import CheckState, ObservationSource
from app.models import CheckObservation, EligibilityCheck


def observation_from_row(row: CheckObservation) -> Observation:
    return Observation(
        source=row.source,
        outcome=row.outcome,
        context=ObservationContext.model_validate(row.context),
        sp_api=(
            SpApiSignals.model_validate(row.signals)
            if row.signals and row.source == ObservationSource.SP_API_RESTRICTIONS
            else None
        ),
        browser=(
            BrowserSignals.model_validate(row.signals)
            if row.signals and row.source != ObservationSource.SP_API_RESTRICTIONS
            else None
        ),
        http_status=row.http_status,
        error_detail=row.error_detail,
        observed_at=row.observed_at,
        evidence_ref=row.evidence_ref,
        raw=row.raw,
    )


def add_observation(
    session: Session, check: EligibilityCheck, obs: Observation
) -> CheckObservation:
    row = CheckObservation(
        check_id=check.id,
        seller_account_id=check.seller_account_id,
        source=obs.source,
        outcome=obs.outcome,
        http_status=obs.http_status,
        error_detail=obs.error_detail,
        context=obs.context.model_dump(),
        signals=(obs.sp_api or obs.browser).model_dump() if (obs.sp_api or obs.browser) else None,
        raw=obs.raw,
        evidence_ref=obs.evidence_ref,
        observed_at=obs.observed_at,
    )
    session.add(row)
    session.flush()
    if row.evidence_ref is None:
        row.evidence_ref = f"db:check_observations/{row.id}"
    return row


def apply_classification(check: EligibilityCheck, result: Classification) -> None:
    check.state = CheckState.DONE
    check.status = result.status
    check.rule_id = result.rule_id
    check.rationale = result.rationale
    check.rules_version = result.rules_version
    check.reason_codes = list(result.reason_codes)
    check.messages = list(result.messages)
    check.approval_links = [link.model_dump() for link in result.approval_links]
    check.verification_methods = [m.value for m in result.verification_methods]
    check.checked_at = utcnow()


def reclassify_check(session: Session, check: EligibilityCheck) -> Classification:
    """Re-derive a check's status from every stored observation (also used to re-derive
    old results after a RULES_VERSION change)."""
    rows = session.scalars(
        select(CheckObservation)
        .where(CheckObservation.check_id == check.id)
        .order_by(CheckObservation.observed_at, CheckObservation.id)
    ).all()
    result = classify_check([observation_from_row(r) for r in rows])
    apply_classification(check, result)
    return result
