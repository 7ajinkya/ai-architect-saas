<script setup lang="ts">
/**
 * Workspace status bar.
 *
 * Reports only what the backend stated. There is no computed "VALID"
 * label: the architecture artifact carries no validation verdict, so the
 * bar shows the design-run status and the artifact version instead of
 * inventing a health claim.
 */
import { computed } from "vue";

import { formatUsd, totalMonthlyCost } from "../api/client";
import type { SessionView } from "../types";

const props = defineProps<{
  session: SessionView;
  unpricedCount: number;
}>();

const architecture = computed(() => props.session.stages.architecture);
const cost = computed(() => props.session.stages.cost);
const total = computed(() => totalMonthlyCost(cost.value));
const statusLabel = computed(() => {
  if (props.session.stages.design_status) return props.session.stages.design_status;
  return props.session.stages.has_design_run ? "unknown" : "not run";
});
</script>

<template>
  <footer class="statusbar" aria-label="Workspace status">
    <span class="statusbar__item">
      <span class="statusbar__key">Design status</span>
      <span class="statusbar__value" data-testid="status-design">{{ statusLabel }}</span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Requirements</span>
      <span class="statusbar__value">{{ props.session.status || "unknown" }}</span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Nodes</span>
      <span class="statusbar__value" data-testid="status-nodes">
        {{ architecture ? architecture.node_count : 0 }}
      </span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Cost</span>
      <span class="statusbar__value" data-testid="status-cost">
        {{ cost ? formatUsd(total) : "not available" }}
      </span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Unpriced</span>
      <span class="statusbar__value" data-testid="status-unpriced">
        {{ cost ? props.unpricedCount : "not available" }}
      </span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Warnings</span>
      <span class="statusbar__value" data-testid="status-warnings">
        {{ props.session.warnings.length + (architecture ? architecture.warning_count : 0) }}
      </span>
    </span>

    <span class="statusbar__item">
      <span class="statusbar__key">Artifact</span>
      <span class="statusbar__value" data-testid="status-artifact">
        {{ architecture ? `v${architecture.version}` : "—" }}
      </span>
    </span>

    <span v-if="props.session.stages.design_error" class="statusbar__item statusbar__item--error">
      <span class="statusbar__key">Error</span>
      <span class="statusbar__value" data-testid="status-error">
        {{ props.session.stages.design_error }}
      </span>
    </span>
  </footer>
</template>
