<script setup lang="ts">
/**
 * Node details for the selected architecture node.
 *
 * Everything shown comes from the persisted artifact: component name,
 * category, role, capacity values, scaling/availability notes, evidence
 * references and the joined cost link. An unpriced component is shown as
 * unpriced with the backend's reason, never as $0.
 */
import { computed } from "vue";

import { formatUsd } from "../api/client";
import EmptyState from "./EmptyState.vue";
import type { GraphNode } from "../adapters/graph";

const props = defineProps<{
  node: GraphNode | null;
  isEntryPoint: boolean;
  isDataStore: boolean;
}>();

const backend = computed(() => props.node?.data.node ?? null);
const cost = computed(() => props.node?.data.costLink ?? null);
</script>

<template>
  <section class="panel" aria-label="Architecture details">
    <h2 class="panel__title">Architecture details</h2>

    <EmptyState v-if="!props.node" label="Select a node to inspect it." />

    <template v-else>
      <div class="field">
        <p class="detail-name" data-testid="detail-name">{{ props.node.label }}</p>
        <p class="field__meta" data-testid="detail-node-id">
          node_id: {{ props.node.id }}
        </p>
        <p class="detail-category" data-testid="detail-category">
          {{ props.node.category || "—" }}
        </p>
      </div>

      <div class="field">
        <h3 class="field__label">Role in system</h3>
        <p class="field__value" data-testid="detail-role">
          {{ props.node.role || "—" }}
        </p>
      </div>

      <p
        v-if="props.node.data.external"
        class="badge badge--muted"
        data-testid="detail-external"
      >
        External endpoint (not an architecture node)
      </p>

      <p
        v-if="!props.node.data.external && (props.isEntryPoint || props.isDataStore)"
        class="badge badge--muted"
      >
        <template v-if="props.isEntryPoint">Entry point</template>
        <template v-if="props.isEntryPoint && props.isDataStore"> · </template>
        <template v-if="props.isDataStore">Data store</template>
      </p>

      <div v-if="backend && backend.capacity.length > 0" class="field">
        <h3 class="field__label">Capacity</h3>
        <ul class="kv" data-testid="detail-capacity">
          <li
            v-for="item in backend.capacity"
            :key="`${item.metric}-${item.unit}`"
            class="kv__row"
          >
            <span class="kv__key">{{ item.metric }}</span>
            <span class="kv__value">{{ item.value }} {{ item.unit }}</span>
          </li>
        </ul>
      </div>

      <div v-if="backend" class="field">
        <h3 class="field__label">Scaling</h3>
        <p class="field__value">{{ backend.scaling_note || "—" }}</p>
        <h3 class="field__label">Availability</h3>
        <p class="field__value">{{ backend.availability_note || "—" }}</p>
      </div>

      <div v-if="backend && backend.evidence_refs.length > 0" class="field">
        <h3 class="field__label">Evidence</h3>
        <ul class="tags" data-testid="detail-evidence">
          <li v-for="ref in backend.evidence_refs" :key="ref" class="tag">{{ ref }}</li>
        </ul>
      </div>

      <div class="field">
        <h3 class="field__label">Cost</h3>
        <p v-if="!cost" class="field__value" data-testid="detail-cost">
          No cost link for this component.
        </p>
        <p v-else-if="cost.priced" class="field__value" data-testid="detail-cost">
          {{ formatUsd(cost.monthly_cost_usd) }}
          <template v-if="cost.unit"> · {{ cost.unit }}</template>
          <template v-if="cost.pricing_model"> · {{ cost.pricing_model }}</template>
        </p>
        <div v-else class="field__unpriced" data-testid="detail-cost-unpriced">
          <p class="field__value">unpriced</p>
          <p v-if="cost.unpriced_reason" class="field__meta">{{ cost.unpriced_reason }}</p>
        </div>
      </div>
    </template>
  </section>
</template>
