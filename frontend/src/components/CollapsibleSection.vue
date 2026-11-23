<script setup lang="ts">
/**
 * Polished collapsible section wrapper.
 *
 * Organization only: content stays in the DOM (v-show) so nothing is
 * removed, and defaults keep important information visible. Technical
 * or secondary detail may start collapsed via `defaultOpen=false`.
 */
import { ref } from "vue";

const props = withDefaults(
  defineProps<{
    title: string;
    count?: number | null;
    defaultOpen?: boolean;
    testId?: string;
  }>(),
  { count: null, defaultOpen: true, testId: "collapsible" },
);

const open = ref(props.defaultOpen);

function toggle(): void {
  open.value = !open.value;
}
</script>

<template>
  <section class="collapse" :data-testid="props.testId" :data-open="open">
    <button
      type="button"
      class="collapse__toggle"
      :aria-expanded="open"
      :data-testid="`${props.testId}-toggle`"
      @click="toggle"
    >
      <span class="collapse__chevron" aria-hidden="true">{{ open ? "▾" : "▸" }}</span>
      <span class="collapse__title">{{ props.title }}</span>
      <span v-if="props.count !== null" class="collapse__count">
        {{ props.count }}
      </span>
    </button>
    <div
      v-show="open"
      class="collapse__body"
      :data-testid="`${props.testId}-body`"
    >
      <slot />
    </div>
  </section>
</template>
