/**
 * ProvisionPanel tests: the single human-authorized mutation path.
 *
 * The panel renders served plan fields only and walks three explicit
 * steps — run the live plan review, authorize the reviewed tree, then
 * apply it — each through injected functions so no test touches the
 * network. Nothing executes before the review; destroy counts come
 * from the live review, never from an assumed zero; there is no
 * destroy action.
 */
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ProvisionPanel from "./ProvisionPanel.vue";
import type {
  ApprovalView,
  ProvisioningResultView,
  TerraformPlanView,
} from "../types";

beforeEach(() => {
  document.body.innerHTML = "";
});

function plan(overrides?: Partial<TerraformPlanView>): TerraformPlanView {
  return {
    status: "valid",
    artifact_id: "artifact-1",
    spec_id: "spec-1",
    tree_hash: "tree-1",
    provider: "aws",
    region_code: "ap-south-1",
    resources: [
      {
        resource_id: "lambda-fn",
        node_id: "fn",
        kind: "lambda",
        component_name: "Worker",
        module: {
          instance_id: "serverless_function-lambda-fn",
          module_id: "serverless_function",
          version: "1.0.0",
        },
        pending_inputs: [],
      },
    ],
    modules: [],
    selection_errors: [],
    spec_diagnostics: [],
    render_status: "rendered",
    render_diagnostics: [],
    files: {},
    resource_types: [],
    terraform_validation: {
      status: "validate_ok",
      installed: true,
      version: "Terraform v1.9.8",
      exit_codes: {},
      diagnostics: [],
    },
    inventory: {
      analysis: "static_inventory",
      creates: 10,
      destroys: null,
      uncovered: 0,
      by_kind: { lambda: 1 },
      note: "Static inventory, not a live plan.",
    },
    spec_warnings: ["IAM realized as supporting resources."],
    artifacts: [{ name: "demo-handler.zip", sha256: "ab".repeat(32) }],
    generated: [],
    ...overrides,
  };
}

function approval(): ApprovalView {
  return {
    approved: true,
    note: "",
    artifact_id: "artifact-1",
    gate_outcome: "",
    recorded_at: "2026-01-01T00:00:00Z",
  };
}

function planReview(
  destroy = 0,
  status = "plan_ok",
): ProvisioningResultView {
  return {
    status,
    mode: "plan",
    artifact_id: "artifact-1",
    tree_hash: "tree-1",
    exit_codes: { init: 0, plan: 0 },
    output: "Plan: 10 to add, 0 to change, 0 to destroy.",
    summary: { add: 10, change: 0, destroy },
    recorded_at: "2026-01-01T00:00:00Z",
  };
}

function appliedResult(): ProvisioningResultView {
  return {
    status: "apply_ok",
    mode: "apply",
    artifact_id: "artifact-1",
    tree_hash: "tree-1",
    exit_codes: { init: 0, apply: 0 },
    output: "Apply complete.",
    summary: null,
    recorded_at: "2026-01-01T00:00:00Z",
  };
}

describe("ProvisionPanel", () => {
  it("stays hidden without a stored plan", () => {
    const wrapper = mount(ProvisionPanel, {
      props: { sessionId: "s-1", plan: null },
    });
    expect(wrapper.find('[data-testid="provision-panel"]').exists()).toBe(
      false,
    );
  });

  it("shows static context without claiming anything about destroys", () => {
    const wrapper = mount(ProvisionPanel, {
      props: { sessionId: "s-1", plan: plan(), monthlyCost: 9.79 },
      attachTo: document.body,
    });
    const delta = wrapper.find('[data-testid="provision-delta"]').text();
    expect(delta).toContain("+10");
    expect(delta).toContain("live plan review");
    expect(wrapper.find('[data-testid="provision-region"]').text()).toContain(
      "ap-south-1",
    );
    expect(wrapper.find('[data-testid="provision-cost"]').text()).toContain(
      "$9.79/mo",
    );
    expect(
      wrapper.find('[data-testid="provision-warnings"]').text(),
    ).toContain("IAM realized");
    expect(
      wrapper.find('[data-testid="provision-resources"]').text(),
    ).toContain("lambda-fn");
    expect(
      wrapper.find('[data-testid="provision-destroy-warning"]').exists(),
    ).toBe(false);
  });

  it("reviews, authorizes, then applies — in that order", async () => {
    const approve = vi.fn(() => Promise.resolve(approval()));
    let calls = 0;
    const run = vi.fn(() => {
      calls += 1;
      return Promise.resolve(calls === 1 ? planReview() : appliedResult());
    });
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        approveProvisioningFn: approve,
        provisionFn: run,
      },
      attachTo: document.body,
    });
    expect(run).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();

    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith("s-1", "plan", "");
    expect(approve).not.toHaveBeenCalled();
    expect(
      wrapper.find('[data-testid="provision-review-status"]').text(),
    ).toContain("plan_ok");
    expect(
      wrapper.find('[data-testid="provision-review-summary"]').text(),
    ).toContain("+10");
    expect(wrapper.find('[data-testid="approve-provision"]').exists()).toBe(
      true,
    );
    expect(wrapper.find('[data-testid="apply-provision"]').exists()).toBe(
      false,
    );

    await wrapper.find('[data-testid="approve-provision"]').trigger("click");
    await flushPromises();
    expect(approve).toHaveBeenCalledTimes(1);
    expect(approve).toHaveBeenCalledWith("s-1", true, expect.any(String));
    expect(run).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-testid="apply-provision"]').exists()).toBe(
      true,
    );

    await wrapper.find('[data-testid="apply-provision"]').trigger("click");
    await flushPromises();
    expect(run).toHaveBeenCalledTimes(2);
    expect(run).toHaveBeenLastCalledWith("s-1", "apply", "APPLY");
    expect(wrapper.find('[data-testid="provision-status"]').text()).toContain(
      "apply_ok",
    );
    expect(wrapper.find('[data-testid="provision-checks"]').text()).toContain(
      "Terraform initialized",
    );
    expect(wrapper.emitted("provisioned")).toHaveLength(1);
  });

  it("warns with the live review's destroy count", async () => {
    const run = vi.fn(() => Promise.resolve(planReview(1)));
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        provisionFn: run,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    expect(
      wrapper.find('[data-testid="provision-destroy-warning"]').text(),
    ).toContain("destroys 1");
  });

  it("a failed review blocks authorization", async () => {
    const run = vi.fn(() => Promise.resolve(planReview(0, "plan_failed")));
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        provisionFn: run,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    expect(
      wrapper.find('[data-testid="provision-review-status"]').text(),
    ).toContain("plan_failed");
    expect(wrapper.find('[data-testid="approve-provision"]').exists()).toBe(
      false,
    );
  });

  it("stops before apply when authorization fails", async () => {
    const approve = vi.fn(() =>
      Promise.reject(new Error("approval_not_allowed")),
    );
    const run = vi.fn(() => Promise.resolve(planReview()));
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        approveProvisioningFn: approve,
        provisionFn: run,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    await wrapper.find('[data-testid="approve-provision"]').trigger("click");
    await flushPromises();
    expect(run).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-testid="apply-provision"]').exists()).toBe(
      false,
    );
    expect(wrapper.find('[data-testid="provision-error"]').text()).toContain(
      "approval_not_allowed",
    );
    expect(wrapper.emitted("failed")).toHaveLength(1);
  });

  it("disables the review button while the plan is not valid", () => {
    const wrapper = mount(ProvisionPanel, {
      props: { sessionId: "s-1", plan: plan({ status: "incomplete" }) },
      attachTo: document.body,
    });
    const button = wrapper.find('[data-testid="run-plan"]');
    expect((button.element as HTMLButtonElement).disabled).toBe(true);
    expect(wrapper.find('[data-testid="provision-blocked"]').exists()).toBe(
      true,
    );
  });

  it("resets review and authorization when the plan identity changes", async () => {
    const approve = vi.fn(() => Promise.resolve(approval()));
    const run = vi.fn(() => Promise.resolve(planReview()));
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        approveProvisioningFn: approve,
        provisionFn: run,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    await wrapper.find('[data-testid="approve-provision"]').trigger("click");
    await flushPromises();
    expect(
      wrapper.find('[data-testid="apply-provision"]').exists(),
    ).toBe(true);

    await wrapper.setProps({ plan: plan({ tree_hash: "tree-2" }) });
    await flushPromises();
    expect(
      wrapper.find('[data-testid="provision-review-status"]').exists(),
    ).toBe(false);
    expect(wrapper.find('[data-testid="approve-provision"]').exists()).toBe(
      false,
    );
    expect(wrapper.find('[data-testid="apply-provision"]').exists()).toBe(
      false,
    );
  });

  it("exposes no destroy action", async () => {
    const approve = vi.fn(() => Promise.resolve(approval()));
    const run = vi.fn(() => Promise.resolve(planReview()));
    const wrapper = mount(ProvisionPanel, {
      props: {
        sessionId: "s-1",
        plan: plan(),
        approveProvisioningFn: approve,
        provisionFn: run,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="run-plan"]').trigger("click");
    await flushPromises();
    await wrapper.find('[data-testid="approve-provision"]').trigger("click");
    await flushPromises();
    const texts = wrapper.findAll("button").map((button) => button.text());
    expect(texts.join(" ")).toContain("Apply approved plan");
    expect(
      texts.some((text) => text.toLowerCase().includes("destroy")),
    ).toBe(false);
  });
});
