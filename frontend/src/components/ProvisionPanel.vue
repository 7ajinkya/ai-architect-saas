<script setup lang="ts">
/**
 * Provisioning panel: the single human-authorized mutation path.
 *
 * Renders only what the backend stored (plan status, static inventory,
 * region, cost, warnings, validation) and walks the operator through
 * three explicit steps: review the live plan (destroy counts come from
 * this review, never from an assumed zero), authorize that exact
 * reviewed tree, then apply it. Each click calls one controlled
 * backend endpoint; every step re-validates server-side. The panel
 * executes nothing itself: no shell, no commands, no paths, no
 * credentials, and no destroy action.
 */
import { computed, ref, watch } from "vue";

import type {
  ApprovalView,
  ProvisioningResultView,
  TerraformPlanView,
} from "../types";
import { approveProvisioning, provision } from "../api/client";

const props = withDefaults(
  defineProps<{
    sessionId: string;
    plan: TerraformPlanView | null;
    monthlyCost?: number | null;
    approveProvisioningFn?: (
      sessionId: string,
      approved: boolean,
      note: string,
    ) => Promise<ApprovalView>;
    provisionFn?: (
      sessionId: string,
      mode: "plan" | "apply",
      confirm: string,
    ) => Promise<ProvisioningResultView>;
  }>(),
  {
    monthlyCost: null,
    approveProvisioningFn: approveProvisioning,
    provisionFn: provision,
  },
);

const emit = defineEmits<{
  (event: "provisioned", result: ProvisioningResultView): void;
  (event: "failed", message: string): void;
}>();

type Phase = "idle" | "reviewing" | "approving" | "applying";
const phase = ref<Phase>("idle");
const review = ref<ProvisioningResultView | null>(null);
const approval = ref<ApprovalView | null>(null);
const result = ref<ProvisioningResultView | null>(null);
const error = ref<string | null>(null);

const busy = computed(() => phase.value !== "idle");
const planValid = computed(
  () => props.plan !== null && props.plan.status === "valid",
);
const reviewOk = computed(
  () => review.value !== null && review.value.status === "plan_ok",
);
const canRunPlan = computed(() => !busy.value && planValid.value);
const canAuthorize = computed(
  () => !busy.value && reviewOk.value && approval.value === null,
);
const canApply = computed(
  () =>
    !busy.value &&
    reviewOk.value &&
    approval.value !== null &&
    result.value === null,
);
const liveDestroys = computed(() => review.value?.summary?.destroy ?? null);
const blockedReason = computed(() => {
  if (!props.plan) return null;
  if (props.plan.status !== "valid") {
    const reasons = [
      ...props.plan.selection_errors,
      ...props.plan.spec_diagnostics.map((d) => d.message),
      ...props.plan.render_diagnostics.map((d) => d.message),
    ];
    return reasons.length > 0
      ? `Plan is ${props.plan.status}: ${reasons[0]}`
      : `Plan is ${props.plan.status}.`;
  }
  return null;
});
const busyLabel = computed(() =>
  phase.value === "reviewing"
    ? "Running plan review…"
    : phase.value === "approving"
      ? "Recording authorization…"
      : phase.value === "applying"
        ? "Applying infrastructure… (this takes minutes)"
        : "",
);
const checks = computed(() => {
  if (!result.value) return [];
  const exitCodes = result.value.exit_codes ?? {};
  const items: { label: string; ok: boolean }[] = [];
  if ("init" in exitCodes) {
    items.push({
      label: "Terraform initialized",
      ok: exitCodes["init"] === 0,
    });
  }
  items.push({
    label: "Approval verified",
    ok: true,
  });
  if (result.value.status === "apply_ok") {
    items.push({ label: "Infrastructure applied", ok: true });
  }
  return items;
});

function describeError(cause: unknown): string {
  if (cause && typeof cause === "object" && "message" in cause) {
    return String((cause as { message: unknown }).message);
  }
  return "Provisioning failed.";
}

// A different plan identity (new generation, regenerated tree, or a
// cleared plan) invalidates everything shown here: the review, the
// authorization, and any result belong to the previous tree only.
watch(
  () => props.plan?.tree_hash ?? "",
  () => {
    review.value = null;
    approval.value = null;
    result.value = null;
    error.value = null;
  },
);

async function handleRunPlan(): Promise<void> {
  if (!canRunPlan.value) return;
  phase.value = "reviewing";
  error.value = null;
  try {
    review.value = await props.provisionFn(props.sessionId, "plan", "");
    // A fresh review invalidates any earlier authorization: the human
    // authorizes exactly the plan they reviewed.
    approval.value = null;
    result.value = null;
  } catch (cause: unknown) {
    error.value = describeError(cause);
    emit("failed", error.value);
  } finally {
    phase.value = "idle";
  }
}

async function handleAuthorize(): Promise<void> {
  if (!canAuthorize.value) return;
  phase.value = "approving";
  error.value = null;
  try {
    approval.value = await props.approveProvisioningFn(
      props.sessionId,
      true,
      "Authorized the reviewed plan via workspace UI",
    );
  } catch (cause: unknown) {
    error.value = describeError(cause);
    emit("failed", error.value);
  } finally {
    phase.value = "idle";
  }
}

async function handleApply(): Promise<void> {
  if (!canApply.value) return;
  phase.value = "applying";
  error.value = null;
  try {
    const outcome = await props.provisionFn(props.sessionId, "apply", "APPLY");
    result.value = outcome;
    emit("provisioned", outcome);
  } catch (cause: unknown) {
    error.value = describeError(cause);
    emit("failed", error.value);
  } finally {
    phase.value = "idle";
  }
}
</script>

<template>
  <section
    v-if="plan"
    class="panel"
    aria-label="Provisioning"
    data-testid="provision-panel"
  >
    <h2 class="panel__title">Provisioning</h2>

    <div class="field field--inline">
      <h3 class="field__label">Plan</h3>
      <p class="field__value" data-testid="provision-delta">
        +{{ plan.inventory.creates }} to create · destroys are confirmed
        by the live plan review below
      </p>
    </div>

    <div class="field field--inline">
      <h3 class="field__label">Region</h3>
      <p class="field__value" data-testid="provision-region">
        {{ plan.region_code }}
      </p>
    </div>

    <div class="field field--inline">
      <h3 class="field__label">Estimated cost</h3>
      <p class="field__value" data-testid="provision-cost">
        {{
          monthlyCost === null
            ? "unpriced"
            : `$${monthlyCost.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/mo`
        }}
      </p>
    </div>

    <div v-if="plan.spec_warnings.length > 0" class="field">
      <h3 class="field__label">Warnings ({{ plan.spec_warnings.length }})</h3>
      <ul class="plain-list" data-testid="provision-warnings">
        <li v-for="(warning, index) in plan.spec_warnings" :key="index">
          {{ warning }}
        </li>
      </ul>
    </div>

    <div class="field">
      <h3 class="field__label">Resources</h3>
      <ul class="plain-list" data-testid="provision-resources">
        <li v-for="resource in plan.resources" :key="resource.resource_id">
          <code>{{ resource.resource_id }}</code>
          <span class="kv__meta"> · {{ resource.kind }}</span>
        </li>
      </ul>
    </div>

    <p v-if="blockedReason" class="field__meta" data-testid="provision-blocked">
      {{ blockedReason }}
    </p>

    <div class="field field--inline">
      <h3 class="field__label">1 · Review</h3>
      <button
        type="button"
        :disabled="!canRunPlan"
        data-testid="run-plan"
        @click="handleRunPlan"
      >
        {{
          phase === "reviewing"
            ? busyLabel
            : review
              ? "Re-run plan review"
              : "Run plan review"
        }}
      </button>
    </div>

    <div v-if="review" class="field">
      <p class="field__value" data-testid="provision-review-status">
        {{ review.status }}
      </p>
      <p
        v-if="review.summary"
        class="field__meta"
        data-testid="provision-review-summary"
      >
        +{{ review.summary.add }} ~{{ review.summary.change }} -{{
          review.summary.destroy
        }}
      </p>
      <p
        v-else
        class="field__meta"
        data-testid="provision-review-summary-unknown"
      >
        The live plan summary could not be parsed; read the plan output
        before authorizing.
      </p>
    </div>

    <div v-if="liveDestroys !== null && liveDestroys > 0" class="field">
      <p class="field__warning" data-testid="provision-destroy-warning">
        Warning: the reviewed plan destroys {{ liveDestroys }} resource(s).
        Authorizing it authorizes replacements as well as creates.
      </p>
    </div>

    <div v-if="reviewOk" class="field field--inline">
      <h3 class="field__label">2 · Authorize</h3>
      <button
        type="button"
        :disabled="!canAuthorize"
        data-testid="approve-provision"
        @click="handleAuthorize"
      >
        {{
          phase === "approving"
            ? busyLabel
            : approval
              ? "Authorized"
              : "Authorize this plan"
        }}
      </button>
    </div>

    <div v-if="approval" class="field field--inline">
      <h3 class="field__label">3 · Apply</h3>
      <button
        type="button"
        :disabled="!canApply"
        data-testid="apply-provision"
        @click="handleApply"
      >
        {{
          phase === "applying"
            ? busyLabel
            : result
              ? "Applied"
              : "Apply approved plan"
        }}
      </button>
    </div>

    <p v-if="error" class="field__error" data-testid="provision-error">
      {{ error }}
    </p>

    <div v-if="result" class="field">
      <h3 class="field__label">Provisioning result</h3>
      <p class="field__value" data-testid="provision-status">
        {{ result.status }}
      </p>
      <ul class="plain-list" data-testid="provision-checks">
        <li v-for="(check, index) in checks" :key="index">
          {{ check.ok ? "✓" : "✗" }} {{ check.label }}
        </li>
      </ul>
      <p
        v-if="result.summary"
        class="field__meta"
        data-testid="provision-summary"
      >
        +{{ result.summary.add }} ~{{ result.summary.change }} -{{
          result.summary.destroy
        }}
      </p>
    </div>
  </section>
</template>
