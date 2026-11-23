<script setup lang="ts">
/**
 * Architecture graph.
 *
 * Vue Flow is used strictly as a rendering and interaction layer. It
 * receives nodes and edges derived from the persisted artifact by the
 * pure adapter and has no knowledge of architecture generation.
 *
 * Read-only by design: nodes may be dragged, but positions live only in
 * local component state and are never sent to the backend. Architecture
 * editing belongs to a later slice.
 */
import { computed, ref, watch } from "vue";
import {
  Handle,
  MarkerType,
  Position,
  VueFlow,
  type Edge,
  type Node,
  type NodeMouseEvent,
} from "@vue-flow/core";
import { Background } from "@vue-flow/background";
import { Controls } from "@vue-flow/controls";

import EmptyState from "./EmptyState.vue";
import { toGraphModel, type GraphModel, type GraphOrientation } from "../adapters/graph";
import type { ArchitectureResult } from "../types";

const props = defineProps<{
  architecture: ArchitectureResult | null;
  model: GraphModel;
}>();

const emit = defineEmits<{
  (event: "select-node", nodeId: string | null): void;
}>();

/**
 * Canvas orientation is a presentation preference only: vertical
 * (top to bottom) by default, horizontal (left to right) on request.
 * It never touches the architecture artifact, is never sent anywhere,
 * and only re-layouts on explicit switch or a new artifact — never on
 * plain rerenders, so user-dragged nodes stay where they were put.
 */
const orientation = ref<GraphOrientation>("vertical");

const laidOut = computed(() =>
  props.architecture
    ? toGraphModel(props.architecture, orientation.value)
    : props.model,
);;

/**
 * Flow direction decides where edges connect: vertical flows enter at
 * the top and leave at the bottom, horizontal flows enter at the left
 * and leave at the right. Without explicit handles Vue Flow's slot
 * override loses them and every edge attaches bottom-to-top.
 */
function handlePositions(
  orientation: GraphOrientation,
): { target: Position; source: Position } {
  return orientation === "vertical"
    ? { target: Position.Top, source: Position.Bottom }
    : { target: Position.Left, source: Position.Right };
}

function toFlowNodes(model: GraphModel, orientation: GraphOrientation): Node[] {
  const handles = handlePositions(orientation);
  return model.nodes.map((node) => ({
    id: node.id,
    type: "default",
    position: node.position,
    sourcePosition: handles.source,
    targetPosition: handles.target,
    data: {
      label: node.label,
      category: node.category,
      external: node.data.external,
    },
    class: node.data.external ? "flow-node flow-node--external" : "flow-node",
  }));
}

/**
 * Smoothstep routes long multi-layer edges as rounded orthogonal
 * segments instead of wide beziers, and every edge carries a closed
 * arrowhead at its target so direction is readable at a glance.
 */
function toFlowEdges(model: GraphModel): Edge[] {
  return model.edges.map((edge) => ({
    id: edge.id,
    type: "smoothstep",
    source: edge.source,
    target: edge.target,
    label: edge.label,
    markerEnd: MarkerType.ArrowClosed,
  }));
}

// Positions live only here: dragging is local presentation state and
// must never mutate the architecture artifact.
const nodes = ref<Node[]>([]);
const edges = ref<Edge[]>([]);

const handles = computed(() => handlePositions(orientation.value));
const baseNodes = computed(() =>
  toFlowNodes(laidOut.value, orientation.value),
);
const baseEdges = computed(() => toFlowEdges(laidOut.value));

watch(
  baseNodes,
  (next) => {
    nodes.value = next;
  },
  { immediate: true },
);

watch(
  baseEdges,
  (next) => {
    edges.value = next;
  },
  { immediate: true },
);

function onNodeClick(payload: NodeMouseEvent): void {
  emit("select-node", payload.node.id);
}

function onPaneClick(): void {
  emit("select-node", null);
}
</script>

<template>
  <EmptyState
    v-if="!props.architecture || props.model.nodes.length === 0"
    label="No architecture artifact for this session yet."
  />
  <div v-else class="graph" data-testid="architecture-graph">
    <div
      class="graph-toolbar"
      role="group"
      aria-label="Graph layout orientation"
      data-testid="graph-orientation"
    >
      <button
        type="button"
        class="graph-toolbar__btn"
        data-testid="orientation-vertical"
        :aria-pressed="orientation === 'vertical'"
        :class="{ 'graph-toolbar__btn--active': orientation === 'vertical' }"
        @click="orientation = 'vertical'"
      >
        Vertical
      </button>
      <button
        type="button"
        class="graph-toolbar__btn"
        data-testid="orientation-horizontal"
        :aria-pressed="orientation === 'horizontal'"
        :class="{ 'graph-toolbar__btn--active': orientation === 'horizontal' }"
        @click="orientation = 'horizontal'"
      >
        Horizontal
      </button>
    </div>
    <VueFlow
      :key="orientation"
      v-model:nodes="nodes"
      v-model:edges="edges"
      :nodes-draggable="true"
      :nodes-connectable="false"
      :elements-selectable="true"
      :fit-view-on-init="true"
      :min-zoom="0.2"
      :max-zoom="2"
      @node-click="onNodeClick"
      @pane-click="onPaneClick"
    >
      <Background />
      <Controls :show-interactive="false" />
      <template #node-default="nodeProps">
        <Handle type="target" :position="handles.target" />
        <div class="flow-node__label">{{ nodeProps.data.label }}</div>
        <Handle type="source" :position="handles.source" />
      </template>
    </VueFlow>
  </div>
</template>
