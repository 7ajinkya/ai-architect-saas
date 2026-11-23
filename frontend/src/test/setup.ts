/**
 * Test environment shims.
 *
 * jsdom implements neither ResizeObserver nor the SVG layout methods, so
 * the graph renderer cannot measure its viewport or edge labels. These
 * stubs are a test-environment concern only and are unrelated to the
 * product code.
 */

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
    ResizeObserverStub;
}

/**
 * The renderer only needs these measurements to size viewport and edge
 * label boxes, so zero-sized results are sufficient for assertions about
 * which nodes and edges rendered.
 */
function installSvgStubs(): void {
  const scope = globalThis as unknown as {
    SVGElement?: { prototype: Record<string, unknown> };
    DOMMatrix?: new (init?: string | number[]) => unknown;
    DOMMatrixReadOnly?: new (init?: string | number[]) => unknown;
  };

  if (!scope.SVGElement) return;

  const proto = scope.SVGElement.prototype;
  if (typeof proto.getBBox !== "function") {
    proto.getBBox = function getBBox(): DOMRect {
      return {
        x: 0,
        y: 0,
        width: 0,
        height: 0,
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        toJSON: () => ({}),
      } as DOMRect;
    };
  }
  if (typeof proto.getComputedTextLength !== "function") {
    proto.getComputedTextLength = function getComputedTextLength(): number {
      return 0;
    };
  }
  const identity = () =>
    scope.DOMMatrixReadOnly ? new scope.DOMMatrixReadOnly() : undefined;
  if (typeof proto.getScreenCTM !== "function") {
    proto.getScreenCTM = identity;
  }
  if (typeof proto.getCTM !== "function") {
    proto.getCTM = identity;
  }
  if (typeof proto.createSVGMatrix !== "function") {
    proto.createSVGMatrix = () =>
      scope.DOMMatrix ? new scope.DOMMatrix() : undefined;
  }
}

installSvgStubs();

/**
 * jsdom performs no layout, so every element reports a zero-sized box.
 * The graph renderer refuses to initialise without a viewport, so give
 * elements a nominal size to match what a real browser reports.
 */
const rect = (): DOMRect =>
  ({
    x: 0,
    y: 0,
    width: 1200,
    height: 800,
    top: 0,
    left: 0,
    right: 1200,
    bottom: 800,
    toJSON: () => ({}),
  }) as DOMRect;

if (typeof Element !== "undefined") {
  Element.prototype.getBoundingClientRect = function getBoundingClientRect(): DOMRect {
    return rect();
  };
}
if (typeof SVGElement !== "undefined") {
  SVGElement.prototype.getBoundingClientRect = function getBoundingClientRect(): DOMRect {
    return rect();
  };
}

// The renderer measures its viewport with offsetWidth/offsetHeight, which
// jsdom always reports as 0.
if (typeof HTMLElement !== "undefined") {
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
    configurable: true,
    get: () => 1200,
  });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
    configurable: true,
    get: () => 800,
  });
}
