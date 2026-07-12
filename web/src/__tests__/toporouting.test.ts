// The occlusion invariant is the point of this file. React Flow renders edges
// BENEATH nodes, so an edge that overlaps a box is not "a bit ugly" — it is
// invisible. These tests sample the returned path and assert it never enters a
// box it doesn't belong to.
import { describe, expect, it } from "vitest";

import { COL_W, ROW_H } from "../topo/layout";
import { type Rect, routeEdge } from "../topo/routing";

const W = 208; // element node: w-52
const H = 72; // element node height

/** kitchen-sink's real inter-element layout. db-01, edge-gw, mgmt-01,
 * spare-chassis and workers all have depth 1, so they stack in one column
 * (rowOrder sorts by id); chassis-a is the only depth-2 node.
 * spare-chassis is an element with 0 hosts — it still lands at depth 1,
 * which is why the column is five deep, not four. */
const NODES: Record<string, Rect> = {
  "db-01": { x: COL_W, y: 0 * ROW_H, width: W, height: H },
  "edge-gw": { x: COL_W, y: 1 * ROW_H, width: W, height: H },
  "mgmt-01": { x: COL_W, y: 2 * ROW_H, width: W, height: H },
  "spare-chassis": { x: COL_W, y: 3 * ROW_H, width: W, height: H },
  workers: { x: COL_W, y: 4 * ROW_H, width: W, height: H },
  "chassis-a": { x: 2 * COL_W, y: 0, width: W, height: H },
};

/** Sample the two path shapes routing.ts emits: "M x,y L x,y" and
 * "M x,y C c1x,c1y c2x,c2y x,y". */
function samplePath(path: string, steps = 400): [number, number][] {
  const n = (path.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
  const out: [number, number][] = [];
  if (path.includes("L")) {
    const [sx, sy, tx, ty] = n;
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      out.push([sx + (tx - sx) * t, sy + (ty - sy) * t]);
    }
    return out;
  }
  const [sx, sy, c1x, c1y, c2x, c2y, tx, ty] = n;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const u = 1 - t;
    out.push([
      u ** 3 * sx + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t ** 3 * tx,
      u ** 3 * sy + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t ** 3 * ty,
    ]);
  }
  return out;
}

/** Names of the nodes this path disappears behind. */
function boxesUnder(path: string, endpoints: string[]): string[] {
  const hit = new Set<string>();
  for (const [x, y] of samplePath(path)) {
    for (const [name, r] of Object.entries(NODES)) {
      if (endpoints.includes(name)) continue;
      if (x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height) hit.add(name);
    }
  }
  return [...hit].sort();
}

describe("routeEdge — same column", () => {
  it("never routes a multi-row link under an intervening node", () => {
    // app-db and metrics-udp: workers <-> db-01, four rows apart, with
    // edge-gw, mgmt-01 and spare-chassis sitting between them. This is the bug.
    for (const parallelIndex of [0, 1]) {
      const { path } = routeEdge(NODES["db-01"], NODES.workers, parallelIndex, 2);
      expect(boxesUnder(path, ["db-01", "workers"])).toEqual([]);
    }
  });

  it("draws adjacent rows as a straight line between the face centres", () => {
    // tun-demo: edge-gw <-> db-01, one row apart — nothing in between, so the
    // shortest path is also the right one.
    const { path } = routeEdge(NODES["db-01"], NODES["edge-gw"], 0, 1);
    const cx = COL_W + W / 2;
    expect(path).toBe(`M${cx},${H} L${cx},${ROW_H}`);
  });

  it("fans parallel bowed links OUTWARD only", () => {
    // A centred fan would push the inner sibling back under mgmt-01, which is
    // the very box the bow exists to clear.
    const a = routeEdge(NODES["db-01"], NODES.workers, 0, 2);
    const b = routeEdge(NODES["db-01"], NODES.workers, 1, 2);
    expect(b.labelX).toBeGreaterThan(a.labelX);
  });

  it("is symmetric in argument order", () => {
    const down = routeEdge(NODES["db-01"], NODES.workers, 0, 2);
    const up = routeEdge(NODES.workers, NODES["db-01"], 0, 2);
    expect(up.path).toBe(down.path);
  });

  it.each([3, 4])("keeps every fanned sibling distinct for groupSize %i", (groupSize) => {
    // db-01 <-> workers, four rows apart: three (or four) parallel links —
    // e.g. two declared links plus a tunnel — used to collapse onto an
    // identical path/label for every index past 1, once the constant
    // FAN_STEP hit the gutter clamp. Every index must now be distinct.
    const routes = Array.from({ length: groupSize }, (_, i) =>
      routeEdge(NODES["db-01"], NODES.workers, i, groupSize),
    );
    const paths = new Set(routes.map((r) => r.path));
    expect(paths.size).toBe(groupSize);
    for (let i = 1; i < routes.length; i++) {
      expect(routes[i].labelX).toBeGreaterThan(routes[i - 1].labelX);
    }
  });
});

describe("routeEdge — occlusion invariant across row spans", () => {
  /** A synthetic same-depth column of rowSpan+1 element-sized boxes, one row
   * apart, stacked exactly like a real depth column would be however deep
   * it gets. Endpoint-independent of NODES/kitchen-sink on purpose: this is
   * the general regression guard, not a fixture-shaped one. It fails the
   * moment CTRL_Y_MAX is removed (or the bow goes back to scaling with
   * width), because clearance at row span >= 5 goes negative again. */
  function syntheticColumn(rowSpan: number): Rect[] {
    const col: Rect[] = [];
    for (let row = 0; row <= rowSpan; row++) {
      col.push({ x: COL_W, y: row * ROW_H, width: W, height: H });
    }
    return col;
  }

  it.each(
    Array.from({ length: 19 }, (_, i) => i + 2),
  )("clears every intervening node at row span %i", (rowSpan) => {
    const col = syntheticColumn(rowSpan);
    const top = col[0];
    const bottom = col[rowSpan];
    const between = col.slice(1, rowSpan);
    for (const parallelIndex of [0, 1]) {
      const { path } = routeEdge(top, bottom, parallelIndex, 2);
      for (const [x, y] of samplePath(path)) {
        for (const r of between) {
          const inside = x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height;
          expect(inside).toBe(false);
        }
      }
    }
  });
});

describe("routeEdge — cross column", () => {
  it("anchors on the facing sides and stays clear", () => {
    const { path } = routeEdge(NODES["edge-gw"], NODES["chassis-a"], 0, 1);
    expect(path.startsWith(`M${COL_W + W},${ROW_H + H / 2} `)).toBe(true);
    expect(path.endsWith(` ${2 * COL_W},${H / 2}`)).toBe(true);
    expect(boxesUnder(path, ["edge-gw", "chassis-a"])).toEqual([]);
  });

  it("anchors on the facing sides regardless of argument order", () => {
    const forward = routeEdge(NODES["edge-gw"], NODES["chassis-a"], 0, 1);
    const backward = routeEdge(NODES["chassis-a"], NODES["edge-gw"], 0, 1);
    expect(backward.path).toBe(forward.path);
  });
});

describe("routeEdge — label point", () => {
  // The old fannedBezierPath put the label at the FULL perpendicular offset,
  // but a cubic with both interior control points offset by k only reaches
  // 0.75k — so the label floated off its own curve.
  it.each([
    ["same column, bowed", () => routeEdge(NODES["db-01"], NODES.workers, 0, 2)],
    ["cross column, fanned", () => routeEdge(NODES["edge-gw"], NODES["chassis-a"], 0, 2)],
  ])("lies on the curve (%s)", (_name, route) => {
    const { path, labelX, labelY } = route();
    const pts = samplePath(path, 400);
    const [midX, midY] = pts[200];
    expect(labelX).toBeCloseTo(midX, 6);
    expect(labelY).toBeCloseTo(midY, 6);
  });
});
