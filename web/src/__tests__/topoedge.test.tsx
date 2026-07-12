import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES, edgeStyle } from "../topo/edgeStyles";

afterEach(cleanup);

const ALL: TopoEdge["provenance"][] = ["implicit", "declared", "dynamic", "local", "reports-for"];

describe("edgeStyle", () => {
  it("maps provenance to distinct strokes", () => {
    expect(edgeStyle("implicit", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    expect(edgeStyle("declared", false).strokeWidth).toBe(2);
    const styles = ALL.map((p) => JSON.stringify(edgeStyle(p, false)));
    expect(new Set(styles).size).toBe(5);
  });

  it("emphasized state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth,
    );
  });
});

describe("EDGE_STYLES", () => {
  // The legend renders from this table. A provenance without a row would ship
  // a line style that the key silently fails to explain.
  it("has a labelled row for every provenance", () => {
    for (const p of ALL) {
      expect(EDGE_STYLES[p].label.length).toBeGreaterThan(0);
      expect(EDGE_STYLES[p].hint.length).toBeGreaterThan(0);
    }
    expect(Object.keys(EDGE_STYLES).sort()).toEqual([...ALL].sort());
  });

  it("gives tunnels — and only tunnels — a casing", () => {
    expect(EDGE_STYLES.dynamic.casing).toBeDefined();
    for (const p of ALL.filter((x) => x !== "dynamic")) {
      expect(EDGE_STYLES[p].casing).toBeUndefined();
    }
  });
});
