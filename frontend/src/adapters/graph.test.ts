/**
 * Graph adapter tests.
 *
 * These are the core proof that the frontend is generic: the same code
 * path renders a linear chain and a fan-out with an external endpoint,
 * using only the persisted artifact.
 */

import { describe, expect, it } from "vitest";

import {
  computeDepths,
  findGraphNode,
  indexCostLinks,
  referencedEndpoints,
  toGraphModel,
} from "../adapters/graph";
import { FIXTURE_A, FIXTURE_B } from "../fixtures/fixtures";
import type { ArchitectureResult } from "../types";

describe("toGraphModel", () => {
  it("renders every node of a linear architecture", () => {
    const model = toGraphModel(FIXTURE_A);
    expect(model.nodes).toHaveLength(3);
    expect(model.nodes.map((n) => n.id)).toEqual([
      "gateway",
      "worker",
      "records",
    ]);
  });

  it("renders every edge of a linear architecture", () => {
    const model = toGraphModel(FIXTURE_A);
    expect(model.edges).toHaveLength(2);
    expect(model.edges[0]).toMatchObject({
      source: "gateway",
      target: "worker",
    });
  });

  it("renders a fan-out architecture with the same code", () => {
    const model = toGraphModel(FIXTURE_B);
    // Five declared nodes plus the undeclared external endpoint.
    expect(model.nodes).toHaveLength(6);
    expect(FIXTURE_B.nodes).toHaveLength(5);
    expect(model.externalNodeIds).toEqual(["client"]);
    expect(model.nodes.map((n) => n.id)).toContain("client");
    expect(model.edges).toHaveLength(5);
  });

  it("marks undeclared endpoints as external, not architecture nodes", () => {
    const model = toGraphModel(FIXTURE_B);
    const client = findGraphNode(model, "client");
    expect(client).not.toBeNull();
    expect(client!.data.external).toBe(true);
    expect(client!.data.node).toBeNull();
  });

  it("never drops an edge whose endpoint is undeclared", () => {
    const model = toGraphModel(FIXTURE_B);
    for (const edge of model.edges) {
      expect(model.nodes.some((n) => n.id === edge.source)).toBe(true);
      expect(model.nodes.some((n) => n.id === edge.target)).toBe(true);
    }
  });

  it("layers a linear chain top to bottom", () => {
    const model = toGraphModel(FIXTURE_A);
    const depths = model.nodes.map((n) => n.data.depth);
    expect(depths).toEqual([0, 1, 2]);
    const ys = model.nodes.map((n) => n.position.y);
    expect(ys[0]).toBeLessThan(ys[1]);
    expect(ys[1]).toBeLessThan(ys[2]);
    const xs = model.nodes.map((n) => n.position.x);
    expect(xs[0]).toBe(xs[1]);
    expect(xs[1]).toBe(xs[2]);
  });

  it("places a fan-out at the same depth", () => {
    const model = toGraphModel(FIXTURE_B);
    const archive = findGraphNode(model, "archive")!;
    const dashboard = findGraphNode(model, "dashboard")!;
    expect(archive.data.depth).toBe(dashboard.data.depth);
    expect(archive.position.y).toBe(dashboard.position.y);
    expect(archive.position.x).not.toBe(dashboard.position.x);
  });

  it("rails control-only supporting nodes beside the main flow", () => {
    const supporting: ArchitectureResult = {
      ...FIXTURE_A,
      nodes: [
        ...FIXTURE_A.nodes,
        {
          node_id: "telemetry",
          component_name: "Telemetry Sink",
          category: "monitoring",
          role_in_system: "collects signals",
          capacity: [],
          scaling_note: "",
          availability_note: "",
          evidence_refs: [],
        },
      ],
      edges: [
        ...FIXTURE_A.edges,
        {
          edge_id: "worker->telemetry",
          from_node: "worker",
          to_node: "telemetry",
          kind: "control",
          protocol: "",
          description: "emits signals",
          evidence_refs: [],
        },
      ],
    };
    const model = toGraphModel(supporting);
    const sink = findGraphNode(model, "telemetry")!;
    const worker = findGraphNode(model, "worker")!;
    const records = findGraphNode(model, "records")!;
    expect(sink.data.rail).toBe(true);
    expect(worker.data.rail).toBe(false);
    // Same depth row as its main-flow peer, off to the side.
    expect(sink.data.depth).toBe(records.data.depth);
    expect(sink.position.y).toBe(records.position.y);
    expect(sink.position.x).toBeGreaterThan(records.position.x);
  });

  it("lays out deterministically for the same artifact", () => {
    const first = toGraphModel(FIXTURE_B);
    const second = toGraphModel(FIXTURE_B);
    expect(second.nodes.map((n) => n.position)).toEqual(
      first.nodes.map((n) => n.position),
    );
  });

  it("defaults to the vertical orientation", () => {
    expect(toGraphModel(FIXTURE_B)).toEqual(
      toGraphModel(FIXTURE_B, "vertical"),
    );
  });

  it("layers a linear chain left to right in horizontal mode", () => {
    const model = toGraphModel(FIXTURE_A, "horizontal");
    const depths = model.nodes.map((n) => n.data.depth);
    expect(depths).toEqual([0, 1, 2]);
    const xs = model.nodes.map((n) => n.position.x);
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    const ys = model.nodes.map((n) => n.position.y);
    expect(ys[0]).toBe(ys[1]);
    expect(ys[1]).toBe(ys[2]);
  });

  it("places a horizontal fan-out in one column", () => {
    const model = toGraphModel(FIXTURE_B, "horizontal");
    const archive = findGraphNode(model, "archive")!;
    const dashboard = findGraphNode(model, "dashboard")!;
    expect(archive.data.depth).toBe(dashboard.data.depth);
    expect(archive.position.x).toBe(dashboard.position.x);
    expect(archive.position.y).not.toBe(dashboard.position.y);
  });

  it("rails control-only nodes below the main flow in horizontal mode", () => {
    const supporting: ArchitectureResult = {
      ...FIXTURE_A,
      nodes: [
        ...FIXTURE_A.nodes,
        {
          node_id: "telemetry",
          component_name: "Telemetry Sink",
          category: "monitoring",
          role_in_system: "collects signals",
          capacity: [],
          scaling_note: "",
          availability_note: "",
          evidence_refs: [],
        },
      ],
      edges: [
        ...FIXTURE_A.edges,
        {
          edge_id: "worker->telemetry",
          from_node: "worker",
          to_node: "telemetry",
          kind: "control",
          protocol: "",
          description: "emits signals",
          evidence_refs: [],
        },
      ],
    };
    const model = toGraphModel(supporting, "horizontal");
    const sink = findGraphNode(model, "telemetry")!;
    const records = findGraphNode(model, "records")!;
    expect(sink.data.rail).toBe(true);
    expect(sink.data.depth).toBe(records.data.depth);
    // Same depth column, dropped below the main flow.
    expect(sink.position.x).toBe(records.position.x);
    expect(sink.position.y).toBeGreaterThan(records.position.y);
  });

  it("keeps depths and rail membership identical across orientations", () => {
    const vertical = toGraphModel(FIXTURE_B, "vertical");
    const horizontal = toGraphModel(FIXTURE_B, "horizontal");
    expect(vertical.nodes.map((n) => n.id)).toEqual(
      horizontal.nodes.map((n) => n.id),
    );
    expect(vertical.nodes.map((n) => n.data.depth)).toEqual(
      horizontal.nodes.map((n) => n.data.depth),
    );
    expect(vertical.nodes.map((n) => n.data.rail)).toEqual(
      horizontal.nodes.map((n) => n.data.rail),
    );
    expect(vertical.nodes.map((n) => n.position)).not.toEqual(
      horizontal.nodes.map((n) => n.position),
    );
  });

  it("lays out horizontal deterministically for the same artifact", () => {
    const first = toGraphModel(FIXTURE_B, "horizontal");
    const second = toGraphModel(FIXTURE_B, "horizontal");
    expect(second.nodes.map((n) => n.position)).toEqual(
      first.nodes.map((n) => n.position),
    );
  });

  it("reports entry points and data stores from the artifact", () => {
    const a = toGraphModel(FIXTURE_A);
    expect(a.entryPointIds).toEqual(["gateway"]);
    expect(a.dataStoreNodeIds).toEqual(["records"]);

    const b = toGraphModel(FIXTURE_B);
    expect(b.entryPointIds).toEqual(["collector"]);
    expect(b.dataStoreNodeIds).toEqual(["archive", "dashboard"]);
  });

  it("preserves the persisted node id verbatim", () => {
    const model = toGraphModel(FIXTURE_A);
    expect(findGraphNode(model, "gateway")!.id).toBe(FIXTURE_A.nodes[0].node_id);
  });

  it("joins cost links by component name", () => {
    const model = toGraphModel(FIXTURE_B);
    const buffer = findGraphNode(model, "buffer")!;
    expect(buffer.data.costLink?.priced).toBe(true);
    const archive = findGraphNode(model, "archive")!;
    expect(archive.data.costLink?.priced).toBe(false);
    expect(archive.data.costLink?.monthly_cost_usd).toBeNull();
  });

  it("leaves a node without a cost link unpriced rather than guessing", () => {
    const model = toGraphModel(FIXTURE_A);
    for (const node of model.nodes) {
      expect(node.data.costLink).toBeNull();
    }
  });

  it("is deterministic across repeated calls", () => {
    const first = toGraphModel(FIXTURE_B);
    const second = toGraphModel(FIXTURE_B);
    expect(JSON.stringify(second)).toBe(JSON.stringify(first));
  });

  it("returns an empty model for an artifact with no nodes", () => {
    const empty: ArchitectureResult = {
      ...FIXTURE_A,
      nodes: [],
      edges: [],
      entry_points: [],
      data_store_node_ids: [],
    };
    const model = toGraphModel(empty);
    expect(model.nodes).toHaveLength(0);
    expect(model.edges).toHaveLength(0);
  });

  it("tolerates an edge referencing an unknown node on both sides", () => {
    const odd: ArchitectureResult = {
      ...FIXTURE_A,
      nodes: [],
      edges: [
        {
          edge_id: "ghost-a-ghost-b",
          from_node: "ghost-a",
          to_node: "ghost-b",
          kind: "async",
          protocol: "",
          description: "orphan",
          evidence_refs: [],
        },
      ],
    };
    const model = toGraphModel(odd);
    expect(model.externalNodeIds).toEqual(["ghost-a", "ghost-b"]);
    expect(model.edges).toHaveLength(1);
  });
});

describe("computeDepths", () => {
  it("terminates on a cyclic artifact", () => {
    const edges = [
      { from_node: "a", to_node: "b" },
      { from_node: "b", to_node: "a" },
    ];
    const depths = computeDepths(["a", "b"], edges as never);
    expect(depths.get("a")).toBeGreaterThanOrEqual(0);
    expect(depths.get("b")).toBeGreaterThanOrEqual(0);
  });

  it("places roots at depth zero", () => {
    const depths = computeDepths(["a", "b"], [] as never);
    expect(depths.get("a")).toBe(0);
    expect(depths.get("b")).toBe(0);
  });
});

describe("helpers", () => {
  it("indexes cost links by component name", () => {
    const index = indexCostLinks(FIXTURE_B);
    expect(index.get("Durable Buffer")?.priced).toBe(true);
    expect(index.get("Cold Archive")?.unpriced_reason).toBeTruthy();
  });

  it("collects referenced endpoints in first-seen order", () => {
    const { sources, targets } = referencedEndpoints(FIXTURE_A.edges);
    expect(sources).toEqual(["gateway", "worker"]);
    expect(targets).toEqual(["worker", "records"]);
  });

  it("returns null for an unknown or empty selection", () => {
    const model = toGraphModel(FIXTURE_A);
    expect(findGraphNode(model, null)).toBeNull();
    expect(findGraphNode(model, "does-not-exist")).toBeNull();
  });
});
