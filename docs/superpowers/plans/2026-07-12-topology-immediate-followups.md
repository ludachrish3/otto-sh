# Topology Immediate Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the topology canvas's five edge provenances into three visual
classes, stop link-less edges opening a degenerate inspector, stop the inspector
occluding the review bar at narrow widths, guard the Escape listener, and drop
the local node's "you are here" caption.

**Architecture:** The data model does not change — `TopoEdge.provenance` keeps
all five values and the `format:1` export contract is untouched. A new
`EdgeClass` ("static" | "tunnel" | "reports-for") sits between provenance and
style, and everything that *draws* (LinkEdge, TopoLegend, the hover card's
subtitle) goes through it. Inspector selection stops asking about provenance and
starts asking whether an edge carries a `LinkSnapshot`. The inspector aside moves
from a viewport-`fixed` element with a hardcoded chrome offset to an element
`absolute`-positioned inside the canvas box, which removes the constant entirely.

**Tech Stack:** React 19, TypeScript, `@xyflow/react` (React Flow), Tailwind v4,
Vitest + Testing Library, Playwright (via pytest) for the dashboard lane.

**Spec:** `docs/superpowers/specs/2026-07-12-topology-immediate-followups-design.md`
(committed as `7fa4be4`).

## Global Constraints

- **Never `from __future__ import annotations`** in Python files (trips Sphinx
  nitpicky `-W`). Not relevant to most of this plan, but the e2e file is Python.
- **`make web` must run BEFORE any browser test.** `pytest` does not build the
  web dist — only `make web` does — and every dev checkout already has a stale
  one, so a browser test on an unbuilt bundle silently certifies the wrong
  artifact. This has caused two CI reds (#131, #132).
- **`nox -s lint` is `ruff check` AND `ruff format --check`.** Web code is gated
  by Biome (`make web-check`), which also runs `tsc`.
- **Commit convention:** conventional prefix, and an `Assisted-by: Claude Opus 4.8`
  trailer (no `Co-Authored-By`). Self-commit is fine — we are on branch
  `worktree-topology-immediate-followups`, not `main`.
- **No heavy/parallel test load on the dev VM.** Run scoped suites; do not brute
  force the whole matrix.
- The working directory for all commands is the worktree root:
  `/home/vagrant/otto-sh/.claude/worktrees/topology-immediate-followups`.

## File Structure

| File | Responsibility after this plan |
| --- | --- |
| `web/src/topo/edgeStyles.ts` | Owns `EdgeClass`, `edgeClass()`, and the three-entry `EDGE_STYLES`. The single source of truth the canvas and the legend both draw from. |
| `web/src/app.css` | Owns the edge stroke custom properties. Gains a `.dark` alternate for `--topo-edge-reports`; loses `--topo-edge-implicit` and `--topo-edge-local`. |
| `web/src/topo/LinkEdge.tsx` | Draws an edge. Looks its spec up by class, not provenance. |
| `web/src/topo/TopoLegend.tsx` | Renders one legend row per class (three, not five). |
| `web/src/topo/linkText.ts` | Human text for an edge. Exports `primaryLink` — now the shared predicate for "does this edge have a link?" |
| `web/src/topo/LinkInspector.tsx` | The link detail aside. `absolute` inside the canvas; Escape listener only while selected; uses `primaryLink`. |
| `web/src/topo/TopologyPage.tsx` | Owns the canvas box (now `relative`, containing the inspector) and the selection gate. |
| `web/src/topo/nodes.tsx` | `LocalNode` loses its caption. |

---

### Task 1: Collapse the edge encoding into three classes

**Files:**
- Modify: `web/src/topo/edgeStyles.ts` (whole file)
- Modify: `web/src/app.css:27-44`
- Modify: `web/src/topo/LinkEdge.tsx:14`, `:60`
- Modify: `web/src/topo/TopoLegend.tsx:14`, `:22`, `:35-58`, `:67-77`
- Modify: `web/src/topo/linkText.ts:5`, `:33`
- Test: `web/src/__tests__/topoedge.test.tsx` (rewrite)
- Test: `web/src/__tests__/topolegend.test.tsx:37`, `:46-53`
- Test: `web/src/__tests__/topohover.test.tsx:54`, `:61`, `:66`
- Test: `tests/e2e/monitor/dashboard/test_review_shell.py:709`

**Interfaces:**
- Produces: `type EdgeClass = "static" | "tunnel" | "reports-for"`;
  `edgeClass(provenance: Provenance): EdgeClass`;
  `EDGE_STYLES: Record<EdgeClass, EdgeStyleSpec>`;
  `LINK_ORDER: EdgeClass[]`. `edgeStyle(provenance: Provenance, emphasized: boolean)`
  keeps its existing signature — it maps through `edgeClass` internally, so its
  callers do not change.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing test — rewrite `web/src/__tests__/topoedge.test.tsx`**

Replace the whole file:

```tsx
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES, type EdgeClass, edgeClass, edgeStyle } from "../topo/edgeStyles";

afterEach(cleanup);

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_CSS = readFileSync(join(HERE, "../app.css"), "utf-8");

const ALL_PROVENANCE: TopoEdge["provenance"][] = [
  "implicit",
  "declared",
  "dynamic",
  "local",
  "reports-for",
];
const ALL_CLASSES: EdgeClass[] = ["static", "tunnel", "reports-for"];

describe("edgeClass", () => {
  it("collapses declared, implicit and local into one static class", () => {
    // declared, implicit and local are all derived from lab config. The canvas
    // must not imply a difference that does not exist.
    expect(edgeClass("declared")).toBe("static");
    expect(edgeClass("implicit")).toBe("static");
    expect(edgeClass("local")).toBe("static");
  });

  it("keeps a tunnel and a reports-for relation in their own classes", () => {
    expect(edgeClass("dynamic")).toBe("tunnel");
    expect(edgeClass("reports-for")).toBe("reports-for");
  });
});

describe("edgeStyle", () => {
  it("draws declared, implicit and local identically", () => {
    const declared = JSON.stringify(edgeStyle("declared", false));
    expect(JSON.stringify(edgeStyle("implicit", false))).toBe(declared);
    expect(JSON.stringify(edgeStyle("local", false))).toBe(declared);
  });

  it("maps the five provenances onto three distinct strokes", () => {
    expect(edgeStyle("declared", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("declared", false).strokeWidth).toBe(1.5);
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    const styles = ALL_PROVENANCE.map((p) => JSON.stringify(edgeStyle(p, false)));
    expect(new Set(styles).size).toBe(3);
  });

  it("emphasized state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth,
    );
  });
});

describe("EDGE_STYLES", () => {
  // The legend renders from this table. A class without a row would ship a line
  // style that the key silently fails to explain.
  it("has a labelled row for every class", () => {
    for (const c of ALL_CLASSES) {
      expect(EDGE_STYLES[c].label.length).toBeGreaterThan(0);
      expect(EDGE_STYLES[c].hint.length).toBeGreaterThan(0);
    }
    expect(Object.keys(EDGE_STYLES).sort()).toEqual([...ALL_CLASSES].sort());
  });

  it("gives tunnels — and only tunnels — a casing", () => {
    expect(EDGE_STYLES.tunnel.casing).toBeDefined();
    expect(EDGE_STYLES.static.casing).toBeUndefined();
    expect(EDGE_STYLES["reports-for"].casing).toBeUndefined();
  });

  // THE DARK-MODE TRAP. static and reports-for now share a 1.5px width, so
  // colour and dash are all that separate a network link from a
  // metrics-attribution arrow. --topo-edge-static resolves to #9ca3af in dark;
  // if --topo-edge-reports has no dark alternate it resolves to #9ca3af too,
  // and the two become identical but for the dash. Asserting the two specs use
  // *different variables* would NOT catch this — distinct variables can still
  // resolve to the same grey — so this reads the resolved values out of the
  // stylesheet itself.
  it("keeps reports-for dimmer than static in BOTH themes", () => {
    const block = (selector: string): string => {
      const start = APP_CSS.indexOf(selector);
      expect(start, `${selector} block not found in app.css`).toBeGreaterThan(-1);
      return APP_CSS.slice(start, APP_CSS.indexOf("}", start));
    };
    const value = (css: string, name: string): string | undefined =>
      new RegExp(`${name}:\\s*(#[0-9a-fA-F]{3,8})`).exec(css)?.[1]?.toLowerCase();

    const root = block(":root {");
    const dark = block(".dark {");

    // Both themes define both variables...
    for (const [theme, css] of [
      ["light", root],
      ["dark", dark],
    ] as const) {
      expect(value(css, "--topo-edge-static"), `--topo-edge-static in ${theme}`).toBeDefined();
      expect(value(css, "--topo-edge-reports"), `--topo-edge-reports in ${theme}`).toBeDefined();
    }
    // ...and they never collide.
    expect(value(root, "--topo-edge-reports")).not.toBe(value(root, "--topo-edge-static"));
    expect(value(dark, "--topo-edge-reports")).not.toBe(value(dark, "--topo-edge-static"));
  });

  it("has retired the per-provenance stroke variables", () => {
    expect(APP_CSS).not.toContain("--topo-edge-implicit");
    expect(APP_CSS).not.toContain("--topo-edge-local");
    expect(APP_CSS).not.toContain("--topo-edge-declared");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run src/__tests__/topoedge.test.tsx`
Expected: FAIL — `edgeClass` and `EdgeClass` are not exported from `edgeStyles.ts`.

- [ ] **Step 3: Rewrite `web/src/topo/edgeStyles.ts`**

Replace the whole file:

```ts
// Single source of truth for the topology edge encoding: LinkEdge draws from
// it, TopoLegend renders its swatches from it. A legend that restates these
// styles by hand drifts from the canvas the first time a stroke changes — and
// a wrong key is worse than no key at all.
//
// The canvas encodes CLASSES, not provenances. `declared`, `implicit` and
// `local` links are all derived from lab config — there is no functional
// difference between them — so they share one `static` stroke and the legend
// carries one row for the three. `TopoEdge.provenance` keeps all five values
// and the format:1 export contract is untouched; only the DRAWING collapses.
// The inspector's Provenance row is where the surviving distinction lives.
import type { TopoEdge } from "../data/topology";

export type Provenance = TopoEdge["provenance"];

/** What the canvas actually draws: three classes over five provenances. */
export type EdgeClass = "static" | "tunnel" | "reports-for";

export function edgeClass(provenance: Provenance): EdgeClass {
  if (provenance === "dynamic") return "tunnel";
  if (provenance === "reports-for") return "reports-for";
  return "static"; // declared | implicit | local — all lab config
}

export interface EdgeStyleSpec {
  /** Legend row text. */
  label: string;
  /** One-line meaning; shown in the hover card. */
  hint: string;
  stroke: string;
  strokeWidth: number;
  strokeDasharray?: string;
  /** Wide, low-opacity sleeve drawn UNDER the core stroke. Tunnels only — no
   * other edge has a casing, so a tunnel reads as something wrapped *around* a
   * path rather than as a peer of the other links. That is the "topology
   * overlay" Provenance.DYNAMIC's docstring already promises, bought without
   * touching the export contract. The cue is form, not colour: the palette
   * keeps colour reserved for health. */
  casing?: { stroke: string; strokeWidth: number; opacity: number };
}

export const EDGE_STYLES: Record<EdgeClass, EdgeStyleSpec> = {
  static: {
    label: "static",
    hint: "from the lab config — declared, hop-derived, or local",
    stroke: "var(--topo-edge-static)",
    // 1.5, not declared's old 2: `local` fans out to every hop-less element, so
    // promoting the whole class to 2px would turn the local node into a loud
    // hub. The class takes the lighter weight and the map gets quieter.
    strokeWidth: 1.5,
  },
  tunnel: {
    label: "tunnel",
    hint: "realized by an otto tunnel",
    stroke: "var(--topo-edge-static)",
    // The heaviest stroke on the canvas, deliberately: a tunnel is the only
    // edge that is NOT static lab config.
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
  const spec = EDGE_STYLES[edgeClass(provenance)];
  const base = {
    stroke: spec.stroke,
    strokeWidth: spec.strokeWidth,
    ...(spec.strokeDasharray === undefined ? {} : { strokeDasharray: spec.strokeDasharray }),
  };
  return emphasized ? { ...base, strokeWidth: base.strokeWidth + EMPHASIS_WIDTH } : base;
}
```

- [ ] **Step 4: Rewrite the edge stroke variables in `web/src/app.css`**

Replace lines 27–44 (the comment block, the `:root` block and the `.dark` block
for topology edges) with:

```css
/* Topology edge strokes: consumed by inline SVG style objects in LinkEdge.tsx,
   which can't use Tailwind dark: variants — so plain custom properties (not
   @theme tokens), flipped by the same `dark` class on <html> that drives
   everything else.

   `--topo-edge-reports` MUST keep a dark alternate that differs from
   `--topo-edge-static`. Static and reports-for share a 1.5px width, so colour
   and dash are all that separate a network link from a metrics-attribution
   arrow — and without the override below BOTH resolve to #9ca3af in dark mode.
   topoedge.test.tsx pins this. */
:root {
  --topo-edge-static: #4b5563;
  --topo-edge-reports: #9ca3af;
  --topo-edge-tunnel-casing: #9ca3af;
}
.dark {
  --topo-edge-static: #9ca3af;
  --topo-edge-reports: #6b7280;
  --topo-edge-tunnel-casing: #4b5563;
}
```

- [ ] **Step 5: Point `LinkEdge.tsx` at the class**

Line 14, change the import:

```tsx
import { EDGE_STYLES, edgeClass, edgeStyle } from "./edgeStyles";
```

Line 60, change the casing lookup:

```tsx
  const casing = EDGE_STYLES[edgeClass(edge.provenance)].casing;
```

`edgeStyle(edge.provenance, emphasized)` on line 74 is unchanged — `edgeStyle`
still takes a provenance.

- [ ] **Step 6: Rewrite the legend's link section in `web/src/topo/TopoLegend.tsx`**

Line 14, change the import:

```tsx
import { EDGE_STYLES, type EdgeClass } from "./edgeStyles";
```

Line 22, change `LINK_ORDER` (note the type and the values both change):

```tsx
export const LINK_ORDER: EdgeClass[] = ["static", "tunnel", "reports-for"];
```

Lines 35–58, change `Swatch` to take a class. The prop is named `cls` rather
than `edgeClass` to avoid shadowing the imported function:

```tsx
function Swatch({ cls }: { cls: EdgeClass }) {
  const spec = EDGE_STYLES[cls];
  return (
    <svg width="30" height="10" aria-hidden="true" className="shrink-0">
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
```

Lines 67–77, change the map (the loop variable is now a class, and the testid
follows it):

```tsx
            {LINK_ORDER.map((c) => (
              <li
                key={c}
                data-testid={`topo-legend-link-${c}`}
                className={ROW}
                title={EDGE_STYLES[c].hint}
              >
                <Swatch cls={c} />
                {EDGE_STYLES[c].label}
              </li>
            ))}
```

Also update the comment on lines 18–21, which claims the array covers
provenances:

```tsx
// Explicit, not derived from Object.keys(EDGE_STYLES): display order is a
// deliberate design choice, not alphabetical. Exported so a test can assert it
// covers exactly EDGE_STYLES's keys — TypeScript alone doesn't force this
// hand-written array to be exhaustive, only EDGE_STYLES's own Record type is.
```

(That wording still holds; only `Provenance` → `EdgeClass` in the type changed.)

- [ ] **Step 7: Point `linkText.ts`'s subtitle at the class**

Line 5, change the import:

```ts
import { EDGE_STYLES, edgeClass } from "./edgeStyles";
```

Line 33, inside `edgeSubtitle`, change the label lookup:

```ts
  const label = EDGE_STYLES[edgeClass(edge.provenance)].label;
```

- [ ] **Step 8: Update the two other vitest files the labels changed under**

`web/src/__tests__/topohover.test.tsx` — the subtitle now renders the class
label. Change line 54, line 61 and line 66:

```tsx
    expect(edgeSubtitle(declared)).toBe("static · tcp");
```

```tsx
    expect(edgeSubtitle(hopGroup)).toBe("static · 3 links");
```

```tsx
    expect(edgeSubtitle(reports)).toBe("reports for");
```

(The third is unchanged in value — assert it still holds.) Also fix the stale
comment on lines 57–58, which says "The inspector renders these degenerate edges
as a raw id today" — Task 2 removes that. Replace with:

```tsx
  // A collapsed hop group's synthetic id is noise; the card names the pair.
```

`web/src/__tests__/topolegend.test.tsx` needs no edit — it iterates
`Object.keys(EDGE_STYLES)` (line 37) and compares `LINK_ORDER` against those same
keys (lines 51–52), so it follows the collapse automatically. **This is the
point of that test.** Confirm it goes green rather than editing it.

- [ ] **Step 9: Update the e2e legend assertion**

`tests/e2e/monitor/dashboard/test_review_shell.py` line 709 — the loop iterates
provenances:

```python
    for edge_class in ("static", "tunnel", "reports-for"):
        assert legend.locator(f'[data-testid="topo-legend-link-{edge_class}"]').count() == 1
```

- [ ] **Step 10: Run the web tests and the typecheck**

Run: `cd web && npx vitest run && npm run check`
Expected: all vitest files PASS. `npm run check` (Biome + tsc) clean.

If `tsc` complains that `Swatch`'s `provenance` prop is still referenced
anywhere, you missed a call site in Step 6.

- [ ] **Step 11: Commit**

```bash
git add web/src/topo/edgeStyles.ts web/src/app.css web/src/topo/LinkEdge.tsx \
  web/src/topo/TopoLegend.tsx web/src/topo/linkText.ts \
  web/src/__tests__/topoedge.test.tsx web/src/__tests__/topohover.test.tsx \
  tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "feat(monitor): collapse topology edges into three visual classes

declared, implicit and local links are all derived from lab config, so the
canvas no longer implies a difference between them: one 'static' stroke, one
legend row. TopoEdge.provenance keeps all five values and format:1 is
untouched — only the drawing collapses, via a new EdgeClass.

Static takes the 1.5px weight rather than declared's 2px: local fans out to
every hop-less element, so promoting the class would have made the local node
a loud hub.

--topo-edge-reports gains a dark alternate it never had. Static and reports-for
now share a width, and both variables resolve to #9ca3af in dark — the old 2px
declared stroke was the only thing keeping a network link distinct from a
metrics-attribution arrow there.

Assisted-by: Claude Opus 4.8"
```

---

### Task 2: Gate inspector selection on link presence

**Files:**
- Modify: `web/src/topo/linkText.ts:17-19` (export `primaryLink`)
- Modify: `web/src/topo/LinkInspector.tsx:18`, `:41`
- Modify: `web/src/topo/TopologyPage.tsx:24`, `:188-191`
- Test: `web/src/__tests__/topohover.test.tsx` (add a `primaryLink` describe block)
- Test: `tests/e2e/monitor/dashboard/test_review_shell.py` (new test)

**Interfaces:**
- Consumes: nothing from Task 1 (independent, but Task 1 lands first because it
  touches the same files).
- Produces: `primaryLink(edge: TopoEdge): LinkSnapshot | null`, exported from
  `linkText.ts`. This is the single predicate for "does this edge have a link?"

**Background — which edges carry a link:**

| edge | carries |
| --- | --- |
| `declared`, `dynamic` (both views) | `link` |
| `implicit`, **inter-element view only** | `links[]` — a collapsed bundle of real `lab.json` links |
| `local:*` (both views) | nothing — synthesized from `hop == null` |
| `hop:*`, **intra-element view only** | nothing — synthesized from `host.hop` |
| `reports-for` (both views) | nothing — a metrics-attribution relation |

`TopologyPage` currently gates on `provenance !== "local"`, so an intra-view
`hop:*` or a `reports-for` edge opens the inspector with a raw edge id as its
title, no fact rows, and a NetEm box. Task 1 makes this worse: a `hop:*` edge
and a declared link now draw identically, so provenance can no longer be the
gate.

There is deliberately **no summary panel**. A one-line panel restating what the
hover card already says does not earn a slide-over, and the NetEm section — whose
job is to teach that links are configurable objects — would be a lie on a
hop-derived path or a reports-for arrow, which have no link object to configure.

- [ ] **Step 1: Write the failing test — add to `web/src/__tests__/topohover.test.tsx`**

Add `primaryLink` to the import on line 7:

```tsx
import { edgeSubtitle, edgeTitle, primaryLink } from "../topo/linkText";
```

Append this describe block at the end of the file. It reuses the `declared`,
`hopGroup` and `reports` fixtures already defined at the top of that file:

```tsx
// The inspector's whole content is link facts + the NetEm section. Selection
// must therefore be gated on link PRESENCE, not on provenance — after the
// class collapse a synthesized hop path and a declared link draw identically,
// so provenance can no longer tell them apart.
describe("primaryLink", () => {
  it("finds the link on a declared edge", () => {
    expect(primaryLink(declared)?.id).toBe("app-db");
  });

  it("finds the first link of a collapsed hop bundle", () => {
    expect(primaryLink(hopGroup)?.id).toBe("app-db");
  });

  it("returns null for a reports-for edge, which has no link at all", () => {
    expect(primaryLink(reports)).toBeNull();
  });

  it("returns null for a synthesized local edge", () => {
    const local: TopoEdge = {
      id: "local:edge-gw",
      source: "local",
      target: "edge-gw",
      provenance: "local",
      impair: null,
      parallelIndex: 0,
    };
    expect(primaryLink(local)).toBeNull();
  });

  it("returns null for an intra-view hop edge", () => {
    const hop: TopoEdge = {
      id: "hop:db-01",
      source: "edge-gw",
      target: "db-01",
      provenance: "implicit",
      impair: null,
      parallelIndex: 0,
    };
    expect(primaryLink(hop)).toBeNull();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run src/__tests__/topohover.test.tsx`
Expected: FAIL — `primaryLink` is not exported from `linkText.ts`.

- [ ] **Step 3: Export `primaryLink` from `web/src/topo/linkText.ts`**

Lines 17–19 — add the `export` keyword and a docstring:

```ts
/** The link an edge is "about", or null if it has none. Also the selection
 * gate: an edge with no link has nothing for the inspector to inspect, so it
 * is not selectable (TopologyPage). `reports-for` never has one, nor does a
 * synthesized `local:*` or intra-view `hop:*` edge. */
export function primaryLink(edge: TopoEdge): LinkSnapshot | null {
  return edge.link ?? edge.links?.[0] ?? null;
}
```

- [ ] **Step 4: Use it in `web/src/topo/LinkInspector.tsx`**

Line 18, change the import (it already imports `endpointText` from this module):

```tsx
import { endpointText, primaryLink } from "./linkText";
```

Line 41, replace the hand-duplicated fallback:

```tsx
  const primary = primaryLink(edge);
```

- [ ] **Step 5: Gate selection in `web/src/topo/TopologyPage.tsx`**

Line 24 area — add the import alongside the existing topo imports:

```tsx
import { primaryLink } from "./linkText";
```

Lines 188–191, replace the `onEdgeClick` handler:

```tsx
            onEdgeClick={(_evt, edge) => {
              // Link presence, not provenance: after the class collapse a
              // synthesized hop path draws exactly like a declared link, and
              // only one of them has anything to inspect. The hover card
              // already names the ones that don't.
              const data = edge.data as { edge?: TopoEdge } | undefined;
              if (data?.edge && primaryLink(data.edge) !== null) onSelectEdge(data.edge);
            }}
```

- [ ] **Step 6: Run the web tests**

Run: `cd web && npx vitest run && npm run check`
Expected: PASS, clean.

- [ ] **Step 7: Add the e2e proof that a link-less edge is inert**

In `tests/e2e/monitor/dashboard/test_review_shell.py`, add this test immediately
after `test_link_inspector_and_parallel_edges` (which ends at line 560). It uses
the `_click_edge` helper — read its docstring at line 73 before touching edge
clicks, a naive bbox-center click lands on an unrelated node.

`reports:mgmt-01~chassis-a` is the reports-for edge kitchen-sink produces; it is
only rendered once the Sources overlay is toggled on (see
`test_sources_overlay_toggles_reports_edges`).

```python
def test_link_less_edges_do_not_open_the_inspector(shell_dash, page):
    """A reports-for edge carries no LinkSnapshot — there is nothing to inspect,
    so clicking it is inert and the hover card is its whole story. Before the
    edge-class collapse this opened a degenerate panel: raw edge id as the
    title, no fact rows, and a NetEm section for a relation that has no link
    object to configure."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 1"
    )

    _click_edge(page, "reports:mgmt-01~chassis-a")

    # A negative assertion needs a barrier, or it passes trivially by running
    # before the click is even processed. NOT a sleep (this repo has no
    # wait_for_timeout anywhere, and an arbitrary budget is a flake waiting to
    # happen): React Flow selects a clicked edge in its OWN store, independent
    # of our onEdgeClick handler, so once the wrapper carries `.selected` the
    # browser has delivered the click AND React has re-rendered. If the
    # inspector were going to open, it would have by then.
    page.locator(
        '.react-flow__edge.selected [data-testid="topo-link-reports:mgmt-01~chassis-a"]'
    ).wait_for()
    assert page.locator('[data-testid="link-inspector"]').count() == 0
```

- [ ] **Step 8: Build the dist, then run the dashboard lane**

The dist MUST be rebuilt first — pytest does not build it, and the stale one in
this worktree does not contain any of Task 1 or Task 2.

Run:
```bash
make web
uv run pytest tests/e2e/monitor/dashboard -q
```
Expected: all PASS, including the two new/updated tests.

- [ ] **Step 9: Commit**

```bash
git add web/src/topo/linkText.ts web/src/topo/LinkInspector.tsx \
  web/src/topo/TopologyPage.tsx web/src/__tests__/topohover.test.tsx \
  tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "fix(monitor): only let edges with a link open the inspector

Clicking an intra-view hop:* or a reports-for edge opened a degenerate panel —
raw edge id as the title, no fact rows, and a NetEm section advertising the
configurability of a relation that has no link object behind it.

The gate was provenance ('anything but local'), which the edge-class collapse
just invalidated: a synthesized hop path now draws exactly like a declared
link. Gate on link presence instead, via a primaryLink() that LinkInspector was
already hand-duplicating.

No summary panel for the link-less ones: it would restate the hover card and
earn a slide-over for it.

Assisted-by: Claude Opus 4.8"
```

---

### Task 3: Anchor the inspector inside the canvas

**Files:**
- Modify: `web/src/topo/LinkInspector.tsx:44-48` (the aside's classes) and its
  header comment
- Modify: `web/src/topo/TopologyPage.tsx:174`, `:199`
- Modify: `web/src/topo/TopoLegend.tsx:1-9` (header comment)
- Test: `web/src/__tests__/linkinspector.test.tsx` (add a geometry assertion)
- Test: `tests/e2e/monitor/dashboard/test_review_shell.py:641-653` (delete the
  1600px workaround — this is the regression test)

**Interfaces:**
- Consumes: `primaryLink` from Task 2 (already wired; no new surface).
- Produces: no new exports. The canvas `div` in `TopologyPage` becomes the
  inspector's containing block.

**Background — why the todo file's prescription was wrong.** `todo/monitor-topology-followups.md`
item 1 prescribes `top-[6.5rem] bottom-0`. `6.5rem` is a hardcoded guess at the
height of AppBar + ReviewBar. But `ReviewBar` is `flex flex-wrap` carrying a
HISTORICAL badge, source name, session picker, range presets, two
`datetime-local` inputs, Apply and Reset — **at ≤1280px it wraps to a second
row**, putting Apply *below* 6.5rem and still under the aside, at exactly the
width the bug was reported at. An import-error banner can add height too. So the
constant goes; the aside is bounded by the canvas instead, which cannot be wrong
at any width.

**The regression test already exists, bent around the bug.**
`test_link_inspector_survives_range_change` forces a 1600px viewport and its
docstring explains why: "At Playwright's default 1280px width the panel's span
(x >= 896) physically covers the review bar's right end where Apply sits (center
x ~1016)". Straightening that test back to the default viewport *is* the proof.

- [ ] **Step 1: Write the failing test — the e2e straightening**

In `tests/e2e/monitor/dashboard/test_review_shell.py`, replace the docstring and
delete the `set_viewport_size` line (lines 641–653):

```python
def test_link_inspector_survives_range_change(shell_dash, page):
    """Inspector selection is scoped to the view identity: it survives a
    review-bar range apply (a selected link is static config) and closes on
    navigation to another view.

    Runs at Playwright's DEFAULT 1280px width, and that is the point. The
    inspector used to be a viewport-fixed full-height right aside, so at 1280px
    its span (x >= 896) covered the review bar's right end where Apply sits
    (center x ~1016) — this test had to force a 1600px viewport to reach the
    button at all. The aside is now absolutely positioned INSIDE the canvas box,
    so it cannot reach the review bar at any width, and the `range-apply` click
    below is the proof: Playwright's actionability check fails it if anything
    overlays the button."""
    page.goto(shell_dash.url)
```

That is: the `page.set_viewport_size({"width": 1600, "height": 900})` line is
**deleted** and `page.goto(shell_dash.url)` becomes the first statement. Leave
the rest of the test body (lines 654 onward) exactly as it is — the
`page.locator('[data-testid="range-apply"]').click()` at line 679 is what now
carries the assertion.

- [ ] **Step 2: Build the dist and run it to verify it FAILS**

Run:
```bash
make web
uv run pytest tests/e2e/monitor/dashboard/test_review_shell.py::test_link_inspector_survives_range_change -q
```
Expected: FAIL. Playwright's actionability check on `range-apply` times out with
an "intercepts pointer events" / element-not-stable style error, because the
`fixed` aside is on top of the button at 1280px.

**If it PASSES here, stop and investigate** — it would mean the occlusion does
not reproduce, and the premise of this task is wrong. Report before proceeding.

- [ ] **Step 3: Make the canvas div the containing block — `web/src/topo/TopologyPage.tsx`**

Line 174, add `relative` to the canvas wrapper:

```tsx
        <div className="relative min-h-0 grow rounded-lg border border-gray-200 dark:border-gray-800">
```

Then move `<LinkInspector>` INSIDE that div. Currently it sits at line 199, after
the div closes. The end of the component becomes:

```tsx
            <Controls showInteractive={false} />
            <TopoLegend />
          </ReactFlow>
          <LinkInspector edge={selectedEdge} onClose={() => setSelected(null)} />
        </div>
      </main>
    </ReactFlowProvider>
  );
}
```

Note the inspector is a sibling of `<ReactFlow>`, not a child — it must not be a
React Flow `Panel` (it is not part of the canvas's pan/zoom transform).

- [ ] **Step 4: Change the aside's geometry — `web/src/topo/LinkInspector.tsx`**

Lines 44–48, change `fixed inset-y-0` to `absolute inset-y-0`:

```tsx
    <aside
      data-testid="link-inspector"
      className="absolute inset-y-0 right-0 z-30 flex w-96 max-w-full flex-col gap-3 overflow-y-auto
        border-l border-gray-200 bg-white p-4 shadow-lg dark:border-gray-800 dark:bg-gray-950"
    >
```

And replace the header comment's geometry paragraph (lines 1–14) — the non-modal
rationale still holds, but the `fixed` description is now wrong:

```tsx
// Right side-panel link inspector (spec §10): connectivity facts from the
// static snapshot + the reserved NetEm section. Marking NetEm "coming soon"
// is deliberate — the backend query/edit path is a later phase; the section
// existing NOW teaches the mental model that links are configurable objects.
//
// Non-modal by design: §10 describes this as a side-panel, not a dialog — the
// map and the review bar (range presets, sources toggle) must stay interactive
// while a link is under inspection, since the selection itself is meant to
// survive ordinary review-bar interaction. A react-aria Modal (as used by
// SlideOver) traps focus and blocks all pointer/keyboard input to the rest of
// the page, which would make that intent unreachable. So this renders as a
// plain aside with Escape-to-close instead. The events panel stays on SlideOver
// — its own interaction (clicking a row) closes it, so it never needs
// background interactivity while open.
//
// ABSOLUTE, not fixed: TopologyPage's canvas div is `relative`, so this aside is
// bounded by the canvas and physically cannot reach the review bar. It used to
// be `fixed inset-y-0`, spanning the full viewport height and covering the
// review bar's Apply button at <=1280px. The obvious repair — offsetting by the
// chrome height — does not work: ReviewBar is flex-wrap, so at exactly those
// narrow widths it wraps to a second row and any hardcoded offset is already
// stale. Bounding by the canvas needs no constant at all.
```

- [ ] **Step 5: Fix the now-stale comment in `web/src/topo/TopoLegend.tsx`**

Lines 3–6 describe the inspector as `fixed`:

```tsx
// Bottom-left, NOT right: LinkInspector is a right-anchored aside filling the
// canvas's right edge, so a right-anchored panel would be covered the moment an
// edge is selected. `mb-28` lifts it clear of React Flow's own zoom Controls,
// which occupy the same corner.
```

- [ ] **Step 6: Pin the geometry in `web/src/__tests__/linkinspector.test.tsx`**

Add to the first test (`renders link facts, impair, and the reserved NetEm
section`), after the existing backdrop assertion on line 48:

```tsx
    // Bounded by the canvas, not the viewport: a `fixed` aside spans the full
    // viewport height and covers the review bar's Apply button at <=1280px.
    expect(panel.className).toContain("absolute");
    expect(panel.className).not.toContain("fixed");
```

- [ ] **Step 7: Run everything and verify it now passes**

Run:
```bash
cd web && npx vitest run && npm run check && cd ..
make web
uv run pytest tests/e2e/monitor/dashboard -q
```
Expected: vitest PASS, Biome/tsc clean, dashboard lane PASS — including
`test_link_inspector_survives_range_change` now clicking Apply at 1280px.

- [ ] **Step 8: Commit**

```bash
git add web/src/topo/LinkInspector.tsx web/src/topo/TopologyPage.tsx \
  web/src/topo/TopoLegend.tsx web/src/__tests__/linkinspector.test.tsx \
  tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "fix(monitor): stop the link inspector covering the review bar

The aside was \`fixed inset-y-0\`, spanning the full viewport height, so at
<=1280px it covered the review bar's Apply button. The follow-up note prescribed
offsetting it by the chrome height (top-[6.5rem]) — but ReviewBar is flex-wrap
and at exactly those narrow widths it wraps to a second row, so any hardcoded
offset is stale precisely where the bug bites. An import-error banner moves it
too.

Bound it by the canvas instead: the canvas div becomes \`relative\` and the aside
\`absolute\`. No constant, correct at every width, and it stops covering the
topology toolbar as well.

The regression test already existed, bent around the bug:
test_link_inspector_survives_range_change forced a 1600px viewport and its
docstring explained that Apply was unreachable at Playwright's default 1280px.
Straightened it back — its range-apply click is now the proof.

Assisted-by: Claude Opus 4.8"
```

---

### Task 4: Guard the Escape listener

**Files:**
- Modify: `web/src/topo/LinkInspector.tsx:32-38`
- Test: `web/src/__tests__/linkinspector.test.tsx` (new test)

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new.

`LinkInspector`'s keydown effect registers a document-level listener whenever the
topology page is mounted, selected edge or not — so Escape fires `onClose` on a
page with nothing to close.

- [ ] **Step 1: Write the failing test — add to `web/src/__tests__/linkinspector.test.tsx`**

```tsx
  it("registers no key listener while nothing is selected", () => {
    // The effect used to run on every mount of the topology page, so Escape
    // fired onClose with nothing to close.
    const add = vi.spyOn(document, "addEventListener");
    render(<LinkInspector edge={null} onClose={vi.fn()} />);
    expect(add.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(0);
    add.mockRestore();
  });

  it("closes on Escape while an edge is selected", () => {
    const onClose = vi.fn();
    render(<LinkInspector edge={edgeWith({})} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
```

`fireEvent` is not currently imported in this file. Change line 1:

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
```

- [ ] **Step 2: Run it and watch the first one fail**

Run: `cd web && npx vitest run src/__tests__/linkinspector.test.tsx`
Expected: FAIL on "registers no key listener while nothing is selected" — it
finds 1 keydown registration. The Escape test should already PASS (that
behaviour is not changing; it is here to prove the guard doesn't break it).

- [ ] **Step 3: Add the guard — `web/src/topo/LinkInspector.tsx` lines 32-38**

```tsx
  // Guarded on `edge`, not just mounted: without this the listener is live
  // whenever the topology page is, so Escape fires onClose with nothing
  // selected. The `return null` below cannot do this job — hooks can't be
  // conditional, so the effect must decline the work itself.
  useEffect(() => {
    if (edge === null) return;
    const onKeyDown = (evt: KeyboardEvent): void => {
      if (evt.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [edge, onClose]);
```

Note `edge` joins the dependency array.

- [ ] **Step 4: Run it and verify both pass**

Run: `cd web && npx vitest run src/__tests__/linkinspector.test.tsx && npm run check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/topo/LinkInspector.tsx web/src/__tests__/linkinspector.test.tsx
git commit -m "fix(monitor): only listen for Escape while a link is selected

The inspector's keydown effect registered a document-level listener whenever
the topology page was mounted, so Escape fired onClose with nothing to close.
The render-time \`return null\` can't prevent that — hooks can't be conditional,
so the effect declines the work itself.

Assisted-by: Claude Opus 4.8"
```

---

### Task 5: Drop "you are here" from the local node

**Files:**
- Modify: `web/src/topo/nodes.tsx:47-60`
- Test: `web/src/__tests__/toponodes.test.tsx` (new test — the file has no
  `LocalNode` coverage today)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

The local node's position at the root of the map is self-evident; the caption is
noise. Prose references to "you are here" in `src/otto/link/derive.py` docstrings
and in older specs under `docs/superpowers/` are historical — leave them.

- [ ] **Step 1: Write the failing test — add to `web/src/__tests__/toponodes.test.tsx`**

Change line 5 to pull in `LocalNode`:

```tsx
import { ElementNode, HostNode, LocalNode } from "../topo/nodes";
```

Then append at the end of the file. Render bare — the neighbouring `ElementNode`
and `HostNode` tests do, so `<Ports />` clearly tolerates living outside a
`ReactFlowProvider`:

```tsx
describe("LocalNode", () => {
  it("names itself without narrating that the user is here", () => {
    const local: TopoNode = { id: "local", kind: "local", depth: 0, label: "local" };
    render(<LocalNode data={local} />);
    const root = screen.getByTestId("topo-node-local");
    expect(root.textContent).toContain("local");
    expect(root.textContent).not.toContain("you are here");
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run src/__tests__/toponodes.test.tsx`
Expected: FAIL — `textContent` contains "you are here".

- [ ] **Step 3: Delete the caption — `web/src/topo/nodes.tsx` line 56**

Remove the span entirely:

```tsx
export function LocalNode({ data: _data }: { data: TopoNode }) {
  return (
    <div
      data-testid="topo-node-local"
      data-status="local"
      className="rounded-lg border-2 border-brand-500 bg-white px-3 py-2 text-sm font-semibold
        dark:bg-gray-950"
    >
      ◉ local
      <Ports />
    </div>
  );
}
```

- [ ] **Step 4: Run it and verify it passes**

Run: `cd web && npx vitest run src/__tests__/toponodes.test.tsx && npm run check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add web/src/topo/nodes.tsx web/src/__tests__/toponodes.test.tsx
git commit -m "feat(monitor): drop 'you are here' from the topology local node

The local node's position at the root of the map says it already.

Assisted-by: Claude Opus 4.8"
```

---

### Task 6: Close the follow-ups and run the full gates

**Files:**
- Modify: `todo/monitor-topology-followups.md`
- Modify: `todo/TODO.md` (lines 6, 13, 14)

**Interfaces:** none.

- [ ] **Step 1: Strike the resolved items in `todo/monitor-topology-followups.md`**

Delete the "Top item" section (item 1) and items 2 and 3 from the "Mechanical
follow-ups" list, since all three shipped. Renumber the remainder. In item 6
(cosmetics), **delete only** the `--topo-edge-implicit` / `--topo-edge-reports`
aliasing clause — those variables are gone and the collision is fixed — and keep
the `HostNode` separator and `pairKey` clauses, which are still open.

Add a new note under the remaining items, recording the residue this plan
deliberately did not fix:

```markdown
- **`h-[calc(100vh-6.5rem)]` on `TopologyPage`'s `<main>`.** The same stale
  chrome-height constant the inspector used to carry: `ReviewBar` is
  `flex-wrap`, so when it wraps to a second row (≤1280px, or with a session
  picker) the canvas is taller than the space left for it and the page scrolls.
  Pre-existing, and the inspector no longer depends on it (2026-07-12, it is now
  bounded by the canvas box). Fixing it properly means making the shell a flex
  column with `min-h-0` rather than subtracting a guess.
```

- [ ] **Step 2: Strike the resolved lines in `todo/TODO.md`**

Delete line 6 (`Keep the topology follow-ups going by working on
todo/immediate-topology-follow-ups.md` — a pointer to a file that never
existed), line 13 (`Collapse the edge types for implicit, defined, and local
links...`), and line 14 (the batched trio). Leave every other line alone,
**including the uncommitted local edit already in the working tree on `main`** —
this worktree branched from `origin/main`, so that edit is not here.

- [ ] **Step 3: Run the full gates**

The dist must be freshly built before the browser lane. Run, in order:

```bash
make web
cd web && npx vitest run && npm run check && cd ..
uv run pytest tests/e2e/monitor/dashboard -q
nox -s lint
```

Expected: vitest all PASS; Biome + tsc clean; dashboard lane all PASS; `ruff
check` and `ruff format --check` clean.

- [ ] **Step 4: Verify the change in the real app**

Tests do not tell you whether the map still reads well. Serve the dashboard
against the kitchen-sink fixture and look at it, in BOTH themes:

- Three legend rows: static, tunnel, reports for.
- Declared, implicit and local edges are indistinguishable from each other.
- The tunnel is still the heaviest line and still carries its casing.
- **In dark mode**, a reports-for edge (toggle Sources on) is visibly dimmer
  than a static one — this is the trap the CSS override exists for.
- Narrow the window to 1280px, select a link, and click the review bar's Apply
  button. It must be reachable.
- The local node reads `◉ local` with no caption.
- Clicking a hop edge in an element view does nothing; hovering it still names it.

- [ ] **Step 5: Commit**

```bash
git add todo/monitor-topology-followups.md todo/TODO.md
git commit -m "docs(monitor): close the topology follow-ups this branch shipped

Strikes the batched trio (inspector occlusion, degenerate inspector on link-less
edges, Escape guard), the edge-type collapse, and item 6's CSS-variable aliasing
clause. Records the h-[calc(100vh-6.5rem)] residue — the same stale chrome
constant, still on TopologyPage's main, deliberately not fixed here.

Assisted-by: Claude Opus 4.8"
```

---

## Self-Review

**Spec coverage.** Every numbered section of the spec maps to a task: §1 the
static class → Task 1 (including the `--topo-edge-reports` dark override and the
retirement of `--topo-edge-implicit` / `--topo-edge-local`); §2 link-less edges
not selectable → Task 2; §3 inspector anchored to the canvas, and the explicit
non-fix of `h-[calc(100vh-6.5rem)]` → Task 3 and Task 6 Step 1; §4 Escape guard →
Task 4; §5 `LocalNode` → Task 5. The spec's Testing section is distributed across
the tasks that own each behaviour, with the full-gate sweep in Task 6.

**Deviation from the spec, deliberate.** The spec called for a *new*
narrow-viewport e2e test. There is no need to write one:
`test_link_inspector_survives_range_change` already forces a 1600px viewport
*specifically to dodge this bug*, and its docstring says so. Task 3 straightens
that test instead — strictly better, because it also deletes a workaround that
would otherwise have quietly outlived the bug it was hiding.

**Type consistency.** `edgeClass` is the function, `EdgeClass` the type, and
`Swatch`'s prop is `cls` to avoid shadowing the function inside `TopoLegend`.
`edgeStyle(provenance, emphasized)` keeps its original signature — it maps to a
class internally — so `LinkEdge`'s call site on line 74 is untouched.
`primaryLink(edge): LinkSnapshot | null` is used identically in `linkText`,
`LinkInspector` and `TopologyPage`.

**Ordering.** Tasks 1–3 must run in order (they touch overlapping files and Task
3's e2e straightening only passes once Task 1's dist is buildable). Tasks 4 and 5
are independent of everything and of each other.
