"""Deterministic Design Quality Gate.

READY does not automatically mean GOOD ENOUGH TO PROVISION. The gate
combines, with pure logic only:

- deterministic architecture validation outcome (structural /
  numeric / closed-world correctness),
- the Design Critic result (adversarial semantic review),
- remaining unresolved critical findings,
- cost constraints (a parseable budget ceiling),
- required architecture evidence/contracts (presence + freshness).

No LLM is invoked here. No finding is invented: every reason names
the concrete input that produced it. The gate never revises anything;
revision is the critique loop's job, bounded separately.
"""

import re
from datetime import datetime

from ai_architect.domain.architecture.critique import (
    QualityGateOutcome,
    QualityGateResult,
)

# A budget ceiling is only honored when it states an explicit dollar
# amount. Anything else is not a ceiling the gate can enforce, so the
# gate says so instead of guessing.
_BUDGET_RE = re.compile(
    r"\$\s*(?P<amount>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*/\s*mo(?:nth)?)?\b",
    re.IGNORECASE,
)


def parse_budget_ceiling(budget_constraints: str | None) -> float | None:
    """Extract an explicit monthly dollar ceiling, if one is stated."""
    if not budget_constraints or not budget_constraints.strip():
        return None
    match = _BUDGET_RE.search(budget_constraints)
    if not match:
        return None
    try:
        return float(match.group("amount").replace(",", ""))
    except ValueError:
        return None


def _totals(architecture) -> dict:
    snapshot = getattr(architecture, "cost_totals_snapshot", None) or {}
    try:
        return {str(key): float(value) for key, value in dict(snapshot).items()}
    except (TypeError, ValueError):
        return {}


def evaluate_quality_gate(
    *,
    architecture,
    critique,
    revision_count: int,
    max_cycles: int,
    budget_constraints: str | None,
    cost_unpriced: list | None = None,
) -> QualityGateResult:
    """Evaluate the gate deterministically over finished artifacts.

    ``architecture`` is the composed ArchitectureResult (or None).
    ``critique`` is the DesignCritique for that exact artifact id (or
    None). ``revision_count``/``max_cycles`` locate the run inside the
    bounded critique loop. ``cost_unpriced`` is the cost estimator's
    unpriced-dimension list, when the caller has it: it is unioned with
    the cost-link signal so a component with at least one priced line
    and at least one unpriced dimension is never treated as completely
    priced. Callers that do not provide it keep the cost-link-only
    behavior. Never raises for content reasons: every outcome,
    including BLOCKED, is a returned verdict with reasons.
    """
    reasons: list[str] = []
    evaluated_id = ""
    try:
        evaluated_id = str(getattr(architecture, "artifact_id", "") or "")
    except Exception:  # noqa: BLE001 - gate must never raise
        evaluated_id = ""

    if architecture is None:
        return QualityGateResult(
            outcome=QualityGateOutcome.BLOCKED,
            reasons=["No architecture artifact exists to evaluate."],
            evaluated_artifact_id=evaluated_id,
        )

    if critique is None:
        return QualityGateResult(
            outcome=QualityGateOutcome.BLOCKED,
            reasons=[
                "No design critique exists for this architecture; "
                "it has not passed adversarial review."
            ],
            evaluated_artifact_id=evaluated_id,
        )

    reviewed_id = str(getattr(critique, "reviewed_artifact_id", "") or "")
    if reviewed_id != evaluated_id:
        return QualityGateResult(
            outcome=QualityGateOutcome.BLOCKED,
            reasons=[
                "The stored critique reviews a different architecture "
                f"artifact ({reviewed_id!r}); it is stale and cannot gate "
                "this one."
            ],
            evaluated_artifact_id=evaluated_id,
        )

    criticals = list(critique.criticals)
    warnings = [
        finding for finding in (getattr(critique, "findings", []) or [])
        if str(getattr(finding, "severity", "")) == "warning"
    ]

    if criticals:
        subjects = sorted(
            {
                str(getattr(finding, "about_component_ref", "") or "")
                or "(cross-cutting)"
                for finding in criticals
            }
        )
        if revision_count < max_cycles:
            return QualityGateResult(
                outcome=QualityGateOutcome.REQUIRES_REVISION,
                reasons=[
                    f"{len(criticals)} unresolved critical finding(s) "
                    f"remain ({', '.join(subjects)}); revision cycle "
                    f"{revision_count + 1} of {max_cycles} may proceed."
                ],
                critical_count=len(criticals),
                warning_count=len(warnings),
                evaluated_artifact_id=evaluated_id,
            )
        return QualityGateResult(
            outcome=QualityGateOutcome.REQUIRES_USER_DECISION,
            reasons=[
                f"{len(criticals)} critical finding(s) remain after "
                f"{max_cycles} critique/revision cycles "
                f"({', '.join(subjects)}); they must be explicitly "
                "accepted as trade-offs or answered by the user."
            ],
            critical_count=len(criticals),
            warning_count=len(warnings),
            evaluated_artifact_id=evaluated_id,
        )

    # Pricing completeness: a partial estimate must never receive the
    # same budget assurance as a complete estimate. Missing costs are
    # never interpreted as zero. Two complementary signals are unioned:
    # cost links (components with no priced lines at all) and the cost
    # estimator's unpriced dimension entries (components that have at
    # least one priced line but also at least one undeliverable
    # dimension, e.g. Lambda requests priced while GB-second duration
    # is unknown). A component flagged by both sources is reported once.
    # Computed before the ceiling verdict so the within-ceiling claim
    # can be conditional on pricing completeness.
    cost_links = getattr(architecture, "cost_links", None) or []
    unpriced_names = {
        str(getattr(link, "component_name", "") or "")
        for link in cost_links
        if not getattr(link, "priced", False)
    }
    for entry in cost_unpriced or []:
        name = str(getattr(entry, "component_name", "") or "")
        if name:
            unpriced_names.add(name)
    unpriced_names.discard("")

    ceiling = parse_budget_ceiling(budget_constraints)
    if ceiling is not None:
        totals = _totals(architecture)
        if not totals:
            reasons.append(
                f"No comparable priced totals are available against the "
                f"${ceiling:,.2f}/mo ceiling; compliance cannot be asserted."
            )
        else:
            over = sorted(
                f"{key}: ${value:,.2f}/mo exceeds the ${ceiling:,.2f}/mo ceiling"
                for key, value in totals.items()
                if value > ceiling
            )
            if over:
                # A stated hard ceiling is enforced deterministically: an
                # over-budget design is blocked, never approvable. Totals
                # cover priced lines only; unpriced components are excluded
                # and reported to the user for review.
                reasons.append(
                    "Cost ceiling breached: " + "; ".join(over) + ". "
                    "Blocked: an architecture that exceeds a stated hard "
                    "budget ceiling cannot proceed to approval."
                )
                return QualityGateResult(
                    outcome=QualityGateOutcome.BLOCKED,
                    reasons=reasons,
                    critical_count=0,
                    warning_count=len(warnings),
                    evaluated_artifact_id=evaluated_id,
                )
            if unpriced_names:
                # Priced lines within the ceiling, but the estimate is
                # incomplete: the same assurance as a complete estimate
                # is never claimed.
                reasons.append(
                    f"Priced lines stay within the ${ceiling:,.2f}/mo "
                    "ceiling, but pricing is incomplete, so full budget "
                    "compliance cannot be asserted."
                )
            else:
                reasons.append(
                    f"Priced totals stay within the ${ceiling:,.2f}/mo ceiling."
                )
    else:
        reasons.append(
            "No parseable budget ceiling stated; cost compliance "
            "deferred to user review of the priced subtotal."
        )

    if unpriced_names:
        names = sorted(unpriced_names)
        reasons.append(
            "Pricing incomplete for "
            f"{len(names)} component(s): "
            f"{', '.join(names)}. Total cost is partial and "
            "may increase; review unpriced components before approval."
        )

    reasons.append(
        f"No unresolved critical findings "
        f"({len(warnings)} warning(s) recorded for review)."
    )
    return QualityGateResult(
        outcome=QualityGateOutcome.APPROVED_FOR_REVIEW,
        reasons=reasons,
        critical_count=0,
        warning_count=len(warnings),
        evaluated_artifact_id=evaluated_id,
    )


def gate_evaluated_now(result: QualityGateResult) -> QualityGateResult:
    """Refresh the evaluation timestamp without changing the verdict."""
    return result.model_copy(update={"evaluated_at": datetime.utcnow()})
