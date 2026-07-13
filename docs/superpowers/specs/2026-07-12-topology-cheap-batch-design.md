# Topology cheap batch — design

**Date:** 2026-07-12
**Status:** approved, ready for planning
**Base:** `main` @ `d59827d` (the edge-class collapse, merged)

Clears the small remaining items in `todo/monitor-topology-followups.md` so the
next fixture — a larger kitchen-sink built to evaluate D\* — can land without
paying the enumeration tax. Item 1 is a **prerequisite** for that fixture, not
an independent chore: adding a fifth stem today means editing four hardcoded
lists, and the file already records three near-misses from exactly that.

## Status: ready. Rebased onto the #134 fix (`112c0b1`)

**§3 is BACK IN SCOPE.** An earlier revision of this spec dropped it, on a wrong
reading that #134's fix owned the chrome constant. It does not: `112c0b1` made the
inspector a flex sibling that *reserves* horizontal space (`w-96 shrink-0`) with a
re-fit keyed on React Flow's measured **width**. That is the horizontal axis.
`h-[calc(100vh-6.5rem)]` is still on `TopologyPage`'s `<main>` and is still a
hardcoded guess at the chrome height — the **vertical** axis, untouched, and still
this batch's job. The two changes do not interact.

## Scope

1. Retire the fixture-stem enumeration (follow-up item 1).
2. Cosmetics: `HostNode`'s dangling separator; export `pairKey` (item 3).
3. Replace the stale `h-[calc(100vh-6.5rem)]` chrome constant (residue note).
4. `useCallback` for `LinkInspector`'s `onClose` (residue note).
5. MiniMap as a toggle, defaulting off (item 2 — Chris's ruling).

Out: D\* layering, tunnels-as-overlays, obstacle-aware skip-column routing, and
the large fixture itself. Each is a separate design.

## Postmortem from #134 — read before touching the browser tests

`d59827d` turned CI red (#134). The root cause was **the inspector overlaying the
map and hiding `chassis-a`**, fixed by `112c0b1` making the panel reserve space
instead. Three things that batch got wrong, all of which bear on this one:

**Deleting a workaround means inheriting everything it was hiding.** The test
`test_link_inspector_survives_range_change` forced a 1600px viewport. That override
was hiding **two** occlusions, not one. `d59827d` fixed the occlusion it was named
for (the panel covering the review bar), deleted the override, and thereby started
exercising a width regime in which a *second* occlusion — the panel covering the
map's rightmost node — was live. Audit a workaround against the *regime* it avoids,
not the bug it is named for.

**`locator.click()` is not an occlusion check.** Chromium PASSED the whole way
through, and it was a **false pass** — the bug was 100% present there.
`elementFromPoint` returns the panel in chromium too, and a raw `page.mouse.click()`
at that point fails to navigate. Playwright's `locator.click()` auto-scrolls and
retries, so it can manufacture a click a real user could never make. To assert an
element is genuinely reachable, assert `document.elementFromPoint(x, y)` resolves
back to it — the technique `_point_on_edge` in `test_review_shell.py` already uses.

**Do not read a hypothesis out of Playwright's timeout flavour.** The failure said
`"waiting for element to be visible, enabled and stable"`, not `"intercepts pointer
events"`, and that was used to (wrongly) rule out occlusion. Which actionability
check times out first is not a diagnosis.

**The browser gate is `nox -s dashboard`** (chromium + firefox + webkit). A bare
`uv run pytest tests/e2e/monitor/dashboard` is **chromium only**, and reporting it
as "the dashboard lane" is how this shipped.

## 1. One source of truth for fixture stems

`build_all()` in `scripts/gen_monitor_fixtures.py` returns the fixture documents
keyed by file stem. That is the source of truth. Four other sites re-declare the
stems by hand:

| site | today | after |
| --- | --- | --- |
| `tests/unit/scripts/test_monitor_fixture_files.py:18` | `@pytest.mark.parametrize("stem", ["kitchen-sink", "minimal", "drift", "cascade"])` | `@pytest.mark.parametrize("stem", sorted(build_all()))` |
| `web/src/__tests__/exportdoc.test.ts:35` | `for (const doc of [kitchenDoc, minimalDoc, driftDoc, cascadeDoc])` | reads `web/fixtures/*.json` from disk and parses each |
| `tests/unit/scripts/test_gen_monitor_fixtures.py:30` | `assert set(docs) == {"kitchen-sink", "minimal", "drift", "cascade"}` | `assert {"kitchen-sink", "cascade"} <= set(docs)` |
| — | *(nothing checks for orphans)* | new test: stems on disk == `set(build_all())` |

### Why each change

**The drift guard's `parametrize` is the dangerous one.**
`test_monitor_fixture_files.py` exists to prove every committed fixture is
byte-identical to a fresh regeneration. Parametrized over a hardcoded list, a
fifth fixture is simply never checked — the guard passes, silently, while the
committed file rots. Deriving from `sorted(build_all())` makes the guard's reach
follow the generator.

**`exportdoc.test.ts`'s loop is named for a claim it does not make.** The test
says "parses every committed fixture" but iterates four hand-listed imports.
Reading the directory makes the name true by construction. Use `node:fs` +
`readdirSync`/`readFileSync` — already the idiom in `topology.test.ts`,
`pages.test.tsx`, `events_panel.test.tsx` — **not** Vite's `import.meta.glob`,
which would need `vite/client` added to `tsconfig`'s `types` (it is absent
today) and buys nothing here.

The file's *named* static imports (`kitchenDoc`, `cascadeDoc`, …) stay. Those are
not an inventory; they back content-specific assertions ("element derivation
(kitchen-sink)"). Only the enumeration is retired.

**Equality becomes subset.** `test_gen_monitor_fixtures.py`'s
`set(docs) == {four names}` is a duplicate inventory that must be edited every
time a fixture is added — pure tax, and one of the three near-misses. What it
should express is that the fixtures the *web tests hard-depend on* exist, which
is a real contract and is stable as the inventory grows. Hence
`{"kitchen-sink", "cascade"} <= set(docs)`.

**The orphan direction is currently unguarded.** Nothing notices a `.json` in
`web/fixtures/` that `build_all()` no longer generates — it would sit there,
stale, still imported by name from TS tests. One new test asserts the stems on
disk are exactly `set(build_all())`, closing the loop in both directions. This is
the only place the two sources are compared, and both sides of the comparison
are derived.

### How this is verified

By **mutation**, not by inspection: add a throwaway fifth entry to `build_all()`
without committing a fixture file for it, and confirm the drift guard now fails
on that stem (today it passes, silently). Then remove it. A test that merely
passes proves nothing here — the whole defect class is "passes while not
checking".

## 2. Cosmetics

### `HostNode`'s detail line

`web/src/topo/nodes.tsx` concatenates three conditional strings:

```tsx
<p className="mt-0.5 text-xs text-gray-400">
  {status === "unreachable" ? "unreachable · " : ""}
  {slotBadge && data.host?.slot != null ? `slot ${data.host.slot}` : ""}
  {slotBadge && data.host?.slot != null ? "" : (data.host?.board ?? "")}
</p>
```

Two defects. An unreachable host with no slot badge and no board renders
`"unreachable · "` — a separator with nothing after it. And a reachable host with
neither renders an empty `<p>`, which still occupies its `mt-0.5` margin.

Build the parts, join them, and render nothing when there are none:

```tsx
const detail = [
  status === "unreachable" ? "unreachable" : null,
  slotBadge && data.host?.slot != null ? `slot ${data.host.slot}` : (data.host?.board ?? null),
]
  .filter((part) => part !== null && part !== "")
  .join(" · ");
```

…rendering `{detail !== "" && <p …>{detail}</p>}`. The separator now exists only
between two present parts, which is what `·` means.

### `pairKey`

`topology.ts` already has `function pairKey(a, b)` (private). `TopologyPage`
re-derives the same `[source, target].sort().join("~")` twice (lines 102 and
110). Export `pairKey` and call it from both. The rule is one *definition*, not
one call site.

## 3. Shell layout: the chrome constant goes

`TopologyPage`'s `<main>` carries `h-[calc(100vh-6.5rem)]` — a hardcoded guess at
AppBar + ReviewBar height.

**Measured, not assumed** (2026-07-12, headless chromium/firefox/webkit against
`kitchen-sink`):

| regime | real chrome | `6.5rem` claims | canvas is |
| --- | --- | --- | --- |
| ReviewBar unwrapped (≥ ~1123px) | **99px** | 104px | 5px too short |
| ReviewBar wrapped (≤ ~1101px) | **145px** | 104px | **41px too tall** ⇒ page scrolls |

So the constant is wrong in *every* regime — merely harmlessly in one. `ReviewBar`
is `flex flex-wrap`, and it wraps at **~1101–1123px**, not at ≤1280px as earlier
revisions of this spec (and the previous branch's commit messages) asserted. That
threshold claim was never measured and is **corrected here**: it was off by ~180px.
The fix is unaffected — it removes the constant rather than re-tuning it — but the
justification for it is "the guess is simply not the real height", not "the bar
wraps at 1280px".

Make the shell a flex column and let the topology page consume what is left:

```tsx
// App.tsx
<div className="flex min-h-screen flex-col">
  <AppBar />        {/* natural height */}
  <ReviewBar />     {/* natural height — may wrap */}
  <Switch> … </Switch>
</div>
```

```tsx
// TopologyPage.tsx
- <main data-testid="topology-page" className="flex h-[calc(100vh-6.5rem)] flex-col gap-3 p-4">
+ <main data-testid="topology-page" className="flex min-h-0 flex-1 flex-col gap-3 p-4">
```

**`min-h-screen`, not `h-screen`.** With `min-h-screen`, the shell is *at least*
viewport-tall: the topology page's `flex-1` resolves to exactly
viewport-minus-chrome (correct whatever the chrome's real height turns out to
be), while Overview and Subject — which are plain document-flow, `flex flex-col
gap-6 p-4` — simply grow the root past the viewport and **the document keeps
scrolling exactly as it does today**.

The alternative app-shell (`h-screen` + an `overflow-y-auto` content div) is also
correct but moves the scrollbar from the document into an inner element for every
page, changing scroll restoration, sticky behaviour, and anything in the e2e
suite that scrolls. That is a real blast radius for no benefit here, so it is
explicitly rejected.

`EmptyState` (the no-data branch) stays inside the flex column; it is unaffected.

**Interaction with `112c0b1` (the #134 fix): none.** That commit made the inspector
a flex sibling reserving horizontal space, with a re-fit keyed on React Flow's
measured **width**. This change is purely **vertical** — `<main>`'s height. A
wrapping ReviewBar changes the canvas's height, not its width, so it fires no extra
re-fit. What it does buy: `fitView` on load now fits the graph to a canvas whose
height is actually correct, instead of one that is overtall by however much the
review bar wrapped.

## 4. `onClose` stops re-subscribing

`TopologyPage` passes `onClose={() => setSelected(null)}` — a fresh arrow on every
render — so `LinkInspector`'s Escape effect (guarded on `d59827d` to only register
while an edge is selected) tears down and re-subscribes each render. Wrap it:

```tsx
const closeInspector = useCallback(() => setSelected(null), []);
```

`setSelected` is a `useState` setter and is stable, so the empty dep array is
correct.

## 5. MiniMap: a toggle, default off

Add a MiniMap toggle beside the existing Sources button — same `aria-pressed`
pill treatment, `data-testid="minimap-toggle"`, default **off** — rendering React
Flow's `<MiniMap>` inside `<ReactFlow>` when on.

**Ship it WITHOUT `onlyRenderVisibleElements`,** which the follow-up note pairs it
with. That flag culls off-screen elements from the DOM. The dashboard e2e counts
edges (`_wait_for_links(page, at_least=6)`) on a canvas that *already* withholds
edges until both endpoint nodes are measured — and that withholding is precisely
what produced the #130 webkit flake (`assert 0 >= 6`). Layering viewport culling
on top of it is a good way to reinvent that flake, and the flag is a performance
change that should be justified and tested on its own rather than smuggled in
with a UI toggle. Left in the follow-ups file as a separate item.

## Testing

- **Vitest:** `HostNode` renders no dangling separator and no empty `<p>` (cover
  unreachable-with-nothing, reachable-with-nothing, unreachable-with-slot);
  `pairKey` is exported and order-independent; the MiniMap toggle is absent by
  default and present when pressed; `TopologyPage`'s `<main>` carries `flex-1`
  and no `h-[calc(…)]`.
- **Pytest:** the drift guard parametrizes over `build_all()`; the new orphan
  test; the relaxed subset assertion.
- **Mutation check (item 1):** a throwaway fifth `build_all()` entry must turn
  the drift guard red. Prove it, then revert.
- **Dashboard e2e:** the MiniMap toggle round-trip. Existing topology specs must
  stay green — the shell change touches every page's layout, so the whole
  dashboard lane is the gate, not just the topology specs.
- **The browser gate is `nox -s dashboard` — chromium AND firefox AND webkit.**
  A bare `uv run pytest tests/e2e/monitor/dashboard` runs chromium only, and
  reporting that as "the dashboard lane" is exactly how #134 shipped. If a full
  three-engine run is too slow to iterate on, iterate on `-k webkit` (the leg that
  has broken twice: #130 and #134) and run all three before claiming green.
- **The MiniMap is an overlay panel, so assert it does not occlude the map.** It
  renders bottom-right inside the canvas — the same corner class of hazard that
  caused #134. `locator.click()` will NOT catch that: it auto-scrolls and retries,
  so it can manufacture a click a user could never make (chromium false-passed #134
  for exactly this reason). Assert with `document.elementFromPoint(x, y)` at a
  node's centre, resolving back to that node — the technique `_point_on_edge`
  already uses.
- **`make web` before any browser test.** pytest does not build the dist; a stale
  bundle certifies the wrong artifact (#131, #132).

## Files touched

| file | change |
| --- | --- |
| `tests/unit/scripts/test_monitor_fixture_files.py` | parametrize from `build_all()`; new orphan test |
| `tests/unit/scripts/test_gen_monitor_fixtures.py` | equality → subset |
| `web/src/__tests__/exportdoc.test.ts` | directory-derived parse loop |
| `web/src/topo/nodes.tsx` | `HostNode` detail parts |
| `web/src/data/topology.ts` | export `pairKey` |
| `web/src/topo/TopologyPage.tsx` | `pairKey`, `flex-1`, `useCallback`, MiniMap toggle |
| `web/src/App.tsx` | `flex min-h-screen flex-col` shell |
| `web/src/__tests__/*`, `tests/e2e/monitor/dashboard/test_review_shell.py` | as above |
| `todo/monitor-topology-followups.md` | strike what ships; keep `onlyRenderVisibleElements` as its own item |
