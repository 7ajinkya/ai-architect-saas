<script setup lang="ts">
import EmptyState from "./EmptyState.vue";

withDefaults(
  defineProps<{ warnings: string[]; testId?: string }>(),
  { testId: "warnings" },
);
</script>

<template>
  <EmptyState v-if="warnings.length === 0" label="No warnings." />
  <ul v-else class="warnings" :data-testid="testId">
    <li v-for="(warning, index) in warnings" :key="index" class="warning">
      {{ warning }}
    </li>
  </ul>
</template>
