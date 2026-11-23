<script setup lang="ts">
/**
 * Architecture-level decisions, assumptions and warnings.
 *
 * Values are displayed exactly as the backend produced them. The
 * frontend never invents a warning or an assumption.
 */
import EmptyState from "./EmptyState.vue";
import type { ArchitectureResult } from "../types";

defineProps<{ architecture: ArchitectureResult | null }>();
</script>

<template>
  <div class="field">
    <h3 class="field__label">
      Decisions ({{ architecture ? architecture.decisions.length : 0 }})
    </h3>
    <EmptyState
      v-if="!architecture || architecture.decisions.length === 0"
      label="No decisions recorded."
    />
    <ul v-else class="decisions" data-testid="architecture-decisions">
      <li v-for="(decision, index) in architecture.decisions" :key="index" class="decision">
        <p class="decision__text">{{ decision.decision }}</p>
        <p class="decision__rationale">{{ decision.rationale }}</p>
        <p
          v-if="decision.alternatives_considered.length > 0"
          class="decision__alternatives"
        >
          alternatives: {{ decision.alternatives_considered.join(", ") }}
        </p>
      </li>
    </ul>
  </div>
</template>
