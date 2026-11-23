/**
 * Response shapes served by the FastAPI design read API.
 *
 * These mirror the backend domain artifacts. Nothing here encodes
 * architecture knowledge: no workload names, no category branching, no
 * assumptions about how an architecture is produced. The UI renders
 * whatever the backend returns.
 */

export interface ClarificationView {
  requirement_key: string;
  requirement: string;
  question: string;
  reason: string;
  impact: string;
  assumable: boolean;
}

export interface CapacityValue {
  metric: string;
  value: number;
  unit: string;
}

export interface ArchitectureNode {
  node_id: string;
  component_name: string;
  category: string;
  role_in_system: string;
  capacity: CapacityValue[];
  scaling_note: string;
  availability_note: string;
  evidence_refs: string[];
}

export interface ArchitectureEdge {
  edge_id: string;
  from_node: string;
  to_node: string;
  kind: string;
  protocol: string;
  description: string;
  evidence_refs: string[];
}

export interface CostLink {
  component_name: string;
  priced: boolean;
  pricing_model: string | null;
  unit: string | null;
  monthly_cost_usd: number | null;
  pricing_source: string | null;
  unpriced_reason: string | null;
}

export interface ArchitectureDecision {
  decision: string;
  rationale: string;
  alternatives_considered: string[];
  evidence_refs: string[];
}

export interface ArchitectureInputsRef {
  requirements_session_id: string;
  capacity_formula_version: string;
  selection_generated_at: string;
  cost_generated_at: string;
  cost_pricing_as_of: string;
  region_code: string;
  upstream_inputs_hash: string;
}

export interface ArchitectureResult {
  schema_version: string;
  artifact_id: string;
  session_id: string;
  version: number;
  supersedes_artifact_id: string | null;
  nodes: ArchitectureNode[];
  edges: ArchitectureEdge[];
  entry_points: string[];
  data_store_node_ids: string[];
  boundaries: string[];
  cost_links: CostLink[];
  cost_totals_snapshot: Record<string, number>;
  decisions: ArchitectureDecision[];
  assumptions: string[];
  warnings: string[];
  evidence_references: string[];
  inputs_ref: ArchitectureInputsRef;
  generated_at: string;
}

export interface PricedComponentSummary {
  component_name: string;
  monthly_cost_usd: number;
  unit: string;
  pricing_model: string;
  pricing_source: string;
  region_code: string;
}

export interface UnpricedComponentSummary {
  component_name: string;
  reason: string;
}

/** One component-dimension of the backend's pricing coverage report. */
export interface CoverageEntrySummary {
  component_name: string;
  dimension: string;
  label: string;
  /** priced | estimated | usage_unknown | price_unavailable | unsupported
   *  | not_applicable | not_modeled (legacy spelling of unsupported). */
  status: string;
  reason: string;
  /** Registry classification: priceable | estimable | unsupported. Empty
   *  for entries produced before the coverage registry existed. */
  disposition?: string;
}

/** A lower/upper monthly bound, when the backend publishes one. */
export interface EstimatedRangeView {
  low_usd: number;
  high_usd: number;
}

export interface EstimatedUsageSummary {
  component_name: string;
  dimension: string;
  unit: string;
  low_quantity: number;
  typical_quantity: number;
  high_quantity: number;
  provenance: string;
  confidence: string;
  rationale: string;
}

export interface CostSummary {
  totals_usd_per_month: Record<string, number>;
  priced: PricedComponentSummary[];
  /** Estimated lines priced from estimated usage. Present on artifacts
   *  produced after the estimation slice; optional for backward
   *  compatibility with older persisted artifacts. */
  estimated_lines?: PricedComponentSummary[];
  unpriced: UnpricedComponentSummary[];
  coverage: CoverageEntrySummary[];
  warnings: string[];
  assumptions: string[];
  pricing_as_of: string | null;
  fully_priced: boolean;
  /** fully_priced | estimated_with_assumptions | estimated_with_residual_unknowns
   *  | partially_unresolved | unsupported. Deterministic backend verdict. */
  completeness: string;
  /** Best-estimate monthly total, or null while any applicable dimension
   *  is unresolved. Never a partial subtotal relabeled as a total. */
  best_estimate_total_usd_per_month: number | null;
  estimated_range_usd_per_month: EstimatedRangeView | null;
  unresolved_dimension_count: number;
  /** Usage estimates behind estimated_lines. Optional for backward
   *  compatibility with older persisted artifacts. */
  estimates?: EstimatedUsageSummary[];
}

export interface ArchitectureSummary {
  artifact_id: string;
  schema_version: string;
  version: number;
  supersedes_artifact_id: string | null;
  node_count: number;
  edge_count: number;
  entry_points: string[];
  data_store_node_ids: string[];
  region_code: string;
  generated_at: string | null;
  warning_count: number;
}

export interface ComponentCandidate {
  name: string;
  provider: string;
  category: string;
  role: string;
  rationale: string;
  confidence: string;
  evidence_support: string;
  alternatives: string[];
}

export interface ComponentSelectionResult {
  selected_components: ComponentCandidate[];
  rejected_candidates: ComponentCandidate[];
  unresolved_tradeoffs: { description: string; options: string[]; impact: string }[];
  evidence_references: string[];
  assumptions: string[];
  warnings: string[];
}

export interface CapacityModelResult {
  peak_write_qps: number;
  peak_read_qps: number;
  concurrent_connections: number;
  storage_growth_bytes_per_day: number;
  hot_working_set_bytes: number;
  egress_bytes_per_second: number;
  formula_version: string;
  calculation_trace: Record<string, string>;
}

export interface CapacityModelingResult {
  proposal: unknown;
  calculation: CapacityModelResult;
}

export interface DesignStages {
  has_design_run: boolean;
  design_status: string;
  design_error: string | null;
  capacity: CapacityModelingResult | null;
  selection: ComponentSelectionResult | null;
  architecture: ArchitectureSummary | null;
  cost: CostSummary | null;
  quality_gate?: QualityGateView | null;
  critique?: CritiqueSummary | null;
  design_approval?: ApprovalView | null;
}

export interface QualityGateView {
  outcome: string;
  reasons: string[];
  critical_count: number;
  warning_count: number;
  evaluated_artifact_id: string;
}

export interface CritiqueFindingView {
  category: string;
  severity: string;
  statement: string;
  rationale: string;
  about_component_ref: string;
  evidence_refs: string[];
  recommendation: string;
  suggested_route: string;
}

export interface CritiqueSummary {
  artifact_id: string;
  reviewed_artifact_id: string;
  reviewed_version: number;
  finding_count: number;
  critical_count: number;
  warning_count: number;
  summary: string;
}

export interface CritiqueResult {
  artifact_id: string;
  session_id: string;
  reviewed_artifact_id: string;
  reviewed_version: number;
  findings: CritiqueFindingView[];
  summary: string;
}

export interface ApprovalView {
  approved: boolean;
  note: string;
  artifact_id: string;
  gate_outcome: string;
  recorded_at: string;
}

export interface OptimizationRecommendationView {
  recommendation_id: string;
  derived_from_artifact_id: string;
  finding_category: string;
  finding_statement: string;
  proposed_change: string;
  target_component: string | null;
  rationale: string;
  affects_requirement: string | null;
  requirement_change_note: string | null;
  evaluated: boolean;
  status: string;
  disclaimer: string;
}

export interface OptimizationRecommendationsView {
  session_id: string;
  derived_from_artifact_id: string;
  recommendations: OptimizationRecommendationView[];
}

export interface TerraformResourceView {
  resource_id: string;
  node_id: string;
  kind: string;
  component_name: string;
  module: {
    instance_id: string;
    module_id: string;
    version: string;
  } | null;
  pending_inputs: { name: string; reason: string }[];
}

export interface TerraformInputDefaultView {
  resource_id: string;
  name: string;
  value: string;
  source: string;
  rationale: string;
  requires_confirmation: boolean;
}

/**
 * One proposed value for a pending human decision. Advisory only: the
 * operator reviews, edits and submits it through the normal input path.
 */
export interface InputSuggestionView {
  resource_id: string;
  name: string;
  value: string;
  rationale: string;
  source: string;
}

/** One pending input deliberately left to the operator, with the reason. */
export interface SkippedInputView {
  resource_id: string;
  name: string;
  reason: string;
}

export interface InputSuggestionsView {
  suggestions: InputSuggestionView[];
  skipped: SkippedInputView[];
  notes: string[];
}

export interface TerraformPlanView {
  status: string;
  artifact_id: string;
  spec_id: string;
  tree_hash: string;
  provider: string;
  region_code: string;
  resources: TerraformResourceView[];
  modules: { instance_id: string; module_id: string; version: string }[];
  selection_errors: string[];
  spec_diagnostics: { category: string; message: string }[];
  render_status: string;
  render_diagnostics: { category: string; message: string }[];
  files: Record<string, string>;
  resource_types: string[];
  terraform_validation: {
    status: string;
    installed: boolean;
    version: string | null;
    exit_codes: Record<string, number>;
    diagnostics: { category: string; message: string }[];
  };
  inventory: {
    analysis: string;
    creates: number;
    destroys: number | null;
    uncovered: number;
    by_kind: Record<string, number>;
    note: string;
  };
  spec_warnings: string[];
  inputs?: Record<string, Record<string, string>>;
  input_defaults?: TerraformInputDefaultView[];
  input_warnings?: string[];
  artifacts: { name: string; sha256: string }[];
  generated: GeneratedTerraformView[];
}

export interface GeneratedTerraformView {
  resource_id: string;
  node_id: string;
  component_name: string;
  resource_types: string[];
  proposal_sha256: string;
  status: string;
  notes: string[];
}

export interface ProvisioningResultView {
  status: string;
  mode: string;
  artifact_id: string;
  tree_hash: string;
  exit_codes: Record<string, number>;
  output: string;
  summary: { add: number; change: number; destroy: number } | null;
  recorded_at: string;
}

export interface Assumption {
  name: string;
  value: string;
  unit: string;
  source: string;
  evidence: string;
  rationale: string;
  confidence: string;
}

export interface SessionView {
  session_id: string;
  status: string;
  user_request: string;
  requirements: Record<string, unknown> | null;
  assumptions: Assumption[];
  pending_clarification: ClarificationView | null;
  warnings: string[];
  error: string | null;
  stages: DesignStages;
}

export interface DesignRunView {
  session_id: string;
  requirements_session_id: string;
  status: string;
}

/** One row of the recent-designs list. Mirrors the backend summary. */
export interface SessionSummary {
  session_id: string;
  user_request: string;
  requirements_status: string;
  design_status: string;
  has_design_run: boolean;
  has_error: boolean;
}

export interface CostResult {
  lines: {
    component_name: string;
    pricing_model: string;
    unit: string;
    monthly_usage: number;
    unit_price_usd: number;
    monthly_cost_usd: number;
    usage_formula: string;
    pricing_source: string;
    region_code: string;
    /** deterministic_derivation | stated_assumption (estimator_assumption
     *  is reserved for the later estimation slice). */
    usage_provenance?: string;
  }[];
  totals_usd_per_month: Record<string, number>;
  assumptions: string[];
  warnings: string[];
  unpriced: { component_name: string; reason: string }[];
  coverage: CoverageEntrySummary[];
  pricing_as_of: string;
  completeness?: string;
  fully_priced?: boolean;
  best_estimate_total_usd_per_month?: number | null;
  estimated_range_usd_per_month?: EstimatedRangeView | null;
  unresolved_dimension_count?: number;
}
