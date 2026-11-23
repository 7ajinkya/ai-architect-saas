<script setup lang="ts">
/**
 * Per-stage pipeline progress.
 *
 * Thin render layer over the pure `computeStages` adapter: every state
 * shown is derived from served backend fields only.
 */
import { computed, onMounted, ref, watch } from "vue";

import { computeStages, type StageState } from "../adapters/stages";
import type { DesignStages } from "../types";

const props = defineProps<{
  stages: DesignStages | null;
  requirementsStatus: string;
  hasPendingClarification: boolean;
}>();

const pipeline = computed(() =>
  computeStages(
    props.stages,
    props.requirementsStatus,
    props.hasPendingClarification,
  ),
);

const listRef = ref<HTMLOListElement | null>(null);

/** Key of the stage currently being worked or just failed, if any. */
const activeKey = computed(
  () =>
    pipeline.value.find(
      (stage) => stage.state === "in-progress" || stage.state === "failed",
    )?.key ?? null,
);

/**
 * Keep the active stage visible as the run advances. Driven only by
 * the reactive pipeline state (no timers): on mount and whenever the
 * active key changes, bring that step into view inside the scrollable
 * row. Honors reduced-motion preferences; guards APIs jsdom lacks.
 */
function scrollToActive(): void {
  const key = activeKey.value;
  if (!key) return;
  const step = listRef.value?.querySelector<HTMLElement>(
    `[data-testid="stage-${key}"]`,
  );
  const reduceMotion =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  step?.scrollIntoView?.({
    block: "nearest",
    inline: "nearest",
    behavior: reduceMotion ? "auto" : "smooth",
  });
}

onMounted(scrollToActive);
watch(activeKey, scrollToActive);

const STATE_BADGE: Record<StageState, string> = {
  complete: "✓",
  "in-progress": "⟳",
  waiting: "○",
  "not-started": "○",
  failed: "!",
};
</script>

<template>
  <section class="panel pipeline-panel" aria-label="Design pipeline progress">
    <h2 class="panel__title">Pipeline</h2>
    <ol
      ref="listRef"
      class="pipeline pipeline--horizontal"
      data-testid="stage-progress"
    >
      <li
        v-for="(stage, index) in pipeline"
        :key="stage.key"
        class="pipeline__step"
        :data-testid="`stage-${stage.key}`"
        :data-state="stage.state"
        :aria-current="stage.state === 'in-progress' ? 'step' : undefined"
        :title="
          stage.state === 'in-progress'
            ? `${stage.label}: actively processing`
            : undefined
        "
      >
        <span class="pipeline__badge" aria-hidden="true">{{
          STATE_BADGE[stage.state]
        }}</span>
        <span class="pipeline__label">{{ stage.label }}</span>
        <span class="pipeline__state">{{
          stage.state === "in-progress" ? "Working…" : stage.state
        }}</span>
        <span
          v-if="index < pipeline.length - 1"
          class="pipeline__connector"
          aria-hidden="true"
          >→</span
        >
      </li>
    </ol>
  </section>
</template>
