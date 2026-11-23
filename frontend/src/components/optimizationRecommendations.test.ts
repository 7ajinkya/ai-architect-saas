/**
 * OptimizationRecommendations panel tests.
 *
 * The panel is a read-only display of unevaluated diagnostic
 * recommendations. These tests pin the rendered fields, the optional
 * target/requirement rows, the fixed disclaimer, and the empty state —
 * no recommendation is ever presented as evaluated.
 */
import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import OptimizationRecommendations from "./OptimizationRecommendations.vue";
import type {
  OptimizationRecommendationView,
  OptimizationRecommendationsView,
} from "../types";

beforeEach(() => {
  document.body.innerHTML = "";
});

function recommendation(
  overrides: Partial<OptimizationRecommendationView> = {},
): OptimizationRecommendationView {
  return {
    recommendation_id: "rec-1",
    derived_from_artifact_id: "artifact-1",
    finding_category: "cost_ceiling",
    finding_statement: "Priced totals exceed the stated monthly budget ceiling.",
    proposed_change: "component_swap",
    target_component: "Gateway",
    rationale: "A lower-cost alternative may fit the ceiling.",
    affects_requirement: "budget_constraints",
    requirement_change_note: null,
    evaluated: false,
    status: "unevaluated",
    disclaimer:
      "This recommendation has not been priced, composed, or gated. " +
      "It does not represent a feasible, affordable, or " +
      "budget-compliant architecture.",
    ...overrides,
  };
}

function view(
  recommendations: OptimizationRecommendationView[],
): OptimizationRecommendationsView {
  return {
    session_id: "design-s1",
    derived_from_artifact_id: "artifact-1",
    recommendations,
  };
}

function mountPanel(recommendations: OptimizationRecommendationView[]) {
  return mount(OptimizationRecommendations, {
    props: { recommendations: view(recommendations) },
    attachTo: document.body,
  });
}

describe("OptimizationRecommendations panel", () => {
  it("renders category, statement, and rationale for each entry", () => {
    const wrapper = mountPanel([recommendation()]);
    const panel = wrapper.find('[data-testid="optimization-recommendations"]');
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("cost_ceiling");
    expect(panel.text()).toContain(
      "Priced totals exceed the stated monthly budget ceiling.",
    );
    expect(panel.text()).toContain("A lower-cost alternative may fit the ceiling.");
  });

  it("shows the target component and touched requirement when present", () => {
    const wrapper = mountPanel([recommendation()]);
    expect(wrapper.text()).toContain("Target component:");
    expect(wrapper.text()).toContain("Gateway");
    expect(wrapper.text()).toContain("Touches requirement:");
    expect(wrapper.text()).toContain("budget_constraints");
  });

  it("omits target and requirement rows when absent", () => {
    const wrapper = mountPanel([
      recommendation({ target_component: null, affects_requirement: null }),
    ]);
    expect(wrapper.text()).not.toContain("Target component:");
    expect(wrapper.text()).not.toContain("Touches requirement:");
  });

  it("always shows the unevaluated disclaimer", () => {
    const wrapper = mountPanel([recommendation()]);
    expect(wrapper.text()).toContain("not been priced, composed, or gated");
    expect(wrapper.text()).toContain("does not represent a feasible");
    expect(wrapper.text()).toContain("budget-compliant");
  });

  it("shows an empty state when there are no recommendations", () => {
    const wrapper = mountPanel([]);
    expect(wrapper.text()).toContain(
      "No optimization recommendations available for this session.",
    );
  });
});
