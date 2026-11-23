<script setup lang="ts">
/**
 * Requirements / conversation panel.
 *
 * Renders only what the backend reports: the original request, the
 * pending clarification, recorded assumptions and requirements status.
 * It contains no conversational logic; answering posts to the existing
 * /messages endpoint through the client.
 */
import { ref } from "vue";

import CollapsibleSection from "./CollapsibleSection.vue";
import EmptyState from "./EmptyState.vue";
import type { Assumption, ClarificationView } from "../types";

const props = withDefaults(
  defineProps<{
    userRequest: string;
    status: string;
    clarification: ClarificationView | null;
    assumptions: Assumption[];
    submitting: boolean;
    assumptionsOpen?: boolean;
  }>(),
  { assumptionsOpen: true },
);

const emit = defineEmits<{ (event: "submit", text: string): void }>();

const draft = ref("");

function submit(): void {
  const text = draft.value.trim();
  if (!text) return;
  emit("submit", text);
  draft.value = "";
}

/**
 * Delegate an assumable question to the architect. Sends the backend's
 * documented delegation phrase through the same submit path as a typed
 * answer, so no new contract is introduced.
 */
function delegate(): void {
  emit("submit", "you decide");
}
</script>

<template>
  <section class="panel" aria-label="Requirements">
    <h2 class="panel__title">Requirements</h2>

    <div class="field">
      <h3 class="field__label">Your request</h3>
      <p class="request-text">{{ props.userRequest || "—" }}</p>
    </div>

    <div class="field field--inline">
      <h3 class="field__label">Requirements status</h3>
      <span
        class="status-pill"
        :data-tone="props.status === 'ready' ? 'ok' : props.status === 'failed' ? 'error' : 'muted'"
        data-testid="requirements-status"
      >
        {{ props.status || "unknown" }}
      </span>
    </div>

    <div class="field">
      <h3 class="field__label">Outstanding question</h3>
      <div v-if="props.clarification" class="clarification" data-testid="clarification">
        <p class="clarification__question">{{ props.clarification.question }}</p>
        <p
          v-if="props.clarification.reason"
          class="clarification__reason"
          data-testid="clarification-reason"
        >
          {{ props.clarification.reason }}
        </p>
        <p class="clarification__meta">
          {{ props.clarification.requirement_key }} ·
          {{ props.clarification.impact
          }}<template v-if="props.clarification.assumable">
            · architect may assume</template
          >
        </p>
        <div
          v-if="props.clarification.assumable"
          class="clarification__delegate"
          data-testid="clarification-delegate"
        >
          <p class="clarification__delegate-hint">
            This question can be delegated: the architect will record a
            documented assumption instead of waiting for an answer.
          </p>
          <button
            type="button"
            :disabled="props.submitting"
            @click="delegate"
          >
            Let the architect decide
          </button>
        </div>
      </div>
      <EmptyState v-else label="No outstanding question." />
    </div>

    <form class="clarification__form" @submit.prevent="submit">
      <label class="field__label" for="clarification-response">
        {{ props.clarification ? "Your answer" : "Send a message" }}
      </label>
      <textarea
        id="clarification-response"
        v-model="draft"
        :disabled="props.submitting"
        placeholder="Answer the question, or delegate a choice"
        rows="3"
      />
      <button type="submit" :disabled="props.submitting || draft.trim() === ''">
        {{ props.submitting ? "Sending…" : "Send" }}
      </button>
    </form>

    <CollapsibleSection
      title="System-derived assumptions"
      :count="props.assumptions.length"
      :default-open="props.assumptionsOpen"
      test-id="section-requirement-assumptions"
    >
      <EmptyState v-if="props.assumptions.length === 0" label="No assumptions recorded." />
      <ul v-else class="assumptions" data-testid="requirement-assumptions">
        <li
          v-for="(assumption, index) in props.assumptions"
          :key="`${assumption.name}-${index}`"
          class="assumption"
        >
          <div class="assumption__row">
            <span class="assumption__name">{{ assumption.name }}</span>
            <span class="assumption__value">
              {{ assumption.value || "—" }}
              <template v-if="assumption.unit"> {{ assumption.unit }}</template>
            </span>
          </div>
          <div class="assumption__row assumption__row--meta">
            <span>source: {{ assumption.source || "—" }}</span>
            <span>confidence: {{ assumption.confidence || "—" }}</span>
          </div>
          <p v-if="assumption.rationale" class="assumption__rationale">
            {{ assumption.rationale }}
          </p>
        </li>
      </ul>
    </CollapsibleSection>
  </section>
</template>
