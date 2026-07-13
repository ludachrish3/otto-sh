# Topology immediate follow-ups — design

**Date:** 2026-07-12
**Status:** approved, ready for planning
**Source:** `todo/monitor-topology-followups.md` items 1–3 + 6 (partial),
`todo/TODO.md` line 13, and a fresh request to drop the local node's
"you are here" caption.

`todo/TODO.md` line 6 points at `todo/immediate-topology-follow-ups.md`, which
was never created. This spec is that file's content, resolved against the real
list in `todo/monitor-topology-followups.md`.

## Scope

In:

1. Collapse `declared` / `implicit` / `local` into one **static** edge encoding.
2. Stop link-less edges opening a degenerate inspector (follow-up item 2).
3. Stop the inspector occluding the review bar at narrow widths (item 1).
4. Guard the Escape listener so it only registers while an edge is selected
   (item 3).
5. Drop the `you are here` caption from `LocalNode`.

Out, and staying in `todo/monitor-topology-followups.md`:

- Item 4 (fixture-stem enumeration) — Python side, no overlap with these files.
- Item 5 (MiniMap) — still awaiting a call.
- The rest of item 6's cosmetics (the dangling `unreachable ·` separator in
  `HostNode`, exporting `pairKey` from `topology.ts`). Item 6's CSS-variable
  aliasing *is* resolved here, as a side effect of the collapse.
- Items 7–9 (D\* static-link layering, tunnels-as-overlays, obstacle-aware
  skip-column routing) — each needs its own design.

## 1. The static edge class

### What changes

Nothing in the data model. `TopoEdge.provenance` keeps all five values and the
`format:1` export contract is untouched. What collapses is the **canvas
encoding** — a new class sits between provenance and style:

```ts
// edgeStyles.ts
export type EdgeClass = "static" | "tunnel" | "reports-for";

export function edgeClass(p: Provenance): EdgeClass {
  if (p === "dynamic") return "tunnel";
  if (p === "reports-for") return "reports-for";
  return "static"; // declared | implicit | local
}
```

`EDGE_STYLES` re-keys from `Record<Provenance, …>` to `Record<EdgeClass, …>`,
dropping from five specs to three. `LINK_ORDER` in `TopoLegend.tsx` becomes
`["static", "tunnel", "reports-for"]`, so the legend's link section goes from
five rows to three and its testids become `topo-legend-link-static`,
`-tunnel`, `-reports-for`. `LinkEdge` and the legend's `Swatch` both call
`edgeClass(edge.provenance)` before looking a spec up; `linkText.ts`'s
`edgeSubtitle` does the same for its label.

The rationale is Chris's: declared, implicit and local links are all derived
from lab config, so there is no functional difference between them and the map
should not imply one.

### The three specs

| class | stroke | width | dash | casing |
| --- | --- | --- | --- | --- |
| `static` | `var(--topo-edge-static)` | 1.5 | — | — |
| `tunnel` | `var(--topo-edge-static)` | 2 | `7 4` | grey, 7px, 0.35 |
| `reports-for` | `var(--topo-edge-reports)` | 1.5 | `2 5` | — |

Labels and hints:

- `static` — "static" / "from the lab config — declared, hop-derived, or local"
- `tunnel` — "tunnel" / "realized by an otto tunnel" (unchanged)
- `reports-for` — "reports for" / "metrics sourced from a management host"
  (unchanged)

**Width is 1.5, not declared's current 2.** Promoting every local edge to 2px
would make the `local` node a loud hub — it fans out to every `hop == null`
element — so the class adopts the lighter weight instead and the map gets
quieter rather than heavier. Tunnels keep 2px + dash + casing and become the
heaviest stroke on the canvas, which is correct: a tunnel is the only edge that
is *not* static lab config.

**Colour stays declared's darker grey, not implicit's pale one.** `reports-for`
is already a 1.5px pale grey; if static were pale too, the only thing separating
a network link from a metrics-attribution arrow would be the dash pattern. Thin
and dark reads as a link; thin and pale reads as an annotation.

### CSS variables

In `app.css`, rename `--topo-edge-declared` → `--topo-edge-static` (values
unchanged: `#4b5563` light, `#9ca3af` dark) and **delete**
`--topo-edge-implicit` and `--topo-edge-local`, including the `.dark` override
of the latter. `--topo-edge-tunnel-casing` is unchanged.

`--topo-edge-reports` **gains a dark override it does not have today**:

```css
.dark {
  --topo-edge-static: #9ca3af; /* was --topo-edge-declared */
  --topo-edge-reports: #6b7280; /* new: dimmer than static on a dark ground */
  --topo-edge-tunnel-casing: #4b5563;
}
```

Without it the "thin and pale reads as an annotation" rule silently fails in dark
mode. `--topo-edge-reports` has no `.dark` entry, so it resolves to `#9ca3af`
there — and `--topo-edge-declared` *also* resolves to `#9ca3af` in dark. Today
that collision is masked by width (declared 2px vs reports 1.5px); dropping
static to 1.5px would unmask it, leaving a network link and a metrics-attribution
arrow identical in dark mode but for their dash pattern. `#6b7280` is dimmer than
`#9ca3af` against a near-black ground, restoring the light-mode relationship
(static prominent, reports recessive) in both themes.

Together these resolve item 6's "`--topo-edge-implicit` / `--topo-edge-reports`
could alias" note — the implicit variable ceases to exist, and the reports
variable stops colliding rather than being aliased into the collision.

### Knock-on effects, accepted

- A declared link's hover-card subtitle now reads `static · udp` rather than
  `declared · udp`, since `edgeSubtitle` renders the class label.
- The inspector's **Provenance fact row keeps showing the true underlying
  value** (`declared` / `implicit` / `dynamic`). The collapse is a claim about
  the canvas; the detail panel is where the surviving distinction legitimately
  lives, and it is free to keep.
- `EdgeClass` is also the concept the deferred D\* layering (item 7) will key
  on — "layer on static links only" becomes a named class rather than a
  three-way provenance test.

## 2. Link-less edges are not selectable

### The bug

Only some edges carry a `LinkSnapshot`:

| edge | carries |
| --- | --- |
| `declared`, `dynamic` (both views) | `link` |
| `implicit`, **inter-element view** | `links[]` — a collapsed bundle of real `lab.json` links |
| `local:*` (both views) | nothing — synthesized from `hop == null` |
| `hop:*`, **intra-element view** | nothing — synthesized from `host.hop` |
| `reports-for` (both views) | nothing — a metrics-attribution relation |

`TopologyPage`'s `onEdgeClick` gates on `provenance !== "local"`, so clicking an
intra-view `hop:*` or a `reports-for` edge opens the inspector with the raw edge
id as its title, no fact rows, and a NetEm box — item 2's degenerate panel. The
collapse makes this worse: a `hop:*` edge and a declared link now draw
identically, so provenance can no longer be the gate.

### The fix: gate on link presence

Gate selection on **link presence**, not provenance. `linkText.ts`'s private
`primaryLink(edge)` becomes exported and is the single predicate:

- `TopologyPage.onEdgeClick` selects only when `primaryLink(edge) !== null`.
  Every other edge is inert on click.
- `LinkInspector` calls it instead of hand-duplicating the
  `edge.link ?? edge.links?.[0] ?? null` fallback it currently inlines.
- `edgeTitle` / `edgeSubtitle` already call it.

No summary panel. A one-line panel restating what the hover card already says
does not earn a slide-over, and the NetEm section — whose job is to teach that
links are configurable objects — would be a lie on a hop-derived management path
or a reports-for arrow, which have no link object to configure. The hover card
(`EdgeHoverCard`, already handling these via `edgeTitle` / `edgeSubtitle` since
2026-07-12) is the whole affordance for link-less edges.

Net effect on the inspector: it is now only ever reachable with a real link in
hand, so its `{primary && …}` conditional is no longer load-bearing from the UI
path.

## 3. Inspector anchors to the canvas

### The bug, and why the prescribed fix was not enough

`LinkInspector` is `fixed inset-y-0 right-0 w-96`, so it spans the full viewport
height and covers the review bar's `range-apply` button at narrow widths.

`todo/monitor-topology-followups.md` item 1 prescribes `top-[6.5rem] bottom-0`.
That is insufficient. `6.5rem` is a hardcoded guess at the height of AppBar +
ReviewBar, and `ReviewBar` is `flex flex-wrap` carrying a HISTORICAL badge,
source name, session picker, range presets, two `datetime-local` inputs, Apply
and Reset. **At ≤1280px that bar wraps to a second row**, putting Apply *below*
6.5rem — i.e. still covered, at exactly the widths the item is about. An
import-error banner between AppBar and the page adds height too.

### The fix: anchor to the canvas

Drop the constant entirely. Put `relative` on the bordered canvas `div` in
`TopologyPage`, move `LinkInspector` inside it (after `</ReactFlow>`), and change
the aside from `fixed inset-y-0 right-0` to `absolute inset-y-0 right-0`. The
aside is then bounded by the canvas by construction: it cannot reach the review
bar at any width, under any wrap, with or without the banner. It also stops
covering the topology toolbar (View toggle / Sources / Fit), which the `fixed`
version did.

`z-30`, `w-96`, `max-w-full` and the border/shadow are unchanged. `TopoLegend`
stays a bottom-left React Flow `Panel`; its header comment explains that it is
bottom-left *because* the inspector is right-anchored, which remains true, but
the comment's description of the inspector as `fixed` needs updating.

### Explicitly not fixed

`TopologyPage`'s `<main>` carries `h-[calc(100vh-6.5rem)]` — the same stale
constant, which makes the canvas slightly overtall when the review bar wraps.
That is pre-existing, it is not what item 1 reported, and the inspector no
longer depends on it. Leave it and note it in the follow-ups file.

## 4. Escape listener guard

`LinkInspector`'s keydown effect registers a document-level listener whenever
the topology page is mounted, whether or not an edge is selected. Add
`if (edge === null) return;` at the top of the effect body and add `edge` to the
dependency array (currently `[onClose]`). The `if (edge === null) return null`
render guard stays where it is — hooks cannot be conditional.

## 5. LocalNode drops "you are here"

Delete the `<span className="ml-2 …">you are here</span>` from `LocalNode`
(`nodes.tsx`). The node reads `◉ local`. The local node's position at the root of
the map is self-evident; the caption is noise.

Prose references to "you are here" in `src/otto/link/derive.py` docstrings and
in older specs under `docs/superpowers/` are historical and stay as they are.

## Testing

### Vitest

- `topolegend.test.tsx` — three link rows, not five; the `LINK_ORDER`
  exhaustiveness assertion now runs over `EDGE_STYLES`'s `EdgeClass` keys.
- `topoedge.test.tsx` — `edgeClass()` mapping for all five provenances; a
  `local` edge draws the static stroke at 1.5px; a tunnel keeps its casing.
- `topohover.test.tsx` — a declared link's subtitle reads `static · …`.
- `linkinspector.test.tsx` — facts mode unchanged; no keydown listener is
  registered when `edge === null`; the aside is `absolute`, not `fixed`.
- A test for the selection predicate: `primaryLink()` returns `null` for a
  `hop:*`, `local:*` and `reports-for` edge, and non-null for declared, dynamic
  and a collapsed implicit bundle.
- `toponodes.test.tsx` — `LocalNode` has no "you are here" text.

### Dashboard e2e (`tests/e2e/monitor/dashboard/test_review_shell.py`)

- `test_topology_legend_hover_and_tunnel_casing` iterates
  `topo-legend-link-{provenance}` testids — update to the three class testids.
- **New narrow-viewport regression.** At a 1280×800 viewport, with an edge
  selected and the inspector open, `range-apply` must be clickable (a plain
  Playwright `.click()`, no `force`). The existing
  `test_link_inspector_survives_range_change` runs at desktop width and would
  never have caught the reported bug — without this test the fix ships
  unverified.
- A click on a link-less edge (intra-view `hop:*`) must **not** open
  `link-inspector`.

### Gates

`make web` **before** any browser test. `pytest` does not build the web dist —
only `make web` does — and every dev checkout already has one, so a stale bundle
passes locally and fails in CI. This has bitten twice (issues #131, #132). Then
the dashboard lane, then `make coverage` and `nox -s lint` (ruff check *and*
`ruff format --check`).

## Files touched

| file | change |
| --- | --- |
| `web/src/topo/edgeStyles.ts` | `EdgeClass`, `edgeClass()`, three-entry `EDGE_STYLES` |
| `web/src/topo/TopoLegend.tsx` | `LINK_ORDER` → three classes; `Swatch` takes an `EdgeClass` |
| `web/src/topo/LinkEdge.tsx` | look up style via `edgeClass(provenance)` |
| `web/src/topo/linkText.ts` | export `primaryLink`; `edgeSubtitle` via `edgeClass` |
| `web/src/topo/LinkInspector.tsx` | `absolute` geometry, Escape guard, use `primaryLink` |
| `web/src/topo/TopologyPage.tsx` | `relative` canvas div, inspector moves inside it, selection gated on `primaryLink` |
| `web/src/topo/nodes.tsx` | `LocalNode` loses "you are here" |
| `web/src/app.css` | `--topo-edge-static`; delete `--topo-edge-implicit`, `--topo-edge-local` |
| `web/src/__tests__/*` | as above |
| `tests/e2e/monitor/dashboard/test_review_shell.py` | legend testids, narrow-viewport regression, link-less click |
| `todo/monitor-topology-followups.md` | strike items 1–3, note the `h-[calc(100vh-6.5rem)]` residue |
| `todo/TODO.md` | strike lines 6, 13, 14 |
