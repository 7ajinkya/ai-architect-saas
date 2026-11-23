/**
 * Pure adapter: ArchitectureResult -> renderable graph model.
 *
 * This is the only place that knows how a graph is laid out, and it
 * knows nothing about how architectures are generated. It works purely
 * from the persisted artifact: node ids, edges, entry points and
 * declared external endpoints.
 *
 * Everything here is deterministic and side-effect free so it can be
 * unit tested without a rendering library.
 */

import type {
  ArchitectureEdge,
  ArchitectureNode,
  ArchitectureResult,
  CostLink,
} from "../types";

/** Endpoints that are not architecture nodes (matches the backend rule). */
export const EXTERNAL_ENDPOINTS = ["client", "external"] as const;

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 84;
export const COLUMN_GAP = 90;
export const ROW_GAP = 34;
/** Distance between depth layers along the flow axis. */
export const LAYER_GAP = 150;
/** Offset of the supporting-services rail from the main flow. */
export const RAIL_GAP = 140;

/** Presentation orientation for the architecture canvas. */
export type GraphOrientation = "vertical" | "horizontal";

export interface GraphNodeData {
  /** The persisted architecture node, when this is a real node. */
  node: ArchitectureNode | null;
  /** True when the id is a declared external endpoint, not a node. */
  external: boolean;
  /** Cost snapshot joined by component_name, when present. */
  costLink: CostLink | null;
  /** Index of the layer this node was placed in. */
  depth: number;
  /** True when the node sits in the supporting-services rail. */
  rail: boolean;
}

export interface GraphNode {
  id: string;
  label: string;
  category: string;
  role: string;
  position: { x: number; y: number };
  data: GraphNodeData;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  kind: string;
}

export interface GraphModel {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Ids that are external endpoints rather than architecture nodes. */
  externalNodeIds: string[];
  /** Node ids that were declared as entry points. */
  entryPointIds: string[];
  /** Node ids that were declared as data stores. */
  dataStoreNodeIds: string[];
}

/**
 * Join cost snapshots onto nodes by component_name.
 *
 * An exact join only: a node with no matching cost link stays
 * unpriced-looking rather than borrowing another component's price.
 */
export function indexCostLinks(
  architecture: ArchitectureResult,
): Map<string, CostLink> {
  const byName = new Map<string, CostLink>();
  for (const link of architecture.cost_links ?? []) {
    byName.set(link.component_name, link);
  }
  return byName;
}

/** Collect every endpoint referenced by an edge, in first-seen order. */
export function referencedEndpoints(
  edges: ArchitectureEdge[],
): { sources: string[]; targets: string[] } {
  const sources: string[] = [];
  const targets: string[] = [];
  const push = (list: string[], value: string) => {
    if (value && !list.includes(value)) list.push(value);
  };
  for (const edge of edges) {
    push(sources, edge.from_node);
    push(targets, edge.to_node);
  }
  return { sources, targets };
}

/**
 * Assign each node a layer using longest-path layering.
 *
 * Roots (no incoming edge) are layer 0. A node sits one layer below the
 * deepest predecessor it actually has. Cycles are tolerated: a node
 * whose predecessors are all already resolved keeps its current layer,
 * so a malformed graph cannot hang the UI.
 */
export function computeDepths(
  nodeIds: string[],
  edges: ArchitectureEdge[],
): Map<string, number> {
  const depths = new Map<string, number>();
  const incoming = new Map<string, string[]>();
  for (const id of nodeIds) {
    depths.set(id, 0);
    incoming.set(id, []);
  }
  for (const edge of edges) {
    if (!depths.has(edge.to_node) || !depths.has(edge.from_node)) continue;
    incoming.get(edge.to_node)!.push(edge.from_node);
  }

  const unresolved = new Set(nodeIds);
  // Bound iterations so a cyclic artifact cannot spin forever.
  const maxPasses = nodeIds.length + 1;
  for (let pass = 0; pass < maxPasses && unresolved.size > 0; pass += 1) {
    let progressed = false;
    for (const id of Array.from(unresolved)) {
      const parents = incoming.get(id)!;
      let settled = true;
      let depth = 0;
      for (const parent of parents) {
        if (unresolved.has(parent)) {
          settled = false;
          break;
        }
        depth = Math.max(depth, depths.get(parent)! + 1);
      }
      if (!settled) continue;
      depths.set(id, depth);
      unresolved.delete(id);
      progressed = true;
    }
    if (!progressed) break;
  }

  // Anything still unresolved (a pure cycle) is placed deterministically
  // after its resolved peers rather than being dropped.
  let fallback = 0;
  for (const id of nodeIds) {
    if (!unresolved.has(id)) continue;
    const parents = incoming.get(id)!.filter((p) => !unresolved.has(p));
    const depth = parents.length
      ? Math.max(...parents.map((p) => depths.get(p)!)) + 1
      : fallback;
    depths.set(id, depth);
    fallback = depth + 1;
  }

  return depths;
}

/**
 * Build the renderable graph model for any valid architecture artifact.
 *
 * `orientation` is a pure presentation preference (default vertical):
 * the same longest-path layering drives both directions, transposed.
 * Vertical flows top to bottom with the supporting rail on the right;
 * horizontal flows left to right with the supporting rail at the
 * bottom. Depths, rail membership and every id are identical either
 * way; only coordinates change.
 */
export function toGraphModel(
  architecture: ArchitectureResult,
  orientation: GraphOrientation = "vertical",
): GraphModel {
  const nodes = architecture.nodes ?? [];
  const edges = architecture.edges ?? [];

  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const costByName = indexCostLinks(architecture);

  // Endpoints used by edges but not declared as nodes are rendered as
  // clearly-marked external endpoints instead of dropping the edge.
  const { sources, targets } = referencedEndpoints(edges);
  const externalIds: string[] = [];
  for (const id of [...sources, ...targets]) {
    if (!byId.has(id) && !externalIds.includes(id)) externalIds.push(id);
  }

  const nodeIds = [...nodes.map((n) => n.node_id), ...externalIds];
  const depths = computeDepths(nodeIds, edges);

  // Supporting services (observability, config/health signaling and the
  // like) attach to the system only through control flows. They are
  // placed in a side rail so the main request/data path reads top to
  // bottom without them stretching every layer. Purely structural:
  // edge kinds only, never service names or categories.
  const incidentKinds = new Map<string, Set<string>>();
  for (const id of nodeIds) incidentKinds.set(id, new Set());
  for (const edge of edges) {
    if (incidentKinds.has(edge.from_node)) {
      incidentKinds.get(edge.from_node)!.add(String(edge.kind));
    }
    if (incidentKinds.has(edge.to_node)) {
      incidentKinds.get(edge.to_node)!.add(String(edge.kind));
    }
  }
  const isRail = (id: string): boolean => {
    if (!byId.has(id)) return false;
    const kinds = incidentKinds.get(id)!;
    return kinds.size > 0 && [...kinds].every((k) => k === "control");
  };

  const byDepth = new Map<number, string[]>();
  const railByDepth = new Map<number, string[]>();
  for (const id of nodeIds) {
    const depth = depths.get(id) ?? 0;
    const bucket = isRail(id)
      ? railByDepth
      : byDepth;
    if (!bucket.has(depth)) bucket.set(depth, []);
    bucket.get(depth)!.push(id);
  }

  // Main-flow span anchors the rail: the widest layer sets the span
  // so every layer centers on the same axis.
  //
  // Coordinates are computed on (flow, cross) axes and mapped to (x, y)
  // at the end, so horizontal is the exact transpose of vertical: depth
  // layers stay evenly spaced, siblings share a row/column, and the
  // supporting rail sits clear of the main path in both directions.
  const flowSize = orientation === "vertical" ? NODE_HEIGHT : NODE_WIDTH;
  const crossSize = orientation === "vertical" ? NODE_WIDTH : NODE_HEIGHT;
  const toXY = (
    flow: number,
    cross: number,
  ): { x: number; y: number } =>
    orientation === "vertical"
      ? { x: cross, y: flow }
      : { x: flow, y: cross };
  const layerSpan = (count: number): number =>
    count <= 0 ? 0 : count * crossSize + (count - 1) * COLUMN_GAP;
  const mainSpan = Math.max(
    crossSize,
    ...Array.from(byDepth.values()).map((layer) => layerSpan(layer.length)),
  );

  const graphNodes: GraphNode[] = [];
  const pushNode = (
    id: string,
    flow: number,
    cross: number,
    rail: boolean,
    depth: number,
  ): void => {
    const node = byId.get(id) ?? null;
    const external = node === null;
    graphNodes.push({
      id,
      label: node ? node.component_name : id,
      category: node ? node.category : "external",
      role: node ? node.role_in_system : "external endpoint",
      position: toXY(flow, cross),
      data: {
        node,
        external,
        costLink: node ? costByName.get(node.component_name) ?? null : null,
        depth,
        rail,
      },
    });
  };

  for (const depth of Array.from(byDepth.keys()).sort((a, b) => a - b)) {
    const layer = byDepth.get(depth)!;
    const width = layerSpan(layer.length);
    const flow = depth * (flowSize + LAYER_GAP);
    layer.forEach((id, index) => {
      pushNode(id, flow, (mainSpan - width) / 2 + index * (crossSize + COLUMN_GAP), false, depth);
    });
    // Rail nodes share their depth row, stacked to the side of the
    // main flow in deterministic id order.
    const rail = [...(railByDepth.get(depth) ?? [])].sort();
    rail.forEach((id, index) => {
      pushNode(
        id,
        flow + index * (flowSize + ROW_GAP),
        mainSpan + RAIL_GAP,
        true,
        depth,
      );
    });
  }
  // A rail entry at a depth with no main-flow layer (degenerate, but
  // tolerated) still renders instead of vanishing.
  for (const depth of Array.from(railByDepth.keys()).sort((a, b) => a - b)) {
    if (byDepth.has(depth)) continue;
    const rail = [...railByDepth.get(depth)!].sort();
    const flow = depth * (flowSize + LAYER_GAP);
    rail.forEach((id, index) => {
      pushNode(id, flow + index * (flowSize + ROW_GAP), mainSpan + RAIL_GAP, true, depth);
    });
  }

  const graphEdges: GraphEdge[] = edges.map((edge) => ({
    id: edge.edge_id,
    source: edge.from_node,
    target: edge.to_node,
    label: edge.protocol || edge.kind,
    kind: edge.kind,
  }));

  return {
    nodes: graphNodes,
    edges: graphEdges,
    externalNodeIds: externalIds,
    entryPointIds: (architecture.entry_points ?? []).filter((id) =>
      byId.has(id),
    ),
    dataStoreNodeIds: (architecture.data_store_node_ids ?? []).filter((id) =>
      byId.has(id),
    ),
  };
}

/** Locate one node's full detail payload by its persisted id. */
export function findGraphNode(
  model: GraphModel,
  nodeId: string | null,
): GraphNode | null {
  if (!nodeId) return null;
  return model.nodes.find((node) => node.id === nodeId) ?? null;
}
