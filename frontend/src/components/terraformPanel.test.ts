/**
 * TerraformPanel tests: read-only plan display.
 *
 * Everything renders from the served plan artifact only; the panel
 * generates, approves, and provisions nothing.
 */
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import TerraformPanel from "./TerraformPanel.vue";
import Workspace from "./Workspace.vue";
import { sessionForA } from "../fixtures/fixtures";
import type { SessionView, TerraformPlanView } from "../types";
import { FIXTURE_A } from "../fixtures/fixtures";

beforeEach(() => {
  document.body.innerHTML = "";
});

function plan(): TerraformPlanView {
  return {
    status: "incomplete",
    artifact_id: "artifact-fixture-a",
    spec_id: "spec-1",
    tree_hash: "tree-1",
    provider: "aws",
    region_code: "us-east-1",
    resources: [
      {
        resource_id: "s3-records",
        node_id: "records",
        kind: "s3",
        component_name: "Primary Record Store",
        module: {
          instance_id: "object_storage-s3-records",
          module_id: "object_storage",
          version: "1.0.0",
        },
        pending_inputs: [
          { name: "bucket_name", reason: "needs a human decision" },
        ],
      },
      {
        resource_id: "lambda-worker",
        node_id: "worker",
        kind: "lambda",
        component_name: "Stateless Worker",
        module: null,
        pending_inputs: [],
      },
    ],
    modules: [
      {
        instance_id: "object_storage-s3-records",
        module_id: "object_storage",
        version: "1.0.0",
      },
    ],
    selection_errors: [],
    spec_diagnostics: [],
    render_status: "unrepresentable",
    render_diagnostics: [],
    files: { "main.tf": 'resource "x" "y" {}\nline2\n' },
    resource_types: ["aws_s3_bucket"],
    terraform_validation: {
      status: "not_installed",
      installed: false,
      version: null,
      exit_codes: {},
      diagnostics: [],
    },
    inventory: {
      analysis: "static_inventory",
      creates: 1,
      destroys: 0,
      uncovered: 1,
      by_kind: { s3: 1, lambda: 1 },
      note: "Greenfield static inventory, not a live plan.",
    },
    spec_warnings: [],
    artifacts: [],
    generated: [],
  };
}

describe("TerraformPanel", () => {
  it("stays hidden without a stored plan", () => {
    const wrapper = mount(TerraformPanel, {
      props: { plan: null },
    });
    expect(wrapper.find('[data-testid="terraform-panel"]').exists()).toBe(
      false,
    );
  });

  it("shows inventory, validation, pending inputs and gaps", () => {
    const wrapper = mount(TerraformPanel, {
      props: { plan: plan() },
      attachTo: document.body,
    });
    expect(wrapper.find('[data-testid="terraform-status"]').text()).toContain(
      "incomplete",
    );
    expect(
      wrapper.find('[data-testid="terraform-inventory"]').text(),
    ).toContain("1 to create");
    expect(
      wrapper.find('[data-testid="terraform-validation"]').text(),
    ).toContain("not_installed");
    expect(wrapper.find('[data-testid="terraform-pending"]').text()).toContain(
      "bucket_name",
    );
    expect(
      wrapper.find('[data-testid="terraform-uncovered"]').text(),
    ).toContain("Stateless Worker");
    expect(wrapper.find('[data-testid="terraform-files"]').text()).toContain(
      "main.tf",
    );
  });

  it("shows canonical examples for structured DynamoDB inputs", () => {
    const structured = plan();
    structured.resources[0].pending_inputs = [
      {
        name: "attribute_definitions",
        reason: "needs a human decision",
      },
      { name: "key_schema", reason: "needs a human decision" },
    ];
    const wrapper = mount(TerraformPanel, {
      props: { plan: structured },
      attachTo: document.body,
    });
    expect(
      wrapper
        .find(
          '[data-testid="terraform-input-s3-records-attribute_definitions"]',
        )
        .attributes("placeholder"),
    ).toContain("pk:S,sk:N");
    expect(
      wrapper
        .find('[data-testid="terraform-input-s3-records-key_schema"]')
        .attributes("placeholder"),
    ).toContain("HASH:pk,RANGE:sk");
  });

  it("shows plan diagnostics so incomplete plans explain themselves", () => {
    const failed = plan();
    failed.status = "incomplete";
    failed.files = {};
    failed.spec_diagnostics = [
      { category: "structural", message: "Resource x is missing input y." },
    ];
    failed.render_diagnostics = [
      { category: "unrepresentable", message: "No log group for access logs." },
    ];
    failed.selection_errors = ["intra-selection conflict"];
    const wrapper = mount(TerraformPanel, {
      props: { plan: failed },
      attachTo: document.body,
    });
    const diagnostics = wrapper.find(
      '[data-testid="terraform-diagnostics"]',
    );
    expect(diagnostics.exists()).toBe(true);
    expect(diagnostics.text()).toContain("No log group for access logs.");
    expect(diagnostics.text()).toContain("missing input");
  });

  it("explains a refused module selection instead of missing modules", () => {
    const refused = plan();
    refused.status = "incomplete";
    refused.files = {};
    refused.modules = [];
    refused.selection_errors = ["Selection has no selection_id."];
    refused.render_status = "unrepresentable";
    refused.render_diagnostics = [
      {
        category: "structural",
        message:
          "Module 'object_storage' targets provider 'aws', but this " +
          "design declares 'AWS'.",
      },
    ];
    refused.resources = refused.resources.map((resource) => ({
      ...resource,
      module: null,
      pending_inputs: [],
    }));
    const wrapper = mount(TerraformPanel, {
      props: { plan: refused },
      attachTo: document.body,
    });
    const section = wrapper.find(
      '[data-testid="terraform-selection-refused"]',
    );
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("all-or-nothing");
    expect(section.text()).toContain("affected by that refusal");
    expect(section.text()).not.toContain("no verified module");
    // The refusal points at the diagnostics, not at a catalog gap.
    expect(section.find("a").attributes("href")).toBe(
      "#terraform-diagnostics",
    );
    expect(wrapper.find("#terraform-diagnostics").exists()).toBe(true);
    expect(
      wrapper.find('[data-testid="terraform-diagnostics"]').text(),
    ).toContain("declares 'AWS'");
  });

  it("keeps the catalog-gap wording when selection was accepted", () => {
    const wrapper = mount(TerraformPanel, {
      props: { plan: plan() },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="terraform-selection-refused"]').exists(),
    ).toBe(false);
    expect(
      wrapper.find('[data-testid="terraform-uncovered"]').text(),
    ).toContain("no verified module");
  });

  it("distinguishes LLM-generated fallback from verified modules", () => {
    const withFallback = plan();
    withFallback.status = "valid";
    withFallback.generated = [
      {
        resource_id: "iac_fallback-web",
        node_id: "web",
        component_name: "AWS Amplify (Hosting)",
        resource_types: ["aws_amplify_app", "aws_amplify_branch"],
        proposal_sha256: "cd".repeat(32),
        status: "rendered",
        notes: [],
      },
      {
        resource_id: "iac_fallback-trace",
        node_id: "trace",
        component_name: "AWS X-Ray (Tracing)",
        resource_types: [],
        proposal_sha256: "",
        status: "failed",
        notes: ["LLM exploded"],
      },
    ];
    const wrapper = mount(TerraformPanel, {
      props: { plan: withFallback },
      attachTo: document.body,
    });
    const section = wrapper.find('[data-testid="terraform-generated"]');
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("AWS Amplify (Hosting)");
    expect(section.text()).toContain("aws_amplify_app");
    expect(section.text()).toContain("LLM exploded");
    expect(wrapper.text()).toContain("deterministically validated");
    expect(wrapper.text()).toContain("not verified modules");
  });

  it("hides the fallback section when everything is verified", () => {
    const wrapper = mount(TerraformPanel, {
      props: { plan: plan() },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="terraform-generated"]').exists(),
    ).toBe(false);
  });
});

describe("Terraform generation flow", () => {
  it("offers generation after approval while no plan exists", async () => {
    const generate = vi.fn(() => Promise.resolve(plan()));
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: null,
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: generate,
      },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="terraform-generator"]').exists(),
    ).toBe(true);
    expect(generate).not.toHaveBeenCalled();
    await wrapper.find('[data-testid="generate-plan"]').trigger("click");
    expect(generate).toHaveBeenCalledTimes(1);
    expect(generate).toHaveBeenCalledWith("s-1", {});
    await flushPromises();
    expect(wrapper.emitted("generated")).toHaveLength(1);
  });

  it("stays hidden before design approval", () => {
    const wrapper = mount(TerraformPanel, {
      props: { plan: null, sessionId: "s-1", designApproved: false },
    });
    expect(
      wrapper.find('[data-testid="terraform-generator"]').exists(),
    ).toBe(false);
    expect(wrapper.find('[data-testid="terraform-panel"]').exists()).toBe(
      false,
    );
  });

  it("submits typed inputs keyed by resource and name", async () => {
    const generate = vi.fn(() => Promise.resolve(plan()));
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: plan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: generate,
      },
      attachTo: document.body,
    });
    const input = wrapper.find(
      '[data-testid="terraform-input-s3-records-bucket_name"]',
    );
    expect(input.exists()).toBe(true);
    await input.setValue("my-design-bucket");
    await wrapper.find('[data-testid="submit-inputs"]').trigger("click");
    expect(generate).toHaveBeenCalledTimes(1);
    expect(generate).toHaveBeenCalledWith("s-1", {
      "s3-records": { bucket_name: "my-design-bucket" },
    });
    await flushPromises();
    expect(wrapper.emitted("generated")).toHaveLength(1);
  });

  it("surfaces generation failures without losing the form", async () => {
    const generate = vi.fn(() =>
      Promise.reject(new Error("terraform_not_allowed")),
    );
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: plan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: generate,
      },
      attachTo: document.body,
    });
    const input = wrapper.find(
      '[data-testid="terraform-input-s3-records-bucket_name"]',
    );
    await input.setValue("my-design-bucket");
    await wrapper.find('[data-testid="submit-inputs"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="terraform-error"]').text()).toContain(
      "terraform_not_allowed",
    );
    expect(wrapper.emitted("failed")).toHaveLength(1);
    // The typed draft survives the failure.
    expect(
      (
        wrapper.find(
          '[data-testid="terraform-input-s3-records-bucket_name"]',
        ).element as HTMLInputElement
      ).value,
    ).toBe("my-design-bucket");
  });

  it("regenerates an incomplete plan once no inputs are pending", async () => {
    const regenerate = vi.fn(() => Promise.resolve(plan()));
    const incomplete = plan();
    incomplete.status = "incomplete";
    incomplete.files = {};
    incomplete.resources = incomplete.resources.map((resource) => ({
      ...resource,
      pending_inputs: [],
    }));
    incomplete.spec_diagnostics = [
      { category: "structural", message: "Unsupported component." },
    ];
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: incomplete,
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: regenerate,
      },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="submit-inputs"]').exists(),
    ).toBe(false);
    const button = wrapper.find('[data-testid="regenerate-plan"]');
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(regenerate).toHaveBeenCalledWith("s-1", {});
    await flushPromises();
    expect(wrapper.emitted("generated")).toHaveLength(1);
    const result = wrapper.find(
      '[data-testid="terraform-generation-result"]',
    );
    expect(result.exists()).toBe(true);
    expect(result.text()).toContain("regenerated");
    expect(result.text()).toContain("still incomplete");
  });

  it("blocks regeneration for a deterministic fallback refusal", () => {
    const refused = plan();
    refused.status = "incomplete";
    refused.files = {};
    refused.resources = refused.resources.map((resource) => ({
      ...resource,
      pending_inputs: [],
    }));
    refused.render_diagnostics = [
      {
        category: "fallback_refused",
        message:
          "Amazon CloudWatch Metrics (embedded): outside the controlled " +
          "fallback policy.",
      },
    ];
    const generate = vi.fn();
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: refused,
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: generate,
      },
      attachTo: document.body,
    });
    expect(wrapper.find('[data-testid="regenerate-plan"]').exists()).toBe(
      false,
    );
    const blocked = wrapper.find(
      '[data-testid="terraform-regeneration-blocked"]',
    );
    expect(blocked.exists()).toBe(true);
    expect(blocked.text()).toContain("deterministically refuses");
    expect(blocked.text()).toContain("Start a new design run");
    expect(blocked.text()).toContain("Amazon CloudWatch Metrics");
    expect(generate).not.toHaveBeenCalled();
  });

  it("reports a successful regeneration when the plan becomes valid", async () => {
    const incomplete = plan();
    incomplete.status = "incomplete";
    incomplete.files = {};
    incomplete.resources = incomplete.resources.map((resource) => ({
      ...resource,
      pending_inputs: [],
    }));
    incomplete.spec_diagnostics = [
      { category: "structural", message: "Unsupported component." },
    ];
    const valid = plan();
    valid.status = "valid";
    valid.tree_hash = "tree-2";
    const regenerate = vi.fn(() => Promise.resolve(valid));
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: incomplete,
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: regenerate,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="regenerate-plan"]').trigger("click");
    await flushPromises();
    expect(
      wrapper.find('[data-testid="terraform-generation-result"]').text(),
    ).toContain("regenerated successfully");
    expect(wrapper.emitted("generated")).toHaveLength(1);
  });

  it("does not offer regeneration while pending inputs remain", () => {
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: plan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: vi.fn(),
      },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="regenerate-plan"]').exists(),
    ).toBe(false);
  });

  it("shows deterministic profile defaults with provenance", () => {
    const withDefaults = plan();
    withDefaults.status = "valid";
    withDefaults.input_defaults = [
      {
        resource_id: "dynamodb-table",
        name: "attribute_definitions",
        value: "pk:S",
        source: "deterministic_default",
        rationale: "Demo profile: one string partition key named pk.",
        requires_confirmation: true,
      },
    ];
    const wrapper = mount(TerraformPanel, {
      props: { plan: withDefaults },
      attachTo: document.body,
    });
    const section = wrapper.find(
      '[data-testid="terraform-input-defaults"]',
    );
    expect(section.exists()).toBe(true);
    expect(section.text()).toContain("attribute_definitions");
    expect(section.text()).toContain("pk:S");
    expect(section.text()).toContain("Demo profile");
  });

  it("shows discarded carried-forward inputs with their reasons", () => {
    const withWarnings = plan();
    withWarnings.status = "valid";
    withWarnings.input_warnings = [
      "Stored input 'retired' for 's3-records' is not a required input " +
        "for this plan and was discarded.",
    ];
    const wrapper = mount(TerraformPanel, {
      props: { plan: withWarnings },
      attachTo: document.body,
    });
    expect(
      wrapper.find('[data-testid="terraform-input-warnings"]').text(),
    ).toContain("retired");
  });
});

describe("TerraformPanel input suggestions", () => {
  function pendingPlan(): TerraformPlanView {
    const base = plan();
    base.resources = [
      {
        ...base.resources[0],
        pending_inputs: [
          { name: "bucket_name", reason: "needs a human decision" },
          { name: "versioning", reason: "needs a human decision" },
        ],
      },
    ];
    return base;
  }

  it("fills the fields from the suggestions and shows their provenance", async () => {
    const suggest = vi.fn(() =>
      Promise.resolve({
        suggestions: [
          {
            resource_id: "s3-records",
            name: "versioning",
            value: "enabled",
            rationale: "The request asks for recoverable uploads.",
            source: "ai_suggestion",
          },
        ],
        skipped: [
          {
            resource_id: "s3-records",
            name: "bucket_name",
            reason: "A bucket name is globally unique in S3.",
          },
        ],
        notes: [],
      }),
    );
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: pendingPlan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: vi.fn(() => Promise.resolve(pendingPlan())),
        suggestInputsFn: suggest,
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="suggest-inputs"]').trigger("click");
    await flushPromises();

    expect(suggest).toHaveBeenCalledWith("s-1");
    const versioning = wrapper.find(
      '[data-testid="terraform-input-s3-records-versioning"]',
    );
    expect((versioning.element as HTMLInputElement).value).toBe("enabled");
    // The human-only field stays empty for the operator.
    const bucket = wrapper.find(
      '[data-testid="terraform-input-s3-records-bucket_name"]',
    );
    expect((bucket.element as HTMLInputElement).value).toBe("");
    expect(
      wrapper.find('[data-testid="terraform-suggestion-list"]').text(),
    ).toContain("versioning");
    expect(
      wrapper.find('[data-testid="terraform-suggestion-skipped"]').text(),
    ).toContain("globally unique");
    expect(
      wrapper.find('[data-testid="terraform-suggested-s3-records-versioning"]')
        .exists(),
    ).toBe(true);
  });

  it("submits suggested values only after the operator submits", async () => {
    const generate = vi.fn(() => Promise.resolve(pendingPlan()));
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: pendingPlan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: generate,
        suggestInputsFn: vi.fn(() =>
          Promise.resolve({
            suggestions: [
              {
                resource_id: "s3-records",
                name: "versioning",
                value: "enabled",
                rationale: "r",
                source: "ai_suggestion",
              },
            ],
            skipped: [],
            notes: [],
          }),
        ),
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="suggest-inputs"]').trigger("click");
    await flushPromises();
    // Suggesting alone changes nothing.
    expect(generate).not.toHaveBeenCalled();

    await wrapper.find('[data-testid="submit-inputs"]').trigger("click");
    await flushPromises();
    expect(generate).toHaveBeenCalledWith("s-1", {
      "s3-records": { versioning: "enabled" },
    });
  });

  it("keeps the typed value when a suggestion arrives", async () => {
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: pendingPlan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: vi.fn(() => Promise.resolve(pendingPlan())),
        suggestInputsFn: vi.fn(() =>
          Promise.resolve({
            suggestions: [
              {
                resource_id: "s3-records",
                name: "bucket_name",
                value: "suggested-bucket",
                rationale: "r",
                source: "ai_suggestion",
              },
            ],
            skipped: [],
            notes: [],
          }),
        ),
      },
      attachTo: document.body,
    });
    const bucket = wrapper.find(
      '[data-testid="terraform-input-s3-records-bucket_name"]',
    );
    await bucket.setValue("my-own-bucket");
    await wrapper.find('[data-testid="suggest-inputs"]').trigger("click");
    await flushPromises();
    const after = wrapper.find(
      '[data-testid="terraform-input-s3-records-bucket_name"]',
    );
    expect((after.element as HTMLInputElement).value).toBe("suggested-bucket");
  });

  it("surfaces a suggestion failure without touching the form", async () => {
    const wrapper = mount(TerraformPanel, {
      props: {
        plan: pendingPlan(),
        sessionId: "s-1",
        designApproved: true,
        generatePlanFn: vi.fn(() => Promise.resolve(pendingPlan())),
        suggestInputsFn: vi.fn(() =>
          Promise.reject(new Error("input_suggestions_unavailable")),
        ),
      },
      attachTo: document.body,
    });
    await wrapper.find('[data-testid="suggest-inputs"]').trigger("click");
    await flushPromises();
    expect(
      wrapper.find('[data-testid="terraform-suggest-error"]').text(),
    ).toContain("input_suggestions_unavailable");
  });
});
describe("Workspace terraform wiring", () => {
  function approvedSession(): SessionView {
    const base = sessionForA();
    return {
      ...base,
      stages: {
        ...base.stages,
        design_approval: {
          approved: true,
          note: "",
          artifact_id: "artifact-fixture-a",
          gate_outcome: "approved_for_review",
          recorded_at: "2026-01-01T00:00:00Z",
        },
      },
    };
  }

  it("loads and shows the stored plan after approval", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(approvedSession()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadTerraformPlan: () => Promise.resolve(plan()),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="terraform-panel"]').exists()).toBe(
      true,
    );
    expect(wrapper.find('[data-testid="terraform-status"]').text()).toContain(
      "incomplete",
    );
  });

  it("stays hidden without an approval", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadTerraformPlan: () => Promise.resolve(plan()),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="terraform-panel"]').exists()).toBe(
      false,
    );
  });

  it("offers generation once approved while no plan is stored", async () => {
    const base = sessionForA();
    const approved: SessionView = {
      ...base,
      stages: {
        ...base.stages,
        design_approval: {
          approved: true,
          note: "",
          artifact_id: "artifact-fixture-a",
          gate_outcome: "approved_for_review",
          recorded_at: "2026-01-01T00:00:00Z",
        },
      },
    };
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-test",
        loadSession: () => Promise.resolve(approved),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        loadCritique: () => Promise.reject(new Error("no critique")),
        loadTerraformPlan: () =>
          Promise.reject(new Error("terraform_not_available")),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(
      wrapper.find('[data-testid="terraform-generator"]').exists(),
    ).toBe(true);
    expect(wrapper.find('[data-testid="terraform-panel"]').exists()).toBe(
      false,
    );
  });
});
