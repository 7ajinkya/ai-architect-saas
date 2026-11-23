/**
 * CollapsibleSection tests: organization without information loss.
 */

import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it } from "vitest";

import CollapsibleSection from "./CollapsibleSection.vue";

beforeEach(() => {
  document.body.innerHTML = "";
});

function render(props: Record<string, unknown> = {}) {
  return mount(CollapsibleSection, {
    props: { title: "Decisions", ...props },
    slots: { default: '<p data-testid="inner">kept content</p>' },
  });
}

describe("CollapsibleSection", () => {
  it("shows title, count and content by default", () => {
    const wrapper = render({ count: 4, testId: "section-x" });
    expect(wrapper.find('[data-testid="section-x-toggle"]').text()).toContain(
      "Decisions",
    );
    expect(wrapper.find('[data-testid="section-x-toggle"]').text()).toContain("4");
    expect(wrapper.find('[data-testid="inner"]').isVisible()).toBe(true);
  });

  it("collapses and reopens while preserving content", async () => {
    const wrapper = render({ testId: "section-x" });
    const section = () => wrapper.find('[data-testid="section-x"]');
    expect(section().attributes("data-open")).toBe("true");
    await wrapper.find('[data-testid="section-x-toggle"]').trigger("click");
    expect(section().attributes("data-open")).toBe("false");
    // Content is hidden, not removed.
    expect(wrapper.find('[data-testid="inner"]').exists()).toBe(true);
    await wrapper.find('[data-testid="section-x-toggle"]').trigger("click");
    expect(section().attributes("data-open")).toBe("true");
    expect(wrapper.find('[data-testid="inner"]').exists()).toBe(true);
  });

  it("supports starting collapsed for secondary detail", () => {
    const wrapper = render({ defaultOpen: false, testId: "section-x" });
    expect(
      wrapper.find('[data-testid="section-x"]').attributes("data-open"),
    ).toBe("false");
    expect(wrapper.find('[data-testid="inner"]').exists()).toBe(true);
  });
});
