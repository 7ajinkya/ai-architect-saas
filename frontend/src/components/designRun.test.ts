/**
 * Slice 3.5 frontend tests: starting a design run and polling its state.
 *
 * All fixtures are generic; no test references a workload. The run flow
 * is driven purely through injected functions, so the assertions are
 * about the UI contract (submit, running, clarification, error, poll)
 * rather than about any particular system being designed.
 */

import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StartDesignRun from "./StartDesignRun.vue";
import Workspace from "./Workspace.vue";
import {
  FIXTURE_A,
  sessionForA,
  sessionWithoutDesignRun,
} from "../fixtures/fixtures";
import type {
  ArchitectureResult,
  OptimizationRecommendationsView,
  QualityGateView,
  SessionSummary,
  SessionView,
} from "../types";

function summaries(): SessionSummary[] {
  return [
    {
      session_id: "design-1",
      user_request: "design a generic ingestion pipeline",
      requirements_status: "ready",
      design_status: "failed",
      has_design_run: true,
      has_error: true,
    },
    {
      session_id: "req-2",
      user_request: "design something not yet run",
      requirements_status: "needs_clarification",
      design_status: "",
      has_design_run: false,
      has_error: false,
    },
  ];
}

function session(overrides: Partial<SessionView> = {}): SessionView {
  return { ...sessionWithoutDesignRun(), ...overrides };
}

function readySession(): SessionView {
  return session({
    session_id: "req-1",
    status: "ready",
    user_request: "a generic ingestion pipeline",
  });
}

function needsClarification(): SessionView {
  return session({
    session_id: "req-1",
    status: "needs_clarification",
    user_request: "a generic ingestion pipeline",
    pending_clarification: {
      requirement_key: "region",
      requirement: "geographic deployment region",
      question: "Which region should this deploy to?",
      reason: "Region changes cost and latency.",
      impact: "architecture",
      assumable: false,
    },
  });
}

function apiError(code: string, message: string) {
  return Object.assign(new Error(message), { code, status: 409 });
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("StartDesignRun", () => {
  it("submits a requirement and reports the design session id", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: vi.fn(() => Promise.resolve(readySession())),
        runDesign: vi.fn(() => Promise.resolve({ session_id: "design-1" })),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue(
      "a generic ingestion pipeline",
    );
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    expect(wrapper.emitted("started")?.[0]).toEqual(["design-1"]);
  });

  it("presents a product landing with the input as the primary action", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: vi.fn(() => Promise.resolve(readySession())),
        runDesign: vi.fn(() => Promise.resolve({ session_id: "design-1" })),
      },
    });

    expect(wrapper.find(".landing__eyebrow").text()).toContain("AI Architect");
    expect(wrapper.find(".landing__title").exists()).toBe(true);
    const input = wrapper.find('[data-testid="requirement-input"]');
    expect(input.element.tagName).toBe("TEXTAREA");
    expect(wrapper.find('[data-testid="start-run"]').text()).toContain(
      "Start designing",
    );
  });

  it("passes the user's own text through unchanged", async () => {
    const createSession = vi.fn(() => Promise.resolve(readySession()));
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession,
        runDesign: () => Promise.resolve({ session_id: "design-1" }),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue(
      "something entirely arbitrary",
    );
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    expect(createSession).toHaveBeenCalledWith("something entirely arbitrary");
  });

  it("opens the requirements session in its workspace when clarification is pending", async () => {
    const runDesign = vi.fn(() => Promise.resolve({ session_id: "design-2" }));
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: () => Promise.resolve(needsClarification()),
        refreshSession: () => Promise.resolve(needsClarification()),
        answerClarification: () => Promise.resolve(readySession()),
        runDesign,
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue("a pipeline");
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    // Landing never renders the question: the workspace owns it.
    expect(wrapper.find('[data-testid="start-clarification"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("Which region should this deploy to?");
    expect(wrapper.emitted("started")?.[0]).toEqual(["req-1"]);
    expect(runDesign).not.toHaveBeenCalled();
  });

  it("opens the requirements session when the run is refused with clarification_required", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: () => Promise.resolve(readySession()),
        refreshSession: () => Promise.resolve(needsClarification()),
        answerClarification: () => Promise.resolve(readySession()),
        runDesign: vi
          .fn()
          .mockRejectedValueOnce(apiError("clarification_required", "not ready")),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue("a pipeline");
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    expect(wrapper.find('[data-testid="start-clarification"]').exists()).toBe(false);
    expect(wrapper.emitted("started")?.[0]).toEqual(["req-1"]);
  });

  it("landing never leaks session clarification content (regression)", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: () => Promise.resolve(needsClarification()),
        runDesign: vi.fn(() => Promise.resolve({ session_id: "design-x" })),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue("a pipeline");
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    // Even with a pending question on the created session, the landing
    // shows only hero + input + browser chrome — never the question.
    expect(wrapper.find('[data-testid="start-clarification"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="answer-input"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="recent-designs"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="requirement-input"]').exists()).toBe(true);
  });

  it("shows an error when session creation fails", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: () => Promise.reject(new Error("requirements extraction failed")),
        runDesign: () => Promise.resolve({ session_id: "design-1" }),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue("a pipeline");
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    const alert = wrapper.find('[role="alert"]');
    expect(alert.text()).toContain("Could not start the design run.");
    expect(alert.text()).toContain("requirements extraction failed");
    expect(wrapper.emitted("started")).toBeUndefined();
  });

  it("shows an error when starting the run fails for another reason", async () => {
    const wrapper = mount(StartDesignRun, {
      props: {
        createSession: () => Promise.resolve(readySession()),
        runDesign: () => Promise.reject(new Error("model gate refused")),
      },
    });

    await wrapper.find('[data-testid="requirement-input"]').setValue("a pipeline");
    await wrapper.find('[data-testid="start-run"]').trigger("submit");
    await flushPromises();

    expect(wrapper.find('[role="alert"]').text()).toContain("model gate refused");
  });
});

describe("Workspace polling while a run is in flight", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function runningSession(): SessionView {
    return session({
      session_id: "design-9",
      status: "ready",
      stages: {
        ...sessionWithoutDesignRun().stages,
        has_design_run: true,
        design_status: "running",
      },
    });
  }

  function completedSession(): SessionView {
    const done = sessionForA();
    return { ...done, session_id: "design-9" };
  }

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
      session_id: "design-9",
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

  function rerunInFlightSession(): SessionView {
    // A rerun is under way and the previous run's BLOCKED gate and
    // architecture summary are still in the snapshot.
    const base = sessionForA();
    return {
      ...base,
      session_id: "design-9",
      stages: {
        ...base.stages,
        has_design_run: true,
        design_status: "running",
        quality_gate: blockedGate("artifact-fixture-a"),
      },
    };
  }

  function rerunBlockedSession(): SessionView {
    const base = sessionForA();
    return {
      ...base,
      session_id: "design-9",
      stages: {
        ...base.stages,
        has_design_run: true,
        design_status: "ready",
        quality_gate: blockedGate("artifact-fixture-a"),
      },
    };
  }

  it("polls until the run settles, then renders the architecture", async () => {
    const loadSession = vi
      .fn()
      .mockResolvedValueOnce(runningSession())
      .mockResolvedValue(completedSession());
    const loadArchitecture = vi.fn(() => Promise.resolve(FIXTURE_A));

    const wrapper = mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: loadArchitecture as () => Promise<ArchitectureResult>,
      },
    });
    await flushPromises();

    // Running state is shown and nothing is fetched yet.
    expect(wrapper.find('[data-testid="status-design"]').text()).toContain("running");
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(false);
    expect(loadArchitecture).not.toHaveBeenCalled();

    // First poll tick.
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(loadSession).toHaveBeenCalledTimes(2);
    expect(loadArchitecture).toHaveBeenCalled();
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="status-design"]').text()).toContain("ready");
    expect(wrapper.findAll(".vue-flow__node").length).toBe(3);
  });

  it("stops polling once the run is no longer running", async () => {
    const loadSession = vi.fn().mockResolvedValue(completedSession());
    mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
    });
    await flushPromises();

    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();

    expect(loadSession).toHaveBeenCalledTimes(1);
  });

  it("surfaces a failed run without polling forever", async () => {
    const failed = session({
      session_id: "design-9",
      status: "ready",
      stages: {
        ...sessionWithoutDesignRun().stages,
        has_design_run: true,
        design_status: "failed",
        design_error: "composer stage failed",
      },
    });
    const loadSession = vi.fn().mockResolvedValue(failed);

    const wrapper = mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
    });
    await flushPromises();

    expect(wrapper.find('[data-testid="status-design"]').text()).toContain("failed");
    expect(wrapper.find('[data-testid="status-error"]').text()).toContain(
      "composer stage failed",
    );

    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(loadSession).toHaveBeenCalledTimes(1);
  });

  it("keeps polling past READY until the architecture exists", async () => {
    /**
     * Regression: the design graph reports READY at the selection and
     * cost stages, before the composer stores an architecture. A client
     * that stopped polling on READY would never load the artifact.
     */
    const readyWithoutArchitecture = session({
      session_id: "design-9",
      status: "ready",
      stages: {
        ...sessionWithoutDesignRun().stages,
        has_design_run: true,
        design_status: "ready",
        cost: {
          totals_usd_per_month: { on_demand: 4.5 },
          priced: [],
          unpriced: [{ component_name: "thing", reason: "no price" }],
          coverage: [],
          warnings: [],
          assumptions: [],
          pricing_as_of: null,
          fully_priced: false,
          completeness: "partially_unresolved",
          best_estimate_total_usd_per_month: null,
          estimated_range_usd_per_month: null,
          unresolved_dimension_count: 0,
        },
        architecture: null,
      },
    });

    const loadSession = vi
      .fn()
      .mockResolvedValueOnce(runningSession())
      .mockResolvedValueOnce(readyWithoutArchitecture)
      .mockResolvedValue(completedSession());
    const loadArchitecture = vi.fn(() => Promise.resolve(FIXTURE_A));

    const wrapper = mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: loadArchitecture as () => Promise<ArchitectureResult>,
      },
    });
    await flushPromises();

    // Poll past the READY-without-architecture state.
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    expect(wrapper.find('[data-testid="status-design"]').text()).toContain("ready");

    // Keep ticking until the artifact appears, then load it.
    await vi.advanceTimersByTimeAsync(4000);
    await flushPromises();
    expect(loadArchitecture).toHaveBeenCalled();
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
    expect(wrapper.findAll(".vue-flow__node").length).toBe(3);

    // And it settles: no further polling.
    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(loadSession.mock.calls.length).toBe(3);
  });

  it("does not fetch recommendations while a rerun is in flight", async () => {
    const loadSession = vi.fn().mockResolvedValue(rerunInFlightSession());
    const loadOptimizationRecommendations = vi.fn(() =>
      Promise.resolve(recommendationsFor("artifact-fixture-a")),
    );

    const wrapper = mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadOptimizationRecommendations,
      },
    });
    await flushPromises();

    // The previous run's BLOCKED gate is still served mid-rerun: it
    // must not trigger a fetch, and no panel may show.
    expect(loadOptimizationRecommendations).not.toHaveBeenCalled();
    expect(
      wrapper.find('[data-testid="optimization-recommendations"]').exists(),
    ).toBe(false);
  });

  it("fetches recommendations once a rerun completes with a blocked artifact", async () => {
    const loadSession = vi
      .fn()
      .mockResolvedValueOnce(rerunInFlightSession())
      .mockResolvedValue(rerunBlockedSession());
    const loadOptimizationRecommendations = vi.fn(() =>
      Promise.resolve(recommendationsFor("artifact-fixture-a")),
    );

    const wrapper = mount(Workspace, {
      props: {
        sessionId: "design-9",
        loadSession,
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadOptimizationRecommendations,
      },
    });
    await flushPromises();

    expect(loadOptimizationRecommendations).not.toHaveBeenCalled();

    // The run completes with a BLOCKED verdict: recommend fresh.
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();

    expect(loadOptimizationRecommendations).toHaveBeenCalledTimes(1);
    expect(loadOptimizationRecommendations).toHaveBeenCalledWith("design-9");
    const panel = wrapper.find('[data-testid="optimization-recommendations"]');
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("cost_ceiling");
  });
});

describe("Recent designs", () => {
  function mountLanding(
    listSessions?: (
      query?: string,
      limit?: number,
      offset?: number,
    ) => Promise<SessionSummary[]>,
  ) {
    return mount(StartDesignRun, {
      props: {
        createSession: vi.fn(() => Promise.resolve(readySession())),
        runDesign: vi.fn(() => Promise.resolve({ session_id: "design-1" })),
        ...(listSessions ? { listSessions } : {}),
      },
    });
  }

  it("lists recent designs by request text with a status, not the UUID", async () => {
    const wrapper = mountLanding(() => Promise.resolve(summaries()));
    await flushPromises();

    const section = wrapper.find('[data-testid="recent-designs"]');
    expect(section.exists()).toBe(true);
    const cards = wrapper.findAll('[data-testid="recent-design"]');
    expect(cards.length).toBe(2);
    expect(cards[0].text()).toContain("design a generic ingestion pipeline");
    expect(cards[0].text()).toContain("Failed");
    expect(cards[1].text()).toContain("Needs input");
    expect(cards[0].text()).not.toContain("design-1");
  });

  it("opens a recent design through the existing started flow", async () => {
    const wrapper = mountLanding(() => Promise.resolve(summaries()));
    await flushPromises();

    await wrapper
      .find('[data-testid="open-recent-design-1"]')
      .trigger("click");
    await flushPromises();

    expect(wrapper.emitted("started")?.[0]).toEqual(["design-1"]);
  });

  it("shows an empty state instead of hiding the browser", async () => {
    const wrapper = mountLanding(() => Promise.resolve([]));
    await flushPromises();

    expect(wrapper.find('[data-testid="recent-designs"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="recent-empty"]').text()).toContain(
      "No designs found",
    );
  });

  it("tolerates a listing failure without breaking the landing", async () => {
    const wrapper = mountLanding(() => Promise.reject(new Error("down")));
    await flushPromises();

    expect(wrapper.find('[data-testid="recent-designs"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="requirement-input"]').exists()).toBe(
      true,
    );
  });

  it("searches by request text and session id", async () => {
    const listSessions = vi.fn(
      (query?: string, limit?: number, offset?: number) => {
        void limit;
        void offset;
        const all = summaries();
        if (!query) return Promise.resolve(all);
        return Promise.resolve(
          all.filter(
            (s) =>
              s.user_request.toLowerCase().includes(query.toLowerCase()) ||
              s.session_id.toLowerCase().includes(query.toLowerCase()),
          ),
        );
      },
    );
    const wrapper = mountLanding(listSessions);
    await flushPromises();
    expect(listSessions).toHaveBeenCalledWith(undefined, 10, 0);

    await wrapper.find('[data-testid="recent-search"]').setValue("ingestion");
    await flushPromises();

    expect(listSessions).toHaveBeenCalledWith("ingestion", 10, 0);
  });

  it("pages with a bounded load-more", async () => {
    const page1 = summaries();
    const page2 = [
      {
        session_id: "design-3",
        user_request: "third design",
        requirements_status: "ready",
        design_status: "",
        has_design_run: false,
        has_error: false,
      },
    ];
    const listSessions = vi.fn(
      (_query?: string, _limit?: number, offset?: number) =>
        Promise.resolve(offset === 0 ? [...page1, ...page1, ...page1, ...page1, ...page1].slice(0, 10) : page2),
    );
    const wrapper = mountLanding(listSessions);
    await flushPromises();

    expect(wrapper.find('[data-testid="recent-load-more"]').exists()).toBe(true);
    await wrapper.find('[data-testid="recent-load-more"]').trigger("click");
    await flushPromises();

    expect(listSessions).toHaveBeenCalledWith(undefined, 10, 10);
    expect(wrapper.findAll('[data-testid="recent-design"]').length).toBeGreaterThan(10);
  });
});
