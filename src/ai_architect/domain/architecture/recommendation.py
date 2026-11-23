"""Structured, unevaluated diagnostic recommendations for actionable
quality-gate failures.

Recommendations describe what *could* change and why, each tied to an
actual gate or critic finding. They are explicitly unevaluated: nothing
here is priced, composed, or gated, and no recommendation represents a
feasible, affordable, or budget-compliant architecture.

This module is additive: it does not modify the quality gate, the
critic, pricing, component selection, architecture validation,
approvals, or Terraform behavior.

Gate semantics: under the current quality gate a BLOCKED verdict is
produced by a budget-ceiling breach, a missing architecture artifact, or
a missing/stale critique. The latter two cases are filtered below and
yield no recommendations, so every recommendation the production
pipeline can generate is budget-breach driven. Critical critic findings
never coexist with a BLOCKED verdict — the gate routes them to
REQUIRES_REVISION or REQUIRES_USER_DECISION before the ceiling check —
so the critical-finding branch below is defensive: it keeps the
generator total over its input space if gate ordering ever changes, and
is exercised only by synthetic tests.
"""

from hashlib import sha256
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.architecture.critique import (
    DesignCritique,
    QualityGateResult,
)
from ai_architect.domain.cost.models import CostEstimationResult

DISCLAIMER = (
    "This recommendation has not been priced, composed, or gated. "
    "It does not represent a feasible, affordable, or "
    "budget-compliant architecture."
)

# Gate reasons that indicate the run never produced an evaluable
# design. These are not remediable by swapping components, so no
# recommendations are generated for them.
_MISSING_ARCHITECTURE = "no architecture artifact exists to evaluate"
_MISSING_CRITIQUE = "no design critique exists for this architecture"
_STALE_CRITIQUE = "the stored critique reviews a different architecture"

# Critic categories that map to a concrete component change.
_COMPONENT_SWAP_CATEGORIES = {"spof", "security", "durability"}
_CONFIGURATION_CATEGORIES = {
    "capacity_headroom",
    "scaling_cliff",
    "observability",
    "cost_ceiling",
}


class OptimizationRecommendation(BaseModel):
    """One unevaluated diagnostic recommendation tied to a finding."""

    recommendation_id: str = Field(
        description="Deterministic identifier derived from the finding."
    )
    derived_from_artifact_id: str = Field(
        description="Architecture artifact this recommendation responds to."
    )
    finding_category: str = Field(
        description="Gate or critic finding category motivating the recommendation."
    )
    finding_statement: str = Field(
        description="Statement of the problem, verbatim from the finding where possible."
    )
    proposed_change: str = Field(
        description="Kind of change suggested: component_swap, configuration, removal, or requirement_clarification."
    )
    target_component: Optional[str] = Field(
        default=None,
        description="Component named by the finding, if any."
    )
    rationale: str = Field(
        description="Why the change may address the finding."
    )
    affects_requirement: Optional[str] = Field(
        default=None,
        description="Requirement key the change would touch, if explicitly stated."
    )
    requirement_change_note: Optional[str] = Field(
        default=None,
        description="User-visible consequence if a requirement must change."
    )
    evaluated: bool = Field(
        default=False,
        description="Always False in this slice: recommendations are never evaluated."
    )
    status: str = Field(
        default="unevaluated",
        description="Lifecycle status; never 'feasible', 'affordable', or 'budget-compliant'."
    )
    disclaimer: str = Field(
        default=DISCLAIMER,
        description="Fixed user-facing notice that the recommendation is unevaluated."
    )

    model_config = ConfigDict(extra="forbid")


def _recommendation_id(
    derived_from: str, category: str, target: Optional[str], statement: str
) -> str:
    """Deterministic id so regenerating recommendations is idempotent."""
    payload = f"{derived_from}|{category}|{target or ''}|{statement}"
    return "rec-" + sha256(payload.encode("utf-8")).hexdigest()[:24]


def _proposed_change_for_category(category: str) -> str:
    if category in _COMPONENT_SWAP_CATEGORIES:
        return "component_swap"
    if category in _CONFIGURATION_CATEGORIES:
        return "configuration"
    return "configuration"


def _requirement_for_category(category: str) -> Optional[str]:
    # The requirements schema has no mandatory-vs-preference distinction,
    # so no requirement is ever assumed mandatory here. Callers must not
    # silently weaken any stated constraint.
    return None


def _reasons_text(reasons) -> str:
    return " ".join(str(r) for r in (reasons or [])).lower()


def _highest_cost_priced_component(
    cost_result: Optional[CostEstimationResult],
    architecture: Optional[ArchitectureResult],
) -> Optional[str]:
    """Return the component name with the largest priced monthly cost."""
    best_name: Optional[str] = None
    best_cost = 0.0

    def _consider(name: Optional[str], cost_usd: object) -> None:
        nonlocal best_name, best_cost
        if not name:
            return
        try:
            cost = float(cost_usd or 0.0)
        except (TypeError, ValueError):
            return
        if cost > best_cost:
            best_cost = cost
            best_name = str(name)

    lines = getattr(cost_result, "lines", None) or []
    for line in lines:
        _consider(getattr(line, "component_name", None), getattr(line, "monthly_cost_usd", None))

    links = getattr(architecture, "cost_links", None) or []
    for link in links:
        if not getattr(link, "priced", False):
            continue
        _consider(
            getattr(link, "component_name", None),
            getattr(link, "monthly_cost_usd", None),
        )

    return best_name


def generate_recommendations(
    architecture: Optional[ArchitectureResult],
    quality_gate: Optional[QualityGateResult],
    critique: Optional[DesignCritique],
    cost_result: Optional[CostEstimationResult],
) -> list[dict]:
    """Generate unevaluated diagnostic recommendations for a blocked gate.

    Returns an empty list when the gate is not blocked, when the block is
    caused by missing architecture or a missing/stale critique, or when
    no actionable finding can be mapped to a component.

    In production the current gate reaches BLOCKED only via a
    budget-ceiling breach, so a non-empty result is budget-breach driven;
    the critique handling below is defensive (see module docstring).
    """
    if quality_gate is None:
        return []
    outcome = str(getattr(quality_gate, "outcome", "") or "")
    if outcome != "blocked":
        return []

    reasons = list(getattr(quality_gate, "reasons", []) or [])
    text = _reasons_text(reasons)

    # Missing architecture / missing or stale critique: not remediable by
    # swapping components; do not generate recommendations.
    if _MISSING_ARCHITECTURE in text:
        return []
    if _MISSING_CRITIQUE in text:
        return []
    if _STALE_CRITIQUE in text:
        return []

    derived_from = (
        str(getattr(architecture, "artifact_id", "") or "")
        if architecture is not None
        else ""
    )

    recommendations: list[dict] = []

    # Budget breach: target the highest-cost priced component. The ceiling
    # itself is never relaxed; the recommendation only suggests a component
    # swap and remains unevaluated.
    if any("ceiling breached" in r or "exceeds the" in r for r in reasons):
        target = _highest_cost_priced_component(cost_result, architecture)
        if target:
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id=_recommendation_id(
                        derived_from, "cost_ceiling", target, "budget ceiling breach"
                    ),
                    derived_from_artifact_id=derived_from,
                    finding_category="cost_ceiling",
                    finding_statement=(
                        "Priced totals exceed the stated monthly budget ceiling."
                    ),
                    proposed_change="component_swap",
                    target_component=target,
                    rationale=(
                        f"Replacing {target} with a lower-cost alternative may bring the "
                        "priced subtotal within the budget ceiling; the ceiling itself "
                        "is not relaxed."
                    ),
                    affects_requirement="budget_constraints",
                    requirement_change_note=None,
                ).model_dump(mode="json")
            )

    # Critical critic findings: target the named component when present.
    # Defensive branch: a BLOCKED verdict never carries critical findings
    # under the current gate (criticals route to REQUIRES_REVISION or
    # REQUIRES_USER_DECISION before the ceiling check). Kept so this
    # function stays correct for every gate outcome it could receive.
    if critique is not None:
        for finding in getattr(critique, "findings", []) or []:
            if str(getattr(finding, "severity", "")) != "critical":
                continue
            statement = str(getattr(finding, "statement", "") or "")
            category = str(getattr(finding, "category", "") or "")
            subject = (
                str(getattr(finding, "about_component_ref", "") or "")
            ) or None
            target = subject if subject and subject.strip() else None
            recommendations.append(
                OptimizationRecommendation(
                    recommendation_id=_recommendation_id(
                        derived_from, category, target or "", statement
                    ),
                    derived_from_artifact_id=derived_from,
                    finding_category=category,
                    finding_statement=statement,
                    proposed_change=_proposed_change_for_category(category),
                    target_component=target,
                    rationale=(
                        f"Address the {category} finding "
                        f"for {subject or 'the system'}; this may require a "
                        "component change or a clarified requirement."
                    ),
                    affects_requirement=_requirement_for_category(category),
                    requirement_change_note=None,
                ).model_dump(mode="json")
            )

    return recommendations
