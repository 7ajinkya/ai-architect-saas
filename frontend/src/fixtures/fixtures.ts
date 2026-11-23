/**
 * Generic architecture fixtures.
 *
 * TEST DATA ONLY. These exist to prove the UI is shape-agnostic: two
 * structurally different architectures rendered by the same components,
 * with no workload names and no production branches anywhere.
 *
 * Fixture A: a linear request chain.
 * Fixture B: a fan-out with a store and a shared dependency.
 */

import type { ArchitectureResult, SessionView } from "../types";

const INPUTS_REF = {
  requirements_session_id: "fixture-requirements",
  capacity_formula_version: "capacity/v1",
  region_code: "us-east-1",
  upstream_inputs_hash: "fixture-hash",
};

function inputs(sessionId: string) {
  return {
    ...INPUTS_REF,
    requirements_session_id: sessionId,
    selection_generated_at: "2026-01-01T00:00:00Z",
    cost_generated_at: "2026-01-01T00:00:00Z",
    cost_pricing_as_of: "2026-01-01T00:00:00Z",
  };
}

/** Fixture A: linear chain, three nodes, two edges, no cost links. */
export const FIXTURE_A: ArchitectureResult = {
  schema_version: "architecture/v1",
  artifact_id: "artifact-fixture-a",
  session_id: "session-fixture-a",
  version: 1,
  supersedes_artifact_id: null,
  nodes: [
    {
      node_id: "gateway",
      component_name: "Edge Gateway",
      category: "networking",
      role_in_system: "terminates inbound traffic",
      capacity: [
        { metric: "peak_read_qps", value: 900, unit: "qps" },
      ],
      scaling_note: "autoscaling on request count",
      availability_note: "multi-region edge",
      evidence_refs: ["ev-gateway"],
    },
    {
      node_id: "worker",
      component_name: "Stateless Worker",
      category: "compute",
      role_in_system: "executes request handling",
      capacity: [
        { metric: "peak_read_qps", value: 900, unit: "qps" },
        { metric: "concurrent_connections", value: 240, unit: "connections" },
      ],
      scaling_note: "horizontal, stateless",
      availability_note: "across three zones",
      evidence_refs: ["ev-worker"],
    },
    {
      node_id: "records",
      component_name: "Primary Record Store",
      category: "database",
      role_in_system: "durable system of record",
      capacity: [
        { metric: "peak_write_qps", value: 300, unit: "qps" },
        { metric: "storage_growth_bytes_per_day", value: 1048576, unit: "bytes/day" },
      ],
      scaling_note: "vertical plus read replicas",
      availability_note: "multi-az synchronous",
      evidence_refs: ["ev-records"],
    },
  ],
  edges: [
    {
      edge_id: "gateway->worker",
      from_node: "gateway",
      to_node: "worker",
      kind: "request",
      protocol: "https",
      description: "request forwarding",
      evidence_refs: ["ev-worker"],
    },
    {
      edge_id: "worker->records",
      from_node: "worker",
      to_node: "records",
      kind: "write",
      protocol: "tls",
      description: "durable writes",
      evidence_refs: ["ev-records"],
    },
  ],
  entry_points: ["gateway"],
  data_store_node_ids: ["records"],
  boundaries: ["public edge", "private data tier"],
  cost_links: [],
  cost_totals_snapshot: {},
  decisions: [
    {
      decision: "keep the worker stateless",
      rationale: "enables horizontal scaling without coordination",
      alternatives_considered: ["session affinity"],
      evidence_refs: ["ev-worker"],
    },
  ],
  assumptions: ["Fixture assumption retained verbatim."],
  warnings: [],
  evidence_references: ["ev-gateway", "ev-worker", "ev-records"],
  inputs_ref: inputs("session-fixture-a"),
  generated_at: "2026-01-01T00:00:00Z",
};

/** Fixture B: fan-out, shared dependency, one unpriced component. */
export const FIXTURE_B: ArchitectureResult = {
  schema_version: "architecture/v1",
  artifact_id: "artifact-fixture-b",
  session_id: "session-fixture-b",
  version: 2,
  supersedes_artifact_id: "artifact-fixture-b-prev",
  nodes: [
    {
      node_id: "collector",
      component_name: "Batch Collector",
      category: "integration",
      role_in_system: "accepts inbound batches",
      capacity: [],
      scaling_note: "",
      availability_note: "",
      evidence_refs: [],
    },
    {
      node_id: "buffer",
      component_name: "Durable Buffer",
      category: "messaging",
      role_in_system: "decouples producers from consumers",
      capacity: [{ metric: "peak_write_qps", value: 1200, unit: "qps" }],
      scaling_note: "partitioned queue",
      availability_note: "across three zones",
      evidence_refs: ["ev-buffer"],
    },
    {
      node_id: "reducer",
      component_name: "Reducer",
      category: "compute",
      role_in_system: "aggregates buffered batches",
      capacity: [{ metric: "peak_read_qps", value: 800, unit: "qps" }],
      scaling_note: "consumer group scaling",
      availability_note: "",
      evidence_refs: [],
    },
    {
      node_id: "archive",
      component_name: "Cold Archive",
      category: "storage",
      role_in_system: "retains raw payloads",
      capacity: [
        { metric: "storage_growth_bytes_per_day", value: 999, unit: "bytes/day" },
      ],
      scaling_note: "lifecycle transition",
      availability_note: "durable",
      evidence_refs: [],
    },
    {
      node_id: "dashboard",
      component_name: "Read Model",
      category: "database",
      role_in_system: "serves aggregated reads",
      capacity: [],
      scaling_note: "",
      availability_note: "",
      evidence_refs: [],
    },
  ],
  edges: [
    {
      edge_id: "collector->buffer",
      from_node: "collector",
      to_node: "buffer",
      kind: "write",
      protocol: "https",
      description: "enqueue batch",
      evidence_refs: ["ev-buffer"],
    },
    {
      edge_id: "buffer->reducer",
      from_node: "buffer",
      to_node: "reducer",
      kind: "async",
      protocol: "amqp",
      description: "consume batch",
      evidence_refs: [],
    },
    {
      edge_id: "reducer->archive",
      from_node: "reducer",
      to_node: "archive",
      kind: "write",
      protocol: "https",
      description: "persist raw payloads",
      evidence_refs: [],
    },
    {
      edge_id: "reducer->dashboard",
      from_node: "reducer",
      to_node: "dashboard",
      kind: "read",
      protocol: "tls",
      description: "write aggregated read model",
      evidence_refs: [],
    },
    {
      edge_id: "dashboard->client",
      from_node: "dashboard",
      to_node: "client",
      kind: "read",
      protocol: "https",
      description: "read model queries",
      evidence_refs: [],
    },
  ],
  entry_points: ["collector"],
  data_store_node_ids: ["archive", "dashboard"],
  boundaries: ["ingest edge", "processing plane"],
  cost_links: [
    {
      component_name: "Durable Buffer",
      priced: true,
      pricing_model: "on_demand",
      unit: "requests",
      monthly_cost_usd: 12.5,
      pricing_source: "aws-price-list-api",
      unpriced_reason: null,
    },
    {
      component_name: "Cold Archive",
      priced: false,
      pricing_model: null,
      unit: null,
      monthly_cost_usd: null,
      pricing_source: null,
      unpriced_reason: "No live Price List fact matched this component.",
    },
  ],
  cost_totals_snapshot: { on_demand: 12.5 },
  decisions: [],
  assumptions: ["Retention window delegated to the architect."],
  warnings: ["One component could not be priced."],
  evidence_references: ["ev-buffer"],
  inputs_ref: inputs("session-fixture-b"),
  generated_at: "2026-01-01T00:00:00Z",
};

/** Session view matching fixture A (design run complete, no cost artifact). */
export function sessionForA(): SessionView {
  return {
    session_id: "session-fixture-a",
    status: "ready",
    user_request: "design a generic three-tier workload",
    requirements: {
      application_description: "generic three-tier workload",
      cloud_provider: "aws",
      region: "us-east-1",
    },
    assumptions: [
      {
        name: "region",
        value: "us-east-1",
        unit: "",
        source: "architect_default",
        evidence: "",
        rationale: "User delegated region selection.",
        confidence: "medium",
      },
    ],
    pending_clarification: null,
    warnings: [],
    error: null,
    stages: {
      has_design_run: true,
      design_status: "ready",
      design_error: null,
      capacity: null,
      selection: null,
      architecture: {
        artifact_id: "artifact-fixture-a",
        schema_version: "architecture/v1",
        version: 1,
        supersedes_artifact_id: null,
        node_count: 3,
        edge_count: 2,
        entry_points: ["gateway"],
        data_store_node_ids: ["records"],
        region_code: "us-east-1",
        generated_at: "2026-01-01T00:00:00Z",
        warning_count: 0,
      },
      cost: null,
      quality_gate: null,
      critique: null,
      design_approval: null,
    },
  };
}

/** Session view matching fixture B (cost present, one unpriced component). */
export function sessionForB(): SessionView {
  return {
    session_id: "session-fixture-b",
    status: "ready",
    user_request: "design a generic batch pipeline",
    requirements: {
      application_description: "generic batch pipeline",
      cloud_provider: "aws",
      region: "eu-west-1",
    },
    assumptions: [],
    pending_clarification: {
      requirement_key: "retention",
      requirement: "retention window",
      question: "How long should raw payloads be retained?",
      reason: "Retention changes storage selection.",
      impact: "architecture",
      assumable: true,
    },
    warnings: ["One component could not be priced."],
    error: null,
    stages: {
      has_design_run: true,
      design_status: "ready",
      design_error: null,
      capacity: null,
      selection: null,
      architecture: {
        artifact_id: "artifact-fixture-b",
        schema_version: "architecture/v1",
        version: 2,
        supersedes_artifact_id: "artifact-fixture-b-prev",
        node_count: 5,
        edge_count: 5,
        entry_points: ["collector"],
        data_store_node_ids: ["archive", "dashboard"],
        region_code: "eu-west-1",
        generated_at: "2026-01-01T00:00:00Z",
        warning_count: 1,
      },
        cost: {
        totals_usd_per_month: { on_demand: 12.5 },
        priced: [
          {
            component_name: "Durable Buffer",
            monthly_cost_usd: 12.5,
            unit: "requests",
            pricing_model: "on_demand",
            pricing_source: "aws-price-list-api",
            region_code: "eu-west-1",
          },
        ],
        unpriced: [
          {
            component_name: "Cold Archive",
            reason: "No live Price List fact matched this component.",
          },
        ],
        coverage: [
          {
            component_name: "Durable Buffer",
            dimension: "requests",
            label: "requests",
            status: "priced",
            reason: "Included in the estimated monthly total.",
          },
        ],
        warnings: ["One component could not be priced."],
        assumptions: [],
        pricing_as_of: "2026-01-01T00:00:00Z",
        fully_priced: false,
        completeness: "partially_unresolved",
        best_estimate_total_usd_per_month: null,
        estimated_range_usd_per_month: null,
        unresolved_dimension_count: 0,
      },
      quality_gate: null,
      critique: null,
      design_approval: null,
    },
  };
}

/** Session view with no design run yet (empty architecture state). */
export function sessionWithoutDesignRun(): SessionView {
  return {
    session_id: "session-empty",
    status: "ready",
    user_request: "design something not yet run",
    requirements: { cloud_provider: "aws" },
    assumptions: [],
    pending_clarification: null,
    warnings: [],
    error: null,
    stages: {
      has_design_run: false,
      design_status: "",
      design_error: null,
      capacity: null,
      selection: null,
      architecture: null,
      cost: null,
      quality_gate: null,
      critique: null,
      design_approval: null,
    },
  };
}

