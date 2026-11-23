"""Architecture domain contract (Slice A: models only, no LLM).

LLM-proposed fields vs Python-resolved fields vs Python-generated metadata:

- LLM proposes: node roles/notes, edge set, decisions, assumptions,
  warnings, new tradeoffs, and capacity *keys*.
- Python resolves: CapacityValue value/unit copies, CostLink snapshots,
  evidence unions, node/edge IDs.
- Python generates: artifact_id, version chain, inputs hash, timestamps.

Capacity values and cost links are therefore never authored by the LLM;
artifact identity lives in artifact_id/version/inputs hash, never in
generated_at alone.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ai_architect.agents.component_selection.models import UnresolvedTradeoff

SCHEMA_VERSION = "architecture/v1"

# Endpoints that are not architecture nodes. Anything else used as an
# edge endpoint must be a declared node_id.
EXTERNAL_ENDPOINTS = ("client", "external")

# Categories whose nodes may be designated as data stores. Checked
# deterministically; the LLM proposes the subset, Python/the validator
# enforces the category rule.
DATA_STORE_CATEGORIES = ("database", "storage", "caching", "backup")


class CapacityMetricKey(str, Enum):
    """Keys into CapacityModelResult. Values/units are copied by Python."""

    PEAK_WRITE_QPS = "peak_write_qps"
    PEAK_READ_QPS = "peak_read_qps"
    CONCURRENT_CONNECTIONS = "concurrent_connections"
    STORAGE_GROWTH_BYTES_PER_DAY = "storage_growth_bytes_per_day"
    HOT_WORKING_SET_BYTES = "hot_working_set_bytes"
    EGRESS_BYTES_PER_SECOND = "egress_bytes_per_second"


class EdgeKind(str, Enum):
    """Flow taxonomy so the future Critic need not parse prose."""

    REQUEST = "request"
    READ = "read"
    WRITE = "write"
    ASYNC = "async"
    MEDIA = "media"
    CONTROL = "control"


class CapacityValue(BaseModel):
    """Resolved load snapshot. Built by Python, never by the LLM."""

    metric: CapacityMetricKey
    value: float
    unit: str

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ArchitectureNode(BaseModel):
    """One selected component in its system role."""

    node_id: str
    component_name: str
    category: str
    role_in_system: str
    capacity: list[CapacityValue] = Field(default_factory=list)
    scaling_note: str = ""
    availability_note: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ArchitectureEdge(BaseModel):
    """One directed flow. Direction is from_node -> to_node."""

    edge_id: str
    from_node: str
    to_node: str
    kind: EdgeKind
    protocol: str = ""
    description: str
    load: CapacityValue | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class CostLink(BaseModel):
    """Cost snapshot for one node. Built by Python via exact join."""

    component_name: str
    priced: bool
    pricing_model: str | None = None
    unit: str | None = None
    monthly_cost_usd: float | None = None
    pricing_source: str | None = None
    unpriced_reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class ArchitectureDecision(BaseModel):
    """One architectural decision and why it was made."""

    decision: str
    rationale: str
    alternatives_considered: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ArchitectureInputsRef(BaseModel):
    """Provenance anchors. All assigned by Python."""

    requirements_session_id: str
    capacity_formula_version: str
    selection_generated_at: datetime
    cost_generated_at: datetime
    cost_pricing_as_of: datetime
    region_code: str
    upstream_inputs_hash: str

    model_config = ConfigDict(extra="forbid")


class ArchitectureResult(BaseModel):
    """Immutable structured architecture artifact (version 1 scope)."""

    schema_version: str = SCHEMA_VERSION
    artifact_id: str
    session_id: str
    version: int = Field(ge=1)
    supersedes_artifact_id: str | None = None
    nodes: list[ArchitectureNode] = Field(default_factory=list)
    edges: list[ArchitectureEdge] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    data_store_node_ids: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    cost_links: list[CostLink] = Field(default_factory=list)
    cost_totals_snapshot: dict[str, float] = Field(default_factory=dict)
    decisions: list[ArchitectureDecision] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unresolved_tradeoffs: list[UnresolvedTradeoff] = Field(
        default_factory=list
    )
    evidence_references: list[str] = Field(default_factory=list)
    inputs_ref: ArchitectureInputsRef
    generated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(extra="forbid")
