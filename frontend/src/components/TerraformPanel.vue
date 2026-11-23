<script setup lang="ts">
/**
 * Terraform plan panel.
 *
 * Renders only what the backend stored: the deterministic plan
 * artifact, its static inventory analysis, validation outcome, and
 * pending human decisions. It also exposes the two existing backend
 * operations that produce the artifact: initial generation (after
 * design approval) and regeneration with human-decided required
 * inputs. Every value the operator types is validated server-side by
 * the module contracts; the panel invents nothing.
 *
 * "Suggest values" asks the backend for a starting point for the
 * pending decisions. The values it returns are advisory: they fill the
 * inputs, they say where they came from, and the operator still edits
 * and submits them explicitly.
 */
import { computed, ref } from "vue";

import type {
  InputSuggestionsView,
  TerraformPlanView,
} from "../types";
import {
  generateTerraformPlan,
  suggestProvisioningInputs,
} from "../api/client";
import EmptyState from "./EmptyState.vue";

const props = withDefaults(
  defineProps<{
    plan: TerraformPlanView | null;
    sessionId?: string;
    designApproved?: boolean;
    generatePlanFn?: (
      sessionId: string,
      inputs: Record<string, Record<string, string>>,
    ) => Promise<TerraformPlanView>;
    suggestInputsFn?: (
      sessionId: string,
    ) => Promise<InputSuggestionsView>;
  }>(),
  {
    sessionId: "",
    designApproved: false,
    generatePlanFn: generateTerraformPlan,
    suggestInputsFn: suggestProvisioningInputs,
  },
);

const emit = defineEmits<{
  (event: "generated", plan: TerraformPlanView): void;
  (event: "failed", message: string): void;
}>();

const busy = ref(false);
const error = ref<string | null>(null);
const draftInputs = ref<Record<string, string>>({});
const lastGenerationResult = ref<{
  tone: "ok" | "warn";
  message: string;
} | null>(null);
const suggestions = ref<InputSuggestionsView | null>(null);
const suggesting = ref(false);
const suggestionError = ref<string | null>(null);

const pendingInputs = computed(() =>
  (props.plan?.resources ?? []).flatMap((resource) =>
    resource.pending_inputs.map((item) => ({
      resource: resource.resource_id,
      name: item.name,
      reason: item.reason,
    })),
  ),
);

const uncovered = computed(() => {
  const generatedIds = new Set(
    (props.plan?.generated ?? []).map((entry) => entry.resource_id),
  );
  return (props.plan?.resources ?? []).filter(
    (r) => !r.module && !generatedIds.has(r.resource_id),
  );
});

/**
 * Module selection is all-or-nothing: when it is refused no resource
 * is bound to a module, so every unbound resource is a victim of the
 * refusal rather than a capability the catalog does not have. An empty
 * module list together with selection errors is exactly that state.
 */
const selectionRefused = computed(
  () =>
    (props.plan?.selection_errors.length ?? 0) > 0 &&
    (props.plan?.modules.length ?? 0) === 0,
);

const generatedEntries = computed(() => props.plan?.generated ?? []);

const inputDefaults = computed(() => props.plan?.input_defaults ?? []);

const inputWarnings = computed(() => props.plan?.input_warnings ?? []);

const regenerationBlockedReasons = computed(() =>
  (props.plan?.render_diagnostics ?? [])
    .filter((item) => item.category === "fallback_refused")
    .map((item) => item.message),
);

const regenerationBlocked = computed(
  () =>
    props.plan !== null &&
    props.plan.status !== "valid" &&
    regenerationBlockedReasons.value.length > 0,
);

const canRegenerate = computed(
  () =>
    props.plan !== null &&
    props.plan.status !== "valid" &&
    pendingInputs.value.length === 0 &&
    !regenerationBlocked.value &&
    props.designApproved &&
    props.sessionId !== "",
);

const diagnostics = computed(() => {
  const plan = props.plan;
  if (!plan) return [];
  const items: { source: string; message: string }[] = [];
  for (const message of plan.selection_errors ?? []) {
    items.push({ source: "selection", message });
  }
  for (const item of plan.spec_diagnostics ?? []) {
    items.push({ source: "spec", message: item.message });
  }
  for (const item of plan.render_diagnostics ?? []) {
    items.push({ source: "render", message: item.message });
  }
  return items;
});

const showGenerator = computed(
  () => props.plan === null && props.designApproved && props.sessionId !== "",
);

function describeError(cause: unknown): string {
  if (cause && typeof cause === "object" && "message" in cause) {
    return String((cause as { message: unknown }).message);
  }
  return "Terraform generation failed.";
}

function draftKey(resource: string, name: string): string {
  return `${resource}::${name}`;
}

const suggestedByKey = computed(() => {
  const map: Record<string, { value: string; rationale: string; source: string }> = {};
  for (const item of suggestions.value?.suggestions ?? []) {
    map[draftKey(item.resource_id, item.name)] = {
      value: item.value,
      rationale: item.rationale,
      source: item.source,
    };
  }
  return map;
});

function suggestionFor(resource: string, name: string) {
  return suggestedByKey.value[draftKey(resource, name)] ?? null;
}

function inputPlaceholder(item: {
  name: string;
  reason: string;
}): string {
  if (item.name === "attribute_definitions") {
    return 'pk:S,sk:N or [{"attribute_name":"pk","attribute_type":"S"}]';
  }
  if (item.name === "key_schema") {
    return 'HASH:pk,RANGE:sk or [{"attribute_name":"pk","key_type":"HASH"}]';
  }
  return item.reason;
}

async function handleSuggestInputs(): Promise<void> {
  if (suggesting.value || props.sessionId === "") return;
  suggesting.value = true;
  suggestionError.value = null;
  try {
    const result = await props.suggestInputsFn(props.sessionId);
    suggestions.value = result;
    const next = { ...draftInputs.value };
    for (const item of result.suggestions) {
      next[draftKey(item.resource_id, item.name)] = item.value;
    }
    draftInputs.value = next;
  } catch (cause: unknown) {
    suggestionError.value = describeError(cause);
  } finally {
    suggesting.value = false;
  }
}

async function runGeneration(
  inputs: Record<string, Record<string, string>>,
): Promise<void> {
  if (busy.value || props.sessionId === "" || regenerationBlocked.value) return;
  const wasRegeneration = props.plan !== null;
  busy.value = true;
  error.value = null;
  lastGenerationResult.value = null;
  try {
    const next = await props.generatePlanFn(props.sessionId, inputs);
    draftInputs.value = {};
    lastGenerationResult.value =
      next.status === "valid"
        ? {
            tone: "ok",
            message: `Terraform plan ${wasRegeneration ? "regenerated" : "generated"} successfully.`,
          }
        : {
            tone: "warn",
            message: `Terraform plan ${wasRegeneration ? "regenerated" : "generated"}, but it is still incomplete. Review the plan diagnostics below.`,
          };
    emit("generated", next);
  } catch (cause: unknown) {
    error.value = describeError(cause);
    emit("failed", error.value);
  } finally {
    busy.value = false;
  }
}

function handleGenerate(): void {
  void runGeneration({});
}

function handleSubmitInputs(): void {
  const inputs: Record<string, Record<string, string>> = {};
  for (const item of pendingInputs.value) {
    const value = (draftInputs.value[draftKey(item.resource, item.name)] ?? "").trim();
    if (!value) continue;
    if (!inputs[item.resource]) inputs[item.resource] = {};
    inputs[item.resource][item.name] = value;
  }
  void runGeneration(inputs);
}

function fileLines(content: string): number {
  return content.split("\n").length;
}
</script>

<template>
  <section
    v-if="showGenerator"
    class="panel"
    aria-label="Terraform generation"
    data-testid="terraform-generator"
  >
    <h2 class="panel__title">Terraform plan</h2>
    <p class="field__meta">
      The design is approved. Generate the deterministic Terraform plan
      for it; anything still undecided comes back as an explicit
      question, never a guess.
    </p>
    <div class="field">
      <button
        type="button"
        :disabled="busy"
        data-testid="generate-plan"
        @click="handleGenerate"
      >
        {{ busy ? "Generating…" : "Generate Terraform plan" }}
      </button>
    </div>
    <p v-if="error" class="field__error" data-testid="terraform-error">
      {{ error }}
    </p>
  </section>

  <section
    v-else-if="plan"
    class="panel"
    aria-label="Terraform plan"
    data-testid="terraform-panel"
  >
    <h2 class="panel__title">Terraform plan</h2>

    <p
      v-if="lastGenerationResult"
      :class="
        lastGenerationResult.tone === 'ok'
          ? 'field__value'
          : 'field__warning'
      "
      role="status"
      data-testid="terraform-generation-result"
    >
      {{ lastGenerationResult.message }}
    </p>

    <div class="field field--inline">
      <h3 class="field__label">Plan status</h3>
      <span
        class="status-pill"
        :data-tone="plan.status === 'valid' ? 'ok' : 'warn'"
        data-testid="terraform-status"
      >
        {{ plan.status }}
      </span>
    </div>

    <div class="field">
      <h3 class="field__label">Inventory (static, not a live plan)</h3>
      <p class="field__value" data-testid="terraform-inventory">
        {{ plan.inventory.creates }} to create ·
        {{ plan.inventory.destroys ?? "unknown" }} to destroy ·
        {{ plan.inventory.uncovered }} uncovered
      </p>
      <p class="field__meta">{{ plan.inventory.note }}</p>
    </div>

    <div class="field">
      <h3 class="field__label">Validation</h3>
      <p class="field__value" data-testid="terraform-validation">
        {{ plan.terraform_validation.status }}
      </p>
    </div>

    <div v-if="pendingInputs.length > 0" class="field">
      <h3 class="field__label">
        Pending human decisions ({{ pendingInputs.length }})
      </h3>
      <p class="field__meta">
        These values are not derivable from the architecture artifact.
        "Suggest values" fills the fields below from this design's own
        requirements; review and edit them, then submit them.
      </p>
      <div class="field">
        <button
          type="button"
          :disabled="suggesting || busy"
          data-testid="suggest-inputs"
          @click="handleSuggestInputs"
        >
          {{ suggesting ? "Suggesting…" : "Suggest values" }}
        </button>
      </div>
      <p
        v-if="suggestionError"
        class="field__error"
        data-testid="terraform-suggest-error"
      >
        {{ suggestionError }}
      </p>
      <div
        v-if="suggestions"
        class="field"
        data-testid="terraform-suggestions"
      >
        <h3 class="field__label">
          Suggested values ({{ suggestions.suggestions.length }})
        </h3>
        <p class="field__meta">
          Advisory starting points, not applied values: nothing was
          provisioned and nothing was submitted. Edit anything you
          disagree with, then submit the fields below.
        </p>
        <ul class="plain-list" data-testid="terraform-suggestion-list">
          <li
            v-for="(item, index) in suggestions.suggestions"
            :key="`suggestion-${index}`"
          >
            <strong>{{ item.resource_id }}</strong> ·
            <code>{{ item.name }}</code> = <code>{{ item.value }}</code>
            <span class="kv__meta">
              ·
              {{
                item.source === "platform_default"
                  ? "platform default"
                  : "suggested"
              }}
            </span>
            — {{ item.rationale }}
          </li>
        </ul>
        <div v-if="suggestions.skipped.length > 0" class="field">
          <h3 class="field__label">
            Left for you ({{ suggestions.skipped.length }})
          </h3>
          <ul class="plain-list" data-testid="terraform-suggestion-skipped">
            <li
              v-for="(item, index) in suggestions.skipped"
              :key="`skipped-${index}`"
            >
              <strong>{{ item.resource_id }}</strong> ·
              <code>{{ item.name }}</code> — {{ item.reason }}
            </li>
          </ul>
        </div>
        <ul
          v-if="suggestions.notes.length > 0"
          class="plain-list"
          data-testid="terraform-suggestion-notes"
        >
          <li v-for="(note, index) in suggestions.notes" :key="`note-${index}`">
            {{ note }}
          </li>
        </ul>
      </div>
      <ul class="plain-list" data-testid="terraform-pending">
        <li v-for="(item, index) in pendingInputs" :key="index">
          <strong>{{ item.resource }}</strong> needs
          <code>{{ item.name }}</code> — {{ item.reason }}
        </li>
      </ul>
      <div
        v-for="(item, index) in pendingInputs"
        :key="`input-${index}`"
        class="field"
      >
        <label
          class="field__label"
          :for="`terraform-input-${index}`"
        >
          {{ item.resource }} · {{ item.name }}
        </label>
        <input
          :id="`terraform-input-${index}`"
          v-model="draftInputs[draftKey(item.resource, item.name)]"
          type="text"
          :disabled="busy"
          :placeholder="inputPlaceholder(item)"
          :title="suggestionFor(item.resource, item.name)?.rationale ?? ''"
          :data-testid="`terraform-input-${item.resource}-${item.name}`"
        />
        <span
          v-if="suggestionFor(item.resource, item.name)"
          class="kv__meta"
          :data-testid="`terraform-suggested-${item.resource}-${item.name}`"
        >
          suggested
        </span>
      </div>
      <button
        type="button"
        :disabled="busy || regenerationBlocked"
        data-testid="submit-inputs"
        @click="handleSubmitInputs"
      >
        {{ busy ? "Regenerating…" : "Submit inputs & regenerate" }}
      </button>
      <p v-if="error" class="field__error" data-testid="terraform-error">
        {{ error }}
      </p>
    </div>

    <div v-if="inputDefaults.length > 0" class="field">
      <h3 class="field__label">
        Deterministic profile defaults ({{ inputDefaults.length }})
      </h3>
      <p class="field__meta">
        The bounded URL-shortener demo profile supplied these decisions
        explicitly. They are visible here and any human-supplied value
        overrides them.
      </p>
      <ul class="plain-list" data-testid="terraform-input-defaults">
        <li v-for="(item, index) in inputDefaults" :key="index">
          <strong>{{ item.resource_id }}</strong> ·
          <code>{{ item.name }}</code> =
          <code>{{ item.value }}</code> — {{ item.rationale }}
        </li>
      </ul>
    </div>

    <div v-if="inputWarnings.length > 0" class="field">
      <h3 class="field__label">
        Discarded carried-forward inputs ({{ inputWarnings.length }})
      </h3>
      <ul class="plain-list" data-testid="terraform-input-warnings">
        <li v-for="(warning, index) in inputWarnings" :key="index">
          {{ warning }}
        </li>
      </ul>
    </div>

    <div
      v-if="regenerationBlocked"
      class="field"
      data-testid="terraform-regeneration-blocked"
    >
      <h3 class="field__label">Regeneration cannot fix this plan</h3>
      <p class="field__meta">
        The approved architecture contains one or more components that the
        provisioning path deterministically refuses. Regenerating the same
        architecture returns the same refusal. Start a new design run so
        component selection can choose an independently provisionable
        component.
      </p>
      <ul class="plain-list">
        <li
          v-for="(reason, index) in regenerationBlockedReasons"
          :key="index"
        >
          {{ reason }}
        </li>
      </ul>
    </div>

    <div v-if="canRegenerate" class="field">
      <p class="field__meta">
        This plan is incomplete. Regenerate it after the reason above is
        addressed; a previous authorization never carries over to a
        regenerated plan.
      </p>
      <button
        type="button"
        :disabled="busy"
        data-testid="regenerate-plan"
        @click="handleGenerate"
      >
        {{ busy ? "Regenerating…" : "Regenerate plan" }}
      </button>
      <p v-if="error" class="field__error" data-testid="terraform-error">
        {{ error }}
      </p>
    </div>

    <div
      v-if="selectionRefused && uncovered.length > 0"
      class="field"
      data-testid="terraform-selection-refused"
    >
      <h3 class="field__label">
        Module selection refused ({{ uncovered.length }} affected)
      </h3>
      <p class="field__meta">
        The verified-module selection is all-or-nothing: at least one
        resource was rejected, so no resource in this plan was bound to
        a module. These resources are affected by that refusal — they
        are not capabilities missing from the catalog. The exact
        rejection reasons are in the
        <a href="#terraform-diagnostics">plan diagnostics</a> below.
      </p>
      <ul class="plain-list" data-testid="terraform-uncovered">
        <li v-for="resource in uncovered" :key="resource.resource_id">
          {{ resource.component_name }} — affected by the refused
          selection; see the plan diagnostics for the rejection reason.
        </li>
      </ul>
    </div>

    <div v-else-if="uncovered.length > 0" class="field">
      <h3 class="field__label">
        Outside verified modules ({{ uncovered.length }})
      </h3>
      <ul class="plain-list" data-testid="terraform-uncovered">
        <li v-for="resource in uncovered" :key="resource.resource_id">
          {{ resource.component_name }} — no verified module; excluded
          with reasons in the plan diagnostics.
        </li>
      </ul>
    </div>

    <div
      v-if="diagnostics.length > 0"
      id="terraform-diagnostics"
      class="field"
    >
      <h3 class="field__label">
        Plan diagnostics ({{ diagnostics.length }})
      </h3>
      <ul class="plain-list" data-testid="terraform-diagnostics">
        <li v-for="(item, index) in diagnostics" :key="index">
          <strong>{{ item.source }}</strong> — {{ item.message }}
        </li>
      </ul>
    </div>

    <div v-if="generatedEntries.length > 0" class="field">
      <h3 class="field__label">
        Generated Terraform — LLM fallback, deterministically validated
        ({{ generatedEntries.length }})
      </h3>
      <p class="field__meta">
        These resources have no verified module. An LLM proposed their
        configuration; deterministic policy checks, the safety guard,
        and terraform validate gated what reached the plan. They are
        not verified modules.
      </p>
      <ul class="plain-list" data-testid="terraform-generated">
        <li v-for="entry in generatedEntries" :key="entry.resource_id">
          <strong>{{ entry.component_name }}</strong>
          <span class="kv__meta"> · {{ entry.resource_types.join(", ") }}</span>
          <span
            class="status-pill"
            :data-tone="entry.status === 'rendered' ? 'ok' : 'error'"
          >
            {{ entry.status }}
          </span>
          <span class="kv__meta"> · sha {{ entry.proposal_sha256.slice(0, 12) }}</span>
          <ul v-if="entry.notes.length > 0" class="plain-list">
            <li v-for="(note, noteIndex) in entry.notes" :key="noteIndex">
              {{ note }}
            </li>
          </ul>
        </li>
      </ul>
    </div>

    <div v-if="Object.keys(plan.files).length > 0" class="field">
      <h3 class="field__label">
        Generated files ({{ Object.keys(plan.files).length }})
      </h3>
      <ul class="plain-list" data-testid="terraform-files">
        <li v-for="(content, path) in plan.files" :key="path">
          <code>{{ path }}</code>
          <span class="kv__meta"> · {{ fileLines(content) }} lines</span>
        </li>
      </ul>
    </div>
    <EmptyState v-else label="No Terraform files generated." />
  </section>
</template>
