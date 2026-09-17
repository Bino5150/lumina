"""
core/generation_spending_policy.py -- MULTIMODAL-M4-GENERATION-SUBSTRATE-DESIGN-01

Owner spending policy for generation jobs. Pure, stdlib-only, zero-I/O --
same discipline as core.capability_router.resolve_capability(): identical
inputs produce identical decisions, and the decision always carries a
human-readable reason. Mirrors RoutingPolicy's own register (an owner
declares this once; nothing here infers or negotiates a policy on its
own).

Deliberately NOT a per-call confirmation modal. A single-job ceiling plus
a session ceiling, evaluated against a cost estimate, lets a
pre-authorized cheap generation just happen while an expensive one stops
at the gate for explicit approval -- the same owner-declares-the-ceiling-
once shape as M1's disabled_providers/quality_order, rather than a new and
unrelated approval mechanism. See
MULTIMODAL_M4_IMAGE_GENERATION_SOURCE_VET_2026-09-17.md Sec 6 item 3.

An unknown cost (the adapter's estimate_cost() returned None) is never
silently treated as free. require_estimate + fail_closed_on_unknown_cost
together decide whether that case is a hard denial or a forced approval
prompt; there is no configuration of this policy under which an unknown
cost resolves to ALLOWED.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "SpendingPolicy",
    "SpendingDecision",
    "OUTCOME_ALLOWED",
    "OUTCOME_REQUIRES_APPROVAL",
    "OUTCOME_DENIED",
    "evaluate_spend",
]

OUTCOME_ALLOWED = "allowed"
OUTCOME_REQUIRES_APPROVAL = "requires_approval"
OUTCOME_DENIED = "denied"


@dataclass(frozen=True)
class SpendingPolicy:
    """Owner-declared spending policy for one capability's generation
    lane (one instance per capability, the same granularity as M1's
    per-capability CapabilityRoute).

    require_estimate:
        If True, a job with no cost estimate available cannot be ALLOWED
        outright -- see fail_closed_on_unknown_cost for what happens
        instead. If False, an unavailable estimate is treated as
        acceptable (the owner has explicitly opted out of requiring one).
    single_job_ceiling / session_ceiling:
        None = unconstrained for that dimension. Both may be set; either
        one being exceeded is sufficient to require approval.
    fail_closed_on_unknown_cost:
        Only consulted when require_estimate is True and no estimate is
        available. True (default) -> DENIED outright. False ->
        REQUIRES_APPROVAL instead of an outright refusal. Never ALLOWED.
    """

    require_estimate: bool = True
    single_job_ceiling: Optional[float] = None
    session_ceiling: Optional[float] = None
    fail_closed_on_unknown_cost: bool = True

    def __post_init__(self):
        for name in ("single_job_ceiling", "session_ceiling"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise ValueError(f"SpendingPolicy.{name} must be a non-negative number or None")


@dataclass(frozen=True)
class SpendingDecision:
    outcome: str
    reason: str


def evaluate_spend(policy: SpendingPolicy, cost_estimate: Optional[float],
                    session_spent: float = 0.0) -> SpendingDecision:
    """PURE: no I/O, no mutation, no clock or randomness. Identical inputs
    -> identical decision. Does not know about GenerationJob, credentials,
    or any adapter -- callers evaluate this BEFORE submitting and act on
    the outcome (submit / prompt the owner / refuse) themselves."""
    if not isinstance(session_spent, (int, float)) or session_spent < 0:
        raise ValueError("session_spent must be a non-negative number")

    if cost_estimate is None:
        if not policy.require_estimate:
            return SpendingDecision(
                OUTCOME_ALLOWED,
                "no cost estimate available; policy does not require one",
            )
        if policy.fail_closed_on_unknown_cost:
            return SpendingDecision(
                OUTCOME_DENIED,
                "cost estimate required by policy but unavailable; failing closed rather than "
                "treating an unknown cost as free",
            )
        return SpendingDecision(
            OUTCOME_REQUIRES_APPROVAL,
            "cost estimate required by policy but unavailable; owner approval required",
        )

    if not isinstance(cost_estimate, (int, float)) or cost_estimate < 0:
        raise ValueError("cost_estimate must be a non-negative number or None")

    if policy.single_job_ceiling is not None and cost_estimate > policy.single_job_ceiling:
        return SpendingDecision(
            OUTCOME_REQUIRES_APPROVAL,
            f"estimated cost {cost_estimate} exceeds single-job ceiling {policy.single_job_ceiling}",
        )
    projected = session_spent + cost_estimate
    if policy.session_ceiling is not None and projected > policy.session_ceiling:
        return SpendingDecision(
            OUTCOME_REQUIRES_APPROVAL,
            f"estimated cost {cost_estimate} plus session spend {session_spent} = {projected} "
            f"would exceed session ceiling {policy.session_ceiling}",
        )
    return SpendingDecision(OUTCOME_ALLOWED, "within policy ceilings")
