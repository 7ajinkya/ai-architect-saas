"""Product-level request/response models for the design API.

Deliberately domain-shaped (sessions, messages, clarifications,
assumptions) rather than exposing internal LangGraph state.

Architecture and cost are returned as their existing domain artifacts
(``ArchitectureResult`` / ``CostEstimationResult``) instead of being
re-modelled here, so deterministic node ids, edge data and cost honesty
rules cannot drift from the backend contract. The models in this module
only add the cross-stage *summaries* the workspace needs in one payload.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ai_architect.agents.capacity.result import CapacityModelingResult
from ai_architect.agents.component_selection.models import (
    ComponentSelectionResult,
)


class CreateSessionRequest(BaseModel):
    user_request: str = Field(
        min_length=1,
        description="Natural-language infrastructure requirements",
    )

    model_config = ConfigDict(extra="forbid")


class SessionMessageRequest(BaseModel):
    text: str = Field(
        min_length=1,
        description=(
            "Clarification answer, or an explicit delegation such as "
            "'you decide' to let the architect assume a documented value"
        ),
    )

    model_config = ConfigDict(extra="forbid")


class ClarificationView(BaseModel):
    requirement_key: str = ""
    requirement: str = ""
    question: str = ""
    reason: str = ""
    impact: str = "unknown"
    assumable: bool = Field(
        default=False,
        description=(
            "Whether the architect may resolve this itself on deferral. "
            "False means the user must answer explicitly."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class ArchitectureSummary(BaseModel):
    """Shape of the persisted architecture artifact, not its contents.

    Full node/edge data comes from /design/sessions/{id}/architecture.
    """

    artifact_id: str
    schema_version: str
    version: int
    supersedes_artifact_id: Optional[str] = None
    node_count: int = 0
    edge_count: int = 0
    entry_points: list[str] = Field(default_factory=list)
    data_store_node_ids: list[str] = Field(default_factory=list)
    region_code: str = ""
    generated_at: Optional[datetime] = None
    warning_count: int = 0

    model_config = ConfigDict(extra="forbid")


class PricedComponentSummary(BaseModel):
    """One priced component line as reported by the cost artifact."""

    component_name: str
    monthly_cost_usd: float
    unit: str = ""
    pricing_model: str = ""
    pricing_source: str = ""
    region_code: str = ""

    model_config = ConfigDict(extra="forbid")


class UnpricedComponentSummary(BaseModel):
    """A component the backend could not price, with its stated reason.

    Never collapsed to a zero amount.
    """

    component_name: str
    reason: str

    model_config = ConfigDict(extra="forbid")


class CoverageEntrySummary(BaseModel):
    """One component-dimension of the estimate's pricing coverage report.

    Disclosure only: states what is inside or outside the priced
    subtotal (priced / estimated / usage_unknown / price_unavailable /
    unsupported / not_applicable; not_modeled is the legacy spelling of
    unsupported). Never carries or changes a dollar amount.
    """

    component_name: str
    dimension: str
    label: str
    status: str
    reason: str = ""
    disposition: Optional[str] = Field(
        default=None,
        description="Registry classification of the dimension: priceable, "
        "estimable, or unsupported. None for entries produced before the "
        "coverage registry existed.",
    )

    model_config = ConfigDict(extra="forbid")


class EstimatedRangeView(BaseModel):
    """A lower/upper monthly bound, when the backend publishes one."""

    low_usd: float
    high_usd: float

    model_config = ConfigDict(extra="forbid")


class EstimatedUsageSummary(BaseModel):
    """One estimated usage quantity served verbatim."""

    component_name: str
    dimension: str
    unit: str
    low_quantity: float
    typical_quantity: float
    high_quantity: float
    provenance: str
    confidence: str
    rationale: str = ""

    model_config = ConfigDict(extra="forbid")


class CostSummary(BaseModel):
    """Cost overview for the workspace cost bar and cost panel."""

    totals_usd_per_month: dict[str, float] = Field(default_factory=dict)
    priced: list[PricedComponentSummary] = Field(default_factory=list)
    estimated_lines: list[PricedComponentSummary] = Field(default_factory=list)
    unpriced: list[UnpricedComponentSummary] = Field(default_factory=list)
    coverage: list[CoverageEntrySummary] = Field(
        default_factory=list,
        description="Per-dimension pricing coverage: priced, estimated, "
        "usage-unknown, price-unavailable, unsupported, and not-applicable "
        "dimensions. Disclosure only; never affects totals.",
    )
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    pricing_as_of: Optional[datetime] = None
    fully_priced: bool = Field(
        default=True,
        description="True only when completeness is fully_priced: every "
        "selected component has every applicable billing dimension "
        "deterministically priced, with no unpriced entries and no "
        "coverage gaps. False means the priced subtotal is partial.",
    )
    completeness: str = Field(
        default="fully_priced",
        description="fully_priced | estimated_with_assumptions | "
        "estimated_with_residual_unknowns | partially_unresolved | "
        "unsupported. Deterministic backend verdict over the coverage "
        "report and unpriced entries.",
    )
    best_estimate_total_usd_per_month: Optional[float] = Field(
        default=None,
        description="Best-estimate monthly total, published only when "
        "every applicable dimension is accounted for. None while anything "
        "applicable is unresolved, so no partial subtotal is ever served "
        "as a total.",
    )
    estimated_range_usd_per_month: Optional[EstimatedRangeView] = Field(
        default=None,
        description="Low/high monthly bound; published for fully priced, "
        "estimated-with-assumptions, and estimated-with-residual-unknowns "
        "results. Illustrative, not a contractual bound.",
    )
    unresolved_dimension_count: int = Field(
        default=0,
        description="Applicable coverage dimensions with status "
        "usage_unknown, price_unavailable, or unsupported (legacy "
        "not_modeled counts as unsupported).",
    )
    estimates: list[EstimatedUsageSummary] = Field(default_factory=list)

    @property
    def unpriced_count(self) -> int:
        return len(self.unpriced)

    model_config = ConfigDict(extra="forbid")


class OptimizationRecommendationView(BaseModel):
    """One unevaluated diagnostic recommendation served verbatim."""

    recommendation_id: str
    derived_from_artifact_id: str
    finding_category: str
    finding_statement: str
    proposed_change: str
    target_component: Optional[str] = None
    rationale: str
    affects_requirement: Optional[str] = None
    requirement_change_note: Optional[str] = None
    evaluated: bool = False
    status: str = "unevaluated"
    disclaimer: str

    model_config = ConfigDict(extra="forbid")


class OptimizationRecommendationsView(BaseModel):
    """Stored unevaluated recommendations for one design session."""

    session_id: str
    derived_from_artifact_id: str = ""
    recommendations: list[OptimizationRecommendationView] = Field(
        default_factory=list
    )

    model_config = ConfigDict(extra="forbid")


class ApproveDesignRequest(BaseModel):
    """Explicit human verdict on a gated design.

    ``approved`` records acceptance of the architecture exactly as
    critiqued and gated, including any remaining findings named in
    ``note``. Nothing is provisioned by approval alone.
    """

    approved: bool = Field(
        description=(
            "True accepts the design for downstream use; False records "
            "an explicit rejection."
        )
    )
    note: str = Field(
        default="",
        max_length=2000,
        description="Accepted trade-offs or reason for rejection",
    )

    model_config = ConfigDict(extra="forbid")


class ApprovalView(BaseModel):
    """Persisted human approval record, served verbatim."""

    approved: bool
    note: str = ""
    artifact_id: str = ""
    gate_outcome: str = ""
    recorded_at: str = ""

    model_config = ConfigDict(extra="forbid")


class CritiqueFindingView(BaseModel):
    """One adversarial finding, served verbatim from the critique."""

    category: str = ""
    severity: str = ""
    statement: str = ""
    rationale: str = ""
    about_component_ref: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    recommendation: str = ""
    suggested_route: str = ""

    model_config = ConfigDict(extra="forbid")


class CritiqueSummary(BaseModel):
    """Shape of the persisted critique artifact, not its full findings.

    Full findings come from /design/sessions/{id}/critique.
    """

    artifact_id: str
    reviewed_artifact_id: str = ""
    reviewed_version: int = 0
    finding_count: int = 0
    critical_count: int = 0
    warning_count: int = 0
    summary: str = ""

    model_config = ConfigDict(extra="forbid")


class QualityGateView(BaseModel):
    """Stored quality-gate verdict, served verbatim."""

    outcome: str = ""
    reasons: list[str] = Field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    evaluated_artifact_id: str = ""

    model_config = ConfigDict(extra="forbid")


class DesignStages(BaseModel):
    """Which downstream stage artifacts exist for this session.

    Absence is reported as absence; no stage is faked or defaulted.
    ``design_status`` is the top-level workflow status
    (running | ready | failed) and is deliberately kept separate from
    ``SessionView.status``, which is the requirements-stage status.
    """

    has_design_run: bool = False
    design_status: str = ""
    design_error: Optional[str] = None
    capacity: Optional[CapacityModelingResult] = None
    selection: Optional[ComponentSelectionResult] = None
    architecture: Optional[ArchitectureSummary] = None
    cost: Optional[CostSummary] = None
    quality_gate: Optional[QualityGateView] = None
    critique: Optional[CritiqueSummary] = None
    design_approval: Optional[ApprovalView] = None

    model_config = ConfigDict(extra="forbid")


class SessionView(BaseModel):
    """Current session snapshot.

    Requirements-stage fields are unchanged; ``stages`` adds the
    downstream artifacts that later slices produce. Architecture and
    cost remain intentionally absent from this payload as full node and
    line data: they are served by their own endpoints.
    """

    session_id: str = Field(
        description=(
            "Design session id, i.e. the id the caller addressed. For a "
            "completed design run this is the top-level workflow thread, "
            "which differs from requirements_session_id."
        )
    )
    requirements_session_id: str = ""
    status: str = Field(
        description="needs_clarification | ready | failed"
    )
    user_request: str = ""
    requirements: dict | None = None
    assumptions: list[dict] = Field(default_factory=list)
    pending_clarification: ClarificationView | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    stages: DesignStages = Field(default_factory=DesignStages)

    model_config = ConfigDict(extra="forbid")


class SessionSummary(BaseModel):
    """One row of the recent-designs list.

    Only values already present in persisted session state: no
    timestamps exist anywhere in the checkpoint store, so none are
    reported. ``requirements_status`` is the requirements-stage status;
    ``design_status`` is the top-level workflow status ("" when no
    design run has happened for this thread).
    """

    session_id: str
    user_request: str = ""
    requirements_status: str = ""
    design_status: str = ""
    has_design_run: bool = False
    has_error: bool = False

    model_config = ConfigDict(extra="forbid")


class DesignRunView(BaseModel):
    """Result of starting one design run.

    ``session_id`` is the new design session id (the design graph thread
    id). The requirements session it was started from is reported
    separately because that thread keeps its own identity.
    """

    session_id: str
    requirements_session_id: str = ""
    status: str = Field(description="running | ready | failed")

    model_config = ConfigDict(extra="forbid")


class TerraformRequest(BaseModel):
    """Human-decided required provisioning inputs.

    Maps resource_id -> {parameter_name: value} (e.g. a globally
    unique bucket name). Every value is validated through the same
    contract machinery as derived parameters; unknown resources,
    collisions, and violations fail loudly.
    """

    inputs: dict[str, dict[str, str]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class PlanDiagnosticView(BaseModel):
    """One deterministic plan diagnostic, as produced by plan_builder.

    Mirrors the {"category", "message"} records (never bare strings),
    so an incomplete plan with pending inputs or render findings
    serializes exactly as stored instead of crashing response
    validation.
    """

    category: str = ""
    message: str = ""

    model_config = ConfigDict(extra="forbid")


class TerraformInputDefaultView(BaseModel):
    """One deterministic default applied while building a plan.

    Defaults are explicit, auditable decisions, never silently invented
    values. ``source`` identifies the rule that supplied the default and
    ``rationale`` explains why it was chosen.
    """

    resource_id: str = ""
    name: str = ""
    value: str = ""
    source: str = ""
    rationale: str = ""
    requires_confirmation: bool = True

    model_config = ConfigDict(extra="forbid")


class TerraformPlanView(BaseModel):
    """Deterministic provisioning plan for an approved architecture.

    Served verbatim from the stored plan artifact. ``inventory`` is
    static inventory analysis, not a live terraform plan; the live
    plan runs only in the approval-gated executor.
    """

    status: str = ""
    artifact_id: str = ""
    spec_id: str = ""
    tree_hash: str = ""
    provider: str = ""
    region_code: str = ""
    resources: list[dict] = Field(default_factory=list)
    modules: list[dict] = Field(default_factory=list)
    selection_errors: list[str] = Field(default_factory=list)
    spec_diagnostics: list[PlanDiagnosticView] = Field(default_factory=list)
    render_status: str = ""
    render_diagnostics: list[PlanDiagnosticView] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    resource_types: list[str] = Field(default_factory=list)
    terraform_validation: dict = Field(default_factory=dict)
    inventory: dict = Field(default_factory=dict)
    spec_warnings: list[str] = Field(default_factory=list)
    inputs: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Explicit human-supplied required inputs this plan was built "
            "with: resource_id -> {parameter_name: value}. Carried "
            "forward on regeneration."
        ),
    )
    input_defaults: list[TerraformInputDefaultView] = Field(
        default_factory=list,
        description=(
            "Deterministic defaults applied while building this plan, with "
            "source and rationale for each default."
        ),
    )
    input_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Carried-forward inputs that no longer applied and were "
            "discarded, with reasons."
        ),
    )
    artifacts: list[dict] = Field(
        default_factory=list,
        description="Approved local artifacts: [{name, sha256}] basenames.",
    )
    generated: list[dict] = Field(
        default_factory=list,
        description=(
            "Controlled LLM-fallback entries: [{resource_id, node_id, "
            "component_name, resource_types, proposal_sha256, status, "
            "notes}]. Empty when every component used a verified module."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class InputSuggestionView(BaseModel):
    """One proposed value for a pending human decision.

    ``source`` states where the value came from: a platform default the
    project owns, or a model proposal validated against the module
    contract. Both are advisory: the operator reviews, edits and
    submits the value through the normal input path.
    """

    resource_id: str = ""
    name: str = ""
    value: str = ""
    rationale: str = ""
    source: str = ""

    model_config = ConfigDict(extra="forbid")


class SkippedInputView(BaseModel):
    """One pending input that was deliberately left to the operator."""

    resource_id: str = ""
    name: str = ""
    reason: str = ""

    model_config = ConfigDict(extra="forbid")


class InputSuggestionsView(BaseModel):
    """Suggested values for the pending decisions of a stored plan.

    Nothing here is applied: the values are a starting point for the
    operator, who still submits them (and any edits) explicitly.
    """

    suggestions: list[InputSuggestionView] = Field(default_factory=list)
    skipped: list[SkippedInputView] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ApproveProvisioningRequest(BaseModel):
    """Explicit human verdict on a stored Terraform plan.

    Recording approval never executes anything; execution happens
    only through the provision endpoint, which re-validates the
    record against the stored plan.
    """

    approved: bool = Field(
        description=(
            "True accepts the stored plan for execution; False "
            "records an explicit rejection."
        )
    )
    note: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid")


class ProvisionRequest(BaseModel):
    """Execute a stored, approved plan.

    ``mode`` selects plan-only or apply. Apply additionally requires
    ``confirm`` to equal "APPLY": the typed confirmation is the final
    human boundary before infrastructure mutation.
    """

    mode: str = Field(
        description="plan | apply | destroy-plan | destroy | drift"
    )
    confirm: str = Field(default="", max_length=32)

    model_config = ConfigDict(extra="forbid")


class ProvisioningResultView(BaseModel):
    """Executor outcome for a stored plan, served verbatim."""

    status: str = ""
    mode: str = ""
    artifact_id: str = ""
    tree_hash: str = ""
    exit_codes: dict = Field(default_factory=dict)
    output: str = ""
    summary: dict | None = None
    plan_file: dict | None = Field(
        default=None,
        description=(
            "Saved-plan binding recorded by plan mode: {name, sha256} "
            "of the exact reviewed terraform plan file apply consumes. "
            "None on apply records and on pre-binding plan records."
        ),
    )
    outputs: dict | None = Field(
        default=None,
        description=(
            "Post-apply terraform outputs ({status, values}); None for "
            "modes that do not inspect a deployment."
        ),
    )
    smoke: dict | None = Field(
        default=None,
        description=(
            "Post-apply HTTP smoke result ({status, url, http_status}); "
            "None for modes that do not inspect a deployment."
        ),
    )
    recorded_at: str = ""

    model_config = ConfigDict(extra="forbid")


class ErrorView(BaseModel):
    detail: str
    code: str = "error"

    model_config = ConfigDict(extra="forbid")
