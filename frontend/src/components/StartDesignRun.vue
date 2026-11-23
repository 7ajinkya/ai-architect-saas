<script setup lang="ts">
/**
 * Start a new design run.
 *
 * Landing-only by construction: hero + new-design input + session
 * browser (search + bounded pages). Clarification belongs exclusively
 * to the active workspace session and is never rendered here.
 *
 * Flow mirrors the backend contract exactly:
 *   1. POST /design/sessions           -> requirements session
 *   2. if requirements are ready      -> POST /{id}/run -> design session
 *   3. if clarification is pending     -> open the requirements session
 *      in its workspace (the workspace owns clarification UI + run start)
 */
import { ref } from "vue";

import {
  createSession,
  fetchSession,
  fetchSessions,
  startDesignRun,
  submitClarification,
} from "../api/client";
import ErrorState from "./ErrorState.vue";
import type { SessionSummary, SessionView } from "../types";

const PAGE_SIZE = 10;

const props = withDefaults(
  defineProps<{
    createSession?: (text: string) => Promise<SessionView>;
    runDesign?: (requirementsSessionId: string) => Promise<{ session_id: string }>;
    answerClarification?: (sessionId: string, text: string) => Promise<SessionView>;
    refreshSession?: (sessionId: string) => Promise<SessionView>;
    listSessions?: (
      query?: string,
      limit?: number,
      offset?: number,
    ) => Promise<SessionSummary[]>;
  }>(),
  {
    createSession,
    runDesign: startDesignRun,
    answerClarification: submitClarification,
    refreshSession: fetchSession,
    listSessions: fetchSessions,
  },
);

const emit = defineEmits<{
  (event: "started", designSessionId: string): void;
}>();

const requirement = ref("");
const busy = ref(false);
const error = ref<string | null>(null);
const recent = ref<SessionSummary[]>([]);
const recentQuery = ref("");
const recentLoading = ref(false);
const recentLoadingMore = ref(false);
const recentHasMore = ref(false);
const recentLoaded = ref(false);

/** Status line for a recent design, derived from served fields only. */
function recentStatus(summary: SessionSummary): string {
  if (summary.has_error || summary.design_status === "failed") return "Failed";
  if (summary.design_status === "running") return "Running";
  if (summary.has_design_run) return "Designed";
  if (summary.requirements_status === "ready") return "Requirements ready";
  if (summary.requirements_status === "needs_clarification") {
    return "Needs input";
  }
  if (summary.requirements_status) return summary.requirements_status;
  return "Draft";
}

async function loadRecent(reset = true): Promise<void> {
  const query = recentQuery.value.trim();
  if (reset) {
    recentLoading.value = true;
  } else {
    recentLoadingMore.value = true;
  }
  try {
    const offset = reset ? 0 : recent.value.length;
    const page = await props.listSessions(
      query || undefined,
      PAGE_SIZE,
      offset,
    );
    recent.value = reset ? page : [...recent.value, ...page];
    recentHasMore.value = page.length === PAGE_SIZE;
    recentLoaded.value = true;
  } catch {
    // Recent designs are a convenience, never a blocker.
    if (reset) recent.value = [];
    recentHasMore.value = false;
  } finally {
    recentLoading.value = false;
    recentLoadingMore.value = false;
  }
}

function onSearch(): void {
  void loadRecent(true);
}

function loadMore(): void {
  if (recentLoadingMore.value || !recentHasMore.value) return;
  void loadRecent(false);
}

function openRecent(sessionId: string): void {
  emit("started", sessionId);
}

void loadRecent(true);

function describe(cause: unknown): string {
  if (cause && typeof cause === "object" && "message" in cause) {
    return String((cause as { message: unknown }).message);
  }
  return "Request failed.";
}

async function submitRequirement(): Promise<void> {
  const text = requirement.value.trim();
  if (!text) return;

  busy.value = true;
  error.value = null;
  try {
    const session = await props.createSession(text);
    // Requirements ready -> start the design run and open the design
    // session. Anything else (clarification pending) -> open the
    // requirements session in its workspace; clarification lives there.
    if (session.status === "ready" && !session.pending_clarification) {
      try {
        const run = await props.runDesign(session.session_id);
        emit("started", run.session_id);
        return;
      } catch (cause: unknown) {
        if ((cause as { code?: string })?.code === "clarification_required") {
          emit("started", session.session_id);
          return;
        }
        throw cause;
      }
    }
    emit("started", session.session_id);
  } catch (cause: unknown) {
    error.value = describe(cause);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="landing" aria-label="Start a design run">
    <p class="landing__eyebrow">AI Architect</p>
    <h2 class="landing__title">Design your cloud architecture from a natural-language description.</h2>
    <p class="landing__lede">
      Turn your requirements into a structured architecture with sized
      components and a monthly cost estimate.
    </p>

    <form class="landing__card" @submit.prevent="submitRequirement">
      <label class="landing__label" for="requirement">
        What do you want to build?
      </label>
      <textarea
        id="requirement"
        v-model="requirement"
        rows="7"
        :disabled="busy"
        placeholder="Describe your system, workload, scale, constraints, and requirements."
        data-testid="requirement-input"
      />
      <button
        type="submit"
        class="landing__cta"
        data-testid="start-run"
        :disabled="busy || requirement.trim() === ''"
      >
        {{ busy ? "Working…" : "Start designing →" }}
      </button>
    </form>

    <ul class="landing__hints" aria-label="What the designer produces">
      <li>Structured requirements</li>
      <li>Capacity modeling</li>
      <li>Component selection</li>
      <li>Cost estimation</li>
      <li>Architecture canvas</li>
    </ul>

    <section
      class="landing__recent"
      aria-label="Recent designs"
      data-testid="recent-designs"
    >
      <h3 class="landing__recent-title">Recent designs</h3>
      <form class="recent-search" @submit.prevent="onSearch">
        <label class="recent-search__label" for="recent-search-input">
          Search designs
        </label>
        <div class="recent-search__row">
          <input
            id="recent-search-input"
            v-model="recentQuery"
            placeholder="Search request text or session ID…"
            data-testid="recent-search"
            @input="onSearch"
          />
        </div>
      </form>
      <p v-if="recentLoading" class="recent-status" data-testid="recent-loading">
        Loading designs…
      </p>
      <p
        v-else-if="recentLoaded && recent.length === 0"
        class="recent-status"
        data-testid="recent-empty"
      >
        No designs found.
      </p>
      <ul v-else class="recent-list">
        <li
          v-for="item in recent"
          :key="item.session_id"
          class="recent-card"
          data-testid="recent-design"
        >
          <div class="recent-card__body">
            <p class="recent-card__title" :title="item.user_request || item.session_id">
              {{ item.user_request || "Untitled design" }}
            </p>
            <p class="recent-card__meta">{{ recentStatus(item) }}</p>
          </div>
          <button
            type="button"
            class="button--secondary"
            :disabled="busy"
            :data-testid="`open-recent-${item.session_id}`"
            @click="openRecent(item.session_id)"
          >
            Open →
          </button>
        </li>
      </ul>
      <button
        v-if="recentHasMore"
        type="button"
        class="button--secondary recent-more"
        :disabled="recentLoadingMore"
        data-testid="recent-load-more"
        @click="loadMore"
      >
        {{ recentLoadingMore ? "Loading…" : "Load more" }}
      </button>
    </section>

    <div v-if="busy" class="state state--loading" role="status" data-testid="running-state">
      Gathering requirements…
    </div>

    <ErrorState v-if="error" label="Could not start the design run." :detail="error" />
  </section>
</template>
