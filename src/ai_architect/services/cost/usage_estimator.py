"""Usage-quantity estimation pipeline (never prices).

For every selected component-dimension the coverage registry declares
ESTIMABLE and for which the pricing service retrieved an on-demand
fact (a price path exists), this module produces a UsageEstimate:

1. Deterministic rules run first with no model involvement
   (provisioned DynamoDB capacity sized to the modeled peak).
2. The remaining factor-parameterized jobs (Lambda GB-seconds) are
   sent in a single bounded proposal request; proposed factor
   scenarios are validated against hard bounds and the declared
   quantities are recomputed from the very factors the model declared
   -- a mismatch is rejected, not trusted.
3. Anything unproposed, invalid, or unreachable stays unresolved and
   is disclosed with an explicit warning. Estimation failure degrades
   to the pre-estimation (unresolved, disclosed) state; it never
   crashes the cost stage and never fabricates a quantity.

Prices never appear here: the calculator pairs every accepted
quantity with the retrieved catalog fact that triggered the job.
"""

from dataclasses import dataclass, field
import math
from typing import Callable, Optional

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.cost.models import (
    DimensionDisposition,
    EstimationConfidence,
    PricedComponent,
    PricingFact,
    PricingModel,
    UsageEstimate,
    UsageProvenance,
)
from ai_architect.services.cost import coverage_registry
from ai_architect.services.cost.dimensions import label_for_dimension
from ai_architect.services.cost.estimation_rules import (
    FACTOR_SPECS_BY_DIMENSION,
    SUPPORTED_ESTIMATION_DIMENSIONS,
    FactorSpec,
    check_gb_second_consistency,
    check_log_ingestion_consistency,
    check_log_storage_consistency,
    check_s3_request_consistency,
    deterministic_estimate,
    monthly_request_count,
    validate_factor_scenario,
)


@dataclass(frozen=True)
class EstimationJob:
    """One estimable component-dimension with a retrieved price fact."""

    component_name: str
    dimension: str
    label: str
    unit: str
    fact: PricingFact
    factor_specs: tuple[FactorSpec, ...] = ()
    known_requests: Optional[float] = None
    service_name: str = ""


@dataclass(frozen=True)
class UsageEstimationOutcome:
    """Estimates produced plus disclosure warnings; never raises on
    model failure."""

    estimates: list[UsageEstimate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


ProposalFn = Callable[
    [list[EstimationJob], CapacityModelingResult, Optional[dict]],
    object,
]


def discover_estimation_jobs(
    capacity_result: CapacityModelingResult,
    selection: ComponentSelectionResult,
    priced: list[PricedComponent],
) -> tuple[list[EstimationJob], list[str]]:
    """Find every dimension the estimator can supply usage for.

    A job exists only when all of the following hold: the component
    has a priced facts holder (no unavailable_reason), its service
    resolves in the registry, the dimension is applicable for the
    resolved configuration, the registry declares it ESTIMABLE, the
    estimator has a rule for it, and an on-demand fact was retrieved
    for it (without a fact there is no price path, so estimating usage
    would only add a number nothing can price).
    """
    warnings: list[str] = []
    jobs: list[EstimationJob] = []
    by_name = {p.component_name: p for p in priced}
    seen: set[tuple[str, str]] = set()
    known_requests = monthly_request_count(capacity_result)

    for comp in selection.selected_components:
        holder = by_name.get(comp.name)
        if holder is None or holder.unavailable_reason:
            continue
        service = coverage_registry.resolve_service(
            comp.name, holder.service_code
        )
        if service is None:
            continue
        resolved = coverage_registry.resolve_configuration(holder)
        applicable, _excluded = coverage_registry.coverage_for(
            service.service_code, **resolved
        )
        facts_by_key: dict[str, PricingFact] = {}
        for fact in holder.facts:
            if fact.pricing_model == PricingModel.ON_DEMAND:
                facts_by_key.setdefault(fact.usage_key, fact)
        for dim in applicable:
            if dim.disposition != DimensionDisposition.ESTIMABLE:
                continue
            if dim.dimension not in SUPPORTED_ESTIMATION_DIMENSIONS:
                continue
            key = (comp.name, dim.dimension)
            if key in seen:
                continue
            fact = facts_by_key.get(dim.dimension)
            if fact is None:
                continue
            seen.add(key)
            factor_specs = FACTOR_SPECS_BY_DIMENSION.get(dim.dimension, ())
            jobs.append(
                EstimationJob(
                    component_name=comp.name,
                    dimension=dim.dimension,
                    label=dim.label or label_for_dimension(dim.dimension),
                    unit=fact.unit,
                    fact=fact,
                    factor_specs=factor_specs,
                    known_requests=(
                        known_requests if factor_specs else None
                    ),
                    service_name=service.canonical_name,
                )
            )

    jobs.sort(key=lambda job: (job.component_name, job.dimension))
    return jobs, warnings


def build_llm_request_payload(
    jobs: list[EstimationJob],
    capacity_result: CapacityModelingResult,
    context: Optional[dict] = None,
) -> dict:
    """JSON-serializable request payload for the estimation agent.

    Carries the jobs, each factor spec's hard bounds, and the
    modeled workload facts the estimator may ground its factor
    scenarios in. Contains no prices and no rate data.
    """
    calc = capacity_result.calculation
    proposal = capacity_result.proposal
    context = context or {}
    return {
        "task": (
            "Estimate monthly usage quantities for the listed billing "
            "dimensions. Never state or use prices or rates. Provide a "
            "low/typical/high factor scenario and the matching low/"
            "typical/high monthly quantity for each dimension."
        ),
        "application_description": str(
            context.get("application_description", "") or ""
        ),
        "workload_facts": {
            "daily_active_users": proposal.daily_active_users,
            "messages_per_user_per_day": proposal.messages_per_user_per_day,
            "read_write_ratio": proposal.read_write_ratio,
            "peak_factor": proposal.peak_factor,
            "average_message_size_bytes": proposal.average_message_size_bytes,
            "peak_write_qps": calc.peak_write_qps,
            "peak_read_qps": calc.peak_read_qps,
            "storage_growth_bytes_per_day": (
                calc.storage_growth_bytes_per_day
            ),
            "egress_bytes_per_second": calc.egress_bytes_per_second,
        },
        "jobs": [
            {
                "component_name": job.component_name,
                "service_name": job.service_name,
                "dimension": job.dimension,
                "label": job.label,
                "unit": job.unit,
                "factor_specs": [
                    {
                        "name": spec.name,
                        "unit": spec.unit,
                        "minimum": spec.minimum,
                        "maximum": spec.maximum,
                        "minimum_exclusive": spec.minimum_exclusive,
                        "description": spec.description,
                    }
                    for spec in job.factor_specs
                ],
                "known_requests_per_month": job.known_requests,
            }
            for job in jobs
        ],
    }


def _estimate_from_proposal(
    item,
    job: EstimationJob,
) -> UsageEstimate:
    """Validate one proposed estimate against its job and rebuild it.

    The proposal's factor scenarios are validated against the rule's
    hard bounds, and every declared quantity must be reproducible from
    its own declared factors within tolerance. Provenance is assigned
    here (ESTIMATOR_ASSUMPTION); the proposal model cannot claim one.
    """
    if job.factor_specs and job.known_requests is None:
        raise ValueError(
            f"{job.component_name}/{job.dimension}: the estimation job "
            "has factor rules but no known request volume to ground "
            "them."
        )
    for scenario, declared in (
        ("low", item.low_quantity),
        ("typical", item.typical_quantity),
        ("high", item.high_quantity),
    ):
        factors = getattr(item.factors, scenario)
        validated = validate_factor_scenario(job.dimension, factors, scenario)
        if job.dimension == "gb_second":
            check_gb_second_consistency(
                job.known_requests,
                validated["average_invocation_duration_ms"],
                validated["memory_mb"],
                declared,
                scenario,
            )
        elif job.dimension in ("s3_put_requests", "s3_get_requests"):
            check_s3_request_consistency(
                job.dimension,
                job.known_requests,
                validated["s3_calls_per_request"],
                validated["tier1_share"],
                declared,
                scenario,
            )
        elif job.dimension == "log_data_ingested_gb":
            check_log_ingestion_consistency(
                job.known_requests,
                validated["log_bytes_per_request"],
                declared,
                scenario,
            )
        elif job.dimension == "log_storage_gbmo":
            check_log_storage_consistency(
                job.known_requests,
                validated["log_bytes_per_request"],
                validated["log_retention_days"],
                declared,
                scenario,
            )
    return UsageEstimate(
        component_name=job.component_name,
        dimension=job.dimension,
        unit=job.unit,
        low_quantity=item.low_quantity,
        typical_quantity=item.typical_quantity,
        high_quantity=item.high_quantity,
        provenance=UsageProvenance.ESTIMATOR_ASSUMPTION,
        confidence=item.confidence,
        rationale=item.rationale,
        assumptions=list(item.assumptions),
        facts_used=list(item.facts_used),
        unresolved_questions=list(item.unresolved_questions),
    )


# Dimension pairs that describe linked quantities of one component.
# Both members must be proposed with identical shared factors per
# scenario; otherwise the pair is rejected together so neither a
# double-counted call volume nor mismatched log-byte assumptions can
# reach the calculator.
_PAIR_CONSISTENCY_RULES: tuple[
    tuple[str, str, tuple[str, ...], str], ...
] = (
    (
        "s3_put_requests",
        "s3_get_requests",
        ("s3_calls_per_request", "tier1_share"),
        "the two dimensions split one modeled S3 call volume "
        "(requests * calls * share) and would otherwise double-count it",
    ),
    (
        "log_data_ingested_gb",
        "log_storage_gbmo",
        ("log_bytes_per_request",),
        "storage is derived from the same log bytes as ingestion and "
        "mixing different byte assumptions makes the quantities "
        "incoherent",
    ),
)


def _reject_inconsistent_pairs(
    accepted_items: dict[tuple[str, str], object],
    estimates_by_key: dict[tuple[str, str], UsageEstimate],
    warnings: list[str],
) -> None:
    for left, right, factor_names, explanation in _PAIR_CONSISTENCY_RULES:
        components = sorted({
            component_name
            for (component_name, dimension) in accepted_items
            if dimension in (left, right)
        })
        for component_name in components:
            left_item = accepted_items.get((component_name, left))
            right_item = accepted_items.get((component_name, right))
            if left_item is None or right_item is None:
                continue
            mismatch = False
            for scenario in ("low", "typical", "high"):
                left_factors = getattr(left_item.factors, scenario)
                right_factors = getattr(right_item.factors, scenario)
                for factor in factor_names:
                    if not math.isclose(
                        left_factors[factor],
                        right_factors[factor],
                        rel_tol=1e-9,
                        abs_tol=1e-12,
                    ):
                        mismatch = True
            if not mismatch:
                continue
            estimates_by_key.pop((component_name, left), None)
            estimates_by_key.pop((component_name, right), None)
            warnings.append(
                f"Rejected {left} and {right} estimates for "
                f"{component_name}: both must declare identical "
                f"{', '.join(factor_names)} in each scenario because "
                f"{explanation}. Both dimensions stay unresolved and "
                "are not priced."
            )


def validate_proposals(
    proposal,
    jobs: list[EstimationJob],
) -> tuple[list[UsageEstimate], list[str]]:
    """Turn a raw proposal into validated estimates plus warnings.

    Proposals that do not match a discovered job are ignored with a
    warning; duplicates keep the first valid proposal; a proposal that
    fails its rule's validation is rejected with a warning. Linked
    dimension pairs (S3 request classes, log ingestion/storage) must
    agree on their shared factors or both are rejected. Jobs left
    without a valid proposal are warned about explicitly so the
    unresolved dimension is never silently dropped.
    """
    warnings: list[str] = []
    jobs_by_key = {
        (job.component_name, job.dimension): job for job in jobs
    }
    estimates_by_key: dict[tuple[str, str], UsageEstimate] = {}
    accepted_items: dict[tuple[str, str], object] = {}
    for item in getattr(proposal, "estimates", []) or []:
        key = (item.component_name, item.dimension)
        job = jobs_by_key.get(key)
        if job is None:
            warnings.append(
                f"Estimation proposal for {item.component_name}/"
                f"{item.dimension} does not match any discovered "
                "estimation job; ignored."
            )
            continue
        if key in accepted_items:
            warnings.append(
                f"Duplicate estimation proposal for {item.component_name}/"
                f"{item.dimension}; the first valid proposal was kept."
            )
            continue
        try:
            estimate = _estimate_from_proposal(item, job)
        except Exception as e:
            warnings.append(
                f"Rejected estimation proposal for {item.component_name}/"
                f"{item.dimension}: {e}"
            )
            continue
        estimates_by_key[key] = estimate
        accepted_items[key] = item
    _reject_inconsistent_pairs(
        accepted_items, estimates_by_key, warnings
    )
    for key, job in jobs_by_key.items():
        if key not in estimates_by_key:
            warnings.append(
                f"No usable usage estimate for {job.component_name}/"
                f"{job.dimension} ({job.label}); the dimension stays "
                "unresolved and is not priced."
            )
    estimates = [
        estimates_by_key[key] for key in sorted(estimates_by_key)
    ]
    return estimates, warnings


def _deterministic_usage_estimate(
    job: EstimationJob, capacity_result: CapacityModelingResult
) -> UsageEstimate | None:
    det = deterministic_estimate(job.dimension, capacity_result)
    if det is None:
        return None
    return UsageEstimate(
        component_name=job.component_name,
        dimension=job.dimension,
        unit=job.unit,
        low_quantity=det.low_quantity,
        typical_quantity=det.typical_quantity,
        high_quantity=det.high_quantity,
        provenance=UsageProvenance.DETERMINISTIC_DERIVATION,
        confidence=EstimationConfidence.HIGH,
        rationale=(
            f"Deterministically derived from the modeled workload: "
            f"{det.formula}."
        ),
        assumptions=list(det.assumptions),
        facts_used=list(det.facts_used),
        unresolved_questions=[],
    )


class UsageEstimator:
    """Deterministic-first estimation with a single bounded proposal call.

    ``proposal_fn`` is injectable (tests pass a fake; the graph passes
    nothing and the default LLM-backed implementation is imported
    lazily). A proposal call failure degrades to warnings: the
    already-derived deterministic estimates still stand and the
    unproposed jobs stay unresolved and disclosed.
    """

    def __init__(self, proposal_fn: ProposalFn | None = None):
        self._proposal_fn = proposal_fn

    def estimate(
        self,
        capacity_result: CapacityModelingResult,
        selection: ComponentSelectionResult,
        priced: list[PricedComponent],
        *,
        context: Optional[dict] = None,
    ) -> UsageEstimationOutcome:
        jobs, warnings = discover_estimation_jobs(
            capacity_result, selection, priced
        )
        estimates: list[UsageEstimate] = []
        llm_jobs: list[EstimationJob] = []
        for job in jobs:
            try:
                deterministic = _deterministic_usage_estimate(
                    job, capacity_result
                )
            except Exception as e:
                warnings.append(
                    f"Deterministic estimation rule failed for "
                    f"{job.component_name}/{job.dimension}: {e}"
                )
                deterministic = None
            if deterministic is not None:
                estimates.append(deterministic)
            else:
                llm_jobs.append(job)

        if llm_jobs:
            proposal = None
            try:
                proposal_fn = self._proposal_fn
                if proposal_fn is None:
                    from ai_architect.agents.cost_estimation.run import (
                        request_usage_estimation_proposal,
                    )

                    proposal_fn = request_usage_estimation_proposal
                proposal = proposal_fn(llm_jobs, capacity_result, context)
            except Exception as e:
                warnings.append(
                    "Usage estimation model call failed; no estimated "
                    "quantities were produced for "
                    f"{len(llm_jobs)} dimension(s): {e}"
                )
            if proposal is not None:
                validated, validation_warnings = validate_proposals(
                    proposal, llm_jobs
                )
                estimates.extend(validated)
                warnings.extend(validation_warnings)

        estimates.sort(
            key=lambda estimate: (
                estimate.component_name,
                estimate.dimension,
            )
        )
        return UsageEstimationOutcome(estimates=estimates, warnings=warnings)
