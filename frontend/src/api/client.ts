/**
 * Read client for the design API.
 *
 * The frontend performs no orchestration: it fetches artifacts and
 * displays them. It never derives requirements, capacity, selection,
 * pricing or architecture semantics of its own.
 */

import type {
  ApprovalView,
  ArchitectureResult,
  CostResult,
  CostSummary,
  CritiqueResult,
  DesignRunView,
  InputSuggestionsView,
  OptimizationRecommendationsView,
  ProvisioningResultView,
  QualityGateView,
  SessionSummary,
  SessionView,
  TerraformPlanView,
} from "../types";

export interface ApiError {
  code: string;
  message: string;
  status: number;
}

/** Extract a readable message from the API's {detail: {detail, code}} shape. */
export function parseApiError(payload: unknown, status: number): ApiError {
  const fallback = `Request failed with status ${status}`;
  if (!payload || typeof payload !== "object") {
    return { code: "unknown", message: fallback, status };
  }
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return { code: "error", message: detail, status };
  }
  if (detail && typeof detail === "object") {
    const typed = detail as { detail?: unknown; code?: unknown };
    return {
      code: typeof typed.code === "string" ? typed.code : "error",
      message:
        typeof typed.detail === "string" ? typed.detail : fallback,
      status,
    };
  }
  return { code: "unknown", message: fallback, status };
}

/**
 * Local demo token, sent as a bearer header so the API's mutating
 * endpoints accept the request. Set VITE_AI_ARCHITECT_API_TOKEN in
 * .env.local (gitignored) to the same value as the backend's
 * AI_ARCHITECT_API_TOKEN. Read endpoints and /health need no token.
 */
function authHeaders(): Record<string, string> {
  const token = import.meta.env.VITE_AI_ARCHITECT_API_TOKEN;
  if (typeof token !== "string" || !token.trim()) {
    return {};
  }
  return { Authorization: `Bearer ${token.trim()}` };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw parseApiError(payload, response.status) as unknown as Error;
  }
  return (await response.json()) as T;
}

export function fetchSession(sessionId: string): Promise<SessionView> {
  return request<SessionView>(
    `/design/sessions/${encodeURIComponent(sessionId)}`,
  );
}

/**
 * List recent design sessions, most recently active first.
 * Server-side search + pagination: the browser never receives the
 * full history. `query` matches the persisted request or session id;
 * `limit`/`offset` bound the page (page size 10).
 */
export function fetchSessions(
  query?: string,
  limit = 10,
  offset = 0,
): Promise<SessionSummary[]> {
  const params = new URLSearchParams();
  if (query && query.trim()) params.set("query", query.trim());
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  return request<SessionSummary[]>(`/design/sessions?${params.toString()}`);
}

/**
 * Create a requirements session from the user's natural-language request.
 * The same endpoint backs the CLI-driven flow; a design run is started
 * separately once requirements are ready.
 */
export function createSession(userRequest: string): Promise<SessionView> {
  return request<SessionView>("/design/sessions", {
    method: "POST",
    body: JSON.stringify({ user_request: userRequest }),
  });
}

export function fetchArchitecture(
  sessionId: string,
): Promise<ArchitectureResult> {
  return request<ArchitectureResult>(
    `/design/sessions/${encodeURIComponent(sessionId)}/architecture`,
  );
}

export function fetchCost(sessionId: string): Promise<CostResult> {
  return request<CostResult>(
    `/design/sessions/${encodeURIComponent(sessionId)}/cost`,
  );
}

export function submitClarification(
  sessionId: string,
  text: string,
): Promise<SessionView> {
  return request<SessionView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/messages`,
    { method: "POST", body: JSON.stringify({ text }) },
  );
}

/**
 * Start one design run for a session whose requirements are already
 * ready. The backend delegates to the same orchestration entry point the
 * CLI uses; this client only reports the new design session id.
 */
export function startDesignRun(
  requirementsSessionId: string,
): Promise<DesignRunView> {
  return request<DesignRunView>(
    `/design/sessions/${encodeURIComponent(requirementsSessionId)}/run`,
    { method: "POST" },
  );
}

/** Fetch the stored design critique verbatim (404 while unavailable). */
export function fetchCritique(
  sessionId: string,
): Promise<CritiqueResult> {
  return request<CritiqueResult>(
    `/design/sessions/${encodeURIComponent(sessionId)}/critique`,
  );
}

/** Fetch the stored quality-gate verdict verbatim. */
export function fetchQualityGate(
  sessionId: string,
): Promise<QualityGateView> {
  return request<QualityGateView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/quality-gate`,
  );
}

/**
 * Fetch unevaluated diagnostic recommendations for a blocked gate.
 * Recommendations are served verbatim: none represents a feasible,
 * affordable, or budget-compliant architecture.
 */
export function fetchOptimizationRecommendations(
  sessionId: string,
): Promise<OptimizationRecommendationsView> {
  return request<OptimizationRecommendationsView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/optimization-recommendations`,
  );
}

/**
 * Record an explicit human verdict on the gated design. Allowed only
 * when the stored gate verdict permits it; approval alone provisions
 * nothing.
 */
export function approveDesign(
  sessionId: string,
  approved: boolean,
  note = "",
): Promise<ApprovalView> {
  return request<ApprovalView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/approve-design`,
    { method: "POST", body: JSON.stringify({ approved, note }) },
  );
}

/** Fetch the stored Terraform plan artifact verbatim (404 until generated). */
export function fetchTerraformPlan(
  sessionId: string,
): Promise<TerraformPlanView> {
  return request<TerraformPlanView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/terraform`,
  );
}

/**
 * Generate the deterministic Terraform plan for an approved design.
 * Requires a recorded design approval; anything else is refused before
 * any Terraform is produced. `inputs` maps resource_id to
 * human-decided required parameters; every value is validated
 * server-side by the module contracts.
 */
export function generateTerraformPlan(
  sessionId: string,
  inputs: Record<string, Record<string, string>> = {},
): Promise<TerraformPlanView> {
  return request<TerraformPlanView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/terraform`,
    { method: "POST", body: JSON.stringify({ inputs }) },
  );
}

/**
 * Ask the backend for suggested values for the plan's pending human
 * decisions. The response is advisory and changes nothing: the operator
 * reviews the values in the panel, edits them if needed, and submits
 * them through the normal input path.
 */
export function suggestProvisioningInputs(
  sessionId: string,
): Promise<InputSuggestionsView> {
  return request<InputSuggestionsView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/provisioning/suggest-inputs`,
    { method: "POST" },
  );
}

/**
 * Record an explicit human verdict on the stored Terraform plan.
 * Approval alone executes nothing; it only unlocks the provision
 * endpoint, which re-validates the record before running.
 */
export function approveProvisioning(
  sessionId: string,
  approved: boolean,
  note = "",
): Promise<ApprovalView> {
  return request<ApprovalView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/approve-provisioning`,
    { method: "POST", body: JSON.stringify({ approved, note }) },
  );
}

/**
 * Execute the stored, approved plan via the controlled executor.
 * `confirm` must equal "APPLY" for apply mode; the backend refuses
 * anything else before any subprocess starts. The only mutation the
 * UI can ever trigger flows through this call.
 */
export function provision(
  sessionId: string,
  mode: "plan" | "apply",
  confirm = "",
): Promise<ProvisioningResultView> {
  return request<ProvisioningResultView>(
    `/design/sessions/${encodeURIComponent(sessionId)}/provision`,
    { method: "POST", body: JSON.stringify({ mode, confirm }) },
  );
}

/** Total across every pricing model, copied from backend totals. */
export function totalMonthlyCost(summary: CostSummary | null): number | null {
  if (!summary) return null;
  const values = Object.values(summary.totals_usd_per_month ?? {});
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0);
}

export function formatUsd(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "unpriced";
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}/mo`;
}
