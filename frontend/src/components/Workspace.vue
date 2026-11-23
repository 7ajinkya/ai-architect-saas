<script setup lang="ts">
/**
 * Generic architecture workspace.
 *
 * Composes the panels from structured backend state only. It contains
 * no workload concept, no architecture generation logic and no cost
 * calculation: it fetches artifacts and renders them.
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import ArchitectureAssumptions from "./ArchitectureAssumptions.vue";
import ArchitectureDecisions from "./ArchitectureDecisions.vue";
import ArchitectureGraph from "./ArchitectureGraph.vue";
import ApprovalPanel from "./ApprovalPanel.vue";
import OptimizationRecommendations from "./OptimizationRecommendations.vue";
import ProvisionPanel from "./ProvisionPanel.vue";
import TerraformPanel from "./TerraformPanel.vue";
import CollapsibleSection from "./CollapsibleSection.vue";
import CostPanel from "./CostPanel.vue";
import FailurePanel from "./FailurePanel.vue";
import EmptyState from "./EmptyState.vue";
import ErrorState from "./ErrorState.vue";
import LoadingState from "./LoadingState.vue";
import NodeDetails from "./NodeDetails.vue";
import RequirementsPanel from "./RequirementsPanel.vue";
import StageProgress from "./StageProgress.vue";
import StatusBar from "./StatusBar.vue";
import WarningsList from "./WarningsList.vue";
import {
  fetchArchitecture,
  fetchCritique,
  fetchOptimizationRecommendations,
  fetchQualityGate,
  fetchSession,
  fetchTerraformPlan,
  startDesignRun,
  submitClarification,
  totalMonthlyCost,
} from "../api/client";
import { findGraphNode, toGraphModel } from "../adapters/graph";
import type {
  ArchitectureResult,
  CritiqueResult,
  OptimizationRecommendationsView,
  QualityGateView,
  SessionView,
  TerraformPlanView,
} from "../types";

const props = withDefaults(
  defineProps<{
    sessionId: string;
    loadSession?: (id: string) => Promise<SessionView>;
    loadArchitecture?: (id: string) => Promise<ArchitectureResult>;
    loadCritique?: (id: string) => Promise<CritiqueResult>;
    loadQualityGate?: (id: string) => Promise<QualityGateView>;
    loadOptimizationRecommendations?: (
      id: string
    ) => Promise<OptimizationRecommendationsView>;
    loadTerraformPlan?: (id: string) => Promise<TerraformPlanView>;
    onSendMessage?: (id: string, text: string) => Promise<SessionView>;
    startRun?: (requirementsSessionId: string) => Promise<{ session_id: string }>;
  }>(),
  {
    loadSession: fetchSession,
    loadArchitecture: fetchArchitecture,
    loadCritique: fetchCritique,
    loadQualityGate: fetchQualityGate,
    loadOptimizationRecommendations: fetchOptimizationRecommendations,
    loadTerraformPlan: fetchTerraformPlan,
    onSendMessage: submitClarification,
    startRun: startDesignRun,
  },
);

const emit = defineEmits<{ (event: "back"): void; (event: "open-session", id: string): void }>();

const session = ref<SessionView | null>(null);
const architecture = ref<ArchitectureResult | null>(null);
const critique = ref<CritiqueResult | null>(null);
const recommendations = ref<OptimizationRecommendationsView | null>(null);
const terraformPlan = ref<TerraformPlanView | null>(null);
const loading = ref(true);
const architectureLoading = ref(false);
const error = ref<string | null>(null);
const selectedNodeId = ref<string | null>(null);
const submitting = ref(false);
const heroDraft = ref("");
const startingRun = ref(false);

// Presentation-only workspace state. Collapsing never destroys panel
// state: panes are hidden with v-show so answers, selection and scroll
// positions survive.
const leftCollapsed = ref(false);
const rightCollapsed = ref(false);
const focusMode = ref(false);
const leftWidth = ref(330);
const rightWidth = ref(350);
const LEFT_MIN = 240;
const LEFT_MAX = 560;
const RIGHT_MIN = 260;
const RIGHT_MAX = 560;
let dragging: "left" | "right" | null = null;
let dragStartX = 0;
let dragStartWidth = 0;

function clampWidth(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function endSplitterDrag(): void {
  dragging = null;
  document.body.classList.remove("workspace--resizing");
  window.removeEventListener("mousemove", onSplitterMove);
}

function onSplitterDown(side: "left" | "right", event: MouseEvent): void {
  // Defensive: a missed mouseup (e.g. release outside the window) must
  // never leave a stale drag behind.
  endSplitterDrag();
  dragging = side;
  // Delta-based sizing: robust to any grid geometry (centered,
  // max-width-capped, padded). Absolute-position math with magic
  // offsets silently clamps on layouts it was not measured for.
  dragStartX = event.clientX;
  dragStartWidth = side === "left" ? leftWidth.value : rightWidth.value;
  event.preventDefault();
  document.body.classList.add("workspace--resizing");
  window.addEventListener("mousemove", onSplitterMove);
  window.addEventListener("mouseup", onSplitterUp, { once: true });
}

function onSplitterMove(event: MouseEvent): void {
  if (!dragging) return;
  const delta = event.clientX - dragStartX;
  if (dragging === "left") {
    leftWidth.value = clampWidth(dragStartWidth + delta, LEFT_MIN, LEFT_MAX);
  } else {
    rightWidth.value = clampWidth(dragStartWidth - delta, RIGHT_MIN, RIGHT_MAX);
  }
}

function onSplitterUp(): void {
  endSplitterDrag();
}

const gridStyle = computed(() => {
  if (focusMode.value) return { gridTemplateColumns: "minmax(0, 1fr)" };
  if (leftCollapsed.value && rightCollapsed.value) {
    return { gridTemplateColumns: "minmax(0, 1fr)" };
  }
  if (leftCollapsed.value) {
    return { gridTemplateColumns: `minmax(0, 1fr) 8px ${rightWidth.value}px` };
  }
  if (rightCollapsed.value) {
    return { gridTemplateColumns: `${leftWidth.value}px 8px minmax(0, 1fr)` };
  }
  return {
    gridTemplateColumns:
      `${leftWidth.value}px 8px minmax(0, 1fr) 8px ${rightWidth.value}px`,
  };
});

const showGraph = computed(() => model.value !== null);

/**
 * Recommendations are displayed only while the current gate is BLOCKED
 * and only for the architecture artifact they were generated from. The
 * load-time lifecycle keeps the ref consistent, but this guard also
 * covers a session replaced mid-life (for example by clarification
 * answers), so stale advice can never outlive its artifact.
 */
const activeRecommendations = computed(() => {
  const current = recommendations.value;
  if (!current) return null;
  const stages = session.value?.stages;
  if (stages?.quality_gate?.outcome !== "blocked") return null;
  const artifactId = stages.architecture?.artifact_id ?? "";
  if (current.derived_from_artifact_id !== artifactId) return null;
  return current;
});
const isFailed = computed(
  () => session.value?.stages.design_status === "failed",
);
const canStartRun = computed(() => {
  const s = session.value;
  if (!s) return false;
  return (
    s.status === "ready" &&
    s.pending_clarification === null &&
    !s.stages.has_design_run
  );
});

/**
 * Whether supporting detail starts expanded. When the center column
 * carries the focus (canvas, failure, live run, or an outstanding
 * question), secondary sections start collapsed so the workspace
 * leads with what matters; everything stays one click away.
 * Derived from served fields only.
 */
const supportingOpen = computed(() => {
  const current = session.value;
  if (!current) return true;
  if (showGraph.value || isFailed.value) return false;
  if (isRunInFlight()) return false;
  if (current.pending_clarification) return false;
  return true;
});
const heroClarification = computed(
  () => session.value?.pending_clarification ?? null,
);

/** Header status pill, derived from served fields only. */
const headerStatus = computed(() => {
  if (!session.value) return { label: "Loading", tone: "muted" };
  if (session.value.stages.design_status === "failed") {
    return { label: "Failed", tone: "error" };
  }
  if (session.value.pending_clarification) {
    return { label: "Needs input", tone: "warn" };
  }
  if (isRunInFlight()) {
    return { label: "Running", tone: "active" };
  }
  if (session.value.stages.architecture) {
    return { label: "Ready", tone: "ok" };
  }
  return {
    label: session.value.status || "Draft",
    tone: "muted",
  };
});

const model = computed(() =>
  architecture.value ? toGraphModel(architecture.value) : null,
);

const selectedNode = computed(() =>
  model.value ? findGraphNode(model.value, selectedNodeId.value) : null,
);

const warnings = computed(() => [
  ...(session.value?.warnings ?? []),
  ...(architecture.value?.warnings ?? []),
]);

const isEntryPoint = computed(
  () => !!selectedNode.value && !!model.value?.entryPointIds.includes(selectedNode.value.id),
);

const isDataStore = computed(
  () => !!selectedNode.value && !!model.value?.dataStoreNodeIds.includes(selectedNode.value.id),
);

const unpricedCount = computed(() => session.value?.stages.cost?.unpriced.length ?? 0);

/** Estimated monthly total for the provision panel, served only. */
const monthlyCost = computed(() =>
  totalMonthlyCost(session.value?.stages.cost ?? null),
);

/**
 * While a run is in flight the backend reports RUNNING; poll the same
 * session endpoint until the architecture is available or the run fails.
 * No streaming is involved.
 *
 * The graph reports READY as early as the component-selection and cost
 * stages, which is *before* the composer stores an architecture, so
 * READY on its own is not a finished run: polling stops only once the
 * artifact exists, the run failed, or the bounded attempt budget runs
 * out (so a run that legitimately ends without an artifact cannot spin
 * forever).
 */
const POLL_INTERVAL_MS = 2000;
const MAX_EXTRA_READY_POLLS = 30;
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let readyPolls = 0;

function isRunInFlight(): boolean {
  const stages = session.value?.stages;
  if (!stages) return false;
  if (stages.design_status === "failed") return false;
  if (stages.design_status === "running") return true;
  return stages.design_status === "ready" && !stages.architecture;
}

function schedulePollIfRunning(): void {
  clearTimeout(pollTimer);
  if (!isRunInFlight()) {
    readyPolls = 0;
    return;
  }
  if (session.value?.stages.design_status === "ready") {
    readyPolls += 1;
    if (readyPolls > MAX_EXTRA_READY_POLLS) return;
  }
  pollTimer = setTimeout(() => {
    void load(false);
  }, POLL_INTERVAL_MS);
}

onBeforeUnmount(() => {
  clearTimeout(pollTimer);
  endSplitterDrag();
});

function describeError(cause: unknown, what: string): string {
  if (cause && typeof cause === "object" && "message" in cause) {
    return String((cause as { message: unknown }).message);
  }
  return `Failed to load ${what}.`;
}

async function load(reset = true): Promise<void> {
  if (reset) {
    loading.value = true;
    error.value = null;
    selectedNodeId.value = null;
    architecture.value = null;
    critique.value = null;
    terraformPlan.value = null;
  }

  let loaded: SessionView;
  try {
    loaded = await props.loadSession(props.sessionId);
  } catch (cause: unknown) {
    error.value = describeError(cause, "session");
    loading.value = false;
    return;
  }

  session.value = loaded;
  loading.value = false;

  // Recommendation lifecycle, driven only by served fields. While a run
  // is in flight, drop any recommendations: they describe the previous
  // artifact and must not survive into the new run. Outside a run,
  // recommendations are kept only when the gate is BLOCKED and the
  // stored set is already tied to the architecture artifact being
  // shown; otherwise they are cleared and, for a blocked gate, fetched
  // fresh. Recommendations are diagnostic only: none represents a
  // feasible, affordable, or budget-compliant architecture, and
  // absence is never an error.
  const currentArtifactId = loaded.stages.architecture?.artifact_id ?? "";
  if (isRunInFlight() || loaded.stages.quality_gate?.outcome !== "blocked") {
    recommendations.value = null;
  } else if (
    recommendations.value?.derived_from_artifact_id !== currentArtifactId
  ) {
    try {
      recommendations.value = await props.loadOptimizationRecommendations(
        props.sessionId
      );
    } catch {
      recommendations.value = null;
    }
  }

  // Authoritative quality-gate fallback: the session view normally
  // embeds the gate, but when it does not (older payload shape) and a
  // design architecture exists, read the dedicated endpoint once. A
  // 404 means the gate genuinely does not exist yet and stays null;
  // nothing here invents gate or approval state. While a run is still
  // in flight the session poll already re-reads the embedded verdict,
  // so no fetch fires mid-run (no 404 spam for a pending artifact).
  if (
    loaded.stages.architecture &&
    !loaded.stages.quality_gate &&
    loaded.stages.design_status !== "running"
  ) {
    try {
      const gate = await props.loadQualityGate(props.sessionId);
      session.value = {
        ...loaded,
        stages: { ...loaded.stages, quality_gate: gate },
      };
    } catch {
      // Keep the embedded null: absence of a gate is a real state
      // (nothing to approve yet), never an error to fabricate.
    }
  }

  if (loaded.stages.architecture && !architecture.value) {
    architectureLoading.value = true;
    try {
      architecture.value = await props.loadArchitecture(props.sessionId);
    } catch (cause: unknown) {
      error.value = describeError(cause, "architecture");
    } finally {
      architectureLoading.value = false;
    }
  }

  // Full findings only once the backend has a critique summary:
  // fetching mid-run would 404-spam an artifact that cannot exist
  // yet, and the session poll already re-reads the summary.
  if (
    loaded.stages.architecture &&
    !critique.value &&
    loaded.stages.critique
  ) {
    try {
      critique.value = await props.loadCritique(props.sessionId);
    } catch {
      // Critique is unavailable until the critic stage runs; the
      // panel stays hidden rather than erroring.
      critique.value = null;
    }
  }

  if (
    loaded.stages.design_approval?.approved &&
    !terraformPlan.value
  ) {
    try {
      terraformPlan.value = await props.loadTerraformPlan(props.sessionId);
    } catch {
      // No stored plan yet (generation needs its own inputs); the
      // panel stays hidden rather than erroring.
      terraformPlan.value = null;
    }
  }

  schedulePollIfRunning();
}

async function handleSend(text: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed || !session.value) return;
  submitting.value = true;
  try {
    session.value = await props.onSendMessage(props.sessionId, trimmed);
  } catch (cause: unknown) {
    error.value = describeError(cause, "message");
  } finally {
    submitting.value = false;
  }
}

async function handleStartRun(): Promise<void> {
  if (!session.value || startingRun.value) return;
  startingRun.value = true;
  error.value = null;
  try {
    const run = await props.startRun(props.sessionId);
    emit("open-session", run.session_id);
  } catch (cause: unknown) {
    error.value = describeError(cause, "design run");
  } finally {
    startingRun.value = false;
  }
}

function handleHeroSubmit(): void {
  const text = heroDraft.value.trim();
  if (!text) return;
  heroDraft.value = "";
  void handleSend(text);
}

function handleHeroDelegate(): void {
  void handleSend("you decide");
}

function goBack(): void {
  emit("back");
}

function handleSelectNode(nodeId: string | null): void {
  selectedNodeId.value = nodeId;
}

async function handleApproved(): Promise<void> {
  // Re-read the session so the stored approval record appears.
  await load(false);
}

function handleApprovalFailed(message: string): void {
  error.value = message;
}

function handlePlanGenerated(plan: TerraformPlanView): void {
  // The generated artifact is served state; keep the local copy so
  // the plan and provisioning panels appear without another round
  // trip. A later reload re-reads the same stored artifact.
  terraformPlan.value = plan;
}

onMounted(load);
</script>

<template>
  <LoadingState v-if="loading" label="Loading design session…" />

  <ErrorState
    v-else-if="error && !session"
    label="Could not load this design session."
    :detail="error ?? undefined"
  />

  <ErrorState v-else-if="!session" label="No session data returned." />

  <div v-else class="workspace" :class="{ 'workspace--focus': focusMode }">
    <header class="workspace__header">
      <button type="button" class="workspace__back" data-testid="back" @click="goBack">
        ← New
      </button>
      <div class="workspace__title">
        <p class="workspace__request" data-testid="session-request">
          {{ session.user_request || "Untitled design" }}
        </p>
        <p class="workspace__session" data-testid="session-id" :title="session.session_id">
          Session {{ session.session_id }}
        </p>
      </div>
      <span
        class="status-pill"
        :data-tone="headerStatus.tone"
        data-testid="header-status"
      >
        {{ headerStatus.label }}
      </span>
      <button
        v-if="focusMode"
        type="button"
        class="button--secondary"
        data-testid="focus-exit"
        @click="focusMode = false"
      >
        Exit focus
      </button>
    </header>

    <div v-if="error" class="workspace__banner">
      <ErrorState label="Partial data unavailable." :detail="error" />
    </div>

    <StageProgress
      v-if="!focusMode"
      :stages="session.stages"
      :requirements-status="session.status"
      :has-pending-clarification="session.pending_clarification !== null"
    />

    <div class="workspace__toolbar" v-if="!focusMode">
      <button
        type="button"
        class="button--secondary toolbar-btn"
        :data-testid="leftCollapsed ? 'left-expand' : 'left-collapse'"
        @click="leftCollapsed = !leftCollapsed"
      >
        {{ leftCollapsed ? "Show requirements →" : "← Hide requirements" }}
      </button>
      <button
        type="button"
        class="button--secondary toolbar-btn"
        :data-testid="rightCollapsed ? 'right-expand' : 'right-collapse'"
        @click="rightCollapsed = !rightCollapsed"
      >
        {{ rightCollapsed ? "← Show details" : "Hide details →" }}
      </button>
      <button
        v-if="showGraph"
        type="button"
        class="button--secondary toolbar-btn"
        data-testid="focus-toggle"
        @click="focusMode = true"
      >
        Focus architecture
      </button>
    </div>

    <div class="workspace__grid" :style="gridStyle">
      <aside v-show="!leftCollapsed && !focusMode" class="workspace__left">
        <RequirementsPanel
          :user-request="session.user_request"
          :status="session.status"
          :clarification="session.pending_clarification"
          :assumptions="session.assumptions"
          :submitting="submitting"
          :assumptions-open="supportingOpen"
          @submit="handleSend"
        />

        <section class="panel" aria-label="Architecture metadata">
          <h2 class="panel__title">Architecture</h2>
          <CollapsibleSection
            title="Decisions"
            :count="architecture ? architecture.decisions.length : 0"
            :default-open="supportingOpen"
            test-id="section-decisions"
          >
            <ArchitectureDecisions :architecture="architecture" />
          </CollapsibleSection>
          <CollapsibleSection
            title="Assumptions"
            :count="architecture ? architecture.assumptions.length : 0"
            :default-open="supportingOpen"
            test-id="section-assumptions"
          >
            <ArchitectureAssumptions :architecture="architecture" />
          </CollapsibleSection>
          <div class="field">
            <h3 class="field__label">Warnings ({{ warnings.length }})</h3>
            <WarningsList :warnings="warnings" />
          </div>
        </section>
      </aside>

      <div
        v-if="!leftCollapsed && !focusMode"
        class="workspace__splitter"
        data-testid="splitter-left"
        @mousedown="(e) => onSplitterDown('left', e)"
      />

      <main class="workspace__center">
        <LoadingState
          v-if="architectureLoading && !architecture"
          label="Loading architecture…"
        />
        <ArchitectureGraph
          v-else-if="showGraph && model"
          :architecture="architecture"
          :model="model"
          @select-node="handleSelectNode"
        />
        <FailurePanel v-else-if="isFailed" :session="session" />
        <section
          v-else-if="heroClarification"
          class="hero hero--clarification"
          aria-label="Decision needed"
          data-testid="hero-clarification"
        >
          <p class="hero__eyebrow">We need one decision from you</p>
          <h2 class="hero__question">{{ heroClarification.question }}</h2>
          <p v-if="heroClarification.reason" class="hero__reason">
            <strong>Why we need this. </strong>{{ heroClarification.reason }}
          </p>
          <p class="hero__meta">
            {{ heroClarification.requirement_key }} ·
            {{ heroClarification.impact }}
          </p>
          <form class="hero__form" @submit.prevent="handleHeroSubmit">
            <label class="field__label" for="hero-answer">Your answer</label>
            <textarea
              id="hero-answer"
              v-model="heroDraft"
              rows="2"
              :disabled="submitting"
              placeholder="Type the value this design should use"
              data-testid="hero-answer-input"
            />
            <div class="hero__actions">
              <button type="submit" :disabled="submitting || !heroDraft.trim()">
                {{ submitting ? "Sending…" : "Send answer" }}
              </button>
              <span class="hero__or">or</span>
              <button
                type="button"
                class="button--secondary"
                :disabled="submitting"
                data-testid="hero-delegate"
                @click="handleHeroDelegate"
              >
                Let the architect decide
              </button>
            </div>
          </form>
        </section>
        <section
          v-else-if="isRunInFlight()"
          class="hero"
          aria-label="Design in progress"
          data-testid="hero-running"
        >
          <p class="hero__spinner" aria-hidden="true">◌</p>
          <h2 class="hero__title">Designing your system</h2>
          <p class="hero__lede">
            Your architecture will appear here once the design is complete.
          </p>
        </section>
        <section
          v-else-if="canStartRun"
          class="hero"
          aria-label="Ready to design"
          data-testid="hero-ready"
        >
          <p class="hero__eyebrow">Requirements ready</p>
          <h2 class="hero__title">Start your design run</h2>
          <p class="hero__lede">
            Requirements are complete. Run capacity, component selection,
            cost and architecture composition.
          </p>
          <div class="hero__actions">
            <button
              type="button"
              :disabled="startingRun"
              data-testid="start-run"
              @click="handleStartRun"
            >
              {{ startingRun ? "Starting…" : "Start design run →" }}
            </button>
          </div>
        </section>
        <EmptyState
          v-else
          label="No architecture artifact for this session yet."
        />
      </main>

      <div
        v-if="!rightCollapsed && !focusMode"
        class="workspace__splitter workspace__splitter--right"
        data-testid="splitter-right"
        @mousedown="(e) => onSplitterDown('right', e)"
      />

      <aside v-show="!rightCollapsed && !focusMode" class="workspace__right">
        <NodeDetails
          :node="selectedNode"
          :is-entry-point="isEntryPoint"
          :is-data-store="isDataStore"
        />
        <CostPanel
          :cost="session.stages.cost"
          :has-cost-artifact="!!session.stages.cost"
          :details-open="supportingOpen"
        />
        <ApprovalPanel
          :session="session"
          :critique="critique"
          @approved="handleApproved"
          @failed="handleApprovalFailed"
        />
        <TerraformPanel
          :plan="terraformPlan"
          :session-id="session.session_id"
          :design-approved="!!session.stages.design_approval?.approved"
          @generated="handlePlanGenerated"
          @failed="handleApprovalFailed"
        />
        <ProvisionPanel
          :session-id="session.session_id"
          :plan="terraformPlan"
          :monthly-cost="monthlyCost"
          @provisioned="handleApproved"
          @failed="handleApprovalFailed"
        />
        <OptimizationRecommendations
          v-if="activeRecommendations"
          :recommendations="activeRecommendations"
        />
      </aside>
    </div>

    <StatusBar :session="session" :unpriced-count="unpricedCount" />
  </div>
</template>
