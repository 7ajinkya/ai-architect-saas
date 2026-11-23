"""Adapter from persisted design state to API view models.

This module performs no orchestration and owns no domain rules. It maps
already-validated domain artifacts onto the response shapes the
workspace needs, and copies values without recomputing them:

- cost totals and per-line amounts are copied from the cost artifact;
  nothing is summed, converted or defaulted here;
- unpriced components keep the backend's reason and are never turned
  into a zero amount;
- pricing coverage entries are copied verbatim from the cost artifact
  and never carry or change a dollar amount;
- architecture shape (counts, entry points, region) is copied from the
  architecture artifact, whose node ids stay exactly as persisted.
"""

from ai_architect.api.models import (
    ApprovalView,
    ArchitectureSummary,
    CostSummary,
    CoverageEntrySummary,
    CritiqueSummary,
    DesignStages,
    EstimatedRangeView,
    EstimatedUsageSummary,
    PricedComponentSummary,
    QualityGateView,
    UnpricedComponentSummary,
)
from ai_architect.domain.architecture.critique import DesignCritique
from ai_architect.domain.architecture.models import ArchitectureResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.infrastructure.checkpoint.design import (
    DesignSessionSnapshot,
)


def architecture_summary(
    architecture: ArchitectureResult,
) -> ArchitectureSummary:
    return ArchitectureSummary(
        artifact_id=architecture.artifact_id,
        schema_version=architecture.schema_version,
        version=architecture.version,
        supersedes_artifact_id=architecture.supersedes_artifact_id,
        node_count=len(architecture.nodes),
        edge_count=len(architecture.edges),
        entry_points=list(architecture.entry_points),
        data_store_node_ids=list(architecture.data_store_node_ids),
        region_code=architecture.inputs_ref.region_code,
        generated_at=architecture.generated_at,
        warning_count=len(architecture.warnings),
    )


def cost_summary(cost: CostEstimationResult) -> CostSummary:
    return CostSummary(
        totals_usd_per_month=dict(cost.totals_usd_per_month),
        priced=[
            PricedComponentSummary(
                component_name=line.component_name,
                monthly_cost_usd=line.monthly_cost_usd,
                unit=line.unit,
                pricing_model=str(line.pricing_model),
                pricing_source=line.pricing_source,
                region_code=line.region_code,
            )
            for line in cost.lines
        ],
        estimated_lines=[
            PricedComponentSummary(
                component_name=line.component_name,
                monthly_cost_usd=line.monthly_cost_usd,
                unit=line.unit,
                pricing_model=str(line.pricing_model),
                pricing_source=line.pricing_source,
                region_code=line.region_code,
            )
            for line in cost.estimated_lines
        ],
        unpriced=[
            UnpricedComponentSummary(
                component_name=item.component_name,
                reason=item.reason,
            )
            for item in cost.unpriced
        ],
        coverage=[
            CoverageEntrySummary(
                component_name=entry.component_name,
                dimension=entry.dimension,
                label=entry.label,
                status=str(entry.status),
                reason=entry.reason,
                disposition=entry.disposition,
            )
            for entry in cost.coverage
        ],
        warnings=list(cost.warnings),
        assumptions=list(cost.assumptions),
        pricing_as_of=cost.pricing_as_of,
        fully_priced=cost.fully_priced,
        completeness=cost.completeness.value,
        best_estimate_total_usd_per_month=(
            cost.best_estimate_total_usd_per_month
        ),
        estimated_range_usd_per_month=(
            EstimatedRangeView(
                low_usd=cost.estimated_range_usd_per_month.low_usd,
                high_usd=cost.estimated_range_usd_per_month.high_usd,
            )
            if cost.estimated_range_usd_per_month is not None
            else None
        ),
        unresolved_dimension_count=cost.unresolved_dimension_count,
        estimates=[
            EstimatedUsageSummary(
                component_name=estimate.component_name,
                dimension=estimate.dimension,
                unit=estimate.unit,
                low_quantity=estimate.low_quantity,
                typical_quantity=estimate.typical_quantity,
                high_quantity=estimate.high_quantity,
                provenance=estimate.provenance.value,
                confidence=estimate.confidence.value,
                rationale=estimate.rationale,
            )
            for estimate in cost.estimates
        ],
    )


def critique_summary(critique: DesignCritique) -> CritiqueSummary:
    findings = list(critique.findings or [])
    return CritiqueSummary(
        artifact_id=critique.artifact_id,
        reviewed_artifact_id=critique.reviewed_artifact_id,
        reviewed_version=critique.reviewed_version,
        finding_count=len(findings),
        critical_count=len(critique.criticals),
        warning_count=sum(
            1 for finding in findings if str(finding.severity) == "warning"
        ),
        summary=critique.summary,
    )


def quality_gate_view(gate) -> QualityGateView:
    return QualityGateView(
        outcome=str(getattr(gate, "outcome", "") or ""),
        reasons=list(getattr(gate, "reasons", []) or []),
        critical_count=int(getattr(gate, "critical_count", 0) or 0),
        warning_count=int(getattr(gate, "warning_count", 0) or 0),
        evaluated_artifact_id=str(
            getattr(gate, "evaluated_artifact_id", "") or ""
        ),
    )


def design_stages(snapshot: DesignSessionSnapshot) -> DesignStages:
    architecture = snapshot.architecture
    cost = snapshot.cost_result
    return DesignStages(
        has_design_run=snapshot.has_design_run,
        design_status=snapshot.status,
        design_error=snapshot.error,
        capacity=snapshot.capacity_result,
        selection=snapshot.component_selection,
        architecture=(
            architecture_summary(architecture) if architecture else None
        ),
        cost=cost_summary(cost) if cost else None,
        quality_gate=(
            quality_gate_view(snapshot.quality_gate)
            if snapshot.quality_gate is not None
            else None
        ),
        critique=(
            critique_summary(snapshot.critique)
            if snapshot.critique is not None
            else None
        ),
        design_approval=(
            ApprovalView(**snapshot.design_approval)
            if isinstance(snapshot.design_approval, dict)
            else None
        ),
    )
