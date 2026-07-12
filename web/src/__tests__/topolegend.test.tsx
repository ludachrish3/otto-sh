import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ReactFlow, ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, it } from "vitest";

import { EDGE_STYLES } from "../topo/edgeStyles";
import { LINK_ORDER, TopoLegend } from "../topo/TopoLegend";

// jsdom has no ResizeObserver, which @xyflow/react needs at mount; the same
// shim already lives in chartpanel.test.tsx / pages.test.tsx / subjectpage.test.tsx
// for the equivalent jsdom gap (echarts there, React Flow here).
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

afterEach(cleanup);

// <Panel> must live inside a React Flow context.
function renderLegend() {
  return render(
    <ReactFlowProvider>
      <ReactFlow nodes={[]} edges={[]}>
        <TopoLegend />
      </ReactFlow>
    </ReactFlowProvider>,
  );
}

describe("TopoLegend", () => {
  it("explains every line style and every status colour", () => {
    renderLegend();
    // Iterate EDGE_STYLES's own keys, not a second hand-written list here —
    // a hand-written copy in both TopoLegend.tsx and this test would let a
    // sixth provenance ship a canvas style with no legend row and no test
    // catching it. See the LINK_ORDER exhaustiveness test below.
    for (const p of Object.keys(EDGE_STYLES)) {
      expect(screen.getByTestId(`topo-legend-link-${p}`)).toBeTruthy();
    }
    for (const s of ["ok", "down", "unreachable", "no-data", "unknown"]) {
      expect(screen.getByTestId(`topo-legend-status-${s}`)).toBeTruthy();
    }
    expect(screen.getByTestId("topo-legend-link-impair")).toBeTruthy();
  });

  it("LINK_ORDER covers exactly the keys of EDGE_STYLES", () => {
    // TypeScript only forces EDGE_STYLES's Record<Provenance, ...> to be
    // exhaustive; LINK_ORDER is a plain array and could silently drift —
    // missing a provenance (canvas draws it, legend omits it) or carrying a
    // stale one. Compare as sets so either direction fails.
    expect(new Set(LINK_ORDER)).toEqual(new Set(Object.keys(EDGE_STYLES)));
    expect(LINK_ORDER.length).toBe(Object.keys(EDGE_STYLES).length);
  });

  it("starts expanded and collapses on click", () => {
    renderLegend();
    const toggle = screen.getByTestId("topo-legend-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
