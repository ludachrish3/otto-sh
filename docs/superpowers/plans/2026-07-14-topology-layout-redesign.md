# Topology Layout Redesign — Implementation Plan (Phases 1 & 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the topology map lay out the **data plane** by its own structure
and draw the **management plane** as a faint overlay — taking element-link
crossings from 75 → ~7 on a representative ISP core, with **zero invisible
edges** — and make the layout **measured** rather than eyeballed from here on.

**Architecture:** `layout.ts` currently does `x = depth * COL_W` where `depth` is
hops from `local` along the *management* chain. That is replaced by a pipeline:
subtract management → peel leaf services and dock them → root on the spine
cluster → orient by subtree mass (when decisive) → barycentric row-sort on
data-plane links → coordinate assignment. No new dependency.

**Tech Stack:** React 19, TypeScript, `@xyflow/react`, Vitest, Playwright via
pytest, Python 3.12 (the fixture generator).

**Spec:** `docs/superpowers/specs/2026-07-14-topology-layout-redesign-design.md`
(`52d5722`). **Read it before starting.**

**Reference implementation.** A working prototype of every algorithm below —
measured against both fixtures in the real renderer — is archived **outside the
repo** at:

```text
/tmp/claude-1000/-home-vagrant-otto-sh/a8248bb6-1e36-4c6e-848d-8473b660bae2/scratchpad/layout-prototype/
    preview/variants.ts          <- the algorithms (peel, root, orient, row-sort, coord-assign)
    preview/main.tsx             <- the harness
    sprawl-topology.json         <- the emitted fixture shape
    isp-core-topology.json
    layout-preview-report.md     <- all twelve variants, measured
```

It is deliberately NOT in the repo: it imports `elkjs`/`dagre` (from the rejected
bakeoff) and would break `tsc`. **Read it, port the named functions, and test
them — do not re-derive them from scratch.** But do not copy it blindly either:
the prototype hardcodes fixture-specific management IDs, which production must
*infer* (Task 3).

## Global Constraints

- **The browser gate is `nox -s dashboard`** — chromium AND firefox AND webkit
  ([noxfile.py:196](noxfile.py#L196)). A bare `uv run pytest tests/e2e/monitor/dashboard`
  runs **chromium only** and is NOT the gate; reporting it as such is how #134
  shipped. To run one engine under raw pytest the flag is **`--browser webkit`** —
  `-k webkit` is a test-NAME filter that deselects all tests and **exits 0**.
- **`make web` before ANY browser test.** pytest does not build the web dist; a
  stale bundle certifies the wrong artifact (#131, #132).
- Web gates: `cd web && npx vitest run`, `npm run check` (**Biome only**),
  `npm run typecheck` (**tsc — separate command**). Run BOTH.
- `nox -s lint` = `ruff check` AND `ruff format --check`.
- Python: never `from __future__ import annotations` (breaks the Sphinx `-W` build).
- Commits: conventional prefix + an `Assisted-by: Claude Opus 4.8` trailer. **No
  `Co-Authored-By`.**
- **Do NOT add a layout dependency.** ELK (+443KB gzipped) fails the air-gap
  check; dagre produced the worst layout of twelve measured variants. If you find
  yourself running `npm i` for a graph library, stop — that decision is already
  made and recorded in the spec.
- No heavy/parallel test load on the dev VM.
- Working directory: `/home/vagrant/otto-sh/.claude/worktrees/topology-layout-preview`.

## Known constants

`COL_W = 320`, `ROW_H = 110` ([layout.ts](web/src/topo/layout.ts)). Element node
is 208px wide (`w-52`), host node 176px (`w-44`). `routeEdge`
([routing.ts](web/src/topo/routing.ts)) emits exactly two path grammars —
`M sx,sy L tx,ty` and `M sx,sy C c1x,c1y c2x,c2y tx,ty` — so a path sampler is a
short cubic evaluator, no DOM required. The **rects** it routes against, however,
come from React Flow's *measured* node sizes, which is why the budget test
(Task 2) lives in the browser lane.

## The metric (used throughout)

Classify every `TopoEdge`:
- **DATA-PLANE** — `provenance: "declared"` (element ⇄ element network links).
- **MANAGEMENT** — `local:*` edges, `implicit` (hop-derived) edges, `reports-for`.
- **TUNNEL** — `dynamic`. Counted separately; not folded into either.

Only data-plane edges count toward the budget:
- `dp_crossings` — crossings where BOTH edges are data-plane.
- `dp_swallowed` — a data-plane edge whose path passes under a **non-endpoint**
  node. React Flow draws edges beneath nodes, so a swallowed edge is **invisible**.

Management edges may freely cross behind elements. That is honest and explicitly
not clutter. **Do not count them.** Earlier rounds of this investigation did, and
it inverted the ranking.

---

# PHASE 1 — the test bed

Ships no behaviour change. It makes every later claim provable.

### Task 1: Ship the `sprawl` and `isp-core` fixtures

**Files:**
- Modify: `scripts/gen_monitor_fixtures.py` (add `sprawl()` and `isp_core()`; two lines in `build_all()`)
- Create: `web/fixtures/sprawl.json`, `web/fixtures/isp-core.json` (generated, committed)
- Test: `tests/unit/scripts/test_gen_monitor_fixtures.py`

**Interfaces:**
- Produces: two new `build_all()` keys, `"sprawl"` and `"isp-core"`.
- Consumes: nothing.

Adding a fixture is a **one-line change to `build_all()`** — the drift guard and
the orphan guard both derive from it (`59a4ac4`), so freshness and inventory are
checked automatically. That retirement was done precisely to make this free.

**Both fixtures are lab-rich and metrics-SPARSE.** kitchen-sink is 1.26MB with
13,724 metric rows for 9 hosts; these need a few hundred rows — just enough for
health to resolve and a cascade to render. Mirror `kitchen_sink()`'s structure so
`parseExportDocument` accepts the document.

A throwaway generator for both already exists (see
`.superpowers/sdd/layout-preview-report.md` for how they were built, and
`web/public/sprawl-topology.json` / `web/public/isp-core-topology.json` on this
branch for the exact emitted shape). **Port those into `gen_monitor_fixtures.py`
properly.**

**`sprawl` — a deep lab** (19 elements, ~24 hosts, 4 management columns):

| hop depth | elements |
| --- | --- |
| 0 (`hop: null`) | `jump-01`, `mgmt-01`, `tor-sw-a`, `tor-sw-b` |
| 1 (hop `jump-01`) | `edge-gw`, `core-gw`, `db-01`, `db-02` |
| 2 (hop `core-gw`) | `app-01`…`app-04`, `cache-01`, `queue-01`, `workers` (4 hosts) |
| 2 (hop `edge-gw`) | `chassis-a` (lc1/lc2/sup, slots 1-3, type `physical`), `console-01` |
| 3 (hop `console-01`) | `zephyr-01`, `zephyr-02` |

Declared links: `tor-sw-a ⇄ tor-sw-b`; `edge-gw ⇄ core-gw`; `db-01 ⇄ db-02`;
`app-01 ⇄ cache-01`, `app-02 ⇄ cache-01`, `app-03 ⇄ queue-01`, `app-04 ⇄ queue-01`;
`app-01 ⇄ db-01`, `app-02 ⇄ db-01`, `app-03 ⇄ db-02`, `app-04 ⇄ db-02`;
`workers_w1 ⇄ db-01` and `workers_w3 ⇄ db-01` (a parallel-edge fan);
and the skip-column specimens `chassis-a_lc1 ⇄ tor-sw-a`, `app-01 ⇄ tor-sw-a`,
`zephyr-01 ⇄ tor-sw-b`. One `dynamic` link `jump-01 ⇄ zephyr-01`. Put
`impair: "edge-gw"` on `app-01 ⇄ db-01`. Make `core-gw` **down** (cascade).

**`isp-core` — short management paths, deep data plane** (23 elements, ~26 hosts):

| | elements | hop |
| --- | --- | --- |
| management | `jump-01`, `ems-01`, `ems-02` | null |
| border | `pe-01`, `pe-02` | null |
| core | `core-01`, `core-02` | null |
| mobile-core | `mme-01`, `sgw-01`, `pgw-01`, `hss-01` | null |
| aggregation | `agg-01`…`agg-04` (`agg-01` has 3 hosts, slots 1-3, `physical`) | `jump-01` |
| access | `acc-01`…`acc-08` | `jump-01` |

**Every element's management depth is 0 or 1 — that is the whole point.** Declared
links: `pe-0X ⇄ core-0Y` (all four combinations); `core-01 ⇄ core-02`; each
`agg-0X ⇄ core-01` and `⇄ core-02`; `acc-01,02 ⇄ agg-01`, `acc-03,04 ⇄ agg-02`,
`acc-05,06 ⇄ agg-03`, `acc-07,08 ⇄ agg-04`; ring pairs `acc-01 ⇄ acc-02`,
`acc-03 ⇄ acc-04`, `acc-05 ⇄ acc-06`, `acc-07 ⇄ acc-08`; `mme-01 ⇄ core-01`,
`sgw-01 ⇄ core-01`, `pgw-01 ⇄ core-02`, `hss-01 ⇄ core-02`; `pgw-01 ⇄ pe-01`.
One impaired link, one `dynamic` link. `ems-01` is the metrics source for
pe/core/mobile-core; `ems-02` for agg/acc (this produces the `reports-for` star).
Make `core-02` **down**.

- [ ] **Step 1: Write the failing test**

In `tests/unit/scripts/test_gen_monitor_fixtures.py`, extend the existing
by-name-dependents assertion — the two new fixtures are load-bearing for the
layout budget tests, so removing either must fail HERE, not as an ENOENT
downstream:

```python
    assert {"kitchen-sink", "minimal", "drift", "cascade", "sprawl", "isp-core"} <= set(docs)
```

and add a shape test that pins what makes each fixture worth having:

```python
def test_sprawl_is_deep():
    """sprawl exists to exercise DEPTH: a 3-hop management chain, which
    kitchen-sink (max depth 1) cannot produce."""
    lab = build_all()["sprawl"].sessions[0].lab
    by_id = {h.id: h for h in lab.hosts}

    def depth(host):
        n, cur = 0, host.hop
        while cur:
            n += 1
            cur = by_id[cur].hop if cur in by_id else None
        return n

    assert max(depth(h) for h in lab.hosts) >= 3


def test_isp_core_is_shallow_but_meshed():
    """isp-core exists to exercise the DEGENERATE case the redesign targets:
    management paths are SHORT (every element 0 or 1 hops out), so the old
    hops-from-local layout collapses 23 elements into 3 columns — while the
    data plane is deep and richly meshed."""
    lab = build_all()["isp-core"].sessions[0].lab
    by_id = {h.id: h for h in lab.hosts}

    def depth(host):
        n, cur = 0, host.hop
        while cur:
            n += 1
            cur = by_id[cur].hop if cur in by_id else None
        return n

    assert max(depth(h) for h in lab.hosts) <= 1, "management paths must stay short"
    declared = [lk for lk in lab.links if (lk.provenance or "declared") == "declared"]
    assert len(declared) >= 25, "the data plane must be richly meshed"
    assert len({h.element for h in lab.hosts}) >= 20
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/scripts -q`
Expected: FAIL — `"sprawl"` and `"isp-core"` are not in `build_all()`.

- [ ] **Step 3: Implement `sprawl()` and `isp_core()` in `scripts/gen_monitor_fixtures.py`**

Follow `kitchen_sink()`'s structure exactly (session, lab, hosts, links, sparse
metrics, a couple of events, chart specs). Add both to `build_all()`:

```python
        "sprawl": sprawl(),
        "isp-core": isp_core(),
```

- [ ] **Step 4: Generate and commit the fixture JSON**

Run: `make monitor-fixtures`
Then confirm the drift guard is satisfied and the new files exist:
`uv run pytest tests/unit/scripts -q` — expected PASS, and
`test_committed_fixture_is_fresh[sprawl]` / `[isp-core]` should now appear in the
parametrized run **automatically** (that is the enumeration retirement working).

- [ ] **Step 5: Check the size**

Run: `ls -la web/fixtures/`
Expected: `sprawl.json` and `isp-core.json` well under 200KB each. If either
approaches kitchen-sink's 1.26MB you have generated dense metrics — thin them.
Report the actual sizes.

- [ ] **Step 6: Verify the web side parses them**

Run: `cd web && npx vitest run` — `exportdoc.test.ts` reads the fixtures
**directory**, so both new files are parsed automatically with no edit. If they
are malformed, that test fails. This is the orphan/derivation machinery paying off.

- [ ] **Step 7: Commit**

```bash
git add scripts/gen_monitor_fixtures.py web/fixtures/sprawl.json \
  web/fixtures/isp-core.json tests/unit/scripts/test_gen_monitor_fixtures.py
git commit -m "$(cat <<'EOF'
test(monitor): add the sprawl and isp-core topology fixtures

Two labs the existing fixtures cannot express, both needed to judge the map's
layout rather than eyeball it.

sprawl is DEEP: a 3-hop management chain (jump -> gateway -> console -> zephyr)
with skip-column data-plane links. kitchen-sink maxes out at depth 1.

isp-core is the degenerate case the layout redesign targets: every element is 0
or 1 management hops from local, so the current hops-from-local layout collapses
23 elements into THREE columns (1/11/12) with 75 element-link crossings — while
the data plane underneath is four tiers deep and richly meshed.

Both are lab-rich and metrics-sparse: a few hundred metric rows, not
kitchen-sink's 13,724. Adding them was a one-line change to build_all(), which
is exactly what retiring the fixture-stem enumeration (59a4ac4) bought.

Assisted-by: Claude Opus 4.8
EOF
)"
```

---

### Task 2: The layout budget harness — and prove it has teeth

**Files:**
- Create: `web/src/topo/measure.ts` (the geometry helpers — shipped, not test-only)
- Create: `web/src/__tests__/topomeasure.test.ts`
- Create: `tests/e2e/monitor/dashboard/test_topology_budget.py`

**Interfaces:**
- Produces:
  - `classifyEdge(edge: TopoEdge): "data-plane" | "management" | "tunnel"`
  - `samplePath(d: string, n: number): {x: number, y: number}[]` — evaluates the
    `M…L…` / `M…C…` grammar `routeEdge` emits. No DOM.
  - `countSwallowed(edges, rects, samples): number`
  - `countCrossings(edges): number`
- Consumes: `TopoEdge` from `../data/topology`.

**Why the budget test lives in the browser lane.** `routeEdge` routes against
React Flow's **measured** node rects. A unit test would have to *assume* node
heights, and an assumed-geometry test certifies the wrong artifact — the same
class of mistake as testing against a stale bundle. So: **unit tests for the
math, a Playwright test for the budget.**

- [ ] **Step 1: Write the failing unit tests for the geometry helpers**

`web/src/__tests__/topomeasure.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { classifyEdge, countSwallowed, samplePath } from "../topo/measure";

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
```

- [ ] **Step 2: Run and watch it fail**

Run: `cd web && npx vitest run src/__tests__/topomeasure.test.ts`
Expected: FAIL — `../topo/measure` does not exist.

- [ ] **Step 3: Implement `web/src/topo/measure.ts`**

Write `classifyEdge`, `samplePath` (handle `M`/`L`/`C` only — that is the entire
grammar `routeEdge` emits; **throw on anything else** rather than silently
returning fewer points, or the budget could pass by measuring nothing),
`countSwallowed` (a sampled point inside a non-endpoint node's rect), and
`countCrossings` (segment-intersection over consecutive sample pairs).

- [ ] **Step 4: Green**

Run: `cd web && npx vitest run && npm run check && npm run typecheck`
Expected: PASS, clean.

- [ ] **Step 5: Write the budget e2e — against the CURRENT layout**

`tests/e2e/monitor/dashboard/test_topology_budget.py`. It drives the real
dashboard, reads the real rendered node rects and the real edge paths, and
asserts a budget. Baselines are today's MEASURED numbers:

| fixture | dp_crossings | dp_swallowed |
| --- | --- | --- |
| `isp-core` | 75 | 0 |
| `sprawl` | 21 | 3 |

Set the budgets at these values (with a small margin for engine-to-engine font
differences — say +2 on crossings). The test must be a **ratchet**: it asserts
`<=`, so Phase 2 tightens it rather than rewriting it.

Compute the numbers in the page with `page.evaluate`, reusing the same rules as
`measure.ts` (read `.react-flow__node` bounding boxes and each
`[data-testid^="topo-link-"] path` — note React Flow renders BOTH an interaction
path and the visible path; take the visible one). Import the fixture through the
existing `_import_fixture` helper.

- [ ] **Step 6: PROVE THE HARNESS HAS TEETH — do not skip this**

A budget test that cannot fail is worse than none: it certifies whatever it finds.
Perturb the layout and watch the budget go RED, then revert:

1. In `web/src/topo/layout.ts`, temporarily change `COL_W` from `320` to `60`
   (columns collapse; edges get swallowed).
2. `make web`
3. `uv run pytest tests/e2e/monitor/dashboard/test_topology_budget.py --browser chromium -q`
4. It **must FAIL** on `dp_swallowed`. Paste that output into your report.
5. Revert `COL_W` to `320`, `make web`, and confirm it goes green again.

**If it does not fail, the harness is measuring nothing — stop and report.**

- [ ] **Step 7: Full browser gate, then commit**

Run:
```bash
make web
nox -s dashboard
```
All three engines. Report each separately. Then:

```bash
git add web/src/topo/measure.ts web/src/__tests__/topomeasure.test.ts \
  tests/e2e/monitor/dashboard/test_topology_budget.py
git commit -m "$(cat <<'EOF'
test(monitor): measure the topology layout instead of eyeballing it

Adds a budget on the two numbers that decide whether the map is readable:
crossings between ELEMENT links, and element links that pass underneath a node.
React Flow draws edges beneath nodes, so a swallowed edge is INVISIBLE to the
user — today sprawl has three of them and nothing notices.

Management edges (local, hop-derived, reports-for) are deliberately NOT counted.
A faint management line crossing behind an element is honest and unobtrusive;
counting them measures the wrong thing and, in this investigation, inverted the
ranking of every candidate layout.

The budget runs in the browser lane, not in vitest, because routeEdge routes
against React Flow's MEASURED node rects — a unit test would have to assume node
heights, and assumed geometry certifies the wrong artifact.

Budgets are pinned to today's measured baseline (isp-core 75 crossings; sprawl
21 crossings + 3 swallowed) and assert <=, so the layout work ratchets them down
rather than rewriting them. Verified the harness has teeth: collapsing COL_W
320 -> 60 turns it red on dp_swallowed.

Assisted-by: Claude Opus 4.8
EOF
)"
```

---

# PHASE 2 — the layout pipeline

Each task improves the map and must not regress the budget. Task 7 ratchets it down.

### Task 3: Partition the management plane

**Files:**
- Modify: `web/src/data/topology.ts` (expose the partition)
- Modify: `web/src/topo/layout.ts`
- Modify: `web/src/topo/edgeStyles.ts` (a faint management treatment)
- Test: `web/src/__tests__/topology.test.ts`, `web/src/__tests__/topoedge.test.tsx`

**Interfaces:**
- Produces: `isManagementElement(node: TopoNode, edges: TopoEdge[]): boolean` —
  true for the `local` node, and for any element with **zero data-plane links**.
- Consumes: `classifyEdge` from Task 2's `measure.ts`.

**The inference is the whole adoption story.** Jump hosts and EMS typically carry
no `declared` links, so an untouched lab gets the management partition for free —
no schema change, no config. (Explicit `management` / `tier` fields land in a
later phase and merely *override* this.)

- [ ] **Step 1: Write the failing test**

```ts
describe("management partition", () => {
  it("treats an element with no data-plane links as management", () => {
    // isp-core: jump-01 and the two EMS carry only hop/reports-for edges.
    const g = buildTopoGraph(ispCore, effective, { sources: true });
    const mgmt = g.nodes.filter((n) => isManagementElement(n, g.edges)).map((n) => n.id);
    expect(new Set(mgmt)).toEqual(new Set(["local", "jump-01", "ems-01", "ems-02"]));
  });

  it("does NOT treat a network element as management, however few links it has", () => {
    const g = buildTopoGraph(ispCore, effective, { sources: true });
    // hss-01 has exactly ONE declared link. One is not zero.
    expect(isManagementElement(g.nodes.find((n) => n.id === "hss-01")!, g.edges)).toBe(false);
  });
});
```

- [ ] **Step 2: Run it, watch it fail, then implement**

`isManagementElement` in `topology.ts`; management nodes get column 0 (their own
leftmost band) in `layout.ts`; management edges get a faint style in
`edgeStyles.ts` (low opacity — reuse the existing `static`/`reports-for` classes,
add an opacity, do not invent a new class).

Run `cd web && npx vitest run && npm run check && npm run typecheck` — green.

- [ ] **Step 3: Budget must not regress**

Run: `make web && uv run pytest tests/e2e/monitor/dashboard/test_topology_budget.py --browser chromium -q`
Expected: PASS (the ratchet still holds).

- [ ] **Step 4: Commit** (conventional prefix + `Assisted-by: Claude Opus 4.8`)

---

### Task 4: Layer the data plane

**Files:**
- Modify: `web/src/topo/layout.ts`
- Test: `web/src/__tests__/topolayout.test.ts`

**Interfaces:**
- Produces: `dataPlaneColumns(nodes, edges): Map<string, number>`.
- Reference: port `peelDataPlaneBackbone`, `computeW11Columns` and
  `computeW12Columns` from `web/src/preview/variants.ts`.

The rules, exactly (from the spec):

1. **Subtract management** (Task 3's predicate).
2. **Peel leaf services and dock them.** Iteratively strip data-plane degree-1
   elements and dock each into its attachment's final column — not a column of its
   own. Guard: only peel when the neighbour's remaining degree is ≥ 2 (a mutual
   pendant pair stays put). Resolve dock targets transitively through chains.
   **Do NOT peel degree-2 ring peers** — measured, it collapses a real tier into
   the hub's column and regresses toward the baseline.
3. **Root on the spine cluster.** Per connected component, the root set is every
   backbone node whose degree is **≥ 75% of that component's maximum degree**.
   (A true k-core was tried and rejected: it degenerates to "coreness 2 for
   everyone" on both fixtures.) The root set becomes ONE layer, so `core-01` and
   `core-02` share a column.
4. **Orient by subtree mass — ONLY when decisive.** For each root-neighbour,
   measure the subgraph reachable through it (excluding paths back through the
   root). Heavy → downstream (right, layer +1…), light → upstream (left, layer
   −1…). Normalise to start at 0.

   **The confidence guard is load-bearing and is UNPROVEN.** Applied
   unconditionally, mass-orientation helps isp-core (12 → 7 crossings) and
   **regresses sprawl (3 → 9)**, because sprawl's hub has two branches of exactly
   equal size — there is no asymmetry to read, so ties default downstream and the
   graph crams into three columns. So orient only when the mass difference is
   decisive; fall back to symmetric layering on a tie.

- [ ] **Step 1: Write the failing tests — they are the acceptance criteria**

```ts
describe("dataPlaneColumns — isp-core", () => {
  it("keeps the mutually-linked core pair in ONE column", () => {
    const cols = dataPlaneColumns(g.nodes, g.edges);
    expect(cols.get("core-01")).toBe(cols.get("core-02"));
  });

  it("docks leaf services into their attachment's column, not their own", () => {
    // hss/mme/pgw/sgw hang OFF the core. They are not a tier anything passes
    // through — if they get their own column, all 8 agg<->core links must leap
    // over it, which is exactly where the 9 swallowed edges came from.
    const cols = dataPlaneColumns(g.nodes, g.edges);
    const coreCol = cols.get("core-01");
    for (const svc of ["mme-01", "sgw-01", "hss-01", "pgw-01"]) {
      expect(cols.get(svc), `${svc} should dock into core's column`).toBe(coreCol);
    }
  });

  it("puts border upstream of core and aggregation downstream", () => {
    const cols = dataPlaneColumns(g.nodes, g.edges);
    expect(cols.get("pe-01")!).toBeLessThan(cols.get("core-01")!);
    expect(cols.get("agg-01")!).toBeGreaterThan(cols.get("core-01")!);
    expect(cols.get("acc-01")!).toBeGreaterThan(cols.get("agg-01")!);
  });
});

describe("dataPlaneColumns — sprawl (the tie case the guard exists for)", () => {
  it("does not collapse when the hub's branches are equal in mass", () => {
    // Unconditional mass-orientation regresses sprawl 3 -> 9 crossings by
    // defaulting every tie downstream. The guard must decline to orient here.
    const cols = dataPlaneColumns(sprawlG.nodes, sprawlG.edges);
    expect(new Set(cols.values()).size).toBeGreaterThanOrEqual(4);
  });
});
```

- [ ] **Step 2: Run, watch fail, implement, green.**

Port from the prototype; do not re-derive. Run vitest + Biome + tsc.

- [ ] **Step 3: If the confidence guard cannot satisfy BOTH fixtures — STOP and report**

Do not tune per fixture. If one rule cannot deliver both, the spec's recorded
fallback is to **ship the unoriented rule** (W11: isp 12 crossings, sprawl 3) and
say so. That is still 75 → 12 and 21 → 3. Report which you shipped and why.

- [ ] **Step 4: Budget + commit.**

---

### Task 5: Barycentric row-sort on data-plane links only

**Files:** `web/src/topo/layout.ts`; test `web/src/__tests__/topolayout.test.ts`
**Reference:** `barycentricRowSort` in `web/src/preview/variants.ts`.

Today `rowOrder` sorts by slot then id — alphabetically — so linked peers sit far
apart and their links bow across the column. Order each column by the mean row of
its neighbours in the adjacent columns (a few alternating sweeps). **Use
data-plane links only** — management links must not drag the ordering around.

- [ ] **Step 1: failing test** — e.g. on sprawl, `db-01` and `db-02` (which link to
  each other) must end up in adjacent rows; likewise `app-01`/`cache-01`.
- [ ] **Step 2-4:** implement, green, budget, commit.

---

### Task 6: Coordinate assignment

**Files:** `web/src/topo/layout.ts`; test `web/src/__tests__/topolayout.test.ts`
**Reference:** `coordinateAssignment` in `web/src/preview/variants.ts`.

The phase we never wrote. Today `y = row * ROW_H`, so **every column is
top-aligned at y = 0** — which is why `local` sits in the top-left corner with its
edges raking down-and-right instead of radiating from the middle.

Give each node a y from the **median y of its data-plane neighbours** in adjacent
columns; resolve overlaps by pushing apart to a minimum gap of `ROW_H`; sweep a
few times left-to-right then right-to-left; and **centre each column vertically**.

- [ ] **Step 1: failing test**

```ts
it("centres columns instead of top-aligning them", () => {
  // Every column used to start at y = 0. The management column has 4 nodes and
  // the access column has 8; top-aligning them puts `local` in the corner and
  // rakes its edges down-and-right instead of radiating from the middle.
  const pos = layoutTopo(g.nodes, g.edges);
  const ys = (ids: string[]) => ids.map((id) => pos.get(id)!.y);
  const mgmtMid = mean(ys(["local", "jump-01", "ems-01", "ems-02"]));
  const accMid = mean(ys(["acc-01", "acc-02", "acc-03", "acc-04", "acc-05", "acc-06", "acc-07", "acc-08"]));
  expect(Math.abs(mgmtMid - accMid)).toBeLessThan(ROW_H);
});
```

- [ ] **Step 2-4:** implement, green, budget, commit.

---

### Task 7: Ratchet the budget down, and run every gate

**Files:** `tests/e2e/monitor/dashboard/test_topology_budget.py`; `todo/monitor-topology-followups.md`

- [ ] **Step 1: Measure what we actually achieved**

Run the budget test with the assertions temporarily printing the numbers.
Expected, from the prototype: isp-core ~7 crossings / **0** swallowed; sprawl
~3 crossings / **0** swallowed.

- [ ] **Step 2: Tighten the budgets to those numbers, with a small engine margin**

**`dp_swallowed` must be asserted as exactly 0 on both fixtures.** An invisible
edge is a bug and the whole point of this design is that it stops happening. Do
not leave headroom on that one.

- [ ] **Step 3: Prove the tightened budget still has teeth**

Repeat Task 2 Step 6's perturbation (`COL_W` 320 → 60) against the NEW budget and
confirm red, then revert. A tightened budget that cannot fail is no better than
the loose one.

- [ ] **Step 4: Close the follow-ups**

In `todo/monitor-topology-followups.md`: strike **D\* static-link layering**
(dead — measured, it doubles swallowed edges and destroys the axis's meaning) and
**obstacle-aware skip-column routing** (no longer needed — the new layout swallows
nothing). Record the two known warts from the spec: leaf-docking is occasionally
semantically odd (on sprawl it docks both ToR switches into an app server's
column), and the explicit `management` / `tier` fields remain unbuilt (phase 3).

- [ ] **Step 5: FULL GATES**

```bash
make web
cd web && npx vitest run && npm run check && npm run typecheck && cd ..
uv run pytest tests/unit/scripts -q
nox -s dashboard      # ALL THREE ENGINES — this is the gate
nox -s lint
```
Report each engine separately.

- [ ] **Step 6: Look at it**

Serve the dashboard, import `isp-core.json`, and actually look at the map in both
themes. It should read border → core → aggregation → access, with the management
plane faint on the left and `local` radiating from the middle. Screenshot both
fixtures and report the paths. **If it does not read like a network, the numbers
are lying and you should say so.**

- [ ] **Step 7: Commit.**

---

## Self-Review

**Spec coverage.** §1 management partition (inferred) → Task 3. §2 `tier` →
deferred to phase 3, explicitly. §3.1-3.4 layering → Task 4. §3.5 row-sort →
Task 5. §3.6 coordinate assignment → Task 6. §4 faint management band → Task 3.
§5 no dependency → a Global Constraint. Testing (fixtures + budget harness) →
Tasks 1-2, ratcheted in Task 7. Delivery order §: Phase 1 = Tasks 1-2, Phase 2 =
Tasks 3-7. Phase 3 (schema) is out of scope for this plan and gets its own.

**Placeholder scan.** Tasks 5 and 6 give the failing test and point at a named
reference function rather than restating 80 lines of ported algorithm — that is
deliberate: the reference implementation is real, working, and in the tree, and
re-typing it from memory would be strictly worse. Every *rule* it must implement
is stated in full in Task 4 and the spec.

**Type consistency.** `classifyEdge` / `samplePath` / `countSwallowed` /
`countCrossings` (Task 2, `measure.ts`) are consumed by Task 3's
`isManagementElement` and the budget e2e. `dataPlaneColumns(nodes, edges)` (Task
4) feeds Tasks 5 and 6, which both extend `layoutTopo(nodes, edges)` — note
`layoutTopo` gains an `edges` parameter in Task 4; every call site must be updated
then, not later.

**Riskiest task.** Task 4's confidence guard, which is designed but unproven. It
has an explicit STOP-and-report step and a recorded fallback, so it cannot quietly
become per-fixture tuning.
