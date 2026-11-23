<script setup lang="ts">
/**
 * Cost panel.
 *
 * Displays backend values only. Nothing is summed, converted or
 * defaulted here beyond adding up the totals the backend already
 * reported per pricing model. An unpriced component is always shown as
 * unpriced with its reason, never as $0. Coverage rows (dimensions the
 * backend reports outside the priced subtotal — usage not derivable,
 * no retrieved price, outside pricing scope) are disclosed with the
 * backend's status and reason, never assigned an amount, and
 * not-applicable rows are not gaps. The caption switches to
 * "Estimated total" only when the backend's completeness verdict says
 * fully priced; while anything is unresolved the number stays an
 * explicit priced subtotal.
 */
import { computed } from "vue";

import { formatUsd, totalMonthlyCost } from "../api/client";
import CollapsibleSection from "./CollapsibleSection.vue";
import EmptyState from "./EmptyState.vue";
import type { CostSummary } from "../types";

const props = withDefaults(
  defineProps<{
    cost: CostSummary | null;
    hasCostArtifact: boolean;
    detailsOpen?: boolean;
  }>(),
  { detailsOpen: true },
);

const total = computed(() => totalMonthlyCost(props.cost));

/** Backend statuses render verbatim; the label map is presentation only. */
const coverageStatusLabels: Record<string, string> = {
  priced: "included",
  estimated: "estimated · included in the best estimate",
  usage_unknown: "not included · usage not derivable",
  price_unavailable: "not included · no retrieved price",
  unsupported: "not included · not in pricing scope",
  not_modeled: "not included · not modeled",
  not_applicable: "not applicable to this configuration",
};

function coverageStatusLabel(status: string): string {
  return coverageStatusLabels[status] ?? status;
}

/** Statuses that are gaps (outside the priced subtotal). Estimated rows
 *  count toward the best estimate and not-applicable rows are excluded
 *  by definition; priced rows are already in the subtotal. */
const gapStatuses = new Set([
  "usage_unknown",
  "price_unavailable",
  "unsupported",
  "not_modeled",
]);

const coverageGaps = computed(() =>
  (props.cost?.coverage ?? []).filter((entry) => gapStatuses.has(entry.status)),
);

/** Backend count when served; otherwise a display count over served rows. */
const unresolvedCount = computed(() => {
  const served = props.cost?.unresolved_dimension_count;
  if (typeof served === "number") {
    return served;
  }
  return coverageGaps.value.length;
});

/** Wording from served counts only: components unpriced + applicable
 *  dimensions the backend reports outside the priced subtotal. For
 *  estimated states the wording shifts to disclose residual unknowns. */
const incompleteNote = computed(() => {
  const c = props.cost?.completeness;
  if (c === "estimated_with_assumptions") {
    return "Estimated total published — all applicable dimensions are priced or estimated.";
  }
  if (c === "estimated_with_residual_unknowns") {
    const n = unresolvedCount.value;
    return `Estimated total published — ${n} billing dimension(s) remain outside pricing scope and may add cost.`;
  }
  const parts: string[] = [];
  const unpricedCount = props.cost?.unpriced.length ?? 0;
  if (unpricedCount > 0) {
    parts.push(`${unpricedCount} component(s) not priced`);
  }
  if (unresolvedCount.value > 0) {
    parts.push(`${unresolvedCount.value} billing dimension(s) unresolved`);
  }
  if (parts.length === 0) {
    return "Pricing incomplete; total may increase.";
  }
  return `Pricing incomplete — ${parts.join("; ")}; total may increase.`;
});

/** The number is only called a total when the backend says fully priced
 *  or when a best-estimate total is published (estimated states). */
const totalCaption = computed(() => {
  const c = props.cost?.completeness;
  if (c === "fully_priced" || (c === undefined && props.cost?.fully_priced)) {
    return "Estimated total · per month";
  }
  if (c === "estimated_with_assumptions" || c === "estimated_with_residual_unknowns") {
    return "Estimated total · per month";
  }
  return "Estimated priced subtotal · per month";
});

/** Best-estimate total when published, else the priced subtotal. */
const displayTotal = computed(() => {
  const best = props.cost?.best_estimate_total_usd_per_month;
  if (typeof best === "number") {
    return best;
  }
  return total.value;
});

/** Low/typical/high subline when a non-degenerate range is published. */
const rangeNote = computed(() => {
  const r = props.cost?.estimated_range_usd_per_month;
  if (!r || r.low_usd === r.high_usd) return "";
  const typical = props.cost?.best_estimate_total_usd_per_month;
  if (typeof typical === "number") {
    return `Low ${formatUsd(r.low_usd)} · Typical ${formatUsd(typical)} · High ${formatUsd(r.high_usd)} (illustrative, not a contractual bound)`;
  }
  return `Illustrative range: ${formatUsd(r.low_usd)} – ${formatUsd(r.high_usd)} (not a contractual bound)`;
});

/** Explain the absence of a total range rather than inventing one. */
const rangeUnavailableNote = computed(() => {
  const c = props.cost;
  if (!c || (c.estimates?.length ?? 0) === 0) return "";
  if (c.estimated_range_usd_per_month) return "";
  if (
    c.completeness !== "partially_unresolved" &&
    c.completeness !== "unsupported"
  ) {
    return "";
  }
  return `Low/typical/high total unavailable — ${unresolvedCount.value} billing dimension(s) remain unaccounted for. Per-dimension scenarios are listed below.`;
});
</script>

<template>
  <section class="panel" aria-label="Cost">
    <h2 class="panel__title">Cost</h2>

    <div v-if="!props.cost" class="state state--loading">
      <template v-if="props.hasCostArtifact">Loading cost…</template>
      <EmptyState v-else label="No cost artifact for this session yet." />
    </div>

    <template v-else>
      <div class="cost-total" data-testid="cost-total-block">
        <p class="cost-total__amount" data-testid="cost-total">
          {{ formatUsd(displayTotal) }}
        </p>
        <p class="cost-total__caption">{{ totalCaption }}</p>
        <p v-if="rangeNote" class="cost-total__range" data-testid="cost-range">
          {{ rangeNote }}
        </p>
        <p
          v-else-if="rangeUnavailableNote"
          class="cost-total__range"
          data-testid="cost-range-unavailable"
        >
          {{ rangeUnavailableNote }}
        </p>
        <p class="cost-total__coverage" data-testid="cost-coverage">
          {{ props.cost.priced.length }} priced ·
          {{ props.cost.unpriced.length }} unpriced — total excludes
          unpriced components, so real-world cost may be higher.
        </p>
        <p
          v-if="!props.cost.fully_priced"
          class="cost-total__coverage cost-coverage--partial"
          data-testid="cost-coverage-incomplete"
        >
          {{ incompleteNote }}
        </p>
      </div>

      <CollapsibleSection
        title="Priced components"
        :count="props.cost.priced.length"
        :default-open="props.detailsOpen"
        test-id="section-cost-priced"
      >
        <EmptyState v-if="props.cost.priced.length === 0" label="No priced components." />
        <ul v-else class="kv" data-testid="cost-priced">
          <li
            v-for="item in props.cost.priced"
            :key="`${item.component_name}-${item.pricing_model}`"
            class="kv__row"
          >
            <span class="kv__key"
              >{{ item.component_name
              }}<span v-if="item.pricing_model" class="kv__meta">
                · {{ item.pricing_model }}</span
              ></span
            >
            <span class="kv__value">{{ formatUsd(item.monthly_cost_usd) }}</span>
          </li>
        </ul>
      </CollapsibleSection>

      <CollapsibleSection
        title="Estimated lines"
        :count="(props.cost.estimated_lines ?? []).length"
        :default-open="props.detailsOpen"
        test-id="section-cost-estimated"
      >
        <EmptyState
          v-if="(props.cost.estimated_lines ?? []).length === 0"
          label="No estimated lines."
        />
        <ul v-else class="kv" data-testid="cost-estimated">
          <li
            v-for="item in (props.cost.estimated_lines ?? [])"
            :key="`${item.component_name}-${item.pricing_model}-estimated`"
            class="kv__row"
          >
            <span class="kv__key"
              >{{ item.component_name
              }}<span v-if="item.pricing_model" class="kv__meta">
                · {{ item.pricing_model }}</span
              ></span
            >
            <span class="kv__value">{{ formatUsd(item.monthly_cost_usd) }}</span>
          </li>
        </ul>
      </CollapsibleSection>

      <CollapsibleSection
        title="Estimated usage quantities"
        :count="(props.cost.estimates ?? []).length"
        :default-open="props.detailsOpen"
        test-id="section-cost-estimates"
      >
        <EmptyState
          v-if="(props.cost.estimates ?? []).length === 0"
          label="No estimated usage quantities."
        />
        <ul v-else class="kv" data-testid="cost-estimates">
          <li
            v-for="est in (props.cost.estimates ?? [])"
            :key="`${est.component_name}-${est.dimension}`"
            class="kv__row"
          >
            <span class="kv__key">
              {{ est.component_name }} / {{ est.dimension }}
            </span>
            <span class="kv__value">
              low {{ est.low_quantity.toLocaleString("en-US") }} · typical
              {{ est.typical_quantity.toLocaleString("en-US") }} · high
              {{ est.high_quantity.toLocaleString("en-US") }}
              {{ est.unit }}
            </span>
            <span class="kv__meta">
              confidence: {{ est.confidence }} · provenance: {{ est.provenance }}
            </span>
          </li>
        </ul>
      </CollapsibleSection>

      <CollapsibleSection
        title="Unpriced components"
        :count="props.cost.unpriced.length"
        :default-open="props.detailsOpen"
        test-id="section-cost-unpriced"
      >
        <EmptyState v-if="props.cost.unpriced.length === 0" label="Nothing unpriced." />
        <template v-else>
          <p class="unpriced__note">
            Not included in the subtotal above. Reasons come from the
            backend pricing contract — nothing is estimated here.
          </p>
          <ul class="unpriced" data-testid="cost-unpriced">
            <li v-for="item in props.cost.unpriced" :key="item.component_name" class="unpriced__item">
              <span class="unpriced__name">{{ item.component_name }}</span>
              <span class="unpriced__value">unpriced</span>
              <span class="unpriced__reason">{{ item.reason }}</span>
            </li>
          </ul>
        </template>
      </CollapsibleSection>

      <CollapsibleSection
        title="Pricing coverage"
        :count="coverageGaps.length"
        :default-open="props.detailsOpen"
        test-id="section-cost-coverage"
      >
        <EmptyState
          v-if="coverageGaps.length === 0"
          label="Every modeled dimension is included in the subtotal."
        />
        <template v-else>
          <p class="coverage__note">
            Dimensions the backend reports as billed but not included in the
            subtotal, with the backend's reason. Nothing is estimated here.
          </p>
          <ul class="coverage" data-testid="cost-coverage-entries">
            <li
              v-for="entry in coverageGaps"
              :key="`${entry.component_name}-${entry.dimension}`"
              class="coverage__item"
              data-testid="cost-coverage-gap"
            >
              <span class="coverage__name">{{ entry.component_name }}</span>
              <span class="coverage__status">{{
                coverageStatusLabel(entry.status)
              }}</span>
              <span class="coverage__dimension">{{ entry.label }}</span>
              <span class="coverage__reason">{{ entry.reason }}</span>
            </li>
          </ul>
        </template>
      </CollapsibleSection>

      <div v-if="props.cost.warnings.length > 0" class="field">
        <h3 class="field__label">⚠ Cost warnings</h3>
        <ul class="warnings" data-testid="cost-warnings">
          <li v-for="(warning, index) in props.cost.warnings" :key="index" class="warning">
            {{ warning }}
          </li>
        </ul>
      </div>
    </template>
  </section>
</template>
