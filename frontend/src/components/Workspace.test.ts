/**
 * Workspace rendering tests (Vue 3).
 *
 * Behavioral coverage is carried over unchanged from the approved React
 * implementation: both structurally different fixtures render through
 * the same components, and no test references a workload.
 *
 * The real Vue Flow renderer is used rather than a stub, so these tests
 * assert that the adapter's nodes and edges actually reach the DOM.
 */

import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import Workspace from "./Workspace.vue";
import {
  FIXTURE_A,
  FIXTURE_B,
  sessionForA,
  sessionForB,
  sessionWithoutDesignRun,
} from "../fixtures/fixtures";
import type {
  ArchitectureResult,
  OptimizationRecommendationsView,
  QualityGateView,
  SessionView,
} from "../types";

type LoadSession = (id: string) => Promise<SessionView>;
type LoadArchitecture = (id: string) => Promise<ArchitectureResult>;
type SendMessage = (id: string, text: string) => Promise<SessionView>;

async function render(
  loadSession: LoadSession,
  loadArchitecture: LoadArchitecture,
  onSendMessage?: SendMessage,
): Promise<VueWrapper> {
  const wrapper = mount(Workspace, {
    props: { sessionId: "session-test", loadSession, loadArchitecture, onSendMessage },
    attachTo: document.body,
  });
  await flushPromises();
  return wrapper;
}

const renderA = () =>
  render(
    () => Promise.resolve(sessionForA()),
    () => Promise.resolve(FIXTURE_A),
  );

const renderB = () =>
  render(
    () => Promise.resolve(sessionForB()),
    () => Promise.resolve(FIXTURE_B),
  );

const failWith =
  (message: string) =>
  (): Promise<never> =>
    Promise.reject(new Error(message));

function text(wrapper: VueWrapper, testId: string): string {
  return wrapper.find(`[data-testid="${testId}"]`).text();
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("Workspace with fixture A (linear chain, no cost artifact)", () => {
  it("renders requirements, graph, edges, status and assumptions", async () => {
    const wrapper = await renderA();

    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("design a generic three-tier workload");
    expect(text(wrapper, "requirements-status")).toContain("ready");
    expect(text(wrapper, "requirement-assumptions")).toContain("us-east-1");
    expect(text(wrapper, "requirement-assumptions")).toContain("architect_default");

    // Nodes reach the DOM with their persisted ids.
    const rendered = wrapper.findAll(".vue-flow__node");
    expect(rendered).toHaveLength(3);
    const ids = rendered.map((node) => node.attributes("data-id"));
    expect(ids).toEqual(["gateway", "worker", "records"]);

    // Both edges render.
    expect(wrapper.findAll(".vue-flow__edge")).toHaveLength(2);

    expect(text(wrapper, "status-design")).toContain("ready");
    expect(text(wrapper, "status-nodes")).toContain("3");
    expect(text(wrapper, "status-artifact")).toContain("v1");
  });

  it("shows per-stage progress derived from the session stages", async () => {
    const wrapper = await renderA();
    expect(wrapper.find('[data-testid="stage-progress"]').exists()).toBe(true);
    expect(
      wrapper.find('[data-testid="stage-requirements"]').attributes("data-state"),
    ).toBe("complete");
    expect(
      wrapper.find('[data-testid="stage-architecture"]').attributes("data-state"),
    ).toBe("complete");
  });

  it("shows architecture decisions and assumptions verbatim", async () => {
    const wrapper = await renderA();
    expect(text(wrapper, "architecture-decisions")).toContain(
      "keep the worker stateless",
    );
    expect(text(wrapper, "architecture-assumptions")).toContain(
      "Fixture assumption retained verbatim.",
    );
  });

  it("shows node details when a node is selected", async () => {
    const wrapper = await renderA();
    const worker = wrapper
      .findAll(".vue-flow__node")
      .find((node) => node.attributes("data-id") === "worker");
    expect(worker).toBeDefined();

    await worker!.trigger("click");
    await flushPromises();

    expect(text(wrapper, "detail-name")).toContain("Stateless Worker");
    expect(text(wrapper, "detail-node-id")).toContain("node_id: worker");
    expect(text(wrapper, "detail-category")).toContain("compute");
    expect(text(wrapper, "detail-role")).toContain("executes request handling");
    expect(text(wrapper, "detail-capacity")).toContain("concurrent_connections");
    expect(text(wrapper, "detail-evidence")).toContain("ev-worker");
    expect(text(wrapper, "detail-cost")).toContain("No cost link");
  });

  it("marks an entry point node", async () => {
    const wrapper = await renderA();
    const gateway = wrapper
      .findAll(".vue-flow__node")
      .find((node) => node.attributes("data-id") === "gateway");
    await gateway!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Entry point");
  });

  it("marks a data store node", async () => {
    const wrapper = await renderA();
    const records = wrapper
      .findAll(".vue-flow__node")
      .find((node) => node.attributes("data-id") === "records");
    await records!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Data store");
  });

  it("reports cost as unavailable rather than zero", async () => {
    const wrapper = await renderA();
    expect(text(wrapper, "status-cost")).toContain("not available");
    expect(text(wrapper, "status-unpriced")).toContain("not available");
    expect(wrapper.text()).not.toContain("$0.00");
  });
});

describe("Workspace with fixture B (fan-out, cost with unpriced component)", () => {
  it("renders all nodes and edges including the external endpoint", async () => {
    const wrapper = await renderB();
    const rendered = wrapper.findAll(".vue-flow__node");
    expect(rendered).toHaveLength(6);
    const ids = rendered.map((node) => node.attributes("data-id"));
    expect(ids).toContain("client");
    expect(wrapper.findAll(".vue-flow__edge")).toHaveLength(5);
  });

  it("shows the outstanding clarification question", async () => {
    const wrapper = await renderB();
    expect(text(wrapper, "clarification")).toContain(
      "How long should raw payloads be retained?",
    );
  });

  it("shows cost totals and priced components", async () => {
    const wrapper = await renderB();
    expect(text(wrapper, "cost-total")).toContain("$12.50/mo");
    expect(text(wrapper, "cost-priced")).toContain("Durable Buffer");
    expect(text(wrapper, "cost-priced")).toContain("$12.50/mo");
  });

  it("shows unpriced components honestly with a reason, never $0", async () => {
    const wrapper = await renderB();
    const unpriced = text(wrapper, "cost-unpriced");
    expect(unpriced).toContain("Cold Archive");
    expect(unpriced).toContain("unpriced");
    expect(unpriced).toContain("No live Price List fact matched this component.");
    expect(unpriced).not.toContain("$0");
    expect(text(wrapper, "status-unpriced")).toContain("1");
  });

  it("shows backend warnings without inventing any", async () => {
    const wrapper = await renderB();
    const warningItems = wrapper.findAll('[data-testid="warnings"] .warning');
    expect(warningItems.length).toBeGreaterThan(0);
    expect(warningItems.map((item) => item.text())).toContain(
      "One component could not be priced.",
    );
    expect(text(wrapper, "status-warnings")).toContain("2");
  });

  it("surfaces an unpriced node as unpriced in the details panel", async () => {
    const wrapper = await renderB();
    const archive = wrapper
      .findAll(".vue-flow__node")
      .find((node) => node.attributes("data-id") === "archive");
    await archive!.trigger("click");
    await flushPromises();

    const unpriced = text(wrapper, "detail-cost-unpriced");
    expect(unpriced).toContain("unpriced");
    expect(unpriced).toContain("No live Price List fact matched this component.");
    expect(unpriced).not.toContain("$0.00");
  });

  it("marks an undeclared external endpoint as external", async () => {
    const wrapper = await renderB();
    const client = wrapper
      .findAll(".vue-flow__node")
      .find((node) => node.attributes("data-id") === "client");
    await client!.trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="detail-external"]').exists()).toBe(true);
    expect(text(wrapper, "detail-node-id")).toContain("node_id: client");
  });

  it("reports the artifact version", async () => {
    const wrapper = await renderB();
    expect(text(wrapper, "status-artifact")).toContain("v2");
    expect(text(wrapper, "status-nodes")).toContain("5");
  });
});

describe("Workspace states", () => {
  it("shows a loading state before the session arrives", () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => new Promise<SessionView>(() => undefined),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
    });
    expect(wrapper.find('[role="status"]').text()).toContain(
      "Loading design session…",
    );
  });

  it("offers a design run when requirements are ready but no run exists", async () => {
    const wrapper = await render(
      () => Promise.resolve(sessionWithoutDesignRun()),
      () => Promise.resolve(FIXTURE_A),
    );
    expect(wrapper.find('[data-testid="hero-ready"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("Start your design run");
    expect(text(wrapper, "status-design")).toContain("not run");
    expect(text(wrapper, "status-nodes")).toContain("0");
  });

  it("shows an error state when the session cannot be loaded", async () => {
    const wrapper = await render(
      failWith("Unknown session: missing"),
      () => Promise.resolve(FIXTURE_A),
    );
    const alert = wrapper.find('[role="alert"]');
    expect(alert.text()).toContain("Could not load this design session.");
    expect(alert.text()).toContain("Unknown session: missing");
  });

  it("keeps the workspace usable when the architecture cannot be loaded", async () => {
    const wrapper = await render(
      () => Promise.resolve(sessionForA()),
      failWith("Session has no architecture artifact yet"),
    );
    const alert = wrapper.find('[role="alert"]');
    expect(alert.text()).toContain("Partial data unavailable.");
    expect(alert.text()).toContain("Session has no architecture artifact yet");
    // Requirements still render.
    expect(wrapper.text()).toContain("design a generic three-tier workload");
  });

  it("shows an error state for a malformed artifact response", async () => {
    const wrapper = await render(
      () => Promise.resolve(sessionForA()),
      failWith("Stored architecture is invalid and cannot be reconstructed"),
    );
    expect(wrapper.find('[role="alert"]').text()).toContain(
      "Stored architecture is invalid",
    );
  });

  it("does not call the architecture endpoint when no design run exists", async () => {
    const loadArchitecture = vi.fn(() => Promise.resolve(FIXTURE_A));
    await render(() => Promise.resolve(sessionWithoutDesignRun()), loadArchitecture);
    expect(loadArchitecture).not.toHaveBeenCalled();
  });
});

describe("center states", () => {
  function failedSession(): SessionView {
    const base = sessionForA();
    return {
      ...base,
      stages: {
        ...base.stages,
        design_status: "failed",
        design_error: "Cost estimation stage failed: empty region",
        architecture: null,
        cost: null,
      },
    };
  }

  it("renders the failure panel instead of an empty canvas on failure", async () => {
    const wrapper = await render(
      () => Promise.resolve(failedSession()),
      () => Promise.resolve(FIXTURE_A),
    );

    expect(wrapper.find('[data-testid="failure-panel"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="failure-stage"]').text()).toContain(
      "Capacity",
    );
    expect(wrapper.find('[data-testid="failure-technical"]').text()).toContain(
      "Cost estimation stage failed: empty region",
    );
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(
      false,
    );
  });

  it("shows a progress hero while a run is in flight", async () => {
    const base = sessionWithoutDesignRun();
    const running: SessionView = {
      ...base,
      stages: { ...base.stages, has_design_run: true, design_status: "running" },
    };
    const wrapper = await render(
      () => Promise.resolve(running),
      () => Promise.resolve(FIXTURE_A),
    );

    expect(wrapper.find('[data-testid="hero-running"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(
      false,
    );
    expect(wrapper.find('[data-testid="failure-panel"]').exists()).toBe(false);
  });

  it("focuses a pending clarification when no artifact exists", async () => {
    const base = sessionWithoutDesignRun();
    const waiting: SessionView = {
      ...base,
      status: "needs_clarification",
      pending_clarification: {
        requirement_key: "region",
        requirement: "geographic deployment region",
        question: "Which region should this deploy to?",
        reason: "Region changes cost and latency.",
        impact: "architecture",
        assumable: true,
      },
    };
    // Stay on the clarification so both actions can be exercised.
    const onSendMessage = vi.fn((_id: string, _text: string) =>
      Promise.resolve(waiting),
    );
    const wrapper = await render(
      () => Promise.resolve(waiting),
      () => Promise.resolve(FIXTURE_A),
      onSendMessage,
    );

    expect(wrapper.find('[data-testid="hero-clarification"]').exists()).toBe(
      true,
    );
    await wrapper.find('[data-testid="hero-answer-input"]').setValue("us-east-1");
    await wrapper.find('[data-testid="hero-clarification"] button[type="submit"]').trigger("submit");
    await flushPromises();
    expect(onSendMessage).toHaveBeenCalledWith("session-test", "us-east-1");

    await wrapper
      .find('[data-testid="hero-delegate"]')
      .trigger("click");
    await flushPromises();
    expect(onSendMessage).toHaveBeenCalledWith("session-test", "you decide");
  });

  it("shows the header request title and status instead of a bare UUID", async () => {
    const wrapper = await renderA();
    expect(wrapper.find('[data-testid="session-request"]').text()).toContain(
      "design a generic three-tier workload",
    );
    expect(wrapper.find('[data-testid="header-status"]').text()).toContain("Ready");
  });
});

describe("phase-aware supporting sections", () => {
  async function openness(loadSession: LoadSession) {
    const wrapper = await render(
      loadSession,
      () => Promise.resolve(FIXTURE_A),
    );
    const state = (id: string) => {
      const found = wrapper.find(`[data-testid="${id}"]`);
      return found.exists()
        ? found.attributes("data-open")
        : undefined;
    };
    return {
      decisions: state("section-decisions"),
      assumptions: state("section-assumptions"),
      priced: state("section-cost-priced"),
      unpriced: state("section-cost-unpriced"),
      reqAssumptions: state("section-requirement-assumptions"),
    };
  }

  it("starts collapsed once the architecture exists", async () => {
    // Fixture B carries both an architecture summary and a cost artifact.
    const states = await openness(() => Promise.resolve(sessionForB()));
    expect(states).toEqual({
      decisions: "false",
      assumptions: "false",
      priced: "false",
      unpriced: "false",
      reqAssumptions: "false",
    });
  });

  it("starts collapsed while a run is in flight", async () => {
    const base = sessionWithoutDesignRun();
    const running: SessionView = {
      ...base,
      stages: { ...base.stages, has_design_run: true, design_status: "running" },
    };
    const states = await openness(() => Promise.resolve(running));
    expect(states.decisions).toBe("false");
    expect(states.reqAssumptions).toBe("false");
  });

  it("starts collapsed after a failure", async () => {
    const base = sessionWithoutDesignRun();
    const failed: SessionView = {
      ...base,
      stages: {
        ...base.stages,
        has_design_run: true,
        design_status: "failed",
        design_error: "Component selection stage failed: refused",
      },
    };
    const states = await openness(() => Promise.resolve(failed));
    expect(states.decisions).toBe("false");
    expect(states.assumptions).toBe("false");
  });

  it("starts open for an idle requirements session", async () => {
    const states = await openness(() =>
      Promise.resolve(sessionWithoutDesignRun()),
    );
    // No cost artifact exists yet, so only the rendered sections apply.
    expect(states.decisions).toBe("true");
    expect(states.assumptions).toBe("true");
    expect(states.reqAssumptions).toBe("true");
    expect(states.priced).toBeUndefined();
  });

  it("keeps collapsed content in the DOM for assistive tech", async () => {
    const wrapper = await render(
      () => Promise.resolve(sessionForA()),
      () => Promise.resolve(FIXTURE_A),
    );
    // Collapsed sections hide content without removing it.
    expect(
      wrapper.find('[data-testid="section-decisions-body"]').exists(),
    ).toBe(true);
  });
});

describe("clarification submission", () => {
  it("posts the answer and refreshes the panel", async () => {
    const onSendMessage = vi.fn((_id: string, _text: string) =>
      Promise.resolve(sessionForA()),
    );
    const wrapper = await render(
      () => Promise.resolve(sessionForB()),
      () => Promise.resolve(FIXTURE_B),
      onSendMessage,
    );

    await wrapper.find("textarea").setValue("keep for 30 days");
    await wrapper.find('button[type="submit"]').trigger("submit");
    await flushPromises();

    expect(onSendMessage).toHaveBeenCalledWith(
      "session-test",
      "keep for 30 days",
    );
    // The refreshed session has no pending question any more.
    expect(wrapper.text()).toContain("No outstanding question.");
  });

  it("delegates an assumable clarification with the backend phrase", async () => {
    const onSendMessage = vi.fn((_id: string, _text: string) =>
      Promise.resolve(sessionForA()),
    );
    const wrapper = await render(
      () => Promise.resolve(sessionForB()),
      () => Promise.resolve(FIXTURE_B),
      onSendMessage,
    );

    // Fixture B's clarification is assumable and carries a reason.
    expect(wrapper.find('[data-testid="clarification-reason"]').text()).toContain(
      "Retention changes storage selection.",
    );
    await wrapper
      .find('[data-testid="clarification-delegate"] button')
      .trigger("click");
    await flushPromises();

    expect(onSendMessage).toHaveBeenCalledWith("session-test", "you decide");
  });

  it("hides delegation for a non-assumable clarification", async () => {
    const base = sessionForB();
    const nonAssumable: SessionView = {
      ...base,
      pending_clarification:
        base.pending_clarification === null
          ? null
          : { ...base.pending_clarification, assumable: false },
    };
    const onSendMessage = vi.fn((_id: string, _text: string) =>
      Promise.resolve(sessionForA()),
    );
    const wrapper = await render(
      () => Promise.resolve(nonAssumable),
      () => Promise.resolve(FIXTURE_B),
      onSendMessage,
    );

    expect(wrapper.find('[data-testid="clarification"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="clarification-delegate"]').exists()).toBe(
      false,
    );
    expect(onSendMessage).not.toHaveBeenCalled();
  });

  it("surfaces a failed submission", async () => {
    const wrapper = await render(
      () => Promise.resolve(sessionForB()),
      () => Promise.resolve(FIXTURE_B),
      failWith("Clarification could not be applied"),
    );
    await wrapper.find("textarea").setValue("anything");
    await wrapper.find('button[type="submit"]').trigger("submit");
    await flushPromises();
    expect(wrapper.find('[role="alert"]').text()).toContain(
      "Clarification could not be applied",
    );
  });
});

describe("quality-gate fallback", () => {
  // sessionForA carries an architecture summary with NO embedded
  // quality_gate: exactly the payload shape that used to hide the
  // approval panel with no recourse.
  function gate(): QualityGateView {
    return {
      outcome: "approved_for_review",
      reasons: ["No unresolved critical findings (0 warnings)."],
      critical_count: 0,
      warning_count: 0,
      evaluated_artifact_id: "artifact-fixture-a",
    };
  }

  it("loads the gate from the dedicated endpoint and shows approval", async () => {
    const loadQualityGate = vi.fn(() => Promise.resolve(gate()));
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadQualityGate,
        loadTerraformPlan: () => Promise.reject(new Error("no plan")),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(loadQualityGate).toHaveBeenCalledTimes(1);
    expect(loadQualityGate).toHaveBeenCalledWith("session-test");
    expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(
      true,
    );
    expect(wrapper.find('[data-testid="gate-outcome"]').text()).toContain(
      "approved_for_review",
    );
    expect(wrapper.find('[data-testid="approve-button"]').exists()).toBe(
      true,
    );
  });

  it("keeps approval hidden when the gate genuinely does not exist", async () => {
    const loadQualityGate = vi.fn(() =>
      Promise.reject(new Error("quality_gate_not_available")),
    );
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadQualityGate,
        loadTerraformPlan: () => Promise.reject(new Error("no plan")),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(loadQualityGate).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(
      false,
    );
  });

  it("does not refetch when the session already embeds the gate", async () => {
    const base = sessionForA();
    const embedded: SessionView = {
      ...base,
      stages: { ...base.stages, quality_gate: gate() },
    };
    const loadQualityGate = vi.fn(() => Promise.resolve(gate()));
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(embedded),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadQualityGate,
        loadTerraformPlan: () => Promise.reject(new Error("no plan")),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(loadQualityGate).not.toHaveBeenCalled();
    expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(
      true,
    );
  });

  it("does not fetch pending artifacts while the run is in flight", async () => {
    // Architecture exists but critic/gate are still being computed:
    // fetching now would 404-spam artifacts that cannot exist yet.
    // The session poll re-reads them once stored.
    const base = sessionForA();
    const running: SessionView = {
      ...base,
      stages: { ...base.stages, design_status: "running" },
    };
    const loadQualityGate = vi.fn(() => Promise.resolve(gate()));
    const loadCritique = vi.fn(() =>
      Promise.reject(new Error("should not be called")),
    );
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(running),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique,
        loadQualityGate,
        loadTerraformPlan: () => Promise.reject(new Error("no plan")),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(loadQualityGate).not.toHaveBeenCalled();
    expect(loadCritique).not.toHaveBeenCalled();
    expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(
      false,
    );
  });
});

describe("optimization recommendations lifecycle", () => {
  function blockedGate(artifactId: string): QualityGateView {
    return {
      outcome: "blocked",
      reasons: [
        "Cost ceiling breached: on_demand: $2,039.27/mo exceeds the " +
          "$50.00/mo ceiling.",
      ],
      critical_count: 0,
      warning_count: 0,
      evaluated_artifact_id: artifactId,
    };
  }

  function recommendationsFor(
    artifactId: string,
  ): OptimizationRecommendationsView {
    return {
      session_id: "session-test",
      derived_from_artifact_id: artifactId,
      recommendations: [
        {
          recommendation_id: "rec-test",
          derived_from_artifact_id: artifactId,
          finding_category: "cost_ceiling",
          finding_statement:
            "Priced totals exceed the stated monthly budget ceiling.",
          proposed_change: "component_swap",
          target_component: "Gateway",
          rationale: "A lower-cost alternative may fit the ceiling.",
          affects_requirement: "budget_constraints",
          requirement_change_note: null,
          evaluated: false,
          status: "unevaluated",
          disclaimer:
            "This recommendation has not been priced, composed, or gated.",
        },
      ],
    };
  }

  function sessionWithGate(outcome: QualityGateView): SessionView {
    const base = sessionForA();
    return {
      ...base,
      session_id: "session-test",
      stages: { ...base.stages, quality_gate: outcome },
    };
  }

  function mountWith(
    session: SessionView,
    loadOptimizationRecommendations: () => Promise<OptimizationRecommendationsView>,
  ): ReturnType<typeof mount> {
    return mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(session),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadOptimizationRecommendations,
        loadTerraformPlan: () => Promise.reject(new Error("no plan")),
      },
      attachTo: document.body,
    });
  }

  const panel = '[data-testid="optimization-recommendations"]';

  it("fetches and shows recommendations for a blocked gate", async () => {
    const loadOptimizationRecommendations = vi.fn(() =>
      Promise.resolve(recommendationsFor("artifact-fixture-a")),
    );
    const wrapper = mountWith(
      sessionWithGate(blockedGate("artifact-fixture-a")),
      loadOptimizationRecommendations,
    );
    await flushPromises();

    expect(loadOptimizationRecommendations).toHaveBeenCalledTimes(1);
    expect(loadOptimizationRecommendations).toHaveBeenCalledWith(
      "session-test",
    );
    expect(wrapper.find(panel).exists()).toBe(true);
    expect(wrapper.find(panel).text()).toContain("cost_ceiling");
  });

  it("does not fetch or show recommendations when the gate is not blocked", async () => {
    const approved: QualityGateView = {
      outcome: "approved_for_review",
      reasons: ["No unresolved critical findings (0 warnings)."],
      critical_count: 0,
      warning_count: 0,
      evaluated_artifact_id: "artifact-fixture-a",
    };
    const loadOptimizationRecommendations = vi.fn(() =>
      Promise.resolve(recommendationsFor("artifact-fixture-a")),
    );
    const wrapper = mountWith(
      sessionWithGate(approved),
      loadOptimizationRecommendations,
    );
    await flushPromises();

    expect(loadOptimizationRecommendations).not.toHaveBeenCalled();
    expect(wrapper.find(panel).exists()).toBe(false);
  });

  it("hides recommendations generated for a different artifact", async () => {
    const loadOptimizationRecommendations = vi.fn(() =>
      Promise.resolve(recommendationsFor("artifact-other")),
    );
    const wrapper = mountWith(
      sessionWithGate(blockedGate("artifact-fixture-a")),
      loadOptimizationRecommendations,
    );
    await flushPromises();

    expect(loadOptimizationRecommendations).toHaveBeenCalledTimes(1);
    expect(wrapper.find(panel).exists()).toBe(false);
  });
});
