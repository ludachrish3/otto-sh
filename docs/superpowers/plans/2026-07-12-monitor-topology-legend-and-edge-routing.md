# Monitor Topology — Legend, Hover Card & Edge Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the topology canvas explain itself (anchored legend + edge hover card) and stop edges from anchoring absurdly or disappearing behind nodes.

**Architecture:** One `EDGE_STYLES` table becomes the single source of truth for the edge encoding, consumed by both the renderer and the legend so the key cannot drift from the canvas. Edge geometry moves out of `LinkEdge` into a pure, unit-testable `routing.ts` that computes anchors from node rects (via `useInternalNode`) instead of React Flow's fixed left/right handles.

**Tech Stack:** React 19, TypeScript, `@xyflow/react` 12.11, react-aria-components 1.19, Tailwind v4, vitest + @testing-library/react, Playwright (Python, `tests/e2e/monitor/dashboard/`).

**Spec:** `docs/superpowers/specs/2026-07-12-monitor-topology-legend-and-edge-routing-design.md`

## Global Constraints

- **React Flow renders edges BENEATH nodes.** An edge overlapping a node box does not cross it — it *disappears behind it*. "Does this path pass under a box" is a correctness question, not an aesthetic one. This is why Task 2's occlusion test exists.
- **No new npm dependencies.** `react-aria-components` (already present) supplies `Disclosure`; `@xyflow/react` supplies `Panel`. The build is air-gapped — adding a package means an allowlist change, which is out of scope.
- **No `from __future__ import annotations`** in any Python touched (repo-wide ban).
- Per-task gate is **`make coverage`** from the repo root. A scoped `vitest` run passing does not mean the suite passes.
- Fresh worktrees need `uv sync` **and** `npm ci` in `web/` before the gates will run.
- Web lint/format is Biome: `cd web && npx biome check --write .` before committing.
- Commit style: conventional prefix, and every commit ends with the trailer `Assisted-by: Claude Opus 4.8`.
- Styling is **not** a test contract in this repo (`ui.test.tsx` says so explicitly). Assert roles, test ids, and geometry — never class names.

---

### Task 1: `EDGE_STYLES` — one style table, plus the tunnel casing

The encoding currently lives only inside `edgeStyle()` in `LinkEdge.tsx`. Lift it into a table that carries the *legend* text alongside the stroke, so Task 5's swatches render from the same source. Add the tunnel sleeve while we're in here.

**Files:**
- Create: `web/src/topo/edgeStyles.ts`
- Modify: `web/src/topo/LinkEdge.tsx` (delete `edgeStyle`, import it instead; draw the casing)
- Modify: `web/src/app.css` (new `--topo-edge-tunnel-casing` token)
- Test: `web/src/__tests__/topoedge.test.tsx` (exists — repoint the import, add exhaustiveness)

**Interfaces:**
- Consumes: `TopoEdge["provenance"]` from `web/src/data/topology.ts`.
- Produces: `EDGE_STYLES: Record<Provenance, EdgeStyleSpec>`, `EMPHASIS_WIDTH: number`, `edgeStyle(provenance, emphasized) => {stroke, strokeWidth, strokeDasharray?}`, and `type Provenance`. Tasks 3, 4 and 5 all import from here.

- [ ] **Step 1: Write the failing test**

Replace the whole of `web/src/__tests__/topoedge.test.tsx`:

```tsx
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
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd web && npx vitest run src/__tests__/topoedge.test.tsx
```

Expected: FAIL — `Failed to resolve import "../topo/edgeStyles"`.

- [ ] **Step 3: Create the table**

`web/src/topo/edgeStyles.ts`:

```ts
// Single source of truth for the topology edge encoding: LinkEdge draws from
// it, TopoLegend renders its swatches from it. A legend that restates these
// styles by hand drifts from the canvas the first time a stroke changes — and
// a wrong key is worse than no key at all.
import type { TopoEdge } from "../data/topology";

export type Provenance = TopoEdge["provenance"];

export interface EdgeStyleSpec {
  /** Legend row text. */
  label: string;
  /** One-line meaning; shown in the hover card. */
  hint: string;
  stroke: string;
  strokeWidth: number;
  strokeDasharray?: string;
  /** Wide, low-opacity sleeve drawn UNDER the core stroke. Tunnels only — no
   * other edge has a casing, so a dynamic link reads as something wrapped
   * *around* a path rather than as a peer of the other links. That is the
   * "topology overlay" Provenance.DYNAMIC's docstring already promises, bought
   * without touching the export contract. The cue is form, not colour: the
   * palette keeps colour reserved for health. */
  casing?: { stroke: string; strokeWidth: number; opacity: number };
}

export const EDGE_STYLES: Record<Provenance, EdgeStyleSpec> = {
  declared: {
    label: "declared",
    hint: "data-plane route from lab.json",
    stroke: "var(--topo-edge-declared)",
    strokeWidth: 2,
  },
  implicit: {
    label: "hop (implicit)",
    hint: "management path derived from hop",
    stroke: "var(--topo-edge-implicit)",
    strokeWidth: 1.5,
  },
  dynamic: {
    label: "tunnel",
    hint: "realized by an otto tunnel",
    stroke: "var(--topo-edge-declared)",
    strokeWidth: 2,
    strokeDasharray: "7 4",
    casing: { stroke: "var(--topo-edge-tunnel-casing)", strokeWidth: 7, opacity: 0.35 },
  },
  "reports-for": {
    label: "reports for",
    hint: "metrics sourced from a management host",
    stroke: "var(--topo-edge-reports)",
    strokeWidth: 1.5,
    strokeDasharray: "2 5",
  },
  local: {
    label: "from local",
    hint: "directly reachable from this machine",
    stroke: "var(--topo-edge-local)",
    strokeWidth: 1.5,
  },
};

/** Stroke-width delta when an edge is selected or hovered. */
export const EMPHASIS_WIDTH = 1.5;

/** The core stroke, as an inline SVG style object. Strokes are CSS custom
 * properties (app.css), not hex: inline style objects don't participate in
 * Tailwind's `dark:` variant, so the dark alternates flip via the vars'
 * `.dark` overrides instead. */
export function edgeStyle(
  provenance: Provenance,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string } {
  const spec = EDGE_STYLES[provenance];
  const base = {
    stroke: spec.stroke,
    strokeWidth: spec.strokeWidth,
    ...(spec.strokeDasharray === undefined ? {} : { strokeDasharray: spec.strokeDasharray }),
  };
  return emphasized ? { ...base, strokeWidth: base.strokeWidth + EMPHASIS_WIDTH } : base;
}
```

- [ ] **Step 4: Add the casing token**

In `web/src/app.css`, extend the existing topology blocks (do not create new `:root` / `.dark` rules — add to the ones already there):

```css
:root {
  --topo-edge-implicit: #9ca3af;
  --topo-edge-declared: #4b5563;
  --topo-edge-local: #d1d5db;
  --topo-edge-reports: #9ca3af;
  --topo-edge-tunnel-casing: #9ca3af;
}
.dark {
  --topo-edge-declared: #9ca3af;
  --topo-edge-local: #374151;
  --topo-edge-tunnel-casing: #4b5563;
}
```

- [ ] **Step 5: Point `LinkEdge` at the table and draw the casing**

In `web/src/topo/LinkEdge.tsx`: **delete** the local `edgeStyle` function and the `Provenance` type alias, and import instead. Then wrap the `<BaseEdge>` with the casing path. The `<g data-testid>` wrapper and `LinkEdgeData` stay exactly as they are.

```tsx
import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from "@xyflow/react";

import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES, edgeStyle } from "./edgeStyles";
```

and inside the returned `<g>`, immediately before `<BaseEdge …>`:

```tsx
{EDGE_STYLES[edge.provenance].casing && (
  <path
    d={path}
    fill="none"
    strokeLinecap="round"
    stroke={EDGE_STYLES[edge.provenance].casing?.stroke}
    strokeWidth={EDGE_STYLES[edge.provenance].casing?.strokeWidth}
    strokeOpacity={EDGE_STYLES[edge.provenance].casing?.opacity}
  />
)}
<BaseEdge id={id} path={path} style={edgeStyle(edge.provenance, selected ?? false)} />
```

Everything else in `LinkEdge` (the `fannedBezierPath` helper, `FAN_SCALE`, the impair pill) is untouched — Tasks 3 and 4 replace it.

- [ ] **Step 6: Run the tests**

```bash
cd web && npx vitest run src/__tests__/topoedge.test.tsx && npx tsc --noEmit && npx biome check --write .
```

Expected: 4 tests PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add web/src/topo/edgeStyles.ts web/src/topo/LinkEdge.tsx web/src/app.css web/src/__tests__/topoedge.test.tsx
git commit -m "feat(web): lift the topology edge encoding into one EDGE_STYLES table

Adds the tunnel casing (a wide low-opacity sleeve under the dashed core) so a
dynamic link reads as an overlay riding a path rather than as a peer of the
other links — the topology overlay Provenance.DYNAMIC already promises, with no
export-contract change.

Assisted-by: Claude Opus 4.8"
```

---

### Task 2: `routing.ts` — pure edge geometry, with the occlusion invariant

The load-bearing task. Pure functions, no React, so the "no edge hides behind a node" property is a plain unit test. **Nothing is wired up yet** — Task 3 does that.

**Files:**
- Create: `web/src/topo/routing.ts`
- Test: `web/src/__tests__/toporouting.test.ts`

**Interfaces:**
- Consumes: `ROW_H` from `web/src/topo/layout.ts`.
- Produces: `interface Rect { x, y, width, height }`, `interface EdgeGeometry { path: string; labelX: number; labelY: number }`, and `routeEdge(source: Rect, target: Rect, parallelIndex: number, groupSize: number) => EdgeGeometry`. Task 3 consumes exactly this.

- [ ] **Step 1: Write the failing test**

`web/src/__tests__/toporouting.test.ts`:

```ts
// The occlusion invariant is the point of this file. React Flow renders edges
// BENEATH nodes, so an edge that overlaps a box is not "a bit ugly" — it is
// invisible. These tests sample the returned path and assert it never enters a
// box it doesn't belong to.
import { describe, expect, it } from "vitest";

import { COL_W, ROW_H } from "../topo/layout";
import { type Rect, routeEdge } from "../topo/routing";

const W = 208; // element node: w-52
const H = 72; // element node height

/** kitchen-sink's real inter-element layout. db-01, edge-gw, mgmt-01 and
 * workers all have depth 1, so they stack in one column (rowOrder sorts by
 * id); chassis-a is the only depth-2 node. */
const NODES: Record<string, Rect> = {
  "db-01": { x: COL_W, y: 0 * ROW_H, width: W, height: H },
  "edge-gw": { x: COL_W, y: 1 * ROW_H, width: W, height: H },
  "mgmt-01": { x: COL_W, y: 2 * ROW_H, width: W, height: H },
  workers: { x: COL_W, y: 3 * ROW_H, width: W, height: H },
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
    // app-db and metrics-udp: workers <-> db-01, three rows apart, with
    // edge-gw and mgmt-01 sitting between them. This is the bug.
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
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd web && npx vitest run src/__tests__/toporouting.test.ts
```

Expected: FAIL — `Failed to resolve import "../topo/routing"`.

- [ ] **Step 3: Write the geometry**

`web/src/topo/routing.ts`:

```ts
// Pure edge geometry: which face a line leaves from, and what shape it takes
// to get there. No React, no React Flow — so the occlusion invariant is a
// plain unit test (see __tests__/toporouting.test.ts).
//
// THE CONSTRAINT: React Flow renders edges BENEATH nodes. An edge that
// overlaps a node box does not cross it — it disappears behind it. Every
// decision below follows from that.
import { ROW_H } from "./layout";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface EdgeGeometry {
  path: string;
  /** Apex of the curve — where a pill or hover card should sit. */
  labelX: number;
  labelY: number;
}

/** How far in from the box corner a bowed same-column edge anchors. Leaving
 * from the bottom *centre* instead would mean travelling half the box width
 * sideways before clearing it, and the gap to the next row is only 38px — the
 * curve physically cannot turn that fast. Leaning the anchor is what makes the
 * bow fit the EXISTING 72px gutter, with no layout change. */
const LEAN_INSET = 20;

/** Bow, as a fraction of the widest endpoint: 0.46 * 208 = 96px, against a
 * measured minimum of 79px to clear an intervening 208px box. Sized by
 * sampling the curve, not from the apex — the curve clips near the ANCHOR,
 * and a cubic's apex reach (0.75 * bow) flatters it. */
const BOW_FACTOR = 0.46;

/** Extra bow per parallel index. OUTWARD ONLY: a centred fan pushes the inner
 * sibling back under the very box the bow exists to clear. */
const FAN_STEP = 14;

/** Perpendicular fan for cross-column parallel edges — unchanged behaviour
 * (the old `curvature * FAN_SCALE`, i.e. 0.35 * 70). */
const CROSS_FAN = 24.5;

/** A cubic with both interior control points offset by k bulges only 0.75k. */
const BULGE = 0.75;

const centerX = (r: Rect): number => r.x + r.width / 2;
const centerY = (r: Rect): number => r.y + r.height / 2;

function cubic(
  sx: number,
  sy: number,
  c1x: number,
  c1y: number,
  c2x: number,
  c2y: number,
  tx: number,
  ty: number,
): string {
  return `M${sx},${sy} C${c1x},${c1y} ${c2x},${c2y} ${tx},${ty}`;
}

/** Same column: the peers stack vertically, so anchor bottom-face -> top-face
 * — the side of each box nearest the other. */
function routeSameColumn(source: Rect, target: Rect, parallelIndex: number): EdgeGeometry {
  const [upper, lower] = centerY(source) <= centerY(target) ? [source, target] : [target, source];
  const sy = upper.y + upper.height;
  const ty = lower.y;
  const rowSpan = Math.max(1, Math.round((lower.y - upper.y) / ROW_H));

  if (rowSpan <= 1) {
    // Adjacent rows: nothing sits between them, so a straight face-centre line
    // is both the shortest path and the natural one.
    const sx = centerX(upper);
    const tx = centerX(lower);
    return { path: `M${sx},${sy} L${tx},${ty}`, labelX: (sx + tx) / 2, labelY: (sy + ty) / 2 };
  }

  // Two or more rows apart: a straight line would run under every box between,
  // and be swallowed. Lean each anchor along its own face toward the gutter
  // (each rect uses its own width — a column can mix element and host nodes),
  // then bow out into that gutter.
  const sx = upper.x + upper.width - LEAN_INSET;
  const tx = lower.x + lower.width - LEAN_INSET;
  const bow = BOW_FACTOR * Math.max(upper.width, lower.width) + parallelIndex * FAN_STEP;
  // Always +x. The -x side of a column is where local's edges run, and the
  // deepest column always has a free right gutter.
  const anchorX = (sx + tx) / 2;
  const cx = anchorX + bow;
  const dy = ty - sy;
  return {
    path: cubic(sx, sy, cx, sy + dy / 3, cx, sy + (2 * dy) / 3, tx, ty),
    labelX: anchorX + BULGE * bow,
    labelY: sy + dy / 2,
  };
}

/** Different columns: the left node's right face -> the right node's left
 * face, with a perpendicular fan separating parallel links. */
function routeCrossColumn(
  source: Rect,
  target: Rect,
  parallelIndex: number,
  groupSize: number,
): EdgeGeometry {
  const [left, right] = centerX(source) <= centerX(target) ? [source, target] : [target, source];
  const sx = left.x + left.width;
  const sy = centerY(left);
  const tx = right.x;
  const ty = centerY(right);
  const offset = (parallelIndex - (groupSize - 1) / 2) * CROSS_FAN;
  const dx = tx - sx;
  const dy = ty - sy;
  const len = Math.hypot(dx, dy) || 1;
  const nx = (-dy / len) * offset;
  const ny = (dx / len) * offset;
  return {
    path: cubic(
      sx,
      sy,
      sx + dx / 3 + nx,
      sy + dy / 3 + ny,
      sx + (2 * dx) / 3 + nx,
      sy + (2 * dy) / 3 + ny,
      tx,
      ty,
    ),
    labelX: sx + dx / 2 + BULGE * nx,
    labelY: sy + dy / 2 + BULGE * ny,
  };
}

/** Route one edge between two node rects. Symmetric in `source`/`target`:
 * geometry, not graph direction, decides which faces are used. */
export function routeEdge(
  source: Rect,
  target: Rect,
  parallelIndex: number,
  groupSize: number,
): EdgeGeometry {
  const sameColumn = Math.abs(source.x - target.x) < 1;
  return sameColumn
    ? routeSameColumn(source, target, parallelIndex)
    : routeCrossColumn(source, target, parallelIndex, groupSize);
}
```

- [ ] **Step 4: Run the tests**

```bash
cd web && npx vitest run src/__tests__/toporouting.test.ts && npx tsc --noEmit && npx biome check --write .
```

Expected: 8 tests PASS. If "never routes a multi-row link under an intervening node" fails, the bow is too small — do **not** just raise `BOW_FACTOR` blindly; print `boxesUnder(...)` to see which box it clips and whether the clip is near the anchor (raise `LEAN_INSET`) or near the apex (raise `BOW_FACTOR`).

- [ ] **Step 5: Commit**

```bash
git add web/src/topo/routing.ts web/src/__tests__/toporouting.test.ts
git commit -m "feat(web): pure edge-routing geometry with an occlusion invariant

Same-column links anchor bottom-face to top-face; links spanning >=2 rows lean
their anchor toward the gutter and bow around the boxes between. Constants are
measured, not guessed: minimum bow to clear is 79px, we use 96px, and the whole
arc fits the existing 72px gutter — no layout change.

Tests sample the returned path and assert it never enters a node rect it does
not belong to. React Flow draws edges beneath nodes, so an overlapping edge is
not ugly, it is invisible.

Assisted-by: Claude Opus 4.8"
```

---

### Task 3: Wire the routing into `LinkEdge`

Replace the handle-derived coordinates with rects measured from the nodes themselves. This is the commit where `db-01`'s edges stop looking wrong.

**Files:**
- Modify: `web/src/topo/LinkEdge.tsx` (delete `fannedBezierPath` + `FAN_SCALE`; use `routeEdge`)
- Test: `web/src/__tests__/toporouting.test.ts` (already covers the geometry — no new unit test; Task 6's browser lane proves the wiring)

**Interfaces:**
- Consumes: `routeEdge`, `Rect` (Task 2); `EDGE_STYLES`, `edgeStyle` (Task 1).
- Produces: `LinkEdge` now renders paths from node rects. `LinkEdgeData` is unchanged (`{ edge: TopoEdge; groupSize: number }`).

- [ ] **Step 1: Rewrite `LinkEdge.tsx`**

The `fannedBezierPath` helper, `FAN_SCALE`, and the long adaptation comment about `getBezierPath` all go — `routing.ts` supersedes them. Keep the `<g data-testid>` wrapper (Playwright's contract) and the impair pill exactly as-is; Task 4 changes the pill.

```tsx
// Custom edge: provenance-styled path with a tunnel casing and an impair pill.
// Wrapped in <g data-testid> so Playwright can click/assert edges — BaseEdge's
// own prop passthrough is not part of our contract.
//
// Anchors come from the NODE RECTS (useInternalNode), not from the handles.
// nodes.tsx only exposes a left target and a right source, so a link between
// two nodes in the same column used to leave a right face and re-enter a left
// face, swinging across the column and back. routing.ts picks the face nearest
// the peer instead, and bows around anything in the way.
import { BaseEdge, EdgeLabelRenderer, type EdgeProps, useInternalNode } from "@xyflow/react";

import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES, edgeStyle } from "./edgeStyles";
import { type Rect, routeEdge } from "./routing";

export interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  [key: string]: unknown;
}

type InternalNode = ReturnType<typeof useInternalNode>;

function rectOf(node: InternalNode): Rect | null {
  if (node === undefined) return null;
  const { width, height } = node.measured;
  if (width === undefined || height === undefined) return null;
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width,
    height,
  };
}

export function LinkEdge(props: EdgeProps) {
  const { id, source, target, sourceX, sourceY, targetX, targetY, selected } = props;
  const data = props.data as unknown as LinkEdgeData;
  const { edge, groupSize } = data;

  const sourceRect = rectOf(useInternalNode(source));
  const targetRect = rectOf(useInternalNode(target));

  // React Flow only renders an edge once BOTH endpoints are measured, so in
  // practice the rects are always there. The straight-line fallback keeps this
  // total rather than throwing if that ever changes.
  const geom =
    sourceRect !== null && targetRect !== null
      ? routeEdge(sourceRect, targetRect, edge.parallelIndex, groupSize)
      : {
          path: `M${sourceX},${sourceY} L${targetX},${targetY}`,
          labelX: (sourceX + targetX) / 2,
          labelY: (sourceY + targetY) / 2,
        };

  const casing = EDGE_STYLES[edge.provenance].casing;
  return (
    <g data-testid={`topo-link-${edge.id}`}>
      {casing && (
        <path
          d={geom.path}
          fill="none"
          strokeLinecap="round"
          stroke={casing.stroke}
          strokeWidth={casing.strokeWidth}
          strokeOpacity={casing.opacity}
        />
      )}
      <BaseEdge id={id} path={geom.path} style={edgeStyle(edge.provenance, selected ?? false)} />
      {edge.impair !== null && (
        <EdgeLabelRenderer>
          <span
            data-testid={`topo-impair-${edge.id}`}
            style={{
              transform: `translate(-50%, -50%) translate(${geom.labelX}px, ${geom.labelY}px)`,
            }}
            className="absolute rounded-full border border-gray-300 bg-white px-1.5 py-0.5
              text-[10px] text-gray-500 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-400"
          >
            impair · {edge.impair}
          </span>
        </EdgeLabelRenderer>
      )}
    </g>
  );
}
```

- [ ] **Step 2: Run the whole web suite + typecheck**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check --write .
```

Expected: PASS. `topoedge.test.tsx` no longer imports anything from `LinkEdge`, so nothing should break.

- [ ] **Step 3: Verify it in the browser before believing it**

```bash
cd /home/vagrant/otto-sh && make coverage
```

Expected: green, including the existing `test_link_inspector_and_parallel_edges` Playwright test (it clicks an edge stroke — the stroke has moved, so this is a real check that the new paths are still hittable).

- [ ] **Step 4: Commit**

```bash
git add web/src/topo/LinkEdge.tsx
git commit -m "fix(web): anchor topology edges to the face nearest the peer

nodes.tsx exposes only a left target handle and a right source handle, so a
link between two same-column nodes (db-01 and its depth-1 peers in kitchen-sink)
left a right face and re-entered a left face, swinging across the column and
back — and was then swallowed by the boxes it passed under, since React Flow
draws edges beneath nodes.

Anchors now come from the node rects via useInternalNode, and routeEdge picks
the nearest faces.

Assisted-by: Claude Opus 4.8"
```

---

### Task 4: Edge hover card

Per-edge identity without a click. Also the first thing in this view that renders the *degenerate* edges (`reports-for` has no `link`; implicit hop groups carry `links[]`, not `link`) as something other than a raw id.

**Files:**
- Create: `web/src/topo/linkText.ts`
- Create: `web/src/topo/ImpairPill.tsx`
- Create: `web/src/topo/EdgeHoverCard.tsx`
- Modify: `web/src/topo/LinkEdge.tsx` (hover state; use the two new components)
- Modify: `web/src/topo/LinkInspector.tsx` (import the shared `endpointText`; delete its private copy)
- Test: `web/src/__tests__/topohover.test.tsx`

**Interfaces:**
- Consumes: `EDGE_STYLES` (Task 1), `routeEdge` (Task 2).
- Produces: `endpointText(link) => string`, `edgeTitle(edge) => string`, `edgeSubtitle(edge) => string` from `linkText.ts`; `<ImpairPill impair?={string} testId?={string} x?={number} y?={number} />`; `<EdgeHoverCard edge x y />`. Task 5's legend imports `ImpairPill`.

- [ ] **Step 1: Write the failing test**

`web/src/__tests__/topohover.test.tsx`:

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { EdgeHoverCard } from "../topo/EdgeHoverCard";
import { edgeSubtitle, edgeTitle } from "../topo/linkText";

afterEach(cleanup);

const appDbLink: LinkSnapshot = {
  id: "app-db",
  name: "app-db",
  protocol: "tcp",
  provenance: "declared",
  endpoints: [
    { host: "workers_w1", interface: "eth0", ip: "10.20.2.21" },
    { host: "db-01", interface: "eth0", ip: "10.20.3.31" },
  ],
};

const declared: TopoEdge = {
  id: "app-db",
  source: "workers",
  target: "db-01",
  provenance: "declared",
  link: appDbLink,
  impair: null,
  parallelIndex: 0,
};

const hopGroup: TopoEdge = {
  id: "implicit:chassis-a~edge-gw",
  source: "edge-gw",
  target: "chassis-a",
  provenance: "implicit",
  links: [appDbLink, appDbLink, appDbLink],
  impair: null,
  parallelIndex: 0,
};

const reports: TopoEdge = {
  id: "reports:mgmt-01~chassis-a",
  source: "mgmt-01",
  target: "chassis-a",
  provenance: "reports-for",
  impair: null,
  parallelIndex: 0,
};

describe("edge text", () => {
  it("names a declared link by its name", () => {
    expect(edgeTitle(declared)).toBe("app-db");
    expect(edgeSubtitle(declared)).toBe("declared · tcp");
  });

  // The inspector renders these degenerate edges as a raw id today. The hover
  // card must not repeat that.
  it("summarises a collapsed hop group rather than showing its synthetic id", () => {
    expect(edgeTitle(hopGroup)).toBe("edge-gw ⇄ chassis-a");
    expect(edgeSubtitle(hopGroup)).toBe("hop (implicit) · 3 links");
  });

  it("describes a reports-for edge, which has no link at all", () => {
    expect(edgeTitle(reports)).toBe("mgmt-01 → chassis-a");
    expect(edgeSubtitle(reports)).toBe("reports for");
  });
});

describe("EdgeHoverCard", () => {
  it("names the link and its endpoints", () => {
    render(<EdgeHoverCard edge={declared} x={10} y={20} />);
    const card = screen.getByTestId("topo-hover-app-db");
    expect(card.textContent).toContain("app-db");
    expect(card.textContent).toContain("tcp");
    expect(card.textContent).toContain("10.20.2.21");
  });

  it("renders for an edge with no link", () => {
    render(<EdgeHoverCard edge={reports} x={0} y={0} />);
    expect(screen.getByTestId("topo-hover-reports:mgmt-01~chassis-a").textContent).toContain(
      "reports for",
    );
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd web && npx vitest run src/__tests__/topohover.test.tsx
```

Expected: FAIL — cannot resolve `../topo/EdgeHoverCard`.

- [ ] **Step 3: Write the text helpers**

`web/src/topo/linkText.ts`:

```ts
// Human text for an edge. Shared by the hover card and the inspector so the
// two never disagree about what a link is called.
import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES } from "./edgeStyles";

export function endpointText(link: LinkSnapshot): string {
  return link.endpoints
    .map((ep) => {
      const iface = ep.interface ? ` ${ep.interface}` : "";
      const addr = ep.ip ? ` · ${ep.ip}${ep.port != null ? `:${ep.port}` : ""}` : "";
      return `${ep.host}${iface}${addr}`;
    })
    .join("  ⇄  ");
}

function primaryLink(edge: TopoEdge): LinkSnapshot | null {
  return edge.link ?? edge.links?.[0] ?? null;
}

/** Not every edge has a link. `reports-for` never does, and a collapsed hop
 * group carries `links[]` with a synthetic id — showing that id would be
 * noise, so name the pair instead. */
export function edgeTitle(edge: TopoEdge): string {
  if (edge.provenance === "reports-for") return `${edge.source} → ${edge.target}`;
  if (edge.provenance === "local") return `local → ${edge.target}`;
  if (edge.links !== undefined && edge.links.length > 1) return `${edge.source} ⇄ ${edge.target}`;
  const link = primaryLink(edge);
  return link?.name ?? link?.id ?? edge.id;
}

export function edgeSubtitle(edge: TopoEdge): string {
  const label = EDGE_STYLES[edge.provenance].label;
  if (edge.links !== undefined && edge.links.length > 1) {
    return `${label} · ${edge.links.length} links`;
  }
  const protocol = primaryLink(edge)?.protocol;
  return protocol ? `${label} · ${protocol}` : label;
}
```

- [ ] **Step 4: Write the pill and the card**

`web/src/topo/ImpairPill.tsx`:

```tsx
// The impair marker. Positioned on the canvas; bare in the legend — one
// component so the key and the thing it explains cannot diverge.
export function ImpairPill(props: {
  impair?: string;
  testId?: string;
  x?: number;
  y?: number;
}) {
  const { impair, testId, x, y } = props;
  const positioned = x !== undefined && y !== undefined;
  return (
    <span
      data-testid={testId}
      style={
        positioned ? { transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` } : undefined
      }
      className={`${positioned ? "absolute " : ""}rounded-full border border-gray-300 bg-white
        px-1.5 py-0.5 text-[10px] whitespace-nowrap text-gray-500 dark:border-gray-700
        dark:bg-gray-950 dark:text-gray-400`}
    >
      {impair === undefined ? "impair" : `impair · ${impair}`}
    </span>
  );
}
```

`web/src/topo/EdgeHoverCard.tsx`:

```tsx
// Per-edge identity without a click. Anchored at the CURVE APEX rather than at
// the cursor: the position is then deterministic (so Playwright can assert it)
// and it cannot jitter under the pointer. pointer-events:none — hovering must
// never steal the click that opens the inspector.
import type { TopoEdge } from "../data/topology";
import { edgeSubtitle, edgeTitle, endpointText } from "./linkText";

export function EdgeHoverCard(props: { edge: TopoEdge; x: number; y: number }) {
  const { edge, x, y } = props;
  const link = edge.link ?? edge.links?.[0] ?? null;
  return (
    <div
      data-testid={`topo-hover-${edge.id}`}
      style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}
      className="pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg border
        border-gray-200 bg-white px-2.5 py-1.5 shadow-md dark:border-gray-800 dark:bg-gray-950"
    >
      <p className="text-xs font-semibold whitespace-nowrap">{edgeTitle(edge)}</p>
      <p className="text-[10px] whitespace-nowrap text-gray-500 dark:text-gray-400">
        {edgeSubtitle(edge)}
      </p>
      {link && (
        <p className="font-mono text-[10px] whitespace-nowrap text-gray-500 dark:text-gray-400">
          {endpointText(link)}
        </p>
      )}
      {edge.impair !== null && (
        <p className="text-[10px] whitespace-nowrap text-gray-500 dark:text-gray-400">
          in-path middlebox: {edge.impair}
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Hook hover into `LinkEdge`**

Replace the whole of `web/src/topo/LinkEdge.tsx` with this. It is Task 3's file plus hover state, minus the inline pill markup (now `ImpairPill`).

```tsx
// Custom edge: provenance-styled path with a tunnel casing, an impair pill and
// a hover card. Wrapped in <g data-testid> so Playwright can click/assert edges
// — BaseEdge's own prop passthrough is not part of our contract.
//
// Anchors come from the NODE RECTS (useInternalNode), not from the handles.
// nodes.tsx only exposes a left target and a right source, so a link between
// two nodes in the same column used to leave a right face and re-enter a left
// face, swinging across the column and back. routing.ts picks the face nearest
// the peer instead, and bows around anything in the way.
//
// The mouse handlers sit on the <g>, not on the path: React Flow's wide
// invisible interaction path is a CHILD of it, and that is what the pointer
// actually meets.
import { BaseEdge, EdgeLabelRenderer, type EdgeProps, useInternalNode } from "@xyflow/react";
import { useState } from "react";

import type { TopoEdge } from "../data/topology";
import { EdgeHoverCard } from "./EdgeHoverCard";
import { EDGE_STYLES, edgeStyle } from "./edgeStyles";
import { ImpairPill } from "./ImpairPill";
import { type Rect, routeEdge } from "./routing";

export interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  [key: string]: unknown;
}

type InternalNode = ReturnType<typeof useInternalNode>;

function rectOf(node: InternalNode): Rect | null {
  if (node === undefined) return null;
  const { width, height } = node.measured;
  if (width === undefined || height === undefined) return null;
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width,
    height,
  };
}

export function LinkEdge(props: EdgeProps) {
  const { id, source, target, sourceX, sourceY, targetX, targetY, selected } = props;
  const data = props.data as unknown as LinkEdgeData;
  const { edge, groupSize } = data;
  const [hovered, setHovered] = useState(false);

  const sourceRect = rectOf(useInternalNode(source));
  const targetRect = rectOf(useInternalNode(target));

  // React Flow only renders an edge once BOTH endpoints are measured, so in
  // practice the rects are always there. The straight-line fallback keeps this
  // total rather than throwing if that ever changes.
  const geom =
    sourceRect !== null && targetRect !== null
      ? routeEdge(sourceRect, targetRect, edge.parallelIndex, groupSize)
      : {
          path: `M${sourceX},${sourceY} L${targetX},${targetY}`,
          labelX: (sourceX + targetX) / 2,
          labelY: (sourceY + targetY) / 2,
        };

  const casing = EDGE_STYLES[edge.provenance].casing;
  const emphasized = (selected ?? false) || hovered;
  return (
    <g
      data-testid={`topo-link-${edge.id}`}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {casing && (
        <path
          d={geom.path}
          fill="none"
          strokeLinecap="round"
          stroke={casing.stroke}
          strokeWidth={casing.strokeWidth}
          strokeOpacity={casing.opacity}
        />
      )}
      <BaseEdge id={id} path={geom.path} style={edgeStyle(edge.provenance, emphasized)} />
      <EdgeLabelRenderer>
        {hovered ? (
          // The card replaces the pill rather than stacking on it — both want
          // the same point on the curve.
          <EdgeHoverCard edge={edge} x={geom.labelX} y={geom.labelY} />
        ) : (
          edge.impair !== null && (
            <ImpairPill
              impair={edge.impair}
              testId={`topo-impair-${edge.id}`}
              x={geom.labelX}
              y={geom.labelY}
            />
          )
        )}
      </EdgeLabelRenderer>
    </g>
  );
}
```

- [ ] **Step 6: De-duplicate `endpointText` in the inspector**

In `web/src/topo/LinkInspector.tsx`, delete the private `endpointText` function and the now-unused `LinkSnapshot` import, and import the shared one:

```tsx
import { endpointText } from "./linkText";
```

- [ ] **Step 7: Run the tests**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check --write .
```

Expected: PASS, including the existing `linkinspector.test.tsx` (the shared `endpointText` is byte-identical to the one it had).

- [ ] **Step 8: Commit**

```bash
git add web/src/topo/linkText.ts web/src/topo/ImpairPill.tsx web/src/topo/EdgeHoverCard.tsx \
        web/src/topo/LinkEdge.tsx web/src/topo/LinkInspector.tsx web/src/__tests__/topohover.test.tsx
git commit -m "feat(web): name the link under the cursor on hover

Hovering an edge thickens it and shows name / kind / protocol / endpoints at
the curve apex; click still opens the full inspector. Handles the degenerate
edges the inspector currently botches: reports-for has no link at all, and a
collapsed hop group carries links[] with a synthetic id.

Assisted-by: Claude Opus 4.8"
```

---

### Task 5: The anchored legend

**Files:**
- Create: `web/src/ui/Disclosure.tsx`
- Create: `web/src/topo/TopoLegend.tsx`
- Modify: `web/src/topo/TopologyPage.tsx` (mount it inside `<ReactFlow>`)
- Test: `web/src/__tests__/topolegend.test.tsx`

**Interfaces:**
- Consumes: `EDGE_STYLES` + `Provenance` (Task 1), `ImpairPill` (Task 4), `STATUS_DOT` and `EffectiveStatus` (already exported from `topo/nodes.tsx` and `data/topology.ts`).
- Produces: `<Disclosure title defaultExpanded testId toggleTestId>`, `<TopoLegend />`.

- [ ] **Step 1: Write the failing test**

`web/src/__tests__/topolegend.test.tsx`:

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ReactFlow, ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoEdge } from "../data/topology";
import { TopoLegend } from "../topo/TopoLegend";

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

const PROVENANCES: TopoEdge["provenance"][] = [
  "declared",
  "implicit",
  "dynamic",
  "reports-for",
  "local",
];

describe("TopoLegend", () => {
  it("explains every line style and every status colour", () => {
    renderLegend();
    for (const p of PROVENANCES) {
      expect(screen.getByTestId(`topo-legend-link-${p}`)).toBeTruthy();
    }
    for (const s of ["ok", "down", "unreachable", "no-data", "unknown"]) {
      expect(screen.getByTestId(`topo-legend-status-${s}`)).toBeTruthy();
    }
    expect(screen.getByTestId("topo-legend-link-impair")).toBeTruthy();
  });

  it("starts expanded and collapses on click", () => {
    renderLegend();
    const toggle = screen.getByTestId("topo-legend-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd web && npx vitest run src/__tests__/topolegend.test.tsx
```

Expected: FAIL — cannot resolve `../topo/TopoLegend`.

- [ ] **Step 3: Write the Disclosure primitive**

`web/src/ui/Disclosure.tsx`:

```tsx
// Collapsible section. react-aria supplies the button semantics and
// aria-expanded; the visual is a compact header row + panel, matching the
// other ui/ primitives (open-code Untitled UI: React Aria + Tailwind).
import type { ReactNode } from "react";
import { Button, Disclosure as AriaDisclosure, DisclosurePanel } from "react-aria-components";

export function Disclosure(props: {
  title: string;
  defaultExpanded?: boolean;
  testId?: string;
  toggleTestId?: string;
  children: ReactNode;
}) {
  const { title, defaultExpanded = true, testId, toggleTestId, children } = props;
  return (
    <AriaDisclosure
      defaultExpanded={defaultExpanded}
      data-testid={testId}
      className="overflow-hidden rounded-lg border border-gray-200 bg-white shadow-sm
        dark:border-gray-800 dark:bg-gray-950"
    >
      <Button
        slot="trigger"
        data-testid={toggleTestId}
        className="flex w-full cursor-pointer items-center gap-2 px-2.5 py-1.5 text-[11px]
          font-semibold tracking-wide text-gray-500 uppercase outline-none hover:bg-gray-50
          dark:text-gray-400 dark:hover:bg-gray-900"
      >
        {title}
        <span aria-hidden className="ml-auto text-[9px] text-gray-400">
          ▾
        </span>
      </Button>
      <DisclosurePanel className="border-t border-gray-100 dark:border-gray-800">
        {children}
      </DisclosurePanel>
    </AriaDisclosure>
  );
}
```

- [ ] **Step 4: Write the legend**

`web/src/topo/TopoLegend.tsx`:

```tsx
// The anchored key for the topology canvas.
//
// Bottom-left, NOT right: LinkInspector is a `fixed inset-y-0 right-0 w-96`
// aside, so a right-anchored panel would be covered the moment an edge is
// selected. `mb-28` lifts it clear of React Flow's own zoom Controls, which
// occupy the same corner.
//
// Every swatch renders from EDGE_STYLES / STATUS_DOT — the same tables the
// canvas draws from — so the key cannot drift from what it explains.
import { Panel } from "@xyflow/react";

import type { EffectiveStatus } from "../data/topology";
import { Disclosure } from "../ui/Disclosure";
import { EDGE_STYLES, type Provenance } from "./edgeStyles";
import { ImpairPill } from "./ImpairPill";
import { STATUS_DOT } from "./nodes";

const LINK_ORDER: Provenance[] = ["declared", "implicit", "dynamic", "reports-for", "local"];
const STATUS_ORDER: EffectiveStatus[] = ["ok", "down", "unreachable", "no-data", "unknown"];
const STATUS_LABEL: Record<EffectiveStatus, string> = {
  ok: "ok",
  down: "down",
  unreachable: "unreachable",
  "no-data": "no data",
  unknown: "unknown",
};

const ROW = "flex items-center gap-2 py-0.5 text-[11px] text-gray-600 dark:text-gray-300";
const HEAD = "mb-1 text-[10px] font-semibold tracking-wide text-gray-400 uppercase";

function Swatch({ provenance }: { provenance: Provenance }) {
  const spec = EDGE_STYLES[provenance];
  return (
    <svg width="30" height="10" aria-hidden className="shrink-0">
      {spec.casing && (
        <path
          d="M0,5 L30,5"
          fill="none"
          strokeLinecap="round"
          stroke={spec.casing.stroke}
          strokeWidth={spec.casing.strokeWidth}
          strokeOpacity={spec.casing.opacity}
        />
      )}
      <path
        d="M0,5 L30,5"
        fill="none"
        stroke={spec.stroke}
        strokeWidth={spec.strokeWidth}
        strokeDasharray={spec.strokeDasharray}
      />
    </svg>
  );
}

export function TopoLegend() {
  return (
    <Panel position="bottom-left" className="!mb-28">
      <Disclosure title="Key" testId="topo-legend" toggleTestId="topo-legend-toggle">
        <div className="grid grid-cols-2">
          <ul className="border-r border-gray-100 p-2 dark:border-gray-800">
            <li className={HEAD}>Links</li>
            {LINK_ORDER.map((p) => (
              <li key={p} data-testid={`topo-legend-link-${p}`} className={ROW}>
                <Swatch provenance={p} />
                {EDGE_STYLES[p].label}
              </li>
            ))}
            <li data-testid="topo-legend-link-impair" className={ROW}>
              <span className="flex w-[30px] shrink-0 justify-center">
                <ImpairPill />
              </span>
              middlebox
            </li>
          </ul>
          <ul className="p-2">
            <li className={HEAD}>Status</li>
            {STATUS_ORDER.map((s) => (
              <li key={s} data-testid={`topo-legend-status-${s}`} className={ROW}>
                <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[s]}`} />
                {STATUS_LABEL[s]}
              </li>
            ))}
          </ul>
        </div>
      </Disclosure>
    </Panel>
  );
}
```

- [ ] **Step 5: Mount it**

In `web/src/topo/TopologyPage.tsx`, add the import and drop it in beside `<Controls>`:

```tsx
import { TopoLegend } from "./TopoLegend";
```

```tsx
            <Controls showInteractive={false} />
            <TopoLegend />
```

- [ ] **Step 6: Run the tests**

```bash
cd web && npx vitest run && npx tsc --noEmit && npx biome check --write .
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/ui/Disclosure.tsx web/src/topo/TopoLegend.tsx web/src/topo/TopologyPage.tsx \
        web/src/__tests__/topolegend.test.tsx
git commit -m "feat(web): anchored, collapsible key for the topology canvas

Bottom-left (a right-anchored panel would be covered by LinkInspector's
full-height aside on the first edge click), collapsible via react-aria
Disclosure, expanded by default. Swatches render from EDGE_STYLES and
STATUS_DOT, so the key cannot drift from the canvas it explains.

Assisted-by: Claude Opus 4.8"
```

---

### Task 6: Prove it in the browser

The unit tests prove the geometry; only a real browser proves the wiring. Follows the existing conventions in this file — `_import_fixture`, `_wait_for_links` (edges appear a beat after the page shell; `count()` does not retry).

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_review_shell.py` (append one test)

**Interfaces:**
- Consumes: test ids `topo-legend`, `topo-legend-toggle`, `topo-legend-link-<provenance>`, `topo-legend-status-<status>`, `topo-hover-<edge id>`, `topo-link-<edge id>` (Tasks 1, 4, 5).

- [ ] **Step 1: Append the test**

At the end of `tests/e2e/monitor/dashboard/test_review_shell.py`:

```python
def test_topology_legend_hover_and_tunnel_casing(shell_dash, page):
    """The canvas explains itself: an anchored key decodes every line style and
    status colour, hovering an edge names the link under the cursor without
    opening the inspector, and a tunnel carries its casing."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)

    legend = page.locator('[data-testid="topo-legend"]')
    legend.wait_for()
    for provenance in ("declared", "implicit", "dynamic", "reports-for", "local"):
        assert legend.locator(f'[data-testid="topo-legend-link-{provenance}"]').count() == 1
    for status in ("ok", "down", "unreachable", "no-data", "unknown"):
        assert legend.locator(f'[data-testid="topo-legend-status-{status}"]').count() == 1

    toggle = page.locator('[data-testid="topo-legend-toggle"]')
    assert toggle.get_attribute("aria-expanded") == "true"
    toggle.click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"topo-legend-toggle\"]')"
        ".getAttribute('aria-expanded') === 'false'"
    )

    # The tunnel's casing: a second, wider, translucent stroke on the same path.
    # No other provenance draws one.
    tunnel = page.locator('[data-testid="topo-link-tun-demo"] path[stroke-opacity]')
    assert tunnel.count() == 1

    # Hovering an edge names it — and does NOT open the inspector.
    stroke = page.locator('[data-testid="topo-link-app-db"] path.react-flow__edge-interaction')
    stroke.hover(force=True)
    card = page.locator('[data-testid="topo-hover-app-db"]')
    card.wait_for()
    assert "app-db" in card.inner_text()
    assert page.locator('[data-testid="link-inspector"]').count() == 0
```

- [ ] **Step 2: Run the dashboard lane**

```bash
cd /home/vagrant/otto-sh && uv run pytest tests/e2e/monitor/dashboard/test_review_shell.py -k "legend_hover" -v
```

Expected: PASS on each browser. If the hover card doesn't appear, check that the `<g>` — not the `<path>` — carries the mouse handlers: React Flow's interaction path is a child of it.

- [ ] **Step 3: Run the full gate**

```bash
cd /home/vagrant/otto-sh && make coverage
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "test(e2e): topology legend, edge hover card and tunnel casing

Assisted-by: Claude Opus 4.8"
```

---

### Task 7: Record what we deliberately did not do

The two deferred designs are decisions, not omissions. If they only live in the spec they will be re-litigated from scratch.

**Files:**
- Modify: `todo/monitor-topology-followups.md`

- [ ] **Step 1: Append to the follow-ups file**

Add under a new heading, and **delete follow-up item 2's hover half** (the hover card now handles degenerate edges; the *inspector* still doesn't, so reword rather than remove):

```markdown
## Deferred from the legend + routing spec (2026-07-12)

7. **Static-link layering ("D\*").** Push a hub's peers one column deeper so
   same-column links become forward links — Chris's idea, and it makes db-01's
   peers read as downstream. Layer on **declared + implicit links only**: if a
   *dynamic* link gets a vote, a tunnel coming up shoves edge-gw and chassis-a a
   whole column sideways and the map reflows when the network changes. Needs its
   own design: cycle-safe layer assignment, routing for the skip-column edges it
   creates (`local` is attached to every `hop == null` element, so pushing any of
   them past column 1 *forces* a local edge to skip a column), and a decision on
   redefining the x-axis away from "hops from local" — under D\*, `workers` is 1
   hop from local but sits 2 columns out. Measured on kitchen-sink: layering on
   ALL links takes same-column edges 2 → 0 but swallowed edges 0 → 3. Net worse.
   The static-only variant is 1 and 1.

8. **Tunnels as overlays on their underlay links.** `Tunnel` already carries its
   ordered chain (`path: tuple[TunnelHop, ...]`, `src/otto/tunnel/model.py`) but
   the monitor export **discards it** — a tunnel is flattened to a two-endpoint
   `LinkSnapshot` with `provenance: "dynamic"`. Rendering a tunnel riding the
   links it traverses needs that path exported (a `format:1` change: schema,
   `export.gen.ts`, generator, fixtures, drift guards), then a rule mapping each
   consecutive hop-pair onto an existing link and drawing a bare segment where
   none exists. Note it would not change kitchen-sink at all: `tun-demo` runs
   edge-gw ⇄ db-01, between which no declared or implicit link exists — there is
   nothing to wrap. Exercising it needs a multi-hop tunnel in the fixture. The
   grey casing (shipped) is the cheap stand-in.
```

- [ ] **Step 2: Reword item 2**

Item 2 currently reads "Link-less edges (intra `hop:*`, reports-for) open a degenerate inspector…". Append to it:

```markdown
   *(2026-07-12: the **hover card** now handles these — see `linkText.ts`
   `edgeTitle`/`edgeSubtitle`. The **inspector** still shows a raw id.)*
```

- [ ] **Step 3: Commit**

```bash
git add todo/monitor-topology-followups.md
git commit -m "docs: record the two topology designs deferred from the legend spec

Assisted-by: Claude Opus 4.8"
```

---

## Done when

- `make coverage` is green.
- Opening `#/topology` on `kitchen-sink` shows a **Key** panel bottom-left; collapsing it works.
- `db-01`'s link to `edge-gw` is a short vertical line from `db-01`'s bottom edge to `edge-gw`'s top edge.
- `db-01`'s two links to `workers` arc out to the right of the column and **no part of them is hidden** behind `edge-gw` or `mgmt-01`.
- `tun-demo` has a visible grey sleeve around it.
- Hovering any edge names it; clicking still opens the inspector.
