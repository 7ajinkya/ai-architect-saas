/**
 * Splitter resize regression tests.
 *
 * The requirements pane must stay resizable after the architecture
 * graph renders: the drag handlers are load-independent, so these
 * tests drive the real splitter (mousedown on the splitter, mousemove
 * on window, mouseup on window) in both states and assert the grid
 * track actually moves.
 *
 * Sizing is delta-based and clamped to the documented limits, so the
 * assertions below hold for any grid geometry.
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

function gridWidths(wrapper: ReturnType<typeof mount>): string {
  const grid = wrapper.find(".workspace__grid");
  return String(grid.attributes("style") ?? "");
}

async function dragLeftSplitter(
  wrapper: ReturnType<typeof mount>,
  fromX: number,
  toX: number,
): Promise<void> {
  const splitter = wrapper.find('[data-testid="splitter-left"]');
  expect(splitter.exists()).toBe(true);
  await splitter.trigger("mousedown", { clientX: fromX, button: 0 });
  window.dispatchEvent(new MouseEvent("mousemove", { clientX: toX }));
  await flushPromises();
  window.dispatchEvent(new MouseEvent("mouseup"));
  await flushPromises();
}

describe("splitter resize", () => {
  it("resizes before the architecture loads", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "s",
        loadSession: () => Promise.resolve(sessionWithoutDesignRun()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
      attachTo: document.body,
    });
    await flushPromises();
    const before = gridWidths(wrapper);
    await dragLeftSplitter(wrapper, 500, 650);
    expect(gridWidths(wrapper)).not.toBe(before);
  });

  it("resizes after the architecture graph is rendered", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "s",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
      attachTo: document.body,
    });
    await flushPromises();
    expect(wrapper.find('[data-testid="architecture-graph"]').exists()).toBe(
      true,
    );
    const before = gridWidths(wrapper);
    await dragLeftSplitter(wrapper, 500, 650);
    expect(gridWidths(wrapper)).not.toBe(before);
  });

  it("grows the pane by the drag delta within limits", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "s",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
      attachTo: document.body,
    });
    await flushPromises();
    // Default left width is 330px; a +150px drag lands on 480px.
    await dragLeftSplitter(wrapper, 500, 650);
    expect(gridWidths(wrapper)).toContain("480px");
    // Dragging far past the maximum clamps instead of escaping.
    await dragLeftSplitter(wrapper, 500, 5000);
    expect(gridWidths(wrapper)).toContain("560px");
  });

  it("ends the drag cleanly so a later drag still works", async () => {
    const wrapper = mount(Workspace, {
      props: {
        sessionId: "s",
        loadSession: () => Promise.resolve(sessionForA()),
        loadArchitecture: () => Promise.resolve(FIXTURE_A),
      },
      attachTo: document.body,
    });
    await flushPromises();
    await dragLeftSplitter(wrapper, 500, 600);
    const mid = gridWidths(wrapper);
    // Stray moves with no button held must not resize anything.
    window.dispatchEvent(new MouseEvent("mousemove", { clientX: 900 }));
    await flushPromises();
    expect(gridWidths(wrapper)).toBe(mid);
    // A fresh drag works again afterwards.
    await dragLeftSplitter(wrapper, 500, 400);
    expect(gridWidths(wrapper)).not.toBe(mid);
  });
});
