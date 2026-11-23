<script setup lang="ts">
import EmptyState from "./EmptyState.vue";
import type { ArchitectureResult } from "../types";

defineProps<{ architecture: ArchitectureResult | null }>();
</script>

<template>
  <div class="field">
    <h3 class="field__label">
      Architecture assumptions ({{ architecture ? architecture.assumptions.length : 0 }})
    </h3>
    <EmptyState
      v-if="!architecture || architecture.assumptions.length === 0"
      label="No architecture assumptions."
    />
    <ul v-else class="plain-list" data-testid="architecture-assumptions">
      <li v-for="(assumption, index) in architecture.assumptions" :key="index">
        {{ assumption }}
      </li>
    </ul>
  </div>
</template>
