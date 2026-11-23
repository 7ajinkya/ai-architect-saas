<script setup lang="ts">
/**
 * Review & approval panel.
 *
 * Renders only what the backend reports: the stored quality-gate
 * verdict, the stored critique findings, and the stored approval
 * record. Approving records a human verdict through the existing
 * endpoint; it never provisions anything.
 */
import { computed, ref } from "vue";

import { approveDesign } from "../api/client";
import type {
  ApprovalView,
  CritiqueResult,
  QualityGateView,
  SessionView,
} from "../types";
import EmptyState from "./EmptyState.vue";

const props = withDefaults(
  defineProps<{
    session: SessionView;
    critique: CritiqueResult | null;
    onApprove?: (
      sessionId: string,
      approved: boolean,
      note: string,
    ) => Promise<ApprovalView>;
  }>(),
  { onApprove: approveDesign },
);

const emit = defineEmits<{
  (event: "approved", approval: ApprovalView): void;
  (event: "failed", message: string): void;
}>();

const note = ref("");
const busy = ref(false);

const gate = computed<QualityGateView | null>(
  () => props.session.stages.quality_gate ?? null,
);
const approval = computed<ApprovalView | null>(
  () => props.session.stages.design_approval ?? null,
);
const approvable = computed(
  () =>
    gate.value !== null &&
    (gate.value.outcome === "approved_for_review" ||
      gate.value.outcome === "requires_user_decision") &&
    approval.value === null,
);

async function approve(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = await props.onApprove(
      props.session.session_id,
      true,
      note.value.trim(),
    );
    emit("approved", result);
  } catch (cause: unknown) {
    emit(
      "failed",
      cause instanceof Error ? cause.message : "Approval failed.",
    );
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section
    v-if="gate"
    class="panel"
    aria-label="Review and approval"
    data-testid="approval-panel"
  >
    <h2 class="panel__title">Review &amp; approval</h2>

    <div class="field field--inline">
      <h3 class="field__label">Quality gate</h3>
      <span
        class="status-pill"
        :data-tone="
          gate.outcome === 'approved_for_review'
            ? 'ok'
            : gate.outcome === 'requires_user_decision'
              ? 'warn'
              : gate.outcome === 'blocked'
                ? 'error'
                : 'muted'
        "
        data-testid="gate-outcome"
      >
        {{ gate.outcome || "unknown" }}
      </span>
    </div>

    <ul v-if="gate.reasons.length > 0" class="plain-list">
      <li v-for="(reason, index) in gate.reasons" :key="index">
        {{ reason }}
      </li>
    </ul>

    <div class="field">
      <h3 class="field__label">
        Critic findings ({{ critique?.findings.length ?? 0 }})
      </h3>
      <EmptyState
        v-if="!critique || critique.findings.length === 0"
        label="No critic findings recorded."
      />
      <ul v-else class="decisions" data-testid="critique-findings">
        <li
          v-for="(finding, index) in critique.findings"
          :key="index"
          class="decision"
        >
          <p class="decision__text">
            [{{ finding.severity }}] {{ finding.statement }}
          </p>
          <p class="decision__rationale">{{ finding.recommendation }}</p>
        </li>
      </ul>
    </div>

    <div v-if="approval" class="field" data-testid="approval-record">
      <h3 class="field__label">Decision</h3>
      <p class="field__value">
        {{ approval.approved ? "Approved" : "Rejected" }}
        <span v-if="approval.note" class="kv__meta">
          — {{ approval.note }}
        </span>
      </p>
    </div>

    <div v-else-if="approvable" class="field">
      <h3 class="field__label">Your decision</h3>
      <p class="clarification__delegate-hint">
        Approval unlocks Terraform generation. It provisions nothing.
      </p>
      <div class="clarification__form">
        <label class="field__label" for="approval-note">
          Note (accepted trade-offs)
        </label>
        <textarea
          id="approval-note"
          v-model="note"
          rows="2"
          :disabled="busy"
          placeholder="Name any findings you accept as trade-offs"
          data-testid="approve-note"
        />
        <button
          type="button"
          :disabled="busy"
          data-testid="approve-button"
          @click="approve"
        >
          {{ busy ? "Recording…" : "Approve design →" }}
        </button>
      </div>
    </div>
    <EmptyState
      v-else
      label="This design is not currently approvable."
    />
  </section>
</template>
