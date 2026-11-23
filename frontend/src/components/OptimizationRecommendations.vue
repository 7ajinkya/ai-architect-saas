<script setup lang="ts">
/**
 * Read-only display of unevaluated optimization recommendations.
 *
 * Recommendations are generated deterministically from a blocked
 * quality-gate verdict. None represents a feasible, affordable, or
 * budget-compliant architecture: they are diagnostic starting points
 * for revising the requirements and re-running the design.
 */
import type { OptimizationRecommendationsView } from "../types";

const props = withDefaults(
  defineProps<{
    recommendations: OptimizationRecommendationsView;
  }>(),
  {
    recommendations: () => ({
      session_id: "",
      derived_from_artifact_id: "",
      recommendations: [],
    }),
  }
);
</script>

<template>
  <section
    class="panel"
    aria-label="Optimization recommendations"
    data-testid="optimization-recommendations"
  >
    <h2 class="panel__title">Optimization recommendations</h2>
    <div class="field">
      <p class="field__label">Unevaluated diagnostic suggestions</p>
      <p class="field__note">
        These recommendations have not been priced, composed, or gated.
        None represents a feasible, affordable, or budget-compliant
        architecture. Revise your requirements and re-run the design to
        evaluate any change.
      </p>
    </div>
    <div v-if="recommendations.recommendations.length" class="recommendation-list">
      <div
        v-for="rec in recommendations.recommendations"
        :key="rec.recommendation_id"
        class="recommendation"
      >
        <h3 class="recommendation__category">{{ rec.finding_category }}</h3>
        <p class="recommendation__statement">{{ rec.finding_statement }}</p>
        <p class="recommendation__rationale">{{ rec.rationale }}</p>
        <div v-if="rec.target_component" class="recommendation__target">
          <span class="field__label">Target component:</span>
          {{ rec.target_component }}
        </div>
        <div
          v-if="rec.affects_requirement"
          class="recommendation__requirement"
        >
          <span class="field__label">Touches requirement:</span>
          {{ rec.affects_requirement }}
        </div>
        <p class="recommendation__disclaimer">{{ rec.disclaimer }}</p>
      </div>
    </div>
    <div v-else class="recommendation-list--empty">
      <p>No optimization recommendations available for this session.</p>
    </div>
  </section>
</template>

<style scoped>
.recommendation__disclaimer {
  font-size: 0.8rem;
  opacity: 0.7;
  font-style: italic;
  margin-top: 0.5rem;
}
.field__note {
  font-size: 0.9rem;
  opacity: 0.85;
  margin-bottom: 1rem;
}
.recommendation-list--empty {
  opacity: 0.6;
}
</style>
