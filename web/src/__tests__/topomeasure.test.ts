import { describe, expect, it } from "vitest";

import { classifyEdge, countCrossings, countSwallowed, samplePath } from "../topo/measure";

describe("samplePath", () => {
  it("samples a straight segment", () => {
    const pts = samplePath("M0,0 L10,0", 11);
    expect(pts[0]).toEqual({ x: 0, y: 0 });
    expect(pts[10].x).toBeCloseTo(10);
    expect(pts[5].x).toBeCloseTo(5);
  });

  it("samples a cubic — the only other grammar routeEdge emits", () => {
    // A flat cubic with both control points on the line is a straight line.
    const pts = samplePath("M0,0 C10,0 20,0 30,0", 4);
    expect(pts.at(-1)?.x).toBeCloseTo(30);
    for (const p of pts) expect(p.y).toBeCloseTo(0);
  });
});

describe("classifyEdge", () => {
  it("counts only declared links as data-plane", () => {
    const e = (provenance: string) =>
      ({ id: "e", source: "a", target: "b", provenance, impair: null, parallelIndex: 0 }) as never;
    expect(classifyEdge(e("declared"))).toBe("data-plane");
    expect(classifyEdge(e("implicit"))).toBe("management");
    expect(classifyEdge(e("local"))).toBe("management");
    expect(classifyEdge(e("reports-for"))).toBe("management");
    expect(classifyEdge(e("dynamic"))).toBe("tunnel");
  });
});

describe("countSwallowed", () => {
  it("finds an edge passing under a non-endpoint node", () => {
    // a --------- b, with node c sitting squarely on the line between them.
    const rects = new Map([
      ["a", { x: 0, y: 0, width: 10, height: 10 }],
      ["b", { x: 200, y: 0, width: 10, height: 10 }],
      ["c", { x: 90, y: -10, width: 20, height: 30 }],
    ]);
    const edges = [{ id: "ab", source: "a", target: "b", path: "M5,5 L205,5" }];
    expect(countSwallowed(edges, rects, 40)).toBe(1);
  });

  it("does not count an edge passing over its OWN endpoints", () => {
    const rects = new Map([
      ["a", { x: 0, y: 0, width: 10, height: 10 }],
      ["b", { x: 200, y: 0, width: 10, height: 10 }],
    ]);
    const edges = [{ id: "ab", source: "a", target: "b", path: "M5,5 L205,5" }];
    expect(countSwallowed(edges, rects, 40)).toBe(0);
  });
});

// countCrossings is the most intricate function in this module — segment
// intersection with epsilon handling — and until now was only exercised
// indirectly, by matching a baseline number through the SEPARATELY WRITTEN
// re-implementation in test_topology_budget.py's page.evaluate script. These
// pin its own logic directly.
describe("countCrossings", () => {
  it("counts two segments that plainly cross", () => {
    const edges = [
      { id: "e1", source: "a", target: "b", path: "M0,0 L10,10" },
      { id: "e2", source: "c", target: "d", path: "M0,10 L10,0" },
    ];
    expect(countCrossings(edges)).toBe(1);
  });

  it("does not count two parallel segments", () => {
    const edges = [
      { id: "e1", source: "a", target: "b", path: "M0,0 L10,0" },
      { id: "e2", source: "c", target: "d", path: "M0,5 L10,5" },
    ];
    expect(countCrossings(edges)).toBe(0);
  });

  it("excludes two edges that merely share an endpoint, even when their paths geometrically cross", () => {
    // Identical coordinates to the "plainly cross" case above — if
    // source/target were ignored, this would register as a crossing too.
    // The node two edges both terminate on is not a crossing; it is the
    // shared endpoint, and this is the case most likely to inflate every
    // crossing count if the endpoint check is ever dropped or weakened.
    const edges = [
      { id: "e1", source: "a", target: "shared", path: "M0,0 L10,10" },
      { id: "e2", source: "shared", target: "c", path: "M0,10 L10,0" },
    ];
    expect(countCrossings(edges)).toBe(0);
  });

  it("does not throw and does not count a degenerate zero-length segment", () => {
    // A zero-length "edge" (both samples land on the same point) has no
    // direction, so its cross product with anything is zero — the same
    // "parallel (or collinear)" branch segmentsIntersect uses to bail out,
    // not a division by zero.
    const edges = [
      { id: "e1", source: "a", target: "b", path: "M5,5 L5,5" },
      { id: "e2", source: "c", target: "d", path: "M0,0 L10,10" }, // passes through (5,5)
    ];
    expect(() => countCrossings(edges)).not.toThrow();
    expect(countCrossings(edges)).toBe(0);
  });

  it("does not double-count an overlapping collinear pair sampled at many coincident points", () => {
    // e2 is a subset of e1's own line: nearly every sampled point of e2
    // lands exactly ON e1, giving segmentsIntersect dozens of candidate
    // (k, l) pairs to evaluate for this one edge pair. A per-segment-pair
    // count (instead of the intended per-EDGE-pair count) would wildly
    // inflate this; the collinear "parallel" guard should also keep it at
    // zero rather than counting the overlap as one continuous crossing.
    const edges = [
      { id: "e1", source: "a", target: "b", path: "M0,0 L10,0" },
      { id: "e2", source: "c", target: "d", path: "M2,0 L8,0" },
    ];
    expect(() => countCrossings(edges)).not.toThrow();
    expect(countCrossings(edges)).toBe(0);
  });
});
