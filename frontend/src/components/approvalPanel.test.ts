/**
 * ApprovalPanel tests: gate verdict, findings, and the approval action.
 *
 * Everything renders from served fields only; approval posts through
 * the injected function so no test touches the network.
 */
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ApprovalPanel from "./ApprovalPanel.vue";
import { sessionForA } from "../fixtures/fixtures";
import type { CritiqueResult, SessionView } from "../types";

beforeEach(() => {
  document.body.innerHTML = "";
});

function critique(): CritiqueResult {
  return {
    artifact_id: "critique-1",
    session_id: "session-fixture-a",
    reviewed_artifact_id: "artifact-fixture-a",
    reviewed_version: 1,
    findings: [
      {
        category: "spof",
        severity: "critical",
        statement: "Single entry with no failover",
        rationale: "Entry failure takes the system down",
        about_component_ref: "Edge Gateway",
        evidence_refs: [],
        recommendation: "Add a second entry",
        suggested_route: "composer_revision",
      },
    ],
    summary: "1 critical, 0 warnings, 0 informational",
  };
}

function sessionWithGate(outcome: string): SessionView {
  const base = sessionForA();
  return {
    ...base,
    stages: {
      ...base.stages,
      quality_gate: {
        outcome,
        reasons: ["No unresolved critical findings (0 warnings)."],
        critical_count: outcome === "approved_for_review" ? 0 : 1,
        warning_count: 0,
        evaluated_artifact_id: "artifact-fixture-a",
      },
      design_approval: null,
    },
  };
}

describe("ApprovalPanel", () => {
  it("stays hidden when the session has no gate verdict", () => {
    const wrapper = mount(ApprovalPanel, {
      props: { session: sessionForA(), critique: critique() },
    });
    expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(
      false,
    );
  });

  it("shows the gate verdict and critic findings", async () => {
    const wrapper = mount(ApprovalPanel, {
      props: { session: sessionWithGate("approved_for_review"), critique: critique() },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="gate-outcome"]').text()).toContain(
      "approved_for_review",
    );
    expect(wrapper.find('[data-testid="critique-findings"]').text()).toContain(
      "Single entry with no failover",
    );
  });

  it("records approval with an optional note", async () => {
    const onApprove = vi.fn(() =>
      Promise.resolve({
        approved: true,
        note: "accept",
        artifact_id: "artifact-fixture-a",
        gate_outcome: "approved_for_review",
        recorded_at: "2026-01-01T00:00:00Z",
      }),
    );
    const wrapper = mount(ApprovalPanel, {
      props: {
        session: sessionWithGate("approved_for_review"),
        critique: critique(),
        onApprove,
      },
      attachTo: document.body,
    });
    await flushPromises();
    await wrapper.find('[data-testid="approve-note"]').setValue("accept");
    await wrapper.find('[data-testid="approve-button"]').trigger("click");
    await flushPromises();
    expect(onApprove).toHaveBeenCalledWith(
      "session-fixture-a",
      true,
      "accept",
    );
    expect(wrapper.emitted("approved")?.[0][0]).toMatchObject({
      approved: true,
    });
  });

  it("offers no approval action when the gate blocks", async () => {
    const wrapper = mount(ApprovalPanel, {
      props: { session: sessionWithGate("blocked"), critique: critique() },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="approve-button"]').exists()).toBe(
      false,
    );
  });

  it("shows a recorded approval instead of the form", async () => {
    const base = sessionWithGate("approved_for_review");
    const wrapper = mount(ApprovalPanel, {
      props: {
        session: {
          ...base,
          stages: {
            ...base.stages,
            design_approval: {
              approved: true,
              note: "ship it",
              artifact_id: "artifact-fixture-a",
              gate_outcome: "approved_for_review",
              recorded_at: "2026-01-01T00:00:00Z",
            },
          },
        },
        critique: critique(),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="approval-record"]').text()).toContain(
      "Approved",
    );
    expect(wrapper.find('[data-testid="approve-button"]').exists()).toBe(
      false,
    );
  });
});
