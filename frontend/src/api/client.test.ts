/**
 * API client tests: error parsing and cost display rules.
 *
 * The important guarantee here is cost honesty: an unknown or absent
 * total is never rendered as $0.
 */

import { describe, expect, it, vi, afterEach } from "vitest";

import {
  approveProvisioning,
  formatUsd,
  parseApiError,
  provision,
  totalMonthlyCost,
} from "./client";
import type { CostSummary } from "../types";

const summary = (overrides: Partial<CostSummary> = {}): CostSummary => ({
  totals_usd_per_month: { on_demand: 12.5 },
  priced: [],
  unpriced: [],
  coverage: [],
  warnings: [],
  assumptions: [],
  pricing_as_of: null,
  fully_priced: true,
  completeness: "fully_priced",
  best_estimate_total_usd_per_month: 12.5,
  estimated_range_usd_per_month: { low_usd: 12.5, high_usd: 12.5 },
  unresolved_dimension_count: 0,
  ...overrides,
});

describe("parseApiError", () => {
  it("reads the {detail: {detail, code}} envelope", () => {
    const error = parseApiError(
      { detail: { detail: "Unknown session: x", code: "session_not_found" } },
      404,
    );
    expect(error.code).toBe("session_not_found");
    expect(error.message).toBe("Unknown session: x");
    expect(error.status).toBe(404);
  });

  it("reads a plain string detail", () => {
    const error = parseApiError({ detail: "boom" }, 500);
    expect(error.message).toBe("boom");
    expect(error.code).toBe("error");
  });

  it("falls back safely on an unexpected body", () => {
    const error = parseApiError(null, 500);
    expect(error.message).toContain("500");
    expect(error.code).toBe("unknown");
  });

  it("keeps the persisted-state code for corrupt artifacts", () => {
    const error = parseApiError(
      { detail: { detail: "Stored architecture is invalid", code: "persisted_state_invalid" } },
      409,
    );
    expect(error.code).toBe("persisted_state_invalid");
    expect(error.status).toBe(409);
  });
});

describe("totalMonthlyCost", () => {
  it("adds the totals the backend reported per pricing model", () => {
    expect(
      totalMonthlyCost(summary({ totals_usd_per_month: { on_demand: 10, reserved_3yr: 5 } })),
    ).toBe(15);
  });

  it("returns null when there is no cost summary", () => {
    expect(totalMonthlyCost(null)).toBeNull();
  });

  it("returns null for an empty totals map rather than zero", () => {
    expect(totalMonthlyCost(summary({ totals_usd_per_month: {} }))).toBeNull();
  });
});

describe("formatUsd", () => {
  it("formats a known amount with its period", () => {
    expect(formatUsd(12.5)).toBe("$12.50/mo");
  });

  it("formats a genuine zero as zero", () => {
    expect(formatUsd(0)).toBe("$0.00/mo");
  });

  it("never substitutes zero for an unknown amount", () => {
    expect(formatUsd(null)).toBe("unpriced");
    expect(formatUsd(Number.NaN)).toBe("unpriced");
  });
});

describe("provisioning calls", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubFetch(response: unknown): {
    calls: { url: string; init?: RequestInit }[];
  } {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(response),
        });
      }),
    );
    return { calls };
  }

  it("approves through the approval endpoint, never the executor", async () => {
    const { calls } = stubFetch({ approved: true });
    await approveProvisioning("s-1", true, "note");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toContain("/approve-provisioning");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      approved: true,
      note: "note",
    });
  });

  it("provisions only through the controlled endpoint with an explicit mode", async () => {
    const { calls } = stubFetch({ status: "apply_ok" });
    await provision("s-1", "apply", "APPLY");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toContain("/provision");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      mode: "apply",
      confirm: "APPLY",
    });
  });
});
