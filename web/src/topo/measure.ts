// Geometry helpers for MEASURING the topology layout, instead of judging it
// by eye. Pure, no DOM — routeEdge (routing.ts) emits exactly two path
// grammars, `M sx,sy L tx,ty` and `M sx,sy C c1x,c1y c2x,c2y tx,ty`, so a path
// sampler is a short line/cubic evaluator.
//
// The budget these feed lives in the BROWSER lane (see
// tests/e2e/monitor/dashboard/test_topology_budget.py), not here: routeEdge
// routes against React Flow's *measured* node rects, and a unit test would
// have to assume node heights — assumed geometry certifies the wrong
// artifact, the same class of mistake as testing against a stale bundle.
// These functions are the shared VOCABULARY between that budget and this
// module's own unit tests (__tests__/topomeasure.test.ts), not a substitute
// for measuring the real thing.
import type { TopoEdge } from "../data/topology";
import type { Rect } from "./routing";

/** What the LAYOUT BUDGET counts, as opposed to what the canvas draws
 * (edgeStyles.ts's `EdgeClass`, a rendering concern). This is a product
 * ruling, not a detail:
 *
 * - "data-plane" — `provenance: "declared"` only (element<->element network
 *   links). This is the ONLY class the crossings/swallowed budget counts.
 * - "management" — `local`, `implicit` (hop-derived) and `reports-for`
 *   edges. These may freely cross behind an element; a faint management line
 *   passing behind a node is honest and unobtrusive, not clutter. An earlier
 *   round of this investigation counted them anyway and it inverted the
 *   ranking of every candidate layout.
 * - "tunnel" — `dynamic`. Counted separately; folded into neither of the
 *   above. */
export type EdgeCategory = "data-plane" | "management" | "tunnel";

export function classifyEdge(edge: TopoEdge): EdgeCategory {
  if (edge.provenance === "declared") return "data-plane";
  if (edge.provenance === "dynamic") return "tunnel";
  return "management"; // implicit | local | reports-for
}

export interface Point {
  x: number;
  y: number;
}

/** The minimal edge shape `countSwallowed`/`countCrossings` need: an id pair
 * to tell "meets at a shared node" from "crosses", and the rendered path. */
export interface PathEdge {
  id: string;
  source: string;
  target: string;
  path: string;
}

const NUMBER_RE = /-?\d*\.?\d+(?:e[-+]?\d+)?/gi;

/** The two path grammars `routeEdge` emits, as fixed-arity tuples. Naming the
 * arity is what lets `samplePath` destructure the parsed coordinates at all:
 * `nums.length === 4` buried in a compound condition states the invariant to
 * the reader but not to the type checker, which then types every destructured
 * coordinate `number | undefined` and lets the evaluators below compute
 * silent NaN. */
type LineCoords = readonly [sx: number, sy: number, tx: number, ty: number];
type CubicCoords = readonly [
  sx: number,
  sy: number,
  c1x: number,
  c1y: number,
  c2x: number,
  c2y: number,
  tx: number,
  ty: number,
];

/** A parsed coordinate is usable only if it is present AND finite. Honest
 * accounting of what each half buys, because neither is load-bearing today:
 *
 * - `!== undefined` is the type-checker's price for the arity check. `nums`
 *   comes from `.map(Number)` so it is dense, and the callers below check
 *   `length` first — it cannot fire. It is what carries "there are exactly
 *   four of them" into the destructure, which is the whole point of naming
 *   the invariant here instead of burying it in a compound condition.
 * - `Number.isFinite` CAN fire, but only on an absurd literal: a coordinate
 *   of 309-plus digits parses to `Infinity`. Not the exponent form —
 *   NUMBER_RE matches `1e999`, but `samplePath`'s command scan rejects the
 *   `e` as an unsupported command before any number is parsed, which makes
 *   NUMBER_RE's exponent branch unreachable from here. Kept because it costs
 *   one comparison at a parse boundary and the alternative is silent: an
 *   Infinity coordinate samples the whole edge to Infinity/NaN points, which
 *   `pointInRect` and `segmentsIntersect` both read as "no hit" — the budget
 *   passing by measuring nothing, which is the failure the header comment
 *   exists to prevent. */
function isCoord(v: number | undefined): v is number {
  return v !== undefined && Number.isFinite(v);
}

function lineCoords(nums: number[]): LineCoords | null {
  const [sx, sy, tx, ty] = nums;
  if (nums.length !== 4 || !isCoord(sx) || !isCoord(sy) || !isCoord(tx) || !isCoord(ty))
    return null;
  return [sx, sy, tx, ty];
}

function cubicCoords(nums: number[]): CubicCoords | null {
  const [sx, sy, c1x, c1y, c2x, c2y, tx, ty] = nums;
  if (
    nums.length !== 8 ||
    !isCoord(sx) ||
    !isCoord(sy) ||
    !isCoord(c1x) ||
    !isCoord(c1y) ||
    !isCoord(c2x) ||
    !isCoord(c2y) ||
    !isCoord(tx) ||
    !isCoord(ty)
  ) {
    return null;
  }
  return [sx, sy, c1x, c1y, c2x, c2y, tx, ty];
}

function sampleAt(n: number, at: (t: number) => Point): Point[] {
  const pts: Point[] = [];
  const count = Math.max(1, n);
  for (let i = 0; i < count; i++) {
    const t = count <= 1 ? 0 : i / (count - 1);
    pts.push(at(t));
  }
  return pts;
}

/** Evaluate the EXACT path grammar `routeEdge` emits — `M sx,sy L tx,ty` or
 * `M sx,sy C c1x,c1y c2x,c2y tx,ty` — into `n` evenly-spaced points along the
 * curve (by parameter `t`, not arc length; routeEdge's curves are gentle
 * enough that this is a faithful sample for crossing/containment tests).
 *
 * Throws on anything else, rather than silently skipping an unrecognised
 * command: a sampler that drops unknown commands returns FEWER points, and
 * the budget could pass by measuring nothing. */
export function samplePath(d: string, n: number): Point[] {
  const commands = d.match(/[A-Za-z]/g) ?? [];
  for (const c of commands) {
    if (c !== "M" && c !== "L" && c !== "C") {
      throw new Error(
        `samplePath: unsupported path command "${c}" in "${d}" — routeEdge only ever emits M/L/C`,
      );
    }
  }
  const nums = (d.match(NUMBER_RE) ?? []).map(Number);
  // `commands` is one letter per match, so joining it IS the grammar — "ML"
  // or "MC" — and says so more directly than a length check plus two
  // positional compares.
  const grammar = commands.join("");

  const line = grammar === "ML" ? lineCoords(nums) : null;
  if (line !== null) {
    const [sx, sy, tx, ty] = line;
    return sampleAt(n, (t) => ({ x: sx + (tx - sx) * t, y: sy + (ty - sy) * t }));
  }
  const cubic = grammar === "MC" ? cubicCoords(nums) : null;
  if (cubic !== null) {
    const [sx, sy, c1x, c1y, c2x, c2y, tx, ty] = cubic;
    return sampleAt(n, (t) => {
      const u = 1 - t;
      return {
        x: u * u * u * sx + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * tx,
        y: u * u * u * sy + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * ty,
      };
    });
  }
  throw new Error(`samplePath: unsupported path grammar "${d}" — expected "M..L.." or "M..C.."`);
}

function pointInRect(p: Point, r: Rect): boolean {
  return p.x >= r.x && p.x <= r.x + r.width && p.y >= r.y && p.y <= r.y + r.height;
}

/** Count edges with at least one sampled point inside a NON-ENDPOINT node's
 * rect — React Flow draws edges beneath nodes, so that point (and the rest
 * of the edge around it) is invisible to the user. An edge passing over its
 * OWN source/target is excluded: that is just the edge reaching its anchor,
 * not disappearing behind something else. */
export function countSwallowed(
  edges: PathEdge[],
  rects: Map<string, Rect>,
  samples: number,
): number {
  let count = 0;
  for (const edge of edges) {
    const pts = samplePath(edge.path, samples);
    let hit = false;
    for (const [nodeId, rect] of rects) {
      if (nodeId === edge.source || nodeId === edge.target) continue;
      if (pts.some((p) => pointInRect(p, rect))) {
        hit = true;
        break;
      }
    }
    if (hit) count++;
  }
  return count;
}

/** Sample density for crossing detection. Not exposed as a parameter (unlike
 * `countSwallowed`'s `samples`) because crossing detection needs no
 * per-caller tuning — 40 points resolves every crossing in both fixtures'
 * curves without the O(edges^2 * samples^2) cost growing unreasonably. */
const CROSSING_SAMPLES = 40;

function segmentsIntersect(p1: Point, p2: Point, p3: Point, p4: Point): boolean {
  const d1x = p2.x - p1.x;
  const d1y = p2.y - p1.y;
  const d2x = p4.x - p3.x;
  const d2y = p4.y - p3.y;
  const denom = d1x * d2y - d1y * d2x;
  if (Math.abs(denom) < 1e-9) return false; // parallel (or collinear)
  const t = ((p3.x - p1.x) * d2y - (p3.y - p1.y) * d2x) / denom;
  const u = ((p3.x - p1.x) * d1y - (p3.y - p1.y) * d1x) / denom;
  // Strict interior: an intersection AT t/u == 0 or 1 is two edges meeting at
  // a shared endpoint, not a visual crossing. A small margin (rather than a
  // bare 0/1 compare) absorbs float noise from the cubic evaluation.
  return t > 0.001 && t < 0.999 && u > 0.001 && u < 0.999;
}

/** One straight piece of a sampled polyline. The crossing test consumes
 * segments, not points; materialising them once means the intersection loop
 * below reads two real `Point`s per side instead of indexing `pts[k]` and
 * `pts[k + 1]` and hoping both are in range. */
type Segment = readonly [Point, Point];

/** Consecutive point pairs, in order — `n` points give `n - 1` segments, and
 * fewer than two points give none. */
function segmentsOf(pts: Point[]): Segment[] {
  const out: Segment[] = [];
  let prev: Point | null = null;
  for (const p of pts) {
    if (prev !== null) out.push([prev, p]);
    prev = p;
  }
  return out;
}

/** Count edge PAIRS (not sharing an endpoint) whose sampled polylines
 * intersect at least once — segment-intersection over consecutive sample
 * pairs, the same technique the layout-preview prototype used to measure
 * all twelve candidate layouts. */
export function countCrossings(edges: PathEdge[]): number {
  const polylines = edges.map((e) => ({
    source: e.source,
    target: e.target,
    segments: segmentsOf(samplePath(e.path, CROSSING_SAMPLES)),
  }));
  let count = 0;
  for (const [i, a] of polylines.entries()) {
    // Every LATER polyline, so each unordered pair is considered exactly
    // once — what the old `j = i + 1` inner loop said with an index.
    for (const b of polylines.slice(i + 1)) {
      if (
        a.source === b.source ||
        a.source === b.target ||
        a.target === b.source ||
        a.target === b.target
      ) {
        continue; // edges sharing an endpoint meet there — not a crossing
      }
      // `some` short-circuits on the first hit exactly as the old
      // `&& !found` loop conditions did.
      const crosses = a.segments.some(([a1, a2]) =>
        b.segments.some(([b1, b2]) => segmentsIntersect(a1, a2, b1, b2)),
      );
      if (crosses) count++;
    }
  }
  return count;
}
