/**
 * Workspace layout tests: resizable panes, whole-panel collapse,
 * architecture focus mode, horizontal pipeline.
 */
import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import Workspace from "./Workspace.vue";
import {
  FIXTURE_A,
  sessionForA,
  sessionWithoutDesignRun,
} from "../fixtures/fixtures";

beforeEach(() => {
  document.body.innerHTML = "";
});

async function renderA() {
  const wrapper = mount(Workspace, {
    props: {
      sessionId: "session-test",
      loadSession: () => Promise.resolve(sessionForA()),
      loadArchitecture: () => Promise.resolve(FIXTURE_A),
    },
    attachTo: document.body,
  });
  await flushPromises();
  return wrapper;
}

describe("workspace resizable panes", () => {
  it("renders draggable splitters with sensible defaults", async () => {
    const wrapper = await renderA();
    expect(wrapper.find('[data-testid="splitter-left"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="splitter-right"]').exists()).toBe(true);
    const grid = wrapper.find(".workspace__grid");
    const cols = String(grid.attributes("style") ?? "");
    expect(cols).toContain("330px");
    expect(cols).toContain("350px");
  });
});

describe("whole-panel collapse", () => {
  it("collapses the left panel while the canvas stays", async () => {
    const wrapper = await renderA();
    await wrapper.find('[data-testid="left-collapse"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="left-expand"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
    // State survives: expanding restores the requirements panel.
    await wrapper.find('[data-testid="left-expand"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="left-collapse"]').exists()).toBe(true);
  });

  it("collapses the right panel while the canvas stays", async () => {
    const wrapper = await renderA();
    await wrapper.find('[data-testid="right-collapse"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="right-expand"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
  });
});

describe("architecture focus mode", () => {
  it("offers focus beside the panel controls, not duplicated in the header", async () => {
    const wrapper = await renderA();
    const toolbar = wrapper.find(".workspace__toolbar");
    expect(toolbar.find('[data-testid="left-collapse"]').exists()).toBe(true);
    expect(toolbar.find('[data-testid="right-collapse"]').exists()).toBe(true);
    expect(toolbar.find('[data-testid="focus-toggle"]').exists()).toBe(true);
    // No second focus control in the header while in normal mode.
    expect(wrapper.find("header [data-testid='focus-toggle']").exists()).toBe(false);
  });
  it("expands the canvas and restores sidebars on exit", async () => {
    const wrapper = await renderA();
    await wrapper.find('[data-testid="focus-toggle"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="focus-exit"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(true);
    // Sidebars disappear in focus mode.
    expect(wrapper.find(".workspace__left").isVisible()).toBe(false);
    await wrapper.find('[data-testid="focus-exit"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="focus-toggle"]').exists()).toBe(true);
  });
});

describe("horizontal pipeline", () => {
  it("renders a horizontal stepper with connectors", async () => {
    const wrapper = await renderA();
    const pipeline = wrapper.find('[data-testid="stage-progress"]');
    expect(pipeline.exists()).toBe(true);
    expect(pipeline.classes()).toContain("pipeline--horizontal");
    expect(wrapper.findAll(".pipeline__connector").length).toBeGreaterThan(0);
  });
});

describe("ready-to-run", () => {
  it("starts a design run from the workspace and opens the new session", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "session-empty",
        loadSession: () => Promise.resolve(sessionWithoutDesignRun()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
        startRun: () => Promise.resolve({ session_id: "design-new" }),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="hero-ready"]').exists()).toBe(true);
    await wrapper.find('[data-testid="start-run"]').trigger("click");
    await flushPromises();
    expect(wrapper.emitted("open-session")?.[0]).toEqual(["design-new"]);
  });
});
