# Topology Cheap Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the fixture-stem enumeration (a prerequisite for the larger
D\*-evaluation fixture), fix two topology cosmetics, replace the last stale chrome
constant, stop the Escape listener re-subscribing every render, and add a
default-off MiniMap toggle.

**Architecture:** Five independent changes. `build_all()` in
`scripts/gen_monitor_fixtures.py` becomes the single source of fixture stems, with
every other site deriving from it or from the fixtures directory. The app shell
becomes a `min-h-screen` flex column so the topology canvas can claim the remaining
height rather than subtracting a guessed one. Nothing here touches the `format:1`
export contract or the inspector's horizontal layout (the #134 fix, `112c0b1`).

**Tech Stack:** Python 3.12 + pytest, React 19 + TypeScript, `@xyflow/react`,
Tailwind v4, Vitest, Playwright via pytest.

**Spec:** `docs/superpowers/specs/2026-07-12-topology-cheap-batch-design.md`
(`db42d82`). **Read its "Postmortem from #134" section before touching any browser
test.**

## Global Constraints

- **The browser gate is `nox -s dashboard`** — chromium AND firefox AND webkit
  ([noxfile.py:196](noxfile.py#L196)). A bare
  `uv run pytest tests/e2e/monitor/dashboard` runs **chromium only**, and reporting
  that as "the dashboard lane" is exactly how #134 shipped.
- **To run one engine under raw pytest, the flag is `--browser webkit`.** NOT
  `-k webkit` — that is a *test-name* filter, it matches nothing here, and it
  **deselects all 46 tests and exits 0**. A command that measures nothing and
  reports success is the third instance of this trap in this workstream (after the
  stale web dist and the chromium-only lane). Verified:
  `pytest … -k webkit` → "no tests collected (46 deselected)";
  `pytest … --browser webkit` → "46 tests collected".
  (`nox -s dashboard -k webkit` IS valid — there `-k` filters nox *sessions*, not
  pytest tests. The two `-k`s mean different things; don't mix them up.)
- **`locator.click()` is NOT an occlusion check.** It auto-scrolls and retries, so
  it can manufacture a click a real user could never make — chromium false-passed
  #134 for precisely this reason. To assert an element is genuinely reachable, use
  `document.elementFromPoint(x, y)` at its centre and require it to resolve back to
  that element (the technique `_point_on_edge` in `test_review_shell.py` already
  uses).
- **`make web` before ANY browser test.** pytest does not build the web dist; only
  `make web` does. A stale bundle certifies the wrong artifact (#131, #132).
- Web gates: `cd web && npx vitest run`, `npm run check` (**Biome only**), and
  `npm run typecheck` (**tsc — a separate command**). Run BOTH.
- `nox -s lint` = `ruff check` AND `ruff format --check`.
- No `from __future__ import annotations` in Python (breaks the Sphinx `-W` build).
- Commits: conventional prefix + an `Assisted-by: Claude Opus 4.8` trailer. **No
  `Co-Authored-By`.** Self-commit is expected — this is a worktree branch.
- No heavy/parallel test load on the dev VM; run scoped suites.
- Working directory: `/home/vagrant/otto-sh/.claude/worktrees/topology-cheap-batch`,
  branch `worktree-topology-cheap-batch`, based on `112c0b1`.

## File Structure

| File | Responsibility after this plan |
| --- | --- |
| `scripts/gen_monitor_fixtures.py` | Unchanged. `build_all()` is the sole declaration of which fixtures exist. |
| `tests/unit/scripts/test_monitor_fixture_files.py` | Drift guard, parametrized from `build_all()`; plus the orphan check (disk == generator). |
| `tests/unit/scripts/test_gen_monitor_fixtures.py` | Round-trip + a *subset* assertion naming only the load-bearing fixtures. |
| `web/src/__tests__/exportdoc.test.ts` | Parses whatever is in `web/fixtures/`, derived from disk. |
| `web/src/topo/nodes.tsx` | `HostNode`'s detail line joins present parts; renders nothing when empty. |
| `web/src/data/topology.ts` | Exports `pairKey`. |
| `web/src/topo/TopologyPage.tsx` | Uses `pairKey`; `flex-1` height; memoised `onClose`; MiniMap toggle. |
| `web/src/App.tsx` | `flex min-h-screen flex-col` shell. |

---

### Task 1: Make `build_all()` the only fixture-stem list

**Files:**
- Modify: `tests/unit/scripts/test_monitor_fixture_files.py` (whole file)
- Modify: `tests/unit/scripts/test_gen_monitor_fixtures.py:28-32`
- Modify: `web/src/__tests__/exportdoc.test.ts:1-9`, `:34-40`

**Interfaces:**
- Consumes: `build_all() -> dict[str, MonitorExport]` and `dumps(doc) -> str` from
  `scripts.gen_monitor_fixtures` (both already exist and are unchanged).
- Produces: nothing new. This task only removes duplication.

**Why this is first:** the next piece of work is a larger fixture for evaluating
D\*. Adding a fifth stem today means editing four hardcoded lists, and the
follow-ups file already records **three near-misses** from exactly that. After this
task, adding a fixture is a one-line change to `build_all()`.

- [ ] **Step 1: Write the failing test — rewrite `tests/unit/scripts/test_monitor_fixture_files.py`**

Replace the whole file:

```python
"""Drift guard: committed web/fixtures/ must match regeneration exactly.

If this fails, the generator or the export models changed without
re-stamping the fixtures — run ``make monitor-fixtures`` and commit the
result (the byte-identical guarantee is what makes the fixtures a reliable
contract artifact for the web tests).

Both directions are guarded, and both derive from ``build_all()``. A
hand-written stem list here would silently stop covering the next fixture
someone adds — which is the whole defect class this file used to have.
"""

from pathlib import Path

import pytest

from scripts.gen_monitor_fixtures import build_all, dumps

_FIXTURE_DIR = Path(__file__).parents[3] / "web" / "fixtures"


@pytest.mark.parametrize("stem", sorted(build_all()))
def test_committed_fixture_is_fresh(stem: str):
    committed = (_FIXTURE_DIR / f"{stem}.json").read_text(encoding="utf-8")
    assert committed == dumps(build_all()[stem]), (
        f"web/fixtures/{stem}.json is stale — run 'make monitor-fixtures' and commit"
    )


def test_fixture_dir_matches_the_generator():
    """The other direction. The freshness guard above walks the GENERATOR's keys,
    so it is blind to a committed .json that ``build_all()`` no longer produces —
    an orphan would sit there stale forever, still imported by name from the web
    tests. Compare the two inventories directly."""
    on_disk = {path.stem for path in _FIXTURE_DIR.glob("*.json")}
    assert on_disk == set(build_all()), (
        "web/fixtures/ and build_all() disagree — either an orphan file whose "
        "generator was removed, or a fixture that was generated but never "
        "committed. Run 'make monitor-fixtures'."
    )
```

- [ ] **Step 2: Prove the drift guard's reach actually follows the generator**

This is the point of the task, and a passing test does not demonstrate it — the
defect class is "passes while not checking". Mutate and observe.

Temporarily add a fifth entry to `build_all()` in `scripts/gen_monitor_fixtures.py`
that has no committed file, e.g. inside the returned dict:

```python
        "zz-throwaway": minimal(),
```

Run: `uv run pytest tests/unit/scripts/test_monitor_fixture_files.py -q`

Expected: **FAIL** — `test_committed_fixture_is_fresh[zz-throwaway]` errors because
`web/fixtures/zz-throwaway.json` does not exist, and
`test_fixture_dir_matches_the_generator` fails on the inventory mismatch.

Before this task, the parametrize list was hardcoded, so the new stem would have
been **silently unchecked**. Paste both the failing output and this reasoning into
your report, then **revert the mutation** and re-run to confirm green.

- [ ] **Step 3: Relax the duplicate inventory in `tests/unit/scripts/test_gen_monitor_fixtures.py`**

Lines 28-32 currently read:

```python
def test_documents_round_trip_and_stems():
    docs = build_all()
    assert set(docs) == {"kitchen-sink", "minimal", "drift", "cascade"}
    for doc in docs.values():
        assert MonitorExport.model_validate(json.loads(dumps(doc))) is not None
```

Change the equality to a subset:

```python
def test_documents_round_trip_and_stems():
    docs = build_all()
    # Subset, not equality. An inventory list here has to be edited every time a
    # fixture is added — pure tax, and one of the three near-misses this
    # duplication already caused. What is worth pinning is that the fixtures other
    # suites hard-depend on BY NAME still exist: exportdoc.test.ts and
    # topology.test.ts both reach for kitchen-sink and cascade directly.
    assert {"kitchen-sink", "cascade"} <= set(docs)
    for doc in docs.values():
        assert MonitorExport.model_validate(json.loads(dumps(doc))) is not None
```

- [ ] **Step 4: Derive the fixture list from disk in `web/src/__tests__/exportdoc.test.ts`**

The test on line 34 is called *"parses every committed fixture"* but iterates four
hand-listed imports — it would silently not parse a fifth. Make its name true.

Replace the import block (lines 1-9). **Delete the `minimalDoc`, `driftDoc` and
`cascadeDoc` imports** — after this change they are used nowhere else and Biome
will fail on unused imports. **Keep `kitchenDoc`**, which is still used at lines 62
and 90 for content-specific assertions:

```ts
// The import-path contract, tested against the REAL committed fixtures —
// the same files the Playwright specs and manual dev use.
//
// The "every committed fixture" test below reads the DIRECTORY rather than a
// hand-written list of imports, so its name is true by construction: a new
// fixture is covered the moment it is committed. (node:fs, not Vite's
// import.meta.glob — tsconfig has no `vite/client` in `types`, and readFileSync
// is already the idiom in topology.test.ts and pages.test.tsx.)
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import kitchenDoc from "../../fixtures/kitchen-sink.json";
import {
  ExportParseError,
  metricsForSubject,
  parseExportDocument,
  presetRange,
  sessionBounds,
  subjectKind,
} from "../data/exportDoc";

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "../../fixtures");
```

Then replace the test at lines 34-40:

```ts
  it("parses every committed fixture without warnings", () => {
    const files = readdirSync(FIXTURES).filter((name) => name.endsWith(".json"));
    // Guard the guard: an empty glob would make every assertion below vacuous.
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const result = parseExportDocument(readFileSync(join(FIXTURES, file), "utf-8"));
      expect(result.warnings, file).toEqual([]);
      expect(result.sessions.length, file).toBeGreaterThan(0);
    }
  });
```

Leave the rest of the file alone.

- [ ] **Step 5: Run the suites**

Run:
```bash
uv run pytest tests/unit/scripts -q
cd web && npx vitest run && npm run check && npm run typecheck && cd ..
```
Expected: all PASS, Biome and tsc clean.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/scripts/test_monitor_fixture_files.py \
  tests/unit/scripts/test_gen_monitor_fixtures.py \
  web/src/__tests__/exportdoc.test.ts
git commit -m "test(monitor): make build_all() the only list of fixture stems

Four sites re-declared the fixture stems by hand. The dangerous one was the drift
guard's parametrize: a fifth fixture would simply never be checked for freshness —
the guard passes, silently, while the committed file rots. exportdoc.test.ts had
the same shape, in a test literally named 'parses every committed fixture' that
parsed four hand-listed imports.

Both now derive: the guard from build_all(), the web test from the fixtures
directory. The set-equality assertion in test_gen_monitor_fixtures relaxes to a
subset naming only the fixtures other suites reach for BY NAME — an inventory list
there was pure tax, and one of the three near-misses this duplication has already
caused.

Adds the missing direction: nothing noticed a committed .json that build_all() no
longer generates. Verified by mutation — a fifth build_all() entry with no
committed file now turns the guard red, where before it was silently unchecked.

This unblocks the larger D*-evaluation fixture: adding it is now a one-line change.

Assisted-by: Claude Opus 4.8"
```

---

### Task 2: HostNode's detail line, `pairKey`, and the `onClose` identity

**Files:**
- Modify: `web/src/topo/nodes.tsx` (`HostNode`, ~lines 100-120)
- Modify: `web/src/data/topology.ts:92` (export `pairKey`)
- Modify: `web/src/topo/TopologyPage.tsx:145`, `:153`, `:252`
- Test: `web/src/__tests__/toponodes.test.tsx`, `web/src/__tests__/topology.test.ts`

**Interfaces:**
- Produces: `export function pairKey(a: string, b: string): string` from
  `web/src/data/topology.ts` — the order-independent `~`-joined key for an
  unordered node pair. Already exists as a private function; only the `export`
  keyword and a docstring are added.
- Consumes: nothing from Task 1.

Three small correctness/DRY fixes that share one test cycle.

- [ ] **Step 1: Write the failing tests — `web/src/__tests__/toponodes.test.tsx`**

Append to the existing `describe("HostNode", …)` block:

```tsx
  it("renders no dangling separator when there is nothing to separate", () => {
    // "unreachable · " with nothing after it: the separator is punctuation
    // BETWEEN two parts, so it must not survive when the second part is absent.
    const host: TopoNode = {
      id: "lonely",
      kind: "host",
      depth: 1,
      label: "lonely",
      host: { id: "lonely", element: "lonely" } as TopoNode["host"],
      effective: "unreachable",
    };
    render(<HostNode data={host} />);
    const root = screen.getByTestId("topo-node-lonely");
    expect(root.textContent).toContain("unreachable");
    expect(root.textContent).not.toContain("·");
  });

  it("separates two present parts with a single ·", () => {
    const host: TopoNode = {
      id: "rack-a_n1",
      kind: "host",
      depth: 2,
      label: "rack-a_n1",
      host: { id: "rack-a_n1", element: "rack-a", slot: 1 } as TopoNode["host"],
      effective: "unreachable",
    };
    render(<HostNode data={{ ...host, slotBadge: true }} />);
    expect(screen.getByTestId("topo-node-rack-a_n1").textContent).toContain(
      "unreachable · slot 1",
    );
  });
```

And in `web/src/__tests__/topology.test.ts`, add `pairKey` to the existing import
from `../data/topology`, then append:

```ts
describe("pairKey", () => {
  it("is order-independent, so an unordered pair has one key", () => {
    expect(pairKey("a", "b")).toBe(pairKey("b", "a"));
  });
});
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd web && npx vitest run src/__tests__/toponodes.test.tsx src/__tests__/topology.test.ts`
Expected: FAIL — the dangling-separator test finds `·`, and `pairKey` is not exported.

- [ ] **Step 3: Fix `HostNode`'s detail line — `web/src/topo/nodes.tsx`**

Replace the `<p className="mt-0.5 …">` block (currently three concatenated
conditional strings) with a parts list. Insert above the `return`:

```tsx
  // Parts, then join — not string concatenation with a baked-in separator. The
  // old form emitted "unreachable · " with nothing after it when a host had
  // neither a slot badge nor a board, and an empty <p> (still carrying its
  // margin) when it had neither and was reachable.
  const detail = [
    status === "unreachable" ? "unreachable" : null,
    slotBadge && data.host?.slot != null ? `slot ${data.host.slot}` : (data.host?.board ?? null),
  ]
    .filter((part) => part !== null && part !== "")
    .join(" · ");
```

and render:

```tsx
      {detail !== "" && <p className="mt-0.5 text-xs text-gray-400">{detail}</p>}
```

- [ ] **Step 4: Export `pairKey` — `web/src/data/topology.ts:92`**

```ts
/** The key for an UNORDERED pair of nodes: sorted, so `pairKey(a, b)` and
 * `pairKey(b, a)` agree. Parallel-edge grouping depends on that. */
export function pairKey(a: string, b: string): string {
```

(The body is unchanged.)

- [ ] **Step 5: Use it, and memoise `onClose` — `web/src/topo/TopologyPage.tsx`**

Add `pairKey` to the existing import from `../data/topology`, and add `useCallback`
to the existing `react` import.

Line 145 and line 153 both re-derive the same sort-join. Replace:

```tsx
      const key = pairKey(e.source, e.target);
```

```tsx
      data: { edge: e, groupSize: groupSizes.get(pairKey(e.source, e.target)) ?? 1 },
```

Then memoise the inspector's close handler. Near the `selected` state (line 116):

```tsx
  // Stable identity: LinkInspector's Escape effect depends on `onClose`, so a
  // fresh arrow every render made it tear down and re-subscribe the document
  // keydown listener on every render. `setSelected` is a useState setter and is
  // itself stable, so the empty dep array is correct.
  const closeInspector = useCallback(() => setSelected(null), []);
```

and at line 252:

```tsx
          <LinkInspector edge={selectedEdge} onClose={closeInspector} />
```

- [ ] **Step 6: Run and verify green**

Run: `cd web && npx vitest run && npm run check && npm run typecheck`
Expected: all PASS, clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/topo/nodes.tsx web/src/data/topology.ts web/src/topo/TopologyPage.tsx \
  web/src/__tests__/toponodes.test.tsx web/src/__tests__/topology.test.ts
git commit -m "fix(monitor): tidy HostNode's detail line, dedupe pairKey, stabilise onClose

HostNode built its detail line by concatenating three conditional strings with a
separator baked into the first, so an unreachable host with no slot badge and no
board rendered 'unreachable · ' — a separator with nothing to separate — and a
reachable one with neither rendered an empty <p> that still carried its margin.
Build the parts, filter, join; render nothing when there is nothing.

pairKey already existed (private) in topology.ts while TopologyPage re-derived the
same sort-join twice. Export it: one definition, three call sites.

LinkInspector's onClose was a fresh arrow on every render of TopologyPage, so the
Escape keydown effect tore down and re-subscribed the document listener every
render. useCallback settles it.

Assisted-by: Claude Opus 4.8"
```

---

### Task 3: Kill the last stale chrome constant

**Files:**
- Modify: `web/src/App.tsx:34-38` (shell wrapper)
- Modify: `web/src/topo/TopologyPage.tsx:179` (`<main>`'s height)
- Test: `web/src/__tests__/pages.test.tsx` or `shell.test.tsx` (assert the classes)

**Interfaces:** none — pure layout.

**The bug.** `TopologyPage`'s `<main>` carries `h-[calc(100vh-6.5rem)]`, a hardcoded
guess at AppBar + ReviewBar height. `ReviewBar` is `flex flex-wrap` with eight
controls including two `datetime-local` inputs, so at ≤1280px it **wraps to a second
row** — the chrome is then taller than 6.5rem, the canvas is overtall, and the page
scrolls. An import-error banner adds height too.

**This is the same class of bug as #134's sibling** — a magic constant that is
stale precisely in the regime it is needed. It is the last one.

**It does NOT interact with the #134 fix (`112c0b1`).** That made the inspector a
flex sibling reserving **horizontal** space, with a re-fit keyed on React Flow's
measured **width**. This is the **vertical** axis. A wrapping ReviewBar changes the
canvas's height, not its width, so no extra re-fit fires. What it buys: `fitView` on
load fits the graph to a canvas whose height is actually right.

- [ ] **Step 1: Write the failing test**

Add to `web/src/__tests__/pages.test.tsx` (which already renders the app shell — if
it does not, use `shell.test.tsx`; follow whichever already mounts `<App />`):

```tsx
  it("sizes the topology canvas by flex, not by a guessed chrome height", () => {
    // h-[calc(100vh-6.5rem)] hardcoded AppBar + ReviewBar's height. ReviewBar is
    // flex-wrap, so at <=1280px it wraps and that constant is wrong — the canvas
    // ends up overtall and the page scrolls. Same class as the two occlusion bugs
    // in #134: a magic constant that is stale exactly where it matters.
    renderApp("#/topology");
    const main = screen.getByTestId("topology-page");
    expect(main.className).not.toContain("100vh");
    expect(main.className).toContain("flex-1");
  });
```

Use whatever helper that file already has for mounting the app at a route; do not
invent a new one.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run src/__tests__/pages.test.tsx`
Expected: FAIL — the class list still contains `h-[calc(100vh-6.5rem)]`.

- [ ] **Step 3: Make the shell a flex column — `web/src/App.tsx`**

Wrap the shell's children. `AppBar`, `ReviewBar` and the import-error banner keep
their natural heights; the routed content takes what is left:

```tsx
  return (
    <ImportProvider>
      <div className="flex min-h-screen flex-col">
        <AppBar />
        {hasData ? (
          <Router hook={useHashLocation}>
            <ReviewBar />
            {/* …import-error banner unchanged… */}
            <Switch>
              {/* …routes unchanged… */}
            </Switch>
          </Router>
        ) : (
          <EmptyState />
        )}
      </div>
    </ImportProvider>
  );
```

**`min-h-screen`, NOT `h-screen`.** With `min-h-screen` the shell is *at least*
viewport-tall, so topology's `flex-1` resolves to exactly viewport-minus-chrome
whatever the chrome's real height is — while Overview and Subject (plain
document-flow, `flex flex-col gap-6 p-4`) simply grow the root past the viewport and
**the document keeps scrolling exactly as it does today**. The `h-screen` +
`overflow-y-auto` variant is also correct but moves the scrollbar into an inner div
for every page, changing scroll restoration, sticky behaviour, and anything in the
e2e suite that scrolls. Do not use it.

- [ ] **Step 4: Let the topology page claim the remainder — `web/src/topo/TopologyPage.tsx:179`**

```tsx
      <main data-testid="topology-page" className="flex min-h-0 flex-1 flex-col gap-3 p-4">
```

(`min-h-0` is required: a flex child's default `min-height: auto` refuses to shrink
below its content, which would defeat the inner canvas's own `min-h-0 grow`.)

- [ ] **Step 5: Verify green, then verify in a browser at the width that matters**

Run:
```bash
cd web && npx vitest run && npm run check && npm run typecheck && cd ..
make web
uv run pytest tests/e2e/monitor/dashboard -q -k webkit
```

Then look at it. At **1280×720**, with the review bar WRAPPED, confirm the topology
canvas ends at the viewport bottom rather than overflowing it, and that Overview and
Subject still scroll the document normally. Screenshot both and report the paths.

- [ ] **Step 6: Commit**

```bash
git add web/src/App.tsx web/src/topo/TopologyPage.tsx web/src/__tests__/pages.test.tsx
git commit -m "fix(monitor): size the topology canvas by flex, not a guessed chrome height

h-[calc(100vh-6.5rem)] hardcoded the height of AppBar + ReviewBar. ReviewBar is
flex-wrap with eight controls including two datetime-local inputs, so at <=1280px it
wraps to a second row, the constant is wrong, the canvas is overtall and the page
scrolls. This was the last of the stale chrome constants — the same class as both
occlusion bugs behind #134.

The shell becomes 'flex min-h-screen flex-col' and the topology main takes
'min-h-0 flex-1'. min-h-screen, not h-screen: Overview and Subject keep growing past
the viewport and the DOCUMENT keeps scrolling, so the scrollbar does not move into an
inner div for every page.

Independent of 112c0b1, which reserves HORIZONTAL space for the inspector and re-fits
on measured width. This is the vertical axis; a wrapping review bar changes the
canvas's height, not its width.

Assisted-by: Claude Opus 4.8"
```

---

### Task 4: MiniMap toggle, default off

**Files:**
- Modify: `web/src/topo/TopologyPage.tsx` (toggle state, button, `<MiniMap>`)
- Test: `web/src/__tests__/pages.test.tsx` (or wherever `TopologyPage` is mounted)
- Test: `tests/e2e/monitor/dashboard/test_review_shell.py` (new test)

**Interfaces:** none exported.

Chris's ruling: a toggle, defaulting **off**.

**Ship it WITHOUT `onlyRenderVisibleElements`,** which the follow-ups file pairs it
with. That flag culls off-screen elements from the DOM; the dashboard e2e counts
edges (`_wait_for_links(page, 6)`) on a canvas that *already* withholds edges until
both endpoint nodes are measured — and that withholding is exactly what produced the
#130 webkit flake (`assert 0 >= 6`). It is a performance change that must be
justified and tested on its own. Leave it in the follow-ups file.

- [ ] **Step 1: Write the failing vitest**

```tsx
  it("hides the minimap by default and shows it when toggled", async () => {
    renderApp("#/topology");
    expect(screen.queryByTestId("topo-minimap")).toBeNull();
    fireEvent.click(screen.getByTestId("minimap-toggle"));
    expect(await screen.findByTestId("topo-minimap")).toBeTruthy();
  });
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd web && npx vitest run src/__tests__/pages.test.tsx`
Expected: FAIL — no `minimap-toggle`.

- [ ] **Step 3: Implement — `web/src/topo/TopologyPage.tsx`**

Add `MiniMap` to the existing `@xyflow/react` import. Add state beside the existing
`sources` state:

```tsx
  const [minimap, setMinimap] = useState(false);
```

Add the button immediately after the existing `sources-toggle` button, copying its
pill treatment exactly (same classes, same `aria-pressed` pattern):

```tsx
          <button
            type="button"
            data-testid="minimap-toggle"
            aria-pressed={minimap}
            onClick={() => setMinimap((v) => !v)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              minimap
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            Minimap
          </button>
```

And render it inside `<ReactFlow>`, beside `<Controls />` and `<TopoLegend />`:

```tsx
            {minimap && <MiniMap data-testid="topo-minimap" pannable zoomable />}
```

If React Flow's `<MiniMap>` does not forward `data-testid`, wrap it or use its
`className` and select on that instead — check the rendered DOM rather than assuming.

- [ ] **Step 4: Run vitest green**

Run: `cd web && npx vitest run && npm run check && npm run typecheck`
Expected: PASS, clean.

- [ ] **Step 5: Write the e2e — and assert it does not OCCLUDE the map**

The MiniMap is an overlay panel in the canvas's bottom-right. That is the same
hazard class that caused #134, and `locator.click()` will not catch it: it
auto-scrolls and retries, so it can manufacture a click a user could never make.
Chromium false-passed #134 for exactly this reason. Assert reachability directly.

Add to `tests/e2e/monitor/dashboard/test_review_shell.py`:

```python
def test_minimap_toggles_and_does_not_occlude_the_map(shell_dash, page):
    """The minimap is off by default, toggles on, and — being an overlay panel in
    the canvas's bottom-right — must not cover a node.

    The occlusion assertion is elementFromPoint, NOT locator.click(): click()
    auto-scrolls and retries, so it can manufacture a click on an element a real
    user could never reach. That is precisely how chromium false-passed #134 while
    the panel was covering chassis-a the whole time.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, 6)

    minimap = page.locator('[data-testid="topo-minimap"]')
    assert minimap.count() == 0
    page.locator('[data-testid="minimap-toggle"]').click()
    minimap.wait_for()

    # Every node must still be the top element at its own centre.
    for node in page.locator('[data-testid^="topo-node-"]').all():
        testid = node.get_attribute("data-testid")
        box = node.bounding_box()
        assert box is not None, f"{testid} has no box"
        hit = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.closest('[data-testid]')"
            "?.getAttribute('data-testid')",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        )
        assert hit == testid, f"{testid} is covered at its centre by {hit}"
```

- [ ] **Step 6: Build the dist, then run the FULL browser gate**

Run:
```bash
make web
nox -s dashboard
```

**All three engines.** `uv run pytest tests/e2e/monitor/dashboard` is chromium only
and is NOT the gate — that is how #134 shipped. Paste the result for each engine.

- [ ] **Step 7: Commit**

```bash
git add web/src/topo/TopologyPage.tsx web/src/__tests__/pages.test.tsx \
  tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "feat(monitor): add a minimap toggle to the topology view, default off

Ships the deferred minimap as an opt-in pill beside Sources, per Chris's ruling.

WITHOUT onlyRenderVisibleElements, which the follow-up note paired it with: that
flag culls off-screen elements, and the dashboard e2e counts edges on a canvas that
already withholds them until both endpoints are measured — the exact mechanism
behind the #130 webkit flake. It is a performance change that needs its own
justification and its own test; it stays in the follow-ups.

The e2e asserts the minimap does not occlude any node, via elementFromPoint rather
than locator.click(). click() auto-scrolls and retries, so it can manufacture a
click no user could make — which is how chromium false-passed #134 while the
inspector was covering chassis-a.

Assisted-by: Claude Opus 4.8"
```

---

### Task 5: Close the follow-ups and run the full gates

**Files:**
- Modify: `todo/monitor-topology-followups.md`

- [ ] **Step 1: Update `todo/monitor-topology-followups.md`**

Strike what shipped: the fixture-stem item, the MiniMap item, and both cosmetics
clauses (the dangling `unreachable ·` separator and exporting `pairKey`). Remove
both residue notes (the `h-[calc(100vh-6.5rem)]` constant and the `onClose`
re-subscribe) — both are fixed.

**Add `onlyRenderVisibleElements` as its own item**, since the MiniMap shipped
without it:

```markdown
- **`onlyRenderVisibleElements` for large labs.** Deliberately NOT shipped with the
  minimap toggle (2026-07-12). It culls off-screen elements from the DOM, and the
  dashboard e2e counts edges on a canvas that already withholds them until both
  endpoint nodes are measured — the exact mechanism behind the #130 webkit flake
  (`assert 0 >= 6`). Needs its own justification (measure first: is React Flow
  actually slow at the sizes we hit?) and its own test strategy for the edge-count
  specs before it can land.
```

Keep the deferred design items (D\* static-link layering, tunnels-as-overlays,
obstacle-aware skip-column routing) and renumber.

- [ ] **Step 2: Run the full gates, in order**

```bash
make web
cd web && npx vitest run && npm run check && npm run typecheck && cd ..
uv run pytest tests/unit/scripts -q
nox -s dashboard      # ALL THREE ENGINES — this is the gate
nox -s lint
```

Report each engine's result separately. If you report "the dashboard lane passed"
without having run all three, you have repeated the mistake that shipped #134.

- [ ] **Step 3: Commit**

```bash
git add todo/monitor-topology-followups.md
git commit -m "docs(monitor): close the topology follow-ups this batch shipped

Strikes the fixture-stem enumeration, the minimap, both cosmetics, and both residue
notes. Adds onlyRenderVisibleElements as its own item — the minimap shipped without
it deliberately, and it needs its own justification and test strategy before it can
land near the edge-count specs.

Assisted-by: Claude Opus 4.8"
```

---

## Self-Review

**Spec coverage.** §1 fixture stems → Task 1 (including the mutation proof and the
new orphan direction). §2 cosmetics → Task 2. §3 shell layout → Task 3. §4 `onClose`
→ Task 2 (folded: it is a two-line change in a file Task 2 already edits, and shares
its test cycle). §5 MiniMap → Task 4. Follow-up bookkeeping and the full gate sweep →
Task 5.

**Placeholder scan.** Two steps intentionally instruct the implementer to *look* at
the codebase rather than prescribing code: Task 3 Step 1 ("use whatever helper that
file already has for mounting the app at a route") and Task 4 Step 3 (whether React
Flow's `<MiniMap>` forwards `data-testid`). Both are cases where prescribing code I
have not verified would be worse than telling the implementer to check — the
alternative is inventing a helper name that may not exist.

**Type consistency.** `pairKey(a: string, b: string): string` is used identically in
`topology.ts` (internally, twice) and `TopologyPage.tsx` (twice). `closeInspector`
is the memoised `onClose`. `minimap` / `setMinimap` mirror the existing `sources` /
`setSources` naming.

**Ordering.** Task 1 is independent (Python + one TS test file). Tasks 2, 3 and 4 all
touch `TopologyPage.tsx` and must run in order. Task 5 is last.
