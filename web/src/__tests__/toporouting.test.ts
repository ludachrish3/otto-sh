// The occlusion invariant is the point of this file. React Flow renders edges
// BENEATH nodes, so an edge that overlaps a box is not "a bit ugly" — it is
// invisible. These tests sample the returned path and assert it never enters a
// box it doesn't belong to.
import { describe, expect, it } from "vitest";

import { COL_W, ROW_H } from "../topo/layout";
import { type Point, samplePath } from "../topo/measure";
import { INTERACTION_WIDTH, type Rect, routeEdge } from "../topo/routing";

const W = 208; // element node: w-52
const H = 72; // element node height

/** A synthetic same-column stack, built by hand rather than read off a real
 * layout run: five element-sized boxes 110px apart in one column, plus one
 * more a column over. It borrows kitchen-sink's element names and sizes but
 * is NOT today's actual kitchen-sink positions -- the management partition
 * (Task 3) now pulls edge-gw and mgmt-01 out into column 0 (verified via
 * `deriveManagementIds`/`layoutTopo` against the real fixture: kitchen-sink's
 * management set is exactly {edge-gw, mgmt-01}), leaving column 1 as
 * {chassis-a, db-01, spare-chassis, workers} -- four deep, not five. Built
 * this way instead so db-01 and workers sit a fixed four rows apart with
 * three other boxes between them -- the shape the same-column occlusion test
 * below needs -- and chassis-a is placed one column over for the
 * cross-column tests further down. spare-chassis is an element with 0
 * hosts and still gets a row here. */
// `satisfies`, not `: Record<string, Rect>` — the annotation erased the six
// element names into an index signature, so `NODES.wrokers` type-checked and
// handed routeEdge an undefined Rect at run time. This keeps the Rect check
// AND the literal keys.
const NODES = {
  "db-01": { x: COL_W, y: 0 * ROW_H, width: W, height: H },
  // `1 * ROW_H` is a row INDEX times the row height, not a Number() coercion:
  // the `1` is the second entry of the 0/1/2/3/4 index column below, and
  // dropping it (the rule's only reading of this line that isn't the absurd
  // `Number(ROW_H)` its own unsafe fix proposes) would break the one row of
  // that column that makes this table readable as a table.
  // biome-ignore lint/complexity/noImplicitCoercions: row index, not a coercion
  "edge-gw": { x: COL_W, y: 1 * ROW_H, width: W, height: H },
  "mgmt-01": { x: COL_W, y: 2 * ROW_H, width: W, height: H },
  "spare-chassis": { x: COL_W, y: 3 * ROW_H, width: W, height: H },
  workers: { x: COL_W, y: 4 * ROW_H, width: W, height: H },
  "chassis-a": { x: 2 * COL_W, y: 0, width: W, height: H },
} satisfies Record<string, Rect>;

/** Sample count for the occlusion scan.
 *
 * This file used to carry its OWN copy of the sampler, which took a STEP count
 * and returned steps+1 points; `measure.samplePath` takes the POINT COUNT
 * directly, so every count here is the old step count plus one and the sampled
 * `t` values are unchanged.
 *
 * The local copy was deleted rather than kept, and that is the point of this
 * import. It parsed the coordinate numbers out of the path and destructured
 * four (or eight) of them with NO arity check — the very guard the code it
 * mirrors (`measure.ts`'s `lineCoords`/`cubicCoords`) does perform. Feed it a
 * grammar `routeEdge` does not emit today and every destructured coordinate is
 * `undefined`, every sampled point is `(NaN, NaN)`, and every `x >= r.x` test
 * below is false — so `boxesUnder` returns `[]` and the occlusion assertions,
 * the entire reason this file exists, PASS while measuring nothing. Verified
 * against the deleted code with a quadratic path: the same geometry reports
 * `["box"]` as a cubic and `[]` as a `Q`. `measure.samplePath` throws on an
 * unrecognised command or arity instead. */
const OCCLUSION_SAMPLES = 401;

/** Where a path starts. `samplePath(_, 1)` yields exactly the `t = 0` point,
 * but reading position 0 out of an array cannot say so — this names the
 * invariant once instead of at each call site. */
function pathStart(path: string): Point {
  const [start] = samplePath(path, 1);
  if (start === undefined) throw new Error(`pathStart: no sample for path "${path}"`);
  return start;
}

function inside(p: Point, r: Rect): boolean {
  return p.x >= r.x && p.x <= r.x + r.width && p.y >= r.y && p.y <= r.y + r.height;
}

/** Names of the nodes this path disappears behind. */
function boxesUnder(path: string, endpoints: string[]): string[] {
  const hit = new Set<string>();
  for (const p of samplePath(path, OCCLUSION_SAMPLES)) {
    for (const [name, r] of Object.entries(NODES)) {
      if (endpoints.includes(name)) continue;
      if (inside(p, r)) hit.add(name);
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

  it("fans MULTIPLE parallel links between adjacent rows apart, not onto one line", () => {
    // Two heavily-linked peers can land in adjacent rows of the SAME column
    // -- e.g. kitchen-sink's app-db and metrics-udp both connect
    // workers<->db-01. The management partition (Task 3) pulls edge-gw and
    // mgmt-01 out of that column entirely -- they're kitchen-sink's only two
    // management elements; chassis-a, db-01, spare-chassis and workers all
    // stay. workers and db-01 still land ADJACENT within that four-element
    // column, but only because app-db and metrics-udp connect them: Rule 5's
    // same-column adjacency bias (`barycentricRowSort` in layout.ts) nudges
    // data-plane-linked peers together -- it is not an emptied column. An
    // unfanned centreline drew both parallel links on the EXACT same path --
    // geometrically fine (never swallowed), but only the last-painted one
    // was ever clickable (#131's failure mode, one row apart instead of one
    // column apart).
    const a = routeEdge(NODES["db-01"], NODES["edge-gw"], 0, 2);
    const b = routeEdge(NODES["db-01"], NODES["edge-gw"], 1, 2);
    expect(a.path).not.toBe(b.path);
    const a0 = pathStart(a.path);
    const b0 = pathStart(b.path);
    const cx = COL_W + W / 2;
    // Symmetric around the shared centreline, not shifted wholesale to one side.
    expect(cx - a0.x).toBeCloseTo(b0.x - cx);
    // And far enough apart that each keeps its own INTERACTION_WIDTH pointer
    // target — the same threshold #131 pinned for the bowed multi-row case.
    expect(Math.abs(b0.x - a0.x)).toBeGreaterThan(INTERACTION_WIDTH / 2);
  });

  it("keeps a lone adjacent-row edge exactly centred (groupSize 1 is unaffected)", () => {
    const a = routeEdge(NODES["db-01"], NODES["edge-gw"], 0, 1);
    expect(a.path).toBe(`M${COL_W + W / 2},${H} L${COL_W + W / 2},${ROW_H}`);
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
   * width), because clearance at row span >= 5 goes negative again.
   *
   * Returns the three ROLES the test actually has for those boxes — the two
   * the edge connects and the ones it must clear — rather than a flat array
   * the caller then re-derives them from with `col[0]` / `col[rowSpan]` /
   * `col.slice(1, rowSpan)`. Those reads restated the "rowSpan+1 boxes"
   * invariant at the call site, where nothing enforces it: get the argument
   * and the index out of step and `routeEdge` is silently handed an
   * `undefined` rect. */
  function syntheticColumn(rowSpan: number): { top: Rect; between: Rect[]; bottom: Rect } {
    const rowAt = (row: number): Rect => ({ x: COL_W, y: row * ROW_H, width: W, height: H });
    return {
      top: rowAt(0),
      between: Array.from({ length: rowSpan - 1 }, (_, i) => rowAt(i + 1)),
      bottom: rowAt(rowSpan),
    };
  }

  it.each(Array.from({ length: 19 }, (_, i) => i + 2))(
    "clears every intervening node at row span %i",
    (rowSpan) => {
      const { top, between, bottom } = syntheticColumn(rowSpan);
      for (const parallelIndex of [0, 1]) {
        const { path } = routeEdge(top, bottom, parallelIndex, 2);
        for (const p of samplePath(path, OCCLUSION_SAMPLES)) {
          for (const r of between) {
            expect(inside(p, r)).toBe(false);
          }
        }
      }
    },
  );
});

describe("routeEdge — parallel edges stay independently clickable", () => {
  // The bug this pins (found in CI, issue #131): React Flow's pointer target for
  // an edge is a 20px-wide invisible `react-flow__edge-interaction` path, NOT the
  // 2px visible stroke. Two parallel edges whose centrelines are closer than half
  // of that share one hit target — the one painted last wins and the other becomes
  // completely unclickable: its inspector and hover card are unreachable, and no
  // amount of aiming helps. Shipped constants put app-db 6px from metrics-udp; it
  // was reachable at 0 of 19 sampled points on its own stroke.
  //
  // Occlusion tests could not see this: both edges were perfectly visible and
  // cleared every box. They were simply drawn on top of each other.
  /** How many of 19 evenly-spaced points along `inner` sit far enough from
   * `outer` to escape its hit band — deliberately mirroring the 19-sample scan
   * `_point_on_edge` runs in the Playwright lane, so this fails for the same
   * reason the browser does.
   *
   * Note two parallel edges NECESSARILY converge at their shared endpoints, so
   * some points are always buried. What matters is that a usable stretch in the
   * middle is not. */
  function clickablePoints(inner: string, outer: string): number {
    // 21 samples less both endpoints leaves the 19 interior ones. Dropping
    // them by slicing the array says which points are excluded and why; the
    // `for (let i = 1; i < 20; i++)` this replaces encoded the sample count
    // twice — once as samplePath's argument and once as a loop bound — with
    // nothing tying the two together.
    const innerPts = samplePath(inner, 21).slice(1, -1);
    const outerPts = samplePath(outer, 201);
    let clear = 0;
    for (const p of innerPts) {
      // Distance from this point on the inner curve to the NEAREST point on the
      // outer curve — the outer edge's hit band is centred on its own stroke.
      let nearest = Number.POSITIVE_INFINITY;
      for (const o of outerPts) {
        nearest = Math.min(nearest, Math.hypot(p.x - o.x, p.y - o.y));
      }
      if (nearest > INTERACTION_WIDTH / 2) clear++;
    }
    return clear;
  }

  const top: Rect = { x: COL_W, y: 0, width: W, height: H };

  it.each([
    [2, 3],
    [3, 4],
    [4, 6],
  ])(
    "leaves %i parallel links each with a reachable stretch (row span %i)",
    (groupSize, rowSpan) => {
      const bottom: Rect = { x: COL_W, y: rowSpan * ROW_H, width: W, height: H };
      const paths = Array.from(
        { length: groupSize },
        (_, i) => routeEdge(top, bottom, i, groupSize).path,
      );
      for (let i = 1; i < groupSize; i++) {
        // The inner sibling is the one that gets buried (the outer paints later),
        // so it is the one that must survive. 5 of 19 is roughly the middle
        // quarter of the curve — enough to hit, and far more than the ZERO the
        // shipped constants left it with.
        expect(clickablePoints(paths[i - 1], paths[i])).toBeGreaterThanOrEqual(5);
      }
    },
  );

  it("never emits two identical paths for one parallel group", () => {
    const bottom: Rect = { x: COL_W, y: 4 * ROW_H, width: W, height: H };
    for (const groupSize of [2, 3, 4]) {
      const paths = Array.from(
        { length: groupSize },
        (_, i) => routeEdge(top, bottom, i, groupSize).path,
      );
      expect(new Set(paths).size).toBe(groupSize);
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
    // Three samples are t = 0, 0.5, 1, so the middle one IS the midpoint.
    // `pts[200]` out of 401 was the same t by arithmetic the reader had to do,
    // and silently stopped being the midpoint if the sample count moved.
    const [, mid] = samplePath(path, 3);
    if (mid === undefined) throw new Error(`no midpoint sample for path "${path}"`);
    expect(labelX).toBeCloseTo(mid.x, 6);
    expect(labelY).toBeCloseTo(mid.y, 6);
  });
});
