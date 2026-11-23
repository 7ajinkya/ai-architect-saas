/**
 * StageProgress tests: per-stage pipeline visibility.
 *
 * All states are derived from served backend fields only
 * (DesignStages artifact presence, design_status, requirements status,
 * pending clarification). No timers, no invented progress.
 */

import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import StageProgress from "./StageProgress.vue";
import type {
  ArchitectureSummary,
  CapacityModelingResult,
  ComponentSelectionResult,
  CostSummary,
  DesignStages,
} from "../types";

function present<T>(): T {
  return "present" as unknown as T;
}

function stages(overrides: Partial<DesignStages> = {}): DesignStages {
  return {
    has_design_run: false,
    design_status: "",
    design_error: null,
    capacity: null,
    selection: null,
    architecture: null,
    cost: null,
    ...overrides,
  };
}

function stateOf(
  wrapper: ReturnType<typeof mount>,
  key: string,
): string | undefined {
  return wrapper.find(`[data-testid="stage-${key}"]`).attributes("data-state");
}

function mountProgress(
  stageOverrides: Partial<DesignStages> = {},
  requirementsStatus = "",
  hasPendingClarification = false,
) {
  return mount(StageProgress, {
    props: {
      stages: stages(stageOverrides),
      requirementsStatus,
      hasPendingClarification,
    },
  });
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("StageProgress", () => {
  it("shows all stages not started when nothing ran", () => {
    const wrapper = mountProgress();
    for (const key of [
      "requirements",
      "capacity",
      "selection",
      "cost",
      "architecture",
    ]) {
      expect(stateOf(wrapper, key)).toBe("not-started");
    }
  });

  it("shows requirements complete once ready", () => {
    const wrapper = mountProgress({}, "ready");
    expect(stateOf(wrapper, "requirements")).toBe("complete");
    expect(stateOf(wrapper, "capacity")).toBe("not-started");
  });

  it("shows requirements waiting while a clarification is outstanding", () => {
    const wrapper = mountProgress({}, "needs_clarification", true);
    expect(stateOf(wrapper, "requirements")).toBe("waiting");
    expect(stateOf(wrapper, "capacity")).toBe("not-started");
  });

  it("shows capacity complete once present", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "requirements")).toBe("complete");
    expect(stateOf(wrapper, "capacity")).toBe("complete");
    expect(stateOf(wrapper, "selection")).toBe("in-progress");
    expect(stateOf(wrapper, "cost")).toBe("waiting");
    expect(stateOf(wrapper, "architecture")).toBe("waiting");
  });

  it("shows selection complete once present", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "selection")).toBe("complete");
    expect(stateOf(wrapper, "cost")).toBe("in-progress");
    expect(stateOf(wrapper, "architecture")).toBe("waiting");
  });

  it("shows cost complete once present", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
        cost: present<CostSummary>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "cost")).toBe("complete");
    expect(stateOf(wrapper, "architecture")).toBe("in-progress");
  });

  it("shows every stage complete when the architecture exists", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "ready",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
        cost: present<CostSummary>(),
        architecture: present<ArchitectureSummary>(),
      },
      "ready",
    );
    for (const key of [
      "requirements",
      "capacity",
      "selection",
      "cost",
      "architecture",
    ]) {
      expect(stateOf(wrapper, key)).toBe("complete");
    }
  });

  it("marks the stopped stage failed without touching completed ones", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "failed",
        design_error: "Cost estimation stage failed: empty region",
        capacity: present<CapacityModelingResult>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "requirements")).toBe("complete");
    expect(stateOf(wrapper, "capacity")).toBe("complete");
    expect(stateOf(wrapper, "selection")).toBe("failed");
    expect(stateOf(wrapper, "cost")).toBe("not-started");
    expect(stateOf(wrapper, "architecture")).toBe("not-started");
  });

  it("marks the earliest missing stage failed when nothing completed", () => {
    const wrapper = mountProgress(
      { has_design_run: true, design_status: "failed" },
      "ready",
    );
    expect(stateOf(wrapper, "capacity")).toBe("failed");
    expect(stateOf(wrapper, "selection")).toBe("not-started");
  });

  it("renders incomplete payloads honestly instead of inventing progress", () => {
    // A cost artifact without capacity/selection cannot happen from the
    // sequential backend, but the UI must still render it without
    // crashing or claiming the missing stages completed.
    const wrapper = mountProgress(
      { cost: present<CostSummary>() },
      "ready",
    );
    expect(stateOf(wrapper, "capacity")).toBe("not-started");
    expect(stateOf(wrapper, "selection")).toBe("not-started");
    expect(stateOf(wrapper, "cost")).toBe("complete");
    expect(stateOf(wrapper, "architecture")).toBe("not-started");
  });

  it("handles a null stages payload without crashing", () => {
    const wrapper = mount(StageProgress, {
      props: {
        stages: null,
        requirementsStatus: "",
        hasPendingClarification: false,
      },
    });
    expect(stateOf(wrapper, "requirements")).toBe("not-started");
    expect(stateOf(wrapper, "architecture")).toBe("not-started");
  });

  it("marks the active stage as current without faking progress", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
      },
      "ready",
    );
    const active = wrapper.find('[data-testid="stage-selection"]');
    expect(active.attributes("data-state")).toBe("in-progress");
    expect(active.attributes("aria-current")).toBe("step");
    // Human "Working…" label, while the machine state stays truthful.
    expect(active.find(".pipeline__state").text()).toBe("Working…");
    expect(active.text()).not.toMatch(/\d+%/);
  });

  it("keeps completed stages visually successful while one is active", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
      },
      "ready",
    );
    const done = wrapper.find('[data-testid="stage-capacity"]');
    expect(done.attributes("data-state")).toBe("complete");
    expect(done.attributes("aria-current")).toBeUndefined();
    expect(done.find(".pipeline__badge").text()).toBe("✓");
  });

  it("shows critic and quality gate as first-class stages", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "running",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
        cost: present<CostSummary>(),
        architecture: present<ArchitectureSummary>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "architecture")).toBe("complete");
    expect(stateOf(wrapper, "critic")).toBe("in-progress");
    expect(stateOf(wrapper, "quality-gate")).toBe("waiting");
  });

  it("completes critic and gate from their stored summaries", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "ready",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
        cost: present<CostSummary>(),
        architecture: present<ArchitectureSummary>(),
        critique: present<import("../types").CritiqueSummary>(),
        quality_gate: present<import("../types").QualityGateView>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "critic")).toBe("complete");
    expect(stateOf(wrapper, "quality-gate")).toBe("complete");
  });

  it("marks critic failed when the run stops there", () => {
    const wrapper = mountProgress(
      {
        has_design_run: true,
        design_status: "failed",
        design_error: "Design critic stage failed: boom",
        capacity: present<CapacityModelingResult>(),
        selection: present<ComponentSelectionResult>(),
        cost: present<CostSummary>(),
        architecture: present<ArchitectureSummary>(),
      },
      "ready",
    );
    expect(stateOf(wrapper, "architecture")).toBe("complete");
    expect(stateOf(wrapper, "critic")).toBe("failed");
    expect(stateOf(wrapper, "quality-gate")).toBe("not-started");
  });

  it("brings the active stage into view without timers", async () => {
    const scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
    try {
      const wrapper = mountProgress(
        {
          has_design_run: true,
          design_status: "running",
          capacity: present<CapacityModelingResult>(),
        },
        "ready",
      );
      await wrapper.vm.$nextTick();
      await wrapper.vm.$nextTick();
      expect(scrollIntoView).toHaveBeenCalled();
      const last = scrollIntoView.mock.calls.at(-1)?.[0] as
        | ScrollIntoViewOptions
        | undefined;
      expect(last?.inline).toBe("nearest");
    } finally {
      delete (
        window.HTMLElement.prototype as { scrollIntoView?: unknown }
      ).scrollIntoView;
    }
  });
});
