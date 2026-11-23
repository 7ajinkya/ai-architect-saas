"""Deterministic helpers for the Architecture Composer (Slice A).

These builders copy values out of already-validated upstream artifacts;
they perform no capacity arithmetic and no pricing logic beyond
summing the calculator's own lines per component.
"""

import hashlib
import json
import uuid

from pydantic import BaseModel

from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)
from ai_architect.domain.architecture.models import (
    ArchitectureInputsRef,
    CapacityMetricKey,
    CapacityValue,
    CostLink,
)
from ai_architect.domain.capacity.models import CapacityModelResult
from ai_architect.domain.cost.models import CostEstimationResult
from ai_architect.domain.requirements.models import RequirementsExtraction

# Deterministic unit for each capacity metric key. The LLM selects keys;
# Python attaches these units alongside copied values.
CAPACITY_UNIT_BY_METRIC = {
    CapacityMetricKey.PEAK_WRITE_QPS.value: "requests/s",
    CapacityMetricKey.PEAK_READ_QPS.value: "requests/s",
    CapacityMetricKey.CONCURRENT_CONNECTIONS.value: "connections",
    CapacityMetricKey.STORAGE_GROWTH_BYTES_PER_DAY.value: "bytes/day",
    CapacityMetricKey.HOT_WORKING_SET_BYTES.value: "bytes",
    CapacityMetricKey.EGRESS_BYTES_PER_SECOND.value: "bytes/s",
}


def _metric_key(metric: CapacityMetricKey | str) -> str:
    if isinstance(metric, CapacityMetricKey):
        return metric.value
    return str(metric)


def resolve_capacity_value(
    metric: CapacityMetricKey | str,
    calculation: CapacityModelResult,
) -> CapacityValue:
    """Copy one metric value/unit out of a CapacityModelResult.

    Raises ValueError for unknown metric keys; never invents numbers.
    """
    key = _metric_key(metric)
    if key not in CAPACITY_UNIT_BY_METRIC:
        raise ValueError(
            f"Unknown capacity metric {metric!r}; expected one of "
            f"{sorted(CAPACITY_UNIT_BY_METRIC)}."
        )
    return CapacityValue(
        metric=key,
        value=float(getattr(calculation, key)),
        unit=CAPACITY_UNIT_BY_METRIC[key],
    )


def build_cost_links(
    selection: ComponentSelectionResult,
    cost: CostEstimationResult,
) -> list[CostLink]:
    """Build one CostLink per selected component by exact name join.

    Components with calculator lines aggregate to a summed monthly cost
    (unit/source/model collapse to "mixed" when lines disagree);
    components without lines become explicitly unpriced with the
    upstream reason. Nothing is priced here.
    """
    lines_by_component: dict[str, list] = {}
    for line in cost.lines:
        lines_by_component.setdefault(line.component_name, []).append(line)
    unpriced_reason_by_component = {
        item.component_name: item.reason for item in cost.unpriced
    }

    links: list[CostLink] = []
    for comp in selection.selected_components:
        lines = lines_by_component.get(comp.name, [])
        if lines:
            models = {str(line.pricing_model) for line in lines}
            units = {line.unit for line in lines}
            sources = {line.pricing_source for line in lines}
            links.append(
                CostLink(
                    component_name=comp.name,
                    priced=True,
                    pricing_model=(
                        next(iter(models)) if len(models) == 1 else "mixed"
                    ),
                    unit=next(iter(units)) if len(units) == 1 else "mixed",
                    monthly_cost_usd=sum(
                        line.monthly_cost_usd for line in lines
                    ),
                    pricing_source=(
                        next(iter(sources))
                        if len(sources) == 1
                        else "mixed"
                    ),
                    unpriced_reason=None,
                )
            )
        else:
            links.append(
                CostLink(
                    component_name=comp.name,
                    priced=False,
                    unpriced_reason=unpriced_reason_by_component.get(
                        comp.name,
                        "No pricing lines were produced for this component.",
                    ),
                )
            )
    return links


def _canonical(payload: object) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    )


def hash_upstream_inputs(
    extraction: RequirementsExtraction,
    capacity: CapacityModelResult,
    selection: ComponentSelectionResult,
    cost: CostEstimationResult,
    region_code: str,
) -> str:
    """Deterministic sha256 over canonical upstream JSON plus region."""
    document = {
        "capacity": _canonical(capacity),
        "cost": _canonical(cost),
        "extraction": _canonical(extraction),
        "region_code": region_code,
        "selection": _canonical(selection),
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_inputs_ref(
    *,
    requirements_session_id: str,
    capacity: CapacityModelResult,
    selection: ComponentSelectionResult,
    cost: CostEstimationResult,
    extraction: RequirementsExtraction,
    region_code: str,
) -> ArchitectureInputsRef:
    """Assemble provenance anchors, copying timestamps, never inventing."""
    return ArchitectureInputsRef(
        requirements_session_id=requirements_session_id,
        capacity_formula_version=capacity.formula_version,
        selection_generated_at=selection.generated_at,
        cost_generated_at=cost.generated_at,
        cost_pricing_as_of=cost.pricing_as_of,
        region_code=region_code,
        upstream_inputs_hash=hash_upstream_inputs(
            extraction, capacity, selection, cost, region_code
        ),
    )


def new_artifact_id() -> str:
    """Allocate one fresh immutable artifact identity."""
    return str(uuid.uuid4())
