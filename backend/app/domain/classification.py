"""Deterministic rules engine: observations in, eligibility status out.

No I/O, no clock, no randomness, no LLM. The same observations always yield the same
status, and every status carries the rule that produced it so results can be audited
and re-derived when RULES_VERSION changes.

The engine never turns missing or ambiguous evidence into VERIFIED_LISTABLE. A product
is listable only when an authorised source gave an affirmative, well-formed answer about
the exact marketplace and condition that was asked, and nothing contradicts it.
"""

import re
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.observations import ApprovalLink, Observation, RestrictionReason
from app.enums import (
    BLOCKING_STATUSES,
    EligibilityStatus,
    ObservationOutcome,
    ObservationSource,
)

RULES_VERSION = "2026.10.1"

_BRAND_RE = re.compile(r"\bbrands?\b", re.IGNORECASE)
_CATEGORY_RE = re.compile(
    r"\b(categor(?:y|ies)|sub-?categor(?:y|ies)|product types?|product groups?)\b", re.IGNORECASE
)

# When several blockers apply, the one that rules out listing most completely wins.
_BLOCKER_PRECEDENCE = (
    EligibilityStatus.NOT_ELIGIBLE,
    EligibilityStatus.BRAND_APPROVAL_REQUIRED,
    EligibilityStatus.OTHER_APPROVAL_REQUIRED,
)


class Verdict(BaseModel):
    """Interpretation of a single observation."""

    status: EligibilityStatus
    rule_id: str
    rationale: str
    source: ObservationSource
    # True when the source produced no usable answer (error, expired session, malformed body).
    # Such verdicts never override a real answer from another source.
    evidence_missing: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    approval_links: list[ApprovalLink] = Field(default_factory=list)


class Classification(BaseModel):
    """Final derived status for one ASIN + condition check."""

    status: EligibilityStatus
    rule_id: str
    rationale: str
    reason_codes: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    approval_links: list[ApprovalLink] = Field(default_factory=list)
    verification_methods: list[ObservationSource] = Field(default_factory=list)
    rules_version: str = RULES_VERSION


def approval_scope(message: str | None) -> Literal["BRAND", "OTHER"]:
    """BRAND only when the message is about the brand and not also about a category.

    "You need approval to list in this brand." -> BRAND
    "You need approval to sell in this category." -> OTHER
    Mentions both, neither, or is empty -> OTHER (approval is still required; we just
    do not claim to know it is the brand that is gated).
    """
    if not message:
        return "OTHER"
    if _BRAND_RE.search(message) and not _CATEGORY_RE.search(message):
        return "BRAND"
    return "OTHER"


def _missing(obs: Observation, rule_id: str, rationale: str) -> Verdict:
    return Verdict(
        status=EligibilityStatus.NEEDS_MANUAL_CHECK,
        rule_id=rule_id,
        rationale=rationale,
        source=obs.source,
        evidence_missing=True,
    )


def _ambiguous(obs: Observation, rule_id: str, rationale: str, **extra) -> Verdict:
    return Verdict(
        status=EligibilityStatus.NEEDS_MANUAL_CHECK,
        rule_id=rule_id,
        rationale=rationale,
        source=obs.source,
        **extra,
    )


def _classify_reason(reason: RestrictionReason) -> EligibilityStatus | None:
    """Status for one reason, or None if the code is not one we recognise as a blocker."""
    code = (reason.code or "").strip().upper()
    if code == "NOT_ELIGIBLE":
        return EligibilityStatus.NOT_ELIGIBLE
    if code == "APPROVAL_REQUIRED":
        if approval_scope(reason.message) == "BRAND":
            return EligibilityStatus.BRAND_APPROVAL_REQUIRED
        return EligibilityStatus.OTHER_APPROVAL_REQUIRED
    return None


def classify_sp_api(obs: Observation) -> Verdict:
    source = ObservationSource.SP_API_RESTRICTIONS
    if obs.outcome != ObservationOutcome.OK:
        return _missing(
            obs,
            f"SPAPI_{obs.outcome.value}",
            f"getListingsRestrictions gave no usable answer ({obs.outcome.value}"
            + (f": {obs.error_detail}" if obs.error_detail else "")
            + ").",
        )
    signals = obs.sp_api
    if signals is None or not signals.restrictions_present:
        return _missing(
            obs,
            "SPAPI_NO_RESTRICTIONS_FIELD",
            "Response had no well-formed 'restrictions' list; "
            "missing data is not permission to sell.",
        )

    ctx = obs.context
    applicable = [
        e
        for e in signals.entries
        if (e.marketplace_id is None or e.marketplace_id == ctx.marketplace_id)
        and (e.condition_type is None or e.condition_type == ctx.condition)
    ]
    foreign = len(signals.entries) - len(applicable)
    reasons = [r for e in applicable for r in e.reasons]

    classified = [(r, _classify_reason(r)) for r in reasons]
    blockers = {s for _, s in classified if s is not None}
    codes = [r.code for r, _ in classified if r.code]
    messages = [r.message for r, _ in classified if r.message]
    links = [link for r, _ in classified for link in r.links]

    for status in _BLOCKER_PRECEDENCE:
        if status in blockers:
            return Verdict(
                status=status,
                rule_id=f"SPAPI_{status.value}",
                rationale=f"getListingsRestrictions reported {', '.join(sorted(set(codes)))}.",
                source=source,
                reason_codes=codes,
                messages=messages,
                approval_links=links,
            )

    if not signals.entries:
        return Verdict(
            status=EligibilityStatus.VERIFIED_LISTABLE,
            rule_id="SPAPI_NO_RESTRICTIONS",
            rationale=(
                f"getListingsRestrictions returned an empty restrictions list for ASIN {ctx.asin}, "
                f"condition {ctx.condition}, marketplace {ctx.marketplace_id}."
            ),
            source=source,
        )
    if foreign and not applicable:
        return _ambiguous(
            obs,
            "SPAPI_CONTEXT_MISMATCH",
            "Response described a different marketplace or condition than the one requested.",
        )
    if any((r.code or "").strip().upper() == "ASIN_NOT_FOUND" for r in reasons):
        return _ambiguous(
            obs,
            "SPAPI_ASIN_NOT_FOUND",
            "Catalog returned this ASIN but the restrictions API does not recognise it "
            "in this marketplace.",
            reason_codes=codes,
            messages=messages,
        )
    if not reasons:
        return _ambiguous(
            obs,
            "SPAPI_EMPTY_RESTRICTION_ENTRY",
            "Restriction entry present but it carries no reason; cannot tell what it means.",
        )
    return _ambiguous(
        obs,
        "SPAPI_UNRECOGNIZED_REASON",
        f"Unrecognised restriction reason code(s): {', '.join(sorted(set(codes))) or 'none'}.",
        reason_codes=codes,
        messages=messages,
        approval_links=links,
    )


_CLASSIFIERS: dict[ObservationSource, Callable[[Observation], Verdict]] = {
    ObservationSource.SP_API_RESTRICTIONS: classify_sp_api,
}


def classify_observation(obs: Observation) -> Verdict:
    classifier = _CLASSIFIERS.get(obs.source)
    if classifier is None:
        return _missing(obs, "NO_CLASSIFIER", f"No rules exist for source {obs.source.value}.")
    return classifier(obs)


def _merge(verdicts: Sequence[Verdict], status: EligibilityStatus, rule_id: str, why: str):
    codes: list[str] = []
    messages: list[str] = []
    links: list[ApprovalLink] = []
    methods: list[ObservationSource] = []
    for v in verdicts:
        codes += [c for c in v.reason_codes if c not in codes]
        messages += [m for m in v.messages if m not in messages]
        links += [link for link in v.approval_links if link not in links]
        if v.source not in methods:
            methods.append(v.source)
    return Classification(
        status=status,
        rule_id=rule_id,
        rationale=why,
        reason_codes=codes,
        messages=messages,
        approval_links=links,
        verification_methods=methods,
    )


def combine_verdicts(verdicts: Sequence[Verdict]) -> Classification:
    """Combine one verdict per evidence source into the status of the check."""
    if not verdicts:
        return Classification(
            status=EligibilityStatus.NEEDS_MANUAL_CHECK,
            rule_id="NO_EVIDENCE",
            rationale="No observations have been recorded for this check.",
        )

    usable = [v for v in verdicts if not v.evidence_missing]
    if not usable:
        first = verdicts[0]
        return _merge(
            verdicts,
            EligibilityStatus.NEEDS_MANUAL_CHECK,
            first.rule_id,
            "; ".join(dict.fromkeys(v.rationale for v in verdicts)),
        )

    statuses = {v.status for v in usable}
    if EligibilityStatus.VERIFIED_LISTABLE in statuses and len(statuses) > 1:
        # One source says "listable" while another says blocked or unsure: never pick a side.
        return _merge(
            usable,
            EligibilityStatus.NEEDS_MANUAL_CHECK,
            "SOURCE_CONFLICT",
            "Evidence sources disagree or are ambiguous: "
            + "; ".join(f"{v.source.value}={v.status.value}" for v in usable)
            + ".",
        )

    blockers = statuses & BLOCKING_STATUSES
    if blockers:
        winner = next(s for s in _BLOCKER_PRECEDENCE if s in blockers)
        chosen = [v for v in usable if v.status == winner]
        return _merge(chosen, winner, chosen[0].rule_id, chosen[0].rationale)

    winner_status = next(iter(statuses))
    chosen = [v for v in usable if v.status == winner_status]
    return _merge(chosen, winner_status, chosen[0].rule_id, chosen[0].rationale)


def classify_check(observations: Sequence[Observation]) -> Classification:
    """Classify a check from all of its observations.

    Only the latest observation from each source counts, so a later re-check supersedes
    an earlier attempt instead of conflicting with it.
    """
    latest: dict[ObservationSource, Observation] = {}
    for obs in sorted(observations, key=lambda o: o.observed_at):
        latest[obs.source] = obs
    return combine_verdicts([classify_observation(o) for o in latest.values()])
