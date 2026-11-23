/**
 * Graph orientation tests (ArchitectureGraph).
 *
 * Orientation is presentation-only: the same artifact renders in both
 * directions with direction-aware handles (vertical: top->bottom,
 * horizontal: left->right) and arrowheads on every edge. Switching
 * re-layouts once, and plain rerenders never move nodes (user-dragged
 * positions survive).
 */
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import ArchitectureGraph from "./ArchitectureGraph.vue";
import { toGraphModel } from "../adapters/graph";
import { FIXTURE_A } from "../fixtures/fixtures";

beforeEach(() => {
  document.body.innerHTML = "";
});

function mountGraph() {
  return mount(ArchitectureGraph, {
    props: {
      architecture: FIXTURE_A,
      model: toGraphModel(FIXTURE_A),
    },
    attachTo: document.body,
  });
}

function transforms(wrapper: VueWrapper): string[] {
  return wrapper
    .findAll(".vue-flow__node")
    .map((n) => String(n.attributes("style") ?? ""));
}

function nodeById(wrapper: VueWrapper, nodeId: string) {
  const node = wrapper
    .findAll(".vue-flow__node")
    .find((candidate) => candidate.attributes("data-id") === nodeId);
  if (!node) throw new Error(`node ${nodeId} did not render`);
  return node;
}

function handlePositions(
  wrapper: VueWrapper,
  nodeId: string,
): { target: string | undefined; source: string | undefined } {
  const node = nodeById(wrapper, nodeId);
  return {
    target: node.find(".vue-flow__handle.target").attributes("data-handlepos"),
    source: node.find(".vue-flow__handle.source").attributes("data-handlepos"),
  };
}

function edgeMarkerEnds(wrapper: VueWrapper): string[] {
  return wrapper
    .findAll(".vue-flow__edge-path")
    .map((path) => String(path.attributes("marker-end") ?? ""));
}

describe("graph orientation", () => {
  it("defaults to vertical", async () => {
    const wrapper = mountGraph();
    await flushPromises();
    expect(
      wrapper.find('[data-testid="orientation-vertical"]').attributes("aria-pressed"),
    ).toBe("true");
    expect(
      wrapper.find('[data-testid="orientation-horizontal"]').attributes("aria-pressed"),
    ).toBe("false");
    expect(wrapper.findAll(".vue-flow__node")).toHaveLength(3);
  });

  it("switches to a readable horizontal layout on request", async () => {
    const wrapper = mountGraph();
    await flushPromises();
    const vertical = transforms(wrapper);

    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();

    expect(
      wrapper.find('[data-testid="orientation-horizontal"]').attributes("aria-pressed"),
    ).toBe("true");
    expect(
      wrapper.find('[data-testid="orientation-vertical"]').attributes("aria-pressed"),
    ).toBe("false");
    // Same nodes, new coordinates.
    expect(wrapper.findAll(".vue-flow__node")).toHaveLength(3);
    expect(transforms(wrapper)).not.toEqual(vertical);
  });

  it("round-trips back to the exact vertical positions", async () => {
    const wrapper = mountGraph();
    await flushPromises();
    const vertical = transforms(wrapper);

    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();
    await wrapper.find('[data-testid="orientation-vertical"]').trigger("click");
    await flushPromises();

    expect(transforms(wrapper)).toEqual(vertical);
  });

  it("does not reset positions on plain rerenders", async () => {
    const wrapper = mountGraph();
    await flushPromises();
    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();
    const horizontal = transforms(wrapper);

    // Same artifact object, same orientation: a rerender must not move
    // anything (this is what keeps user-dragged nodes in place during
    // polling and panel updates).
    await wrapper.setProps({
      architecture: FIXTURE_A,
      model: toGraphModel(FIXTURE_A),
    });
    await flushPromises();
    expect(transforms(wrapper)).toEqual(horizontal);
  });
});

describe("graph edge direction", () => {
  it("connects vertical edges top to bottom", async () => {
    const wrapper = mountGraph();
    await flushPromises();

    for (const nodeId of ["gateway", "worker", "records"]) {
      expect(handlePositions(wrapper, nodeId)).toEqual({
        target: "top",
        source: "bottom",
      });
    }
  });

  it("connects horizontal edges left to right", async () => {
    const wrapper = mountGraph();
    await flushPromises();

    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();

    for (const nodeId of ["gateway", "worker", "records"]) {
      expect(handlePositions(wrapper, nodeId)).toEqual({
        target: "left",
        source: "right",
      });
    }
  });

  it("renders an arrowhead on every edge in both orientations", async () => {
    const wrapper = mountGraph();
    await flushPromises();

    const verticalMarkerEnds = edgeMarkerEnds(wrapper);
    expect(verticalMarkerEnds).toHaveLength(2);
    for (const markerEnd of verticalMarkerEnds) {
      expect(markerEnd).toMatch(/^url\(/);
    }
    expect(wrapper.findAll("marker.vue-flow__arrowhead").length).toBeGreaterThan(0);

    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();

    const horizontalMarkerEnds = edgeMarkerEnds(wrapper);
    expect(horizontalMarkerEnds).toHaveLength(2);
    for (const markerEnd of horizontalMarkerEnds) {
      expect(markerEnd).toMatch(/^url\(/);
    }
    expect(wrapper.findAll("marker.vue-flow__arrowhead").length).toBeGreaterThan(0);
  });

  it("keeps the edge set identical across orientations", async () => {
    const wrapper = mountGraph();
    await flushPromises();
    const edgeIds = () =>
      wrapper
        .findAll(".vue-flow__edge")
        .map((edge) => edge.attributes("data-id"));
    const vertical = edgeIds();

    await wrapper.find('[data-testid="orientation-horizontal"]').trigger("click");
    await flushPromises();

    // Rendering never invents or drops relationships: same edges, new
    // coordinates only.
    expect(edgeIds()).toEqual(vertical);
    expect(wrapper.findAll(".vue-flow__edge")).toHaveLength(
      FIXTURE_A.edges.length,
    );
  });
});
