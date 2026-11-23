/**
 * CostPanel priced-subtotal tests.
 *
 * The displayed number is the backend's priced subtotal, not the full
 * architecture cost while unpriced entries remain. These tests
 * pin the coverage wording, the counts, the exclusion note and the
 * per-item backend reasons — without inventing any estimate.
 */
import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import CostPanel from "./CostPanel.vue";
import { sessionForB } from "../fixtures/fixtures";
import type { SessionView } from "../types";

beforeEach(() => {
  document.body.innerHTML = "";
});

function mountPanel(cost: SessionView["stages"]["cost"]) {
  return mount(CostPanel, {
    props: { cost, hasCostArtifact: true, detailsOpen: true },
    attachTo: document.body,
  });
}

describe("CostPanel priced subtotal", () => {
  it("labels the number as a priced subtotal, not the full cost", async () => {
    const wrapper = mountPanel(sessionForB().stages.cost!);
    expect(wrapper.find('[data-testid="cost-total"]').text()).toContain(
      "$12.50/mo",
    );
    expect(wrapper.text()).toContain("Estimated priced subtotal");
    expect(wrapper.text()).not.toContain("Estimated total ·");
  });

  it("shows priced/unpriced counts with an exclusion note", async () => {
    const wrapper = mountPanel(sessionForB().stages.cost!);
    const coverage = wrapper.find('[data-testid="cost-coverage"]');
    expect(coverage.text()).toContain("1 priced");
    expect(coverage.text()).toContain("1 unpriced");
    expect(coverage.text()).toContain("excludes");
  });

  it("surfaces backend unpriced reasons without estimating", async () => {
    const wrapper = mountPanel(sessionForB().stages.cost!);
    expect(wrapper.find('[data-testid="cost-unpriced"]').text()).toContain(
      "No live Price List fact matched this component.",
    );
    expect(wrapper.find('[data-testid="cost-unpriced"]').text()).not.toContain(
      "$0",
    );
    expect(wrapper.text()).toContain("nothing is estimated here");
  });

  it("shows an incompleteness note when fully_priced is false", async () => {
    const wrapper = mountPanel(sessionForB().stages.cost!);
    expect(
      wrapper.find('[data-testid="cost-coverage-incomplete"]').exists(),
    ).toBe(true);
    expect(
      wrapper.find('[data-testid="cost-coverage-incomplete"]').text(),
    ).toContain("Pricing incomplete");
    expect(
      wrapper.find('[data-testid="cost-coverage-incomplete"]').text(),
    ).toContain("1 component(s)");
  });
});

describe("CostPanel fully priced", () => {
  const fullyPricedCost = {
    totals_usd_per_month: { on_demand: 12.5 },
    priced: [
      {
        component_name: "Durable Buffer",
        monthly_cost_usd: 12.5,
        unit: "requests",
        pricing_model: "on_demand",
        pricing_source: "aws-price-list-api",
        region_code: "eu-west-1",
      },
    ],
    unpriced: [],
    coverage: [
      {
        component_name: "Durable Buffer",
        dimension: "requests",
        label: "requests",
        status: "priced",
        reason: "Included in the estimated monthly total.",
      },
    ],
    warnings: [],
    assumptions: [],
    pricing_as_of: "2026-01-01T00:00:00Z",
    fully_priced: true,
    completeness: "fully_priced",
    best_estimate_total_usd_per_month: 12.5,
    estimated_range_usd_per_month: { low_usd: 12.5, high_usd: 12.5 },
    unresolved_dimension_count: 0,
  } as SessionView["stages"]["cost"];

  it("does not show an incompleteness note when fully priced", async () => {
    const wrapper = mountPanel(fullyPricedCost);
    expect(
      wrapper.find('[data-testid="cost-coverage-incomplete"]').exists(),
    ).toBe(false);
  });

  it("calls the number an estimated total only when fully priced", async () => {
    const wrapper = mountPanel(fullyPricedCost);
    expect(wrapper.text()).toContain("Estimated total · per month");
    expect(wrapper.text()).not.toContain("Estimated priced subtotal");
  });

  it("shows fully_priced as true when all components are priced", async () => {
    const wrapper = mountPanel(fullyPricedCost);
    const coverage = wrapper.find('[data-testid="cost-coverage"]');
    expect(coverage.text()).toContain("1 priced");
    expect(coverage.text()).toContain("0 unpriced");
  });
});

describe("CostPanel pricing coverage", () => {
  const gappedCost = {
    totals_usd_per_month: { on_demand: 21.75 },
    priced: [
      {
        component_name: "Ledger Store",
        monthly_cost_usd: 21.75,
        unit: "wru",
        pricing_model: "on_demand",
        pricing_source: "aws-price-list-api",
        region_code: "eu-west-1",
      },
    ],
    unpriced: [
      {
        component_name: "Compute",
        reason:
          "No usage derivation rule for usage key 'gb_second'; not priced rather than estimated.",
      },
    ],
    coverage: [
      {
        component_name: "Compute",
        dimension: "gb_second",
        label: "function duration (GB-seconds)",
        status: "usage_unknown",
        reason:
          "No usage derivation rule for usage key 'gb_second'; not priced rather than estimated.",
      },
      {
        component_name: "Compute",
        dimension: "requests",
        label: "requests",
        status: "priced",
        reason: "Included in the estimated monthly total.",
      },
      {
        component_name: "Ledger Store",
        dimension: "rcu_reserved",
        label: "reserved read capacity",
        status: "not_applicable",
        reason:
          "pay_per_request billing does not charge for reserved capacity.",
        disposition: "priceable",
      },
      {
        component_name: "Ledger Store",
        dimension: "storage_gbmo",
        label: "table storage (GB-month)",
        status: "unsupported",
        reason:
          "DynamoDB table storage is billed per GB-month in addition to read/write request charges.",
        disposition: "unsupported",
      },
      {
        component_name: "Ledger Store",
        dimension: "wru",
        label: "write request units",
        status: "priced",
        reason: "Included in the estimated monthly total.",
      },
    ],
    warnings: [],
    assumptions: [],
    pricing_as_of: "2026-01-01T00:00:00Z",
    fully_priced: false,
    completeness: "partially_unresolved",
    best_estimate_total_usd_per_month: null,
    estimated_range_usd_per_month: null,
    unresolved_dimension_count: 2,
  } as SessionView["stages"]["cost"];

  it("lists only non-priced dimensions with the backend statuses and reasons", async () => {
    const wrapper = mountPanel(gappedCost);
    const gaps = wrapper.findAll('[data-testid="cost-coverage-gap"]');
    expect(gaps).toHaveLength(2);
    const text = wrapper.find('[data-testid="cost-coverage-entries"]').text();
    expect(text).toContain("function duration");
    expect(text).toContain("not included · usage not derivable");
    expect(text).toContain("table storage");
    expect(text).toContain("not included · not in pricing scope");
    expect(text).not.toContain("write request units");
    expect(text).not.toContain("read request units");
    expect(text).not.toContain("not applicable");
    expect(text).not.toContain("$");
  });

  it("names both gap kinds in the incompleteness note", async () => {
    const wrapper = mountPanel(gappedCost);
    const note = wrapper
      .find('[data-testid="cost-coverage-incomplete"]')
      .text();
    expect(note).toContain("Pricing incomplete");
    expect(note).toContain("1 component(s) not priced");
    expect(note).toContain("2 billing dimension(s) unresolved");
  });

  it("shows low, typical, and high cost scenarios when published", async () => {
    const estimated = {
      ...gappedCost,
      unpriced: [],
      coverage: [],
      completeness: "estimated_with_assumptions",
      best_estimate_total_usd_per_month: 25.5,
      estimated_range_usd_per_month: { low_usd: 23, high_usd: 28 },
      unresolved_dimension_count: 0,
    } as SessionView["stages"]["cost"];
    const wrapper = mountPanel(estimated);
    const range = wrapper.find('[data-testid="cost-range"]');
    expect(range.exists()).toBe(true);
    expect(range.text()).toContain("Low $23.00/mo");
    expect(range.text()).toContain("Typical $25.50/mo");
    expect(range.text()).toContain("High $28.00/mo");
  });

  it("explains a withheld total range and still labels quantity scenarios", async () => {
    const partial = {
      ...gappedCost,
      estimates: [
        {
          component_name: "Compute",
          dimension: "gb_second",
          unit: "GB-seconds",
          low_quantity: 100000,
          typical_quantity: 200000,
          high_quantity: 300000,
          provenance: "deterministic_derivation",
          confidence: "medium",
          rationale: "Derived from the stated invocation assumptions.",
        },
      ],
    } as SessionView["stages"]["cost"];
    const wrapper = mountPanel(partial);
    const note = wrapper.find('[data-testid="cost-range-unavailable"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain("Low/typical/high total unavailable");
    const quantities = wrapper.find('[data-testid="cost-estimates"]').text();
    expect(quantities).toContain("low 100,000");
    expect(quantities).toContain("typical 200,000");
    expect(quantities).toContain("high 300,000");
  });

  it("renders legacy not_modeled artifacts with their stored wording", async () => {
    const legacy = {
      ...gappedCost,
      coverage: [
        {
          component_name: "Ledger Store",
          dimension: "storage_gbmo",
          label: "table storage (GB-month)",
          status: "not_modeled",
          reason:
            "DynamoDB table storage is billed per GB-month in addition to read/write request charges.",
        },
      ],
      unresolved_dimension_count: 1,
    } as SessionView["stages"]["cost"];
    const wrapper = mountPanel(legacy);
    const text = wrapper.find('[data-testid="cost-coverage-entries"]').text();
    expect(text).toContain("not included · not modeled");
    const note = wrapper
      .find('[data-testid="cost-coverage-incomplete"]')
      .text();
    expect(note).toContain("1 billing dimension(s) unresolved");
  });

  it("shows no gap entries when every modeled dimension is included", async () => {
    const wrapper = mountPanel(sessionForB().stages.cost!);
    expect(
      wrapper.find('[data-testid="cost-coverage-entries"]').exists(),
    ).toBe(false);
    expect(wrapper.text()).toContain("Every modeled dimension is included");
  });
});
