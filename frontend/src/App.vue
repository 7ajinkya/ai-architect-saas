<script setup lang="ts">
import { ref } from "vue";

import StartDesignRun from "./components/StartDesignRun.vue";
import Workspace from "./components/Workspace.vue";
import { fetchSession, fetchSessions } from "./api/client";
import type { SessionSummary } from "./types";

const initial =
  typeof window !== "undefined"
    ? new URLSearchParams(window.location.search).get("session") ?? ""
    : "";

const sessionId = ref(initial);
const searchText = ref("");
const searchResults = ref<SessionSummary[]>([]);
const searching = ref(false);
const searchError = ref<string | null>(null);
const showResults = ref(false);

let searchTimer: ReturnType<typeof setTimeout> | undefined;

function scheduleSearch(): void {
  clearTimeout(searchTimer);
  const query = searchText.value.trim();
  if (query.length < 2) {
    searchResults.value = [];
    showResults.value = false;
    return;
  }
  searchTimer = setTimeout(() => {
    void runSearch(query);
  }, 250);
}

async function runSearch(query: string): Promise<void> {
  searching.value = true;
  searchError.value = null;
  try {
    searchResults.value = await fetchSessions(query, 5, 0);
    showResults.value = true;
  } catch {
    searchResults.value = [];
    showResults.value = false;
  } finally {
    searching.value = false;
  }
}

async function open(): Promise<void> {
  const text = searchText.value.trim();
  if (!text) return;
  searchError.value = null;
  // Exact session id first: opening by UUID never needs a search round.
  try {
    await fetchSession(text);
    sessionId.value = text;
    showResults.value = false;
    return;
  } catch {
    // Fall through to request-text search.
  }
  try {
    const matches = await fetchSessions(text, 5, 0);
    if (matches.length === 0) {
      searchError.value = "No matching design found.";
      return;
    }
    // Partial UUID or unique request-text match opens directly;
    // ambiguous text shows the shortlist for one click.
    const exact = matches.find((m) => m.session_id === text);
    if (exact || matches.length === 1) {
      sessionId.value = (exact ?? matches[0]).session_id;
      showResults.value = false;
      searchText.value = "";
      return;
    }
    searchResults.value = matches;
    showResults.value = true;
  } catch {
    searchError.value = "Search failed.";
  }
}

function openResult(id: string): void {
  sessionId.value = id;
  showResults.value = false;
  searchText.value = "";
}

/** A started run hands back the new design session id. */
function onStarted(designSessionId: string): void {
  sessionId.value = designSessionId;
}

/** Workspace started a design run for a requirements session. */
function onOpenSession(nextId: string): void {
  sessionId.value = nextId;
}
</script>

<template>
  <div class="app">
    <form class="app__bar" @submit.prevent="open">
      <label for="session-search">Designs</label>
      <div class="app__search">
        <input
          id="session-search"
          v-model="searchText"
          placeholder="Search designs or session ID..."
          data-testid="top-search"
          @input="scheduleSearch"
        />
        <button type="submit" data-testid="top-open">Open</button>
        <div
          v-if="showResults && searchResults.length > 0"
          class="app__results"
          data-testid="top-results"
        >
          <button
            v-for="item in searchResults"
            :key="item.session_id"
            type="button"
            class="app__result"
            :data-testid="`top-result-${item.session_id}`"
            @click="openResult(item.session_id)"
          >
            <span class="app__result-title">{{
              item.user_request || "Untitled design"
            }}</span>
            <span class="app__result-meta">{{ item.session_id.slice(0, 8) }}</span>
          </button>
        </div>
      </div>
      <span v-if="searching" class="app__hint">Searching…</span>
      <span v-if="searchError" class="app__error" data-testid="top-error">{{
        searchError
      }}</span>
    </form>

    <Workspace
      v-if="sessionId"
      :key="sessionId"
      :session-id="sessionId"
      @back="sessionId = ''"
      @open-session="onOpenSession"
    />

    <div v-else class="app__start">
      <StartDesignRun @started="onStarted" />
    </div>
  </div>
</template>

<style scoped>
.app__start {
  display: flex;
  justify-content: center;
  padding: 32px 16px;
}
.app__search {
  position: relative;
  flex: 1;
  display: flex;
  gap: 8px;
}
.app__search input {
  flex: 1;
}
.app__results {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 6px;
  box-shadow: 0 12px 32px rgb(0 0 0 / 0.4);
}
.app__result {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  background: transparent;
  border: 0;
  color: var(--text);
  text-align: left;
  padding: 8px 10px;
  border-radius: 6px;
  font-weight: 500;
}
.app__result:hover {
  background: var(--panel-2);
}
.app__result-meta {
  color: var(--muted);
  font-family: ui-monospace, monospace;
  font-size: 11px;
}
.app__hint {
  color: var(--muted);
  font-size: 12px;
}
.app__error {
  color: var(--error);
  font-size: 12px;
}
</style>
