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

from app.domain.brand import brands_equivalent
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
    # For an ambiguous verdict: True if what was seen suggests a restriction may exist (so it
    # contradicts a "listable" answer from another source); False if it merely carries no
    # information about this ASIN (wrong product, condition not chosen, ASIN unknown).
    signals_restriction: bool = True
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
            signals_restriction=False,
        )
    if any((r.code or "").strip().upper() == "ASIN_NOT_FOUND" for r in reasons):
        return _ambiguous(
            obs,
            "SPAPI_ASIN_NOT_FOUND",
            "Catalog returned this ASIN but the restrictions API does not recognise it "
            "in this marketplace.",
            reason_codes=codes,
            messages=messages,
            signals_restriction=False,
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


# ---- browser / human-entered evidence --------------------------------------------------------

_SELL_LABELS = {"sell this product"}
_APPLY_LABELS = {"apply to sell"}
_APPROVAL_NOTICE_RE = re.compile(r"\bapproval\b", re.IGNORECASE)
_NOT_ELIGIBLE_NOTICE_RE = re.compile(
    r"\b(not eligible|cannot be listed|can't be listed|unable to list"
    r"|not allowed to (list|sell))\b",
    re.IGNORECASE,
)
# "Used, Collectible, Refurbished conditions: This product has other listing limitations."
_CONDITION_NOTICE_RE = re.compile(
    r"^\s*(?P<families>[A-Za-z][A-Za-z ,&/-]*?)\s+conditions?\s*:", re.IGNORECASE
)
_FAMILIES = ("new", "used", "refurbished", "collectible", "club")


def _condition_family(condition: str) -> str:
    return condition.split("_", 1)[0].casefold()


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _notice_status(notice: str) -> EligibilityStatus | None:
    """Status for a notice that says the product is blocked; None if it is not that kind."""
    if _NOT_ELIGIBLE_NOTICE_RE.search(notice):
        return EligibilityStatus.NOT_ELIGIBLE
    if _APPROVAL_NOTICE_RE.search(notice):
        if approval_scope(notice) == "BRAND":
            return EligibilityStatus.BRAND_APPROVAL_REQUIRED
        return EligibilityStatus.OTHER_APPROVAL_REQUIRED
    return None


def _limitation_families(notice: str) -> set[str] | None:
    """Condition families a "<X> conditions: ..." notice is about, or None if it is not one."""
    match = _CONDITION_NOTICE_RE.match(notice)
    if not match:
        return None
    named = {
        word.casefold()
        for word in re.split(r"[,&/ ]+", match.group("families"))
        if word and word.casefold() in _FAMILIES
    }
    return named or None


def classify_browser(obs: Observation) -> Verdict:
    """Rules for what Seller Central showed (read by the adapter or entered by a reviewer).

    Absence of an approval message proves nothing by itself: listing is confirmed only when the
    right product is on screen, the right condition is selected, the sell control is enabled,
    and no notice anywhere in the panel says otherwise.
    """
    ctx = obs.context
    if obs.outcome != ObservationOutcome.OK:
        return _missing(
            obs,
            f"BROWSER_{obs.outcome.value}",
            f"Seller Central gave no usable answer ({obs.outcome.value}"
            + (f": {obs.error_detail}" if obs.error_detail else "")
            + ").",
        )
    sig = obs.browser
    if sig is None:
        return _missing(obs, "BROWSER_NO_SIGNALS", "No screen observations were recorded.")
    if not sig.signed_in:
        return _missing(obs, "BROWSER_SESSION_EXPIRED", "The Seller Central session is signed out.")
    if sig.challenge_detected:
        return _missing(
            obs,
            "BROWSER_CHALLENGE",
            "A sign-in, CAPTCHA or verification challenge appeared; a person must complete it.",
        )
    if not sig.page_loaded:
        return _missing(obs, "BROWSER_PAGE_NOT_LOADED", "The product panel did not finish loading.")
    if sig.product_unavailable:
        return _ambiguous(
            obs, "BROWSER_PRODUCT_UNAVAILABLE", "Seller Central shows the product as unavailable."
        )

    # Is this even the product that was asked about?
    if _norm(sig.page_asin) != _norm(ctx.asin):
        return _ambiguous(
            obs,
            "BROWSER_ASIN_MISMATCH" if sig.page_asin else "BROWSER_ASIN_UNCONFIRMED",
            f"The panel shows ASIN {sig.page_asin or 'none'}, not {ctx.asin}; "
            "it is not evidence about this product.",
            signals_restriction=False,
        )
    if sig.page_brand:
        if ctx.expected_brand and not brands_equivalent(sig.page_brand, ctx.expected_brand):
            return _ambiguous(
                obs,
                "BROWSER_BRAND_MISMATCH",
                f"The panel shows brand {sig.page_brand!r}, not {ctx.expected_brand!r}.",
                signals_restriction=False,
            )
    elif not ctx.brand_validated:
        return _ambiguous(
            obs,
            "BROWSER_BRAND_UNCONFIRMED",
            "The product's brand could not be confirmed on screen or in the catalog.",
            signals_restriction=False,
        )

    notices = [n.strip() for n in sig.notices if n and n.strip()]
    blocked = [(n, _notice_status(n)) for n in notices]
    blockers = {st for _, st in blocked if st is not None}
    for status in _BLOCKER_PRECEDENCE:
        if status in blockers:
            said = [n for n, st in blocked if st is not None]
            return Verdict(
                status=status,
                rule_id=f"BROWSER_{status.value}",
                rationale="Seller Central showed: " + " | ".join(f"'{n}'" for n in said),
                source=obs.source,
                messages=said,
            )

    if _norm(sig.selected_condition) != _norm(ctx.condition):
        return _ambiguous(
            obs,
            "BROWSER_CONDITION_NOT_SELECTED",
            f"Condition {ctx.condition} is not selected "
            f"(shown: {sig.selected_condition or 'none'}), so the sell control says nothing yet.",
            signals_restriction=False,
        )

    target_family = _condition_family(ctx.condition)
    for notice in notices:
        families = _limitation_families(notice)
        if families is None:
            return _ambiguous(
                obs,
                "BROWSER_UNRECOGNIZED_NOTICE",
                f"Unrecognised notice in the panel: '{notice}'.",
                messages=[notice],
            )
        if target_family in families:
            return _ambiguous(
                obs,
                "BROWSER_LISTING_LIMITATION",
                f"A listing limitation applies to the {target_family} condition: '{notice}'.",
                messages=[notice],
            )
        # otherwise it is about other conditions and does not apply to this check

    label = _norm(sig.action_label)
    if label in _APPLY_LABELS:
        return Verdict(
            status=EligibilityStatus.OTHER_APPROVAL_REQUIRED,
            rule_id="BROWSER_APPLY_TO_SELL",
            rationale="Seller Central offers 'Apply to sell' instead of 'Sell this product', "
            "so approval is needed; no message said whether it is for the brand.",
            source=obs.source,
        )
    if label in _SELL_LABELS:
        if sig.action_enabled is True:
            return Verdict(
                status=EligibilityStatus.VERIFIED_LISTABLE,
                rule_id="BROWSER_SELL_ENABLED",
                rationale=(
                    f"For ASIN {ctx.asin}, condition {ctx.condition}, 'Sell this product' is "
                    "enabled and no restriction notice applies."
                ),
                source=obs.source,
            )
        return _ambiguous(
            obs,
            "BROWSER_ACTION_DISABLED",
            "'Sell this product' is shown but is not enabled; listing is not confirmed.",
        )
    return _ambiguous(
        obs,
        "BROWSER_NO_RECOGNISED_ACTION",
        f"No recognised sell control was found (saw {sig.action_label!r}).",
    )


_CLASSIFIERS: dict[ObservationSource, Callable[[Observation], Verdict]] = {
    ObservationSource.SP_API_RESTRICTIONS: classify_sp_api,
    ObservationSource.BROWSER: classify_browser,
    ObservationSource.MANUAL: classify_browser,  # a reviewer records the same screen state
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

    # Ambiguity that carries no information about this ASIN (wrong product on screen, condition
    # not chosen, ASIN unknown to the API) is not a contradiction. Drop it if anything informs.
    informative = [
        v
        for v in usable
        if v.status != EligibilityStatus.NEEDS_MANUAL_CHECK or v.signals_restriction
    ]
    if informative:
        usable = informative

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
    if winner_status == EligibilityStatus.NEEDS_MANUAL_CHECK and len(chosen) > 1:
        # Still unresolved after several sources: lead with the most recent evidence, which is
        # what a reviewer needs to see ("why was my entry not enough?"), and keep every reason.
        why = " | ".join(dict.fromkeys(v.rationale for v in reversed(chosen)))
        return _merge(chosen, winner_status, chosen[-1].rule_id, why)
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
