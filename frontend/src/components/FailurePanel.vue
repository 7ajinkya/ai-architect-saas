<script setup lang="ts">
/**
 * Professional failure presentation.
 *
 * Shows what stopped and what already completed using only served
 * backend fields. The raw backend error is never hidden: it stays
 * available under an expandable technical-details block. No retry
 * affordance is offered because the backend exposes no safe
 * retry-for-session path; starting over is handled by navigation.
 */
import { computed } from "vue";

import { computeStages } from "../adapters/stages";
import type { SessionView } from "../types";

const props = defineProps<{ session: SessionView }>();

const FAILED_STAGE_COPY: Record<string, string> = {
  requirements: "Requirements gathering did not finish.",
  capacity: "Workload sizing did not finish.",
  selection: "Infrastructure components could not be selected.",
  cost: "Monthly cost could not be estimated.",
  architecture: "The final architecture could not be composed.",
};

const pipeline = computed(() =>
  computeStages(
    props.session.stages,
    props.session.status,
    props.session.pending_clarification !== null,
  ),
);

const failedStage = computed(
  () => pipeline.value.find((stage) => stage.state === "failed") ?? null,
);

const completedStages = computed(() =>
  pipeline.value.filter((stage) => stage.state === "complete"),
);
</script>

<template>
  <section
    class="failure-hero"
    aria-label="Design failure"
    data-testid="failure-panel"
  >
    <p class="failure-hero__icon" aria-hidden="true">⚠</p>
    <h2 class="failure-hero__title">Design couldn&rsquo;t be completed</h2>
    <p v-if="failedStage" class="failure-hero__stage" data-testid="failure-stage">
      {{ failedStage.label }} — {{ FAILED_STAGE_COPY[failedStage.key] ?? "This stage did not finish." }}
    </p>
    <p v-else class="failure-hero__stage">
      The design run stopped before it could finish.
    </p>

    <ul
      v-if="completedStages.length > 0"
      class="failure-hero__completed"
      data-testid="failure-completed"
    >
      <li v-for="stage in completedStages" :key="stage.key">
        <span aria-hidden="true">✓</span> {{ stage.label }}
      </li>
    </ul>

    <details
      v-if="props.session.stages.design_error"
      class="failure-hero__technical"
      data-testid="failure-technical"
    >
      <summary>Technical details</summary>
      <p>{{ props.session.stages.design_error }}</p>
    </details>
    <p v-else class="failure-hero__note">
      No further technical detail was reported for this failure.
    </p>
  </section>
</template>
