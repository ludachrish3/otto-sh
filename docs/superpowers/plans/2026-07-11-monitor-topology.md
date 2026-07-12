# Monitor Topology (Plan 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the review-mode topology per spec `docs/superpowers/specs/2026-07-11-monitor-topology-design.md`: hop-layered inter-element map + intra-element view on a React Flow pan/zoom canvas, first-class selectable links with a SlideOver inspector, the Sources overlay, and the down-vs-unreachable reachability cascade — all derived from the imported document, zero backend changes.

**Architecture:** One new pure data module (`data/topology.ts` — reachability + graph building, vitest-heavy), one new fixture scenario (`cascade`), then a `web/src/topo/` component family on `@xyflow/react` (custom DOM nodes/edges keep the data-testid Playwright contract). Only the hop skeleton places nodes (single-parent ⇒ tree by construction); all data-plane links are cross-edges that never affect placement, so connectivity cycles are free. Page-level rendering is proven in the browser lane (Task 6), not jsdom — React Flow mounting in jsdom is deliberately avoided; RTL covers node/inspector components directly.

**Tech Stack:** @xyflow/react (new dep, exact pin), existing react-aria primitives + Tailwind tokens, zustand review store, wouter.

## Global Constraints

- **HOSTLESS ONLY** — nothing may touch lab VMs. Per-task gates: scoped `npx vitest run <files>`, `npm run check:fix && npm run check && npm run typecheck` (from `web/`) before every commit touching web/. Do not run `make coverage` (full suite) — the Python gate for this plan is `make coverage-hostless` (Task 7 only). `make dashboard` only in Tasks 6–7.
- Worktree: `/home/vagrant/otto-sh/.claude/worktrees/monitor-topology`, branch `worktree-monitor-topology` (continue on tip `199c2fb`). Fresh-worktree setup (once, before Task 1's gates): `uv sync` + `make web-install`.
- Commit style: conventional prefix + `Assisted-by: Claude Fable 5` trailer embedded in `-m`; explicit `git add` per file; never `git add -u`. NEVER use the `!` breaking marker in this plan (nothing here breaks public surface).
- New npm deps: `npm install -E <pkg>` (exact pins). Record resolved versions in the task report.
- **Air-gap:** every asset bundles locally. **Known landmine:** React Flow renders an attribution panel linking to `https://reactflow.dev` — an external URL in the dist that WILL fail `scripts/check_airgap.sh`. The sanctioned resolution is `proOptions={{ hideAttribution: true }}` (permitted config on the MIT build) **plus** a credit line in `web/README.md` (Task 3). Never allowlist the URL.
- Vitest conventions (established, copy them): no `test.globals` → explicit `afterEach(cleanup)`; fixtures via `readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../fixtures/<name>.json"), "utf-8")`; store reset in `afterEach` uses the full 7-field shape `{ sessions: [], rawDocument: null, sourceName: null, warnings: [], importError: null, activeSessionId: null, range: null }`.
- Playwright: data-testid contract only (styling classes are never a test contract; the ONE exception in this plan is reading the `.react-flow__viewport` transform attribute in the pan/zoom smoke — the viewport is library chrome with no testid hook). Semantic state is asserted via `data-status` attributes, not classes.
- `data-testid` contract produced by this plan (Task 6 pins them): `view-toggle`, `topology-page`, `topo-node-<id>`, `topo-link-<edgeId>`, `topo-impair-<edgeId>`, `topo-fit`, `sources-toggle`, `topo-warnings`, `link-inspector`, `topo-breadcrumb`.
- Status colors stay reserved: ok/down use the status tokens; **unreachable** is the dimmed treatment defined in Task 3 (node `opacity-60` + hollow dot `border border-status-error/60 bg-transparent`; rollup segment `bg-status-error/25`) — never a series color, never plain gray (gray means no-data/unknown).
- Python edits (Tasks 1, 6): `uv run ruff format <file>` + `uv run ruff check <file>`; no new `noqa`; no `from __future__ import annotations`.
- Timestamps: all internal times epoch ms via `parseTs`.

## Interfaces inventory (ground truth at `199c2fb` — do not re-derive)

- `HostSnapshot` (`web/src/api/export.gen.ts:142`): `id`, `element` (required), `board?`, `slot?: number|null`, `hop?: string|null`, `ip?`, `interfaces?`.
- `LinkSnapshot` (`export.gen.ts:171`): `id`, `endpoints: [LinkEndpointSnapshot, LinkEndpointSnapshot]` (each `{host, interface?, ip?, port?}`), `protocol?`, `provenance?: "implicit"|"declared"|"dynamic"`, `name?`, `impair?: string|null`. Implicit hop edges ARE LinkSnapshots in `lab.links` (generator `_implicit_links`).
- `NormalizedSession.lab.links: LinkSnapshot[]`; `session.elements: DerivedElement[]` (`{id, type: "physical"|"logical", explicit, description, hostIds, singleton}`; implicit elements infer `physical` when any member has a slot); every host belongs to exactly one element (`host.element` is required) — there are no free hosts.
- `healthForHosts(session, range): Map<string, SubjectHealth>`; `SubjectHealth["status"]: "ok"|"down"|"no-data"|"unknown"` (`web/src/data/health.ts`).
- `useActiveSession()`, `useReviewStore((s) => s.range)` (`web/src/data/reviewStore.ts`).
- `SlideOver` (`web/src/ui/SlideOver.tsx`): `{isOpen, onClose, title, children, testId?}`.
- `ToggleGroup` (`web/src/ui/ToggleGroup.tsx`): `{options: {id,label}[], selectedId, onSelect, testId?, label?}`.
- Routes (`web/src/App.tsx:47-49`): `/`, `/host/:id`, `/topology` (placeholder page exists at `web/src/pages/TopologyPage.tsx`).
- Kitchen-sink lab (generator `kitchen_sink()`): `edge-gw` (singleton element, no hop, so depth 1); `chassis-a` = lc1(slot1)/lc2(slot2)/sup(slot5) all `hop="edge-gw"` (element depth 2, physical); `workers` = w1/w2/w3 (logical, no hops, depth 1); `db-01`, `mgmt-01` singletons depth 1. Links: 3 implicit (edge-gw↔each chassis member), declared `app-db` (w1↔db-01, tcp), declared `metrics-udp` (w3↔db-01, udp, `impair="edge-gw"`), dynamic `tun-demo` (edge-gw↔db-01). mgmt-01 sources PSU/Fan series for the three chassis boards.
- Playwright: `_import_fixture(page, "<name>.json")` in `tests/e2e/monitor/dashboard/test_review_shell.py:25` loads any file under `web/fixtures/`.

---

### Task 1: Cascade fixture scenario

**Files:**
- Modify: `scripts/gen_monitor_fixtures.py` (add `cascade()`, register in `build_all()`)
- Create (generated): `web/fixtures/cascade.json` (via `make monitor-fixtures`)
- Modify (only if it hardcodes scenario names): `tests/unit/scripts/test_monitor_fixture_files.py`

**Interfaces:**
- Consumes: existing generator helpers `_host`, `_link`, `_implicit_links`, `_meta`, `_chart_map`, `_host_metrics`, `BASE`, `dumps`.
- Produces: `web/fixtures/cascade.json` — lab `gw-a` (singleton gateway, no hop) → `rack-a_n1` (slot 1) + `rack-a_n2` (slot 2) both `hop="gw-a"` (element `rack-a`, physical by slot inference) + `solo-ok` (singleton, no hop); **gw-a, n1, n2 all go silent from 3600 s to session end** (so raw health at full range says down×3, the cascade must say gw-a=down, n1/n2=unreachable, solo-ok=ok); two declared links between `rack-a_n1`↔`rack-a_n2` (tcp `pair-a`, udp `pair-b` — the intra-view parallel-edge case). CPU-only chart, 30 s cadence, 2 h session, no events/log tabs.

- [ ] **Step 1: Read the generator's existing scenarios** (`kitchen_sink()`, `minimal()`) to match helper usage exactly, and read `tests/unit/scripts/test_monitor_fixture_files.py` to learn whether scenario names are enumerated (extend the enumeration if so — the drift guard must cover cascade.json).

- [ ] **Step 2: Add the scenario** to `scripts/gen_monitor_fixtures.py` (after `minimal()`):

```python
def cascade() -> MonitorExport:
    """Dead-gateway reachability scenario (topology spec 2026-07-11).

    ``gw-a`` and both rack hosts behind it go silent at 60 min: raw health
    reads down x3, the topology cascade must read down x1 (the gateway) +
    unreachable x2 (its children). ``solo-ok`` proves healthy branches are
    untouched. The rack pair also carries two declared links between the
    SAME endpoint pair -- the parallel-edge fan-out case.
    """
    rng = random.Random(20260711)  # noqa: S311 — deterministic dummy data, not cryptography
    hosts = [
        _host("gw-a", "gw-a", "10.30.0.1", interfaces={"eth0": "10.30.0.1", "eth1": "10.30.1.1"}),
        _host("rack-a_n1", "rack-a", "10.30.1.11", board="n1", slot=1, hop="gw-a"),
        _host("rack-a_n2", "rack-a", "10.30.1.12", board="n2", slot=2, hop="gw-a"),
        _host("solo-ok", "solo-ok", "10.30.2.21"),
    ]
    links = [
        *_implicit_links(hosts),
        _link(("rack-a_n1", "eth0", "10.30.1.11"), ("rack-a_n2", "eth0", "10.30.1.12"), name="pair-a"),
        _link(
            ("rack-a_n1", "eth0", "10.30.1.11"),
            ("rack-a_n2", "eth0", "10.30.1.12"),
            protocol="udp",
            name="pair-b",
        ),
    ]
    dead = (3600.0, _DURATION_S)
    metrics = _host_metrics(
        rng,
        [h.id for h in hosts],
        start=BASE,
        duration_s=_DURATION_S,
        cadence_s=30.0,
        gaps_for={"gw-a": (dead,), "rack-a_n1": (dead,), "rack-a_n2": (dead,)},
        charts=[("CPU %", "%", "cpu", 30.0)],
    )
    meta = _meta([("CPU %", "%", "cpu", 30.0)], tables=False)
    session = SessionRecord(
        id="2026-07-01T08-00-00-cascade",
        label="cascade",
        start=BASE,
        end=BASE + timedelta(seconds=_DURATION_S),
        lab=LabSnapshot(hosts=hosts, links=links, elements=[]),
        meta=meta,
        chart_map=_chart_map(meta),
        metrics=metrics,
    )
    return MonitorExport(format=1, sessions=[session])
```

**Adaptation protocol:** the exact keyword signatures of `_host_metrics`/`_meta`/`_link`/`SessionRecord` must be taken from the file (e.g. `_host_metrics` may not accept `cadence_s`/`charts` kwargs — read its definition and produce the same shape through whatever parameters it actually has, disclosing each adaptation in your report). The five REQUIREMENTS that may not be adapted away: the four hosts with exactly those ids/elements/slots/hops; all three silent from 3600 s to session end; two declared links between the same rack pair; CPU-only meta; deterministic seed.

- [ ] **Step 3: Register + regenerate**

In `build_all()`: `return {"kitchen-sink": kitchen_sink(), "minimal": minimal(), "drift": drift(), "cascade": cascade()}`.
Run: `make monitor-fixtures` — writes `web/fixtures/cascade.json`. Verify `git status` shows ONLY `cascade.json` as new (kitchen-sink/minimal/drift byte-identical — determinism check).

- [ ] **Step 4: Drift-guard green**

Run: `uv run pytest tests/unit/scripts/test_monitor_fixture_files.py -q` — extend the test's scenario enumeration first if Step 1 found one. Expected: PASS including cascade.
Then: `uv run ruff format scripts/gen_monitor_fixtures.py && uv run ruff check scripts/gen_monitor_fixtures.py` (plus the test file if modified).

- [ ] **Step 5: Sanity-check the derivation math** (no code): in cascade.json the last samples for the three dead hosts sit just before 3600 s; session end 7200 s ⇒ gap ≥ 3600 s ≫ 3×30 s ⇒ raw `down` for all three at full range. Confirm by eye that solo-ok has samples to the end.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_monitor_fixtures.py web/fixtures/cascade.json
# plus tests/unit/scripts/test_monitor_fixture_files.py if modified
git commit -m "feat(fixtures): cascade scenario — dead gateway + parallel rack links

gw-a and both rack hosts go silent at 60 min: raw health says down x3,
topology reachability must say down x1 + unreachable x2. The rack pair's
two declared links are the parallel-edge fan-out case.

Assisted-by: Claude Fable 5"
```

---

### Task 2: Topology data layer (pure)

**Files:**
- Create: `web/src/data/topology.ts`
- Test: `web/src/__tests__/topology.test.ts`

**Interfaces:**
- Consumes: `NormalizedSession`, `DerivedElement` from `./exportDoc`; `HostSnapshot`, `LinkSnapshot` from `../api/export.gen`; `SubjectHealth` from `./health`.
- Produces (Tasks 3/4/5 rely on — exact):
  - `type EffectiveStatus = SubjectHealth["status"] | "unreachable"`
  - `interface ReachabilityResult { effective: Map<string, EffectiveStatus>; warnings: string[] }`
  - `deriveReachability(session, healths: Map<string, SubjectHealth>): ReachabilityResult`
  - `interface TopoNode { id: string; kind: "local" | "element" | "host"; depth: number; label: string; element?: DerivedElement; host?: HostSnapshot; effective?: EffectiveStatus; rollup?: EffectiveStatus[]; enterTarget?: string }`
  - `interface TopoEdge { id: string; source: string; target: string; provenance: "implicit" | "declared" | "dynamic" | "local" | "reports-for"; link?: LinkSnapshot; links?: LinkSnapshot[]; impair: string | null; parallelIndex: number }`
  - `interface TopoGraph { nodes: TopoNode[]; edges: TopoEdge[]; warnings: string[] }`
  - `buildTopoGraph(session, effective: Map<string, EffectiveStatus>, opts: { expand?: string; sources: boolean }): TopoGraph`

Semantics (spec, binding):
- **Reachability:** a host's effective status is its raw status UNLESS the host is silent (`down`/`no-data`) AND any hop-chain ancestor's raw status is `down` → `unreachable`. A host still reporting is reachable by definition, whatever its hop chain claims. Hop walks carry a visited-set: a cycle yields `unknown` for the walking host + one warning naming the cycle host; a dangling hop (id not in lab) stops the walk silently (treat as attached).
- **Depth:** hop-chain length; no-hop host = 1; local = 0; cycle/dangling clamp at the step they're detected.
- **Inter-element graph** (`expand` unset): local node + one node per `session.elements`. Element depth = min member depth; `rollup` = member effective statuses in slot-then-id order; `enterTarget` = `/host/<memberId>` for singletons else `/topology/<elementId>`. Edges: (a) **implicit links collapse** per element pair — one edge, `links` = the collapsed set, id `implicit:<a>~<b>` (pair sorted); (b) **declared/dynamic links render individually** (`link` set, id = link id); (c) synthesized `local` edges local→element for every element with a hop-less member, id `local:<elementId>`; (d) with `sources: true`, dashed `reports-for` edges source-element→fed-element, deduped, id `reports:<src>~<dst>`. Edges whose two endpoints map to the same node are dropped (intra-element links don't render at this level). No edge ever influences depth.
- **Intra-element graph** (`expand` = elementId): host nodes for the element's members (slot-then-id order), plus every hop-chain ancestor node up to local (kind host, unless it's local), plus any host that is the far endpoint of a rendered link. Rendered links = lab.links with ≥1 endpoint in the member set, all individual (implicit included, id = link id). `local` edges attach hop-less members. `sources: true` adds reports-for edges source-host→member-host plus the source's host node.
- **Parallel index:** after assembling an edge list, group by unordered `source~target` pair; each group's edges get `parallelIndex` 0..n-1 in stable id order.
- **Element membership mapping** uses `session.elements` (NOT `host.element` strings — explicit elements may merge).
- Every id used in node/edge ids is a raw host/element/link id — no escaping (ids are slugged, `~` and `:` cannot appear).

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/topology.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { healthForHosts } from "../data/health";
import { buildTopoGraph, deriveReachability } from "../data/topology";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];
const cascade = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/cascade.json"), "utf-8"),
).sessions[0];

function effectiveOf(session: typeof kitchen) {
  return deriveReachability(session, healthForHosts(session, null));
}

describe("deriveReachability", () => {
  it("cascades: dead gateway makes silent children unreachable, not down", () => {
    const { effective, warnings } = effectiveOf(cascade);
    expect(warnings).toEqual([]);
    expect(effective.get("gw-a")).toBe("down");
    expect(effective.get("rack-a_n1")).toBe("unreachable");
    expect(effective.get("rack-a_n2")).toBe("unreachable");
    expect(effective.get("solo-ok")).toBe("ok");
  });

  it("does not cascade inside a healthy window", () => {
    const range = { from: cascade.startMs, to: cascade.startMs + 50 * 60_000 };
    const { effective } = deriveReachability(cascade, healthForHosts(cascade, range));
    for (const id of ["gw-a", "rack-a_n1", "rack-a_n2", "solo-ok"]) {
      expect(effective.get(id), id).toBe("ok");
    }
  });

  it("a reporting host is never unreachable, whatever its chain says", () => {
    // Synthetic: parent down, child keeps reporting.
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "p", element: "p", ip: "10.0.0.1" },
                { id: "c", element: "c", ip: "10.0.0.2", hop: "p" },
              ],
            },
            meta: {
              interval: 30.0,
              charts: [{ label: "CPU %", y_title: "CPU %", unit: "%", command: "x", chart: "cpu" }],
            },
            chart_map: { "CPU %": "CPU %" },
            metrics: [
              { timestamp: "2026-07-01T08:00:00Z", host: "p", label: "CPU %", value: 1 },
              { timestamp: "2026-07-01T08:59:30Z", host: "c", label: "CPU %", value: 1 },
            ],
          },
        ],
      }),
    ).sessions[0];
    const { effective } = effectiveOf(s);
    expect(effective.get("p")).toBe("down");
    expect(effective.get("c")).toBe("ok");
  });

  it("hop cycles yield unknown + one warning each, and terminate", () => {
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "a", element: "a", ip: "10.0.0.1", hop: "b" },
                { id: "b", element: "b", ip: "10.0.0.2", hop: "a" },
              ],
            },
            meta: { interval: 30.0, charts: [] },
            chart_map: {},
            metrics: [],
          },
        ],
      }),
    ).sessions[0];
    const { effective, warnings } = effectiveOf(s);
    expect(effective.get("a")).toBe("unknown");
    expect(effective.get("b")).toBe("unknown");
    expect(warnings.length).toBeGreaterThanOrEqual(1);
    expect(warnings[0]).toMatch(/hop cycle/);
  });
});

describe("buildTopoGraph — inter-element (kitchen-sink)", () => {
  const { effective } = effectiveOf(kitchen);
  const graph = buildTopoGraph(kitchen, effective, { sources: false });
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  it("has local plus one node per element, at hop depths", () => {
    expect(byId.get("local")?.kind).toBe("local");
    expect(byId.get("edge-gw")?.depth).toBe(1);
    expect(byId.get("chassis-a")?.depth).toBe(2);
    expect(byId.get("workers")?.depth).toBe(1);
    expect(byId.get("db-01")?.depth).toBe(1);
  });

  it("rollup follows slot-then-id order and enterTarget honors singletons", () => {
    expect(byId.get("chassis-a")?.rollup).toHaveLength(3);
    expect(byId.get("chassis-a")?.enterTarget).toBe("/topology/chassis-a");
    expect(byId.get("db-01")?.enterTarget).toBe("/host/db-01");
  });

  it("collapses implicit links per element pair, keeps declared individual", () => {
    const implicit = graph.edges.filter((e) => e.provenance === "implicit");
    expect(implicit).toHaveLength(1);
    expect(implicit[0].links).toHaveLength(3);
    const declared = graph.edges.filter((e) => e.provenance === "declared");
    expect(declared).toHaveLength(2); // app-db + metrics-udp, both workers~db-01
    expect(declared.map((e) => e.parallelIndex).sort()).toEqual([0, 1]);
  });

  it("passes impair through and styles dynamic separately", () => {
    const impaired = graph.edges.find((e) => e.impair !== null);
    expect(impaired?.impair).toBe("edge-gw");
    expect(graph.edges.some((e) => e.provenance === "dynamic")).toBe(true);
  });

  it("attaches hop-less elements to local", () => {
    const locals = graph.edges.filter((e) => e.provenance === "local");
    expect(locals.map((e) => e.target).sort()).toEqual(["db-01", "edge-gw", "mgmt-01", "workers"]);
  });

  it("sources overlay adds deduped reports-for edges only when on", () => {
    expect(graph.edges.some((e) => e.provenance === "reports-for")).toBe(false);
    const withSources = buildTopoGraph(kitchen, effective, { sources: true });
    const reports = withSources.edges.filter((e) => e.provenance === "reports-for");
    expect(reports).toHaveLength(1);
    expect(reports[0].source).toBe("mgmt-01");
    expect(reports[0].target).toBe("chassis-a");
  });
});

describe("buildTopoGraph — intra-element", () => {
  const { effective } = effectiveOf(kitchen);
  const graph = buildTopoGraph(kitchen, effective, { expand: "chassis-a", sources: false });
  const ids = graph.nodes.map((n) => n.id);

  it("renders members, the hop path to local, and per-link implicit edges", () => {
    expect(ids).toContain("local");
    expect(ids).toContain("edge-gw");
    expect(ids).toContain("chassis-a_lc1");
    const implicit = graph.edges.filter((e) => e.provenance === "implicit");
    expect(implicit).toHaveLength(3); // individual at this level
  });

  it("cascade fixture: intra view fans out the parallel rack pair", () => {
    const { effective: eff } = effectiveOf(cascade);
    const intra = buildTopoGraph(cascade, eff, { expand: "rack-a", sources: false });
    const pair = intra.edges.filter(
      (e) => e.provenance === "declared" && e.source !== e.target,
    );
    expect(pair).toHaveLength(2);
    expect(pair.map((e) => e.parallelIndex).sort()).toEqual([0, 1]);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/topology.test.ts`
Expected: FAIL — cannot resolve `../data/topology`.

- [ ] **Step 3: Implement**

Create `web/src/data/topology.ts`:

```ts
// Topology graph model (spec 2026-07-11): reachability cascade + node/edge
// derivation. Pure. Only the hop skeleton (single-parent => tree) determines
// depth; every data-plane link is a cross-edge that never places a node, so
// connectivity cycles cost nothing. Hop cycles (misconfig) are guarded,
// clamped and warned — never an infinite loop.
import type { HostSnapshot, LinkSnapshot } from "../api/export.gen";
import type { DerivedElement, NormalizedSession } from "./exportDoc";
import type { SubjectHealth } from "./health";

export type EffectiveStatus = SubjectHealth["status"] | "unreachable";

export interface ReachabilityResult {
  effective: Map<string, EffectiveStatus>;
  warnings: string[];
}

export interface TopoNode {
  id: string;
  kind: "local" | "element" | "host";
  depth: number;
  label: string;
  element?: DerivedElement;
  host?: HostSnapshot;
  effective?: EffectiveStatus;
  rollup?: EffectiveStatus[];
  enterTarget?: string;
}

export interface TopoEdge {
  id: string;
  source: string;
  target: string;
  provenance: "implicit" | "declared" | "dynamic" | "local" | "reports-for";
  link?: LinkSnapshot;
  links?: LinkSnapshot[];
  impair: string | null;
  parallelIndex: number;
}

export interface TopoGraph {
  nodes: TopoNode[];
  edges: TopoEdge[];
  warnings: string[];
}

interface ChainResult {
  ancestors: HostSnapshot[];
  cyclic: boolean;
  cycleAt: string | null;
}

/** Walk a host's hop chain toward local. Cycle-guarded, dangling-tolerant. */
function chainOf(host: HostSnapshot, byId: Map<string, HostSnapshot>): ChainResult {
  const seen = new Set<string>([host.id]);
  const ancestors: HostSnapshot[] = [];
  let cursor = host.hop ?? null;
  while (cursor != null) {
    if (seen.has(cursor)) return { ancestors, cyclic: true, cycleAt: cursor };
    seen.add(cursor);
    const ancestor = byId.get(cursor);
    if (!ancestor) break; // dangling hop id: treat as attached here
    ancestors.push(ancestor);
    cursor = ancestor.hop ?? null;
  }
  return { ancestors, cyclic: false, cycleAt: null };
}

export function deriveReachability(
  session: NormalizedSession,
  healths: Map<string, SubjectHealth>,
): ReachabilityResult {
  const byId = new Map(session.lab.hosts.map((h) => [h.id, h]));
  const effective = new Map<string, EffectiveStatus>();
  const warnings: string[] = [];
  for (const host of session.lab.hosts) {
    const own: EffectiveStatus = healths.get(host.id)?.status ?? "unknown";
    const chain = chainOf(host, byId);
    if (chain.cyclic) {
      warnings.push(`hop cycle at "${chain.cycleAt}" (walking from ${host.id})`);
      effective.set(host.id, "unknown");
      continue;
    }
    // A host still reporting is reachable by definition; only silent hosts
    // are reinterpreted by a dead ancestor.
    const silent = own === "down" || own === "no-data";
    const deadAncestor = chain.ancestors.some((a) => healths.get(a.id)?.status === "down");
    effective.set(host.id, silent && deadAncestor ? "unreachable" : own);
  }
  return { effective, warnings };
}

function pairKey(a: string, b: string): string {
  return a < b ? `${a}~${b}` : `${b}~${a}`;
}

function assignParallelIndices(edges: TopoEdge[]): void {
  const groups = new Map<string, TopoEdge[]>();
  for (const e of edges) {
    const key = pairKey(e.source, e.target);
    const list = groups.get(key);
    if (list) list.push(e);
    else groups.set(key, [e]);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.id.localeCompare(b.id));
    list.forEach((e, i) => {
      e.parallelIndex = i;
    });
  }
}

function slotThenId(byId: Map<string, HostSnapshot>) {
  return (a: string, b: string): number => {
    const slotA = byId.get(a)?.slot ?? Number.POSITIVE_INFINITY;
    const slotB = byId.get(b)?.slot ?? Number.POSITIVE_INFINITY;
    return slotA - slotB || a.localeCompare(b);
  };
}

export function buildTopoGraph(
  session: NormalizedSession,
  effective: Map<string, EffectiveStatus>,
  opts: { expand?: string; sources: boolean },
): TopoGraph {
  const byId = new Map(session.lab.hosts.map((h) => [h.id, h]));
  const warnings: string[] = [];
  const elementOf = new Map<string, string>();
  for (const el of session.elements) {
    for (const id of el.hostIds) elementOf.set(id, el.id);
  }
  const depthOf = (host: HostSnapshot): number => {
    const chain = chainOf(host, byId);
    if (chain.cyclic) warnings.push(`hop cycle at "${chain.cycleAt}" (walking from ${host.id})`);
    return chain.ancestors.length + 1;
  };
  // Deduplicate cycle warnings (reachability already reported them per host).
  const uniq = (list: string[]): string[] => [...new Set(list)];

  const nodes: TopoNode[] = [{ id: "local", kind: "local", depth: 0, label: "local" }];
  const edges: TopoEdge[] = [];
  const statusOf = (id: string): EffectiveStatus => effective.get(id) ?? "unknown";

  // Distinct (source host, fed host) pairs from metric attribution.
  const feeds = new Map<string, Set<string>>();
  if (opts.sources) {
    for (const m of session.metrics) {
      if (m.source == null || m.host == null) continue;
      if (!byId.has(m.source) || !byId.has(m.host)) continue;
      let set = feeds.get(m.source);
      if (!set) {
        set = new Set();
        feeds.set(m.source, set);
      }
      set.add(m.host);
    }
  }

  if (opts.expand === undefined) {
    // ── Inter-element graph ──────────────────────────────────────────────
    for (const el of session.elements) {
      const members = [...el.hostIds].sort(slotThenId(byId));
      const memberHosts = members
        .map((id) => byId.get(id))
        .filter((h): h is HostSnapshot => h !== undefined);
      const depth = memberHosts.length
        ? Math.min(...memberHosts.map((h) => depthOf(h)))
        : 1;
      nodes.push({
        id: el.id,
        kind: "element",
        depth,
        label: el.id,
        element: el,
        rollup: members.map(statusOf),
        enterTarget: el.singleton ? `/host/${members[0]}` : `/topology/${el.id}`,
      });
      if (memberHosts.some((h) => h.hop == null)) {
        edges.push({
          id: `local:${el.id}`,
          source: "local",
          target: el.id,
          provenance: "local",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
    const nodeOf = (hostId: string): string | undefined => elementOf.get(hostId);
    const implicitByPair = new Map<string, { a: string; b: string; links: LinkSnapshot[] }>();
    for (const link of session.lab.links) {
      const a = nodeOf(link.endpoints[0].host);
      const b = nodeOf(link.endpoints[1].host);
      if (!a || !b || a === b) continue; // dangling or intra-element: not at this level
      if ((link.provenance ?? "declared") === "implicit") {
        const key = pairKey(a, b);
        const group = implicitByPair.get(key);
        if (group) group.links.push(link);
        else implicitByPair.set(key, { a, b, links: [link] });
      } else {
        edges.push({
          id: link.id,
          source: a,
          target: b,
          provenance: link.provenance === "dynamic" ? "dynamic" : "declared",
          link,
          impair: link.impair ?? null,
          parallelIndex: 0,
        });
      }
    }
    for (const [key, group] of implicitByPair) {
      edges.push({
        id: `implicit:${key}`,
        source: group.a,
        target: group.b,
        provenance: "implicit",
        links: group.links,
        impair: null,
        parallelIndex: 0,
      });
    }
    for (const [src, fed] of feeds) {
      const srcNode = nodeOf(src);
      if (!srcNode) continue;
      const targets = new Set([...fed].map((h) => nodeOf(h)).filter((t) => t && t !== srcNode));
      for (const target of targets) {
        edges.push({
          id: `reports:${src}~${target}`,
          source: srcNode,
          target: target as string,
          provenance: "reports-for",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
  } else {
    // ── Intra-element graph ──────────────────────────────────────────────
    const el = session.elements.find((e) => e.id === opts.expand);
    const members = new Set(el?.hostIds ?? []);
    const include = new Map<string, HostSnapshot>();
    const addHost = (h: HostSnapshot | undefined): void => {
      if (h && !include.has(h.id)) include.set(h.id, h);
    };
    for (const id of members) {
      const host = byId.get(id);
      addHost(host);
      if (host) for (const ancestor of chainOf(host, byId).ancestors) addHost(ancestor);
    }
    const rendered = session.lab.links.filter(
      (l) => members.has(l.endpoints[0].host) || members.has(l.endpoints[1].host),
    );
    for (const link of rendered) {
      addHost(byId.get(link.endpoints[0].host));
      addHost(byId.get(link.endpoints[1].host));
    }
    if (opts.sources) {
      for (const [src, fed] of feeds) {
        if ([...fed].some((h) => members.has(h))) addHost(byId.get(src));
      }
    }
    const ordered = [...include.values()].sort((a, b) => slotThenId(byId)(a.id, b.id));
    for (const host of ordered) {
      nodes.push({
        id: host.id,
        kind: "host",
        depth: depthOf(host),
        label: host.id,
        host,
        effective: statusOf(host.id),
        enterTarget: `/host/${host.id}`,
      });
      if (host.hop == null) {
        edges.push({
          id: `local:${host.id}`,
          source: "local",
          target: host.id,
          provenance: "local",
          impair: null,
          parallelIndex: 0,
        });
      } else if (include.has(host.hop)) {
        edges.push({
          id: `hop:${host.id}`,
          source: host.hop,
          target: host.id,
          provenance: "implicit",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
    for (const link of rendered) {
      const [a, b] = [link.endpoints[0].host, link.endpoints[1].host];
      if (!include.has(a) || !include.has(b)) continue;
      if ((link.provenance ?? "declared") === "implicit") continue; // hop edges already drawn
      edges.push({
        id: link.id,
        source: a,
        target: b,
        provenance: link.provenance === "dynamic" ? "dynamic" : "declared",
        link,
        impair: link.impair ?? null,
        parallelIndex: 0,
      });
    }
    if (opts.sources) {
      for (const [src, fed] of feeds) {
        for (const h of fed) {
          if (!members.has(h) || !include.has(src)) continue;
          edges.push({
            id: `reports:${src}~${h}`,
            source: src,
            target: h,
            provenance: "reports-for",
            impair: null,
            parallelIndex: 0,
          });
        }
      }
    }
  }

  assignParallelIndices(edges);
  return { nodes, edges, warnings: uniq(warnings) };
}
```

NOTE for the implementer: the intra view intentionally draws hop edges from `host.hop` (id `hop:<child>`) instead of the implicit LinkSnapshots, so ancestors outside the element still get their edge even when the corresponding implicit link's other endpoint isn't a member. Implicit links are skipped there to avoid double edges. Do not "simplify" this.

- [ ] **Step 4: Verify green**

Run: `cd web && npx vitest run src/__tests__/topology.test.ts && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS. If a kitchen-sink expectation fails, READ the actual graph first (log it) — the fixture facts in the header are ground truth; a mismatch means the code, not the test, is wrong.

- [ ] **Step 5: Commit**

```bash
git add web/src/data/topology.ts web/src/__tests__/topology.test.ts
git commit -m "feat(web): topology data layer — reachability cascade + graph model

Hop-skeleton depths (cycle-guarded), silent-host cascade to unreachable,
element-level implicit collapse, per-pair parallel indices, sources
synthesis from metric attribution.

Assisted-by: Claude Fable 5"
```

---

### Task 3: React Flow dep, layout, node components

**Files:**
- Modify: `web/package.json` (+ `@xyflow/react`, exact pin), `web/README.md` (credit line)
- Create: `web/src/topo/layout.ts`, `web/src/topo/nodes.tsx`
- Test: `web/src/__tests__/topolayout.test.ts`, `web/src/__tests__/toponodes.test.tsx`

**Interfaces:**
- Consumes: `TopoNode`, `EffectiveStatus` (Task 2).
- Produces (Task 4 relies on):
  - `layoutTopo(nodes: TopoNode[]): Map<string, { x: number; y: number }>` — deterministic; `COL_W = 280`, `ROW_H = 110`; x = depth × COL_W; y = row index × ROW_H within the depth column; column order: `kind` local first, then by slot-then-id for hosts / id for elements (stable).
  - `nodeTypes` object `{ local: LocalNode, element: ElementNode, host: HostNode }` (React Flow registration), each component reading `props.data` as a `TopoNode`.
  - `STATUS_DOT: Record<EffectiveStatus, string>` and `STATUS_SEGMENT: Record<EffectiveStatus, string>` class maps (exported for reuse): ok `bg-status-ok`, down `bg-status-error`, unreachable dot `border border-status-error/60 bg-transparent` / segment `bg-status-error/25`, no-data `bg-gray-300 dark:bg-gray-600`, unknown `bg-gray-200 dark:bg-gray-700`.
  - Every node's root div carries `data-testid={`topo-node-${id}`}` and `data-status` (host: its effective; element: worst member status by severity `down > unreachable > no-data > unknown > ok`; local: `"local"`).

- [ ] **Step 1: Install the dep**

Run: `cd web && npm install -E @xyflow/react`
Record the resolved version. `git diff package.json` shows only that line (+ lockfile). Add to `web/README.md` (create a "Third-party" section if none): `Topology canvas: [React Flow](https://reactflow.dev) (@xyflow/react, MIT) — attribution panel disabled for the air-gap; credited here instead.`

- [ ] **Step 2: Write the failing tests**

Create `web/src/__tests__/topolayout.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import type { TopoNode } from "../data/topology";
import { layoutTopo } from "../topo/layout";

function node(id: string, depth: number, kind: TopoNode["kind"] = "element"): TopoNode {
  return { id, kind, depth, label: id };
}

describe("layoutTopo", () => {
  it("maps depth to columns and keeps row order stable", () => {
    const pos = layoutTopo([
      node("local", 0, "local"),
      node("beta", 1),
      node("alpha", 1),
      node("deep", 2),
    ]);
    expect(pos.get("local")).toEqual({ x: 0, y: 0 });
    expect(pos.get("alpha")?.x).toBe(280);
    expect(pos.get("beta")?.x).toBe(280);
    expect(pos.get("deep")?.x).toBe(560);
    // alpha sorts before beta -> row 0 vs row 1
    expect(pos.get("alpha")?.y).toBe(0);
    expect(pos.get("beta")?.y).toBe(110);
  });

  it("orders hosts by slot then id within a column", () => {
    const a: TopoNode = {
      id: "za",
      kind: "host",
      depth: 1,
      label: "za",
      host: { id: "za", element: "e", slot: 1 } as TopoNode["host"],
    };
    const b: TopoNode = {
      id: "ab",
      kind: "host",
      depth: 1,
      label: "ab",
      host: { id: "ab", element: "e", slot: 2 } as TopoNode["host"],
    };
    const pos = layoutTopo([b, a]);
    expect(pos.get("za")?.y).toBe(0); // slot 1 before slot 2 despite id order
    expect(pos.get("ab")?.y).toBe(110);
  });

  it("is deterministic across input order", () => {
    const nodes = [node("local", 0, "local"), node("x", 1), node("y", 1)];
    const one = layoutTopo(nodes);
    const two = layoutTopo([...nodes].reverse());
    expect(two).toEqual(one);
  });
});
```

Create `web/src/__tests__/toponodes.test.tsx`:

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoNode } from "../data/topology";
import { ElementNode, HostNode } from "../topo/nodes";

afterEach(cleanup);

const element: TopoNode = {
  id: "chassis-a",
  kind: "element",
  depth: 2,
  label: "chassis-a",
  element: {
    id: "chassis-a",
    type: "physical",
    explicit: false,
    description: null,
    hostIds: ["lc1", "lc2", "sup"],
    singleton: false,
  },
  rollup: ["ok", "unreachable", "down"],
  enterTarget: "/topology/chassis-a",
};

describe("ElementNode", () => {
  it("renders glyph, rollup segments with statuses, and worst data-status", () => {
    render(<ElementNode data={element} />);
    const root = screen.getByTestId("topo-node-chassis-a");
    expect(root.getAttribute("data-status")).toBe("down");
    expect(root.textContent).toContain("chassis-a");
    expect(root.textContent).toContain("3 hosts");
    const segments = root.querySelectorAll("[data-status-segment]");
    expect(segments).toHaveLength(3);
    expect(segments[1].getAttribute("data-status-segment")).toBe("unreachable");
  });
});

describe("HostNode", () => {
  it("shows slot badge and dimmed unreachable treatment", () => {
    const host: TopoNode = {
      id: "rack-a_n1",
      kind: "host",
      depth: 2,
      label: "rack-a_n1",
      host: { id: "rack-a_n1", element: "rack-a", slot: 1 } as TopoNode["host"],
      effective: "unreachable",
      enterTarget: "/host/rack-a_n1",
    };
    render(<HostNode data={host} slotBadge />);
    const root = screen.getByTestId("topo-node-rack-a_n1");
    expect(root.getAttribute("data-status")).toBe("unreachable");
    expect(root.textContent).toContain("slot 1");
    expect(root.className).toContain("opacity-60");
  });

  it("omits the slot badge when not requested", () => {
    const host: TopoNode = {
      id: "h",
      kind: "host",
      depth: 1,
      label: "h",
      host: { id: "h", element: "h" } as TopoNode["host"],
      effective: "ok",
    };
    render(<HostNode data={host} />);
    expect(screen.getByTestId("topo-node-h").textContent).not.toContain("slot");
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/topolayout.test.ts src/__tests__/toponodes.test.tsx`
Expected: FAIL — modules don't exist.

- [ ] **Step 4: Implement**

Create `web/src/topo/layout.ts`:

```ts
// Deterministic layered layout: depth -> column, stable row order. No force
// physics: positions never jitter, so screenshots and Playwright assertions
// stay reproducible. O(n log n); fine for lab scale (tens of nodes).
import type { TopoNode } from "../data/topology";

export const COL_W = 280;
export const ROW_H = 110;

function rowOrder(a: TopoNode, b: TopoNode): number {
  if (a.kind === "local" !== (b.kind === "local")) return a.kind === "local" ? -1 : 1;
  const slotA = a.host?.slot ?? Number.POSITIVE_INFINITY;
  const slotB = b.host?.slot ?? Number.POSITIVE_INFINITY;
  return slotA - slotB || a.id.localeCompare(b.id);
}

export function layoutTopo(nodes: TopoNode[]): Map<string, { x: number; y: number }> {
  const byDepth = new Map<number, TopoNode[]>();
  for (const n of nodes) {
    const col = byDepth.get(n.depth);
    if (col) col.push(n);
    else byDepth.set(n.depth, [n]);
  }
  const out = new Map<string, { x: number; y: number }>();
  for (const [depth, col] of byDepth) {
    col.sort(rowOrder);
    col.forEach((n, row) => out.set(n.id, { x: depth * COL_W, y: row * ROW_H }));
  }
  return out;
}
```

Create `web/src/topo/nodes.tsx`:

```tsx
// Custom React Flow nodes — plain Tailwind DOM, so testids, tokens and the
// Playwright contract all survive (the reason React Flow beat canvas
// renderers, spec §Decisions). data-status carries semantic state for tests;
// classes are presentation only.
import { Handle, Position } from "@xyflow/react";

import type { EffectiveStatus, TopoNode } from "../data/topology";

export const STATUS_DOT: Record<EffectiveStatus, string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  unreachable: "border border-status-error/60 bg-transparent",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

export const STATUS_SEGMENT: Record<EffectiveStatus, string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  unreachable: "bg-status-error/25",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

const SEVERITY: EffectiveStatus[] = ["down", "unreachable", "no-data", "unknown", "ok"];

function worst(statuses: EffectiveStatus[]): EffectiveStatus {
  for (const s of SEVERITY) if (statuses.includes(s)) return s;
  return "ok";
}

function Ports() {
  return (
    <>
      <Handle type="target" position={Position.Left} className="!h-1 !w-1 !opacity-0" />
      <Handle type="source" position={Position.Right} className="!h-1 !w-1 !opacity-0" />
    </>
  );
}

export function LocalNode({ data }: { data: TopoNode }) {
  return (
    <div
      data-testid="topo-node-local"
      data-status="local"
      className="rounded-lg border-2 border-brand-500 bg-white px-3 py-2 text-sm font-semibold
        dark:bg-gray-950"
    >
      ◉ local
      <span className="ml-2 text-xs font-normal text-gray-400">you are here</span>
      <Ports />
    </div>
  );
}

export function ElementNode({ data }: { data: TopoNode }) {
  const rollup = data.rollup ?? [];
  return (
    <div
      data-testid={`topo-node-${data.id}`}
      data-status={worst(rollup)}
      className="w-52 cursor-pointer rounded-lg border border-gray-200 bg-white px-3 py-2
        hover:border-brand-500 dark:border-gray-800 dark:bg-gray-950 dark:hover:border-brand-500"
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        <span aria-hidden>{data.element?.type === "physical" ? "▦" : "▤"}</span>
        <span className="truncate">{data.label}</span>
        <span aria-hidden className="ml-auto text-gray-400">
          ⤢
        </span>
      </p>
      {rollup.length > 0 && (
        <div className="mt-1.5 flex h-1.5 w-full gap-px overflow-hidden rounded">
          {rollup.map((status, i) => (
            <span
              // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional by design
              key={i}
              data-status-segment={status}
              className={`min-w-1 flex-1 ${STATUS_SEGMENT[status]}`}
            />
          ))}
        </div>
      )}
      <p className="mt-1 text-xs text-gray-400">
        {data.element?.hostIds.length ?? 0} host{(data.element?.hostIds.length ?? 0) === 1 ? "" : "s"}
      </p>
      <Ports />
    </div>
  );
}

export function HostNode({ data, slotBadge = false }: { data: TopoNode; slotBadge?: boolean }) {
  const status = data.effective ?? "unknown";
  return (
    <div
      data-testid={`topo-node-${data.id}`}
      data-status={status}
      className={`w-44 cursor-pointer rounded-lg border border-gray-200 bg-white px-3 py-2
        hover:border-brand-500 dark:border-gray-800 dark:bg-gray-950 dark:hover:border-brand-500
        ${status === "unreachable" ? "opacity-60" : ""}`}
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[status]}`} />
        <span className="truncate">{data.label}</span>
      </p>
      <p className="mt-0.5 text-xs text-gray-400">
        {status === "unreachable" ? "unreachable · " : ""}
        {slotBadge && data.host?.slot != null ? `slot ${data.host.slot}` : ""}
        {slotBadge && data.host?.slot != null ? "" : data.host?.board ?? ""}
      </p>
      <Ports />
    </div>
  );
}
```

**Adaptation protocol:** `nodeTypes` registration and the exact `NodeProps` typing land in Task 4 (the page owns the `<ReactFlow>` mount); these components deliberately take `{ data }` structurally so they are directly RTL-testable. `slotBadge` is passed by Task 4's node mapping (physical intra views only). If `Handle` outside a React Flow provider throws under RTL, the sanctioned adaptation is rendering `Ports` only when inside the provider (`useNodeId() !== null` guard or a `ports?: boolean` prop defaulting true with tests passing false) — disclose which. Do not drop the testids or data-status attributes.

- [ ] **Step 5: Verify green**

Run: `cd web && npx vitest run src/__tests__/topolayout.test.ts src/__tests__/toponodes.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build`
Expected: all PASS; note the dist-size delta from @xyflow/react in your report; `npm run build` must succeed with no new external URLs (the attribution fix lands in Task 4 with the mount — the dep alone must not add URLs to the bundle yet; if the build embeds one already, note it, Task 4's grep run will prove the final state).

- [ ] **Step 6: Commit**

```bash
git add web/package.json web/package-lock.json web/README.md web/src/topo/layout.ts web/src/topo/nodes.tsx web/src/__tests__/topolayout.test.ts web/src/__tests__/toponodes.test.tsx
git commit -m "feat(web): topology layout + node components on @xyflow/react

Deterministic hop-depth columns (no force jitter); element/host/local
nodes as plain Tailwind DOM with data-status semantics; unreachable =
dimmed + hollow dot, distinct from down and from no-data grays.

Assisted-by: Claude Fable 5"
```

---

### Task 4: Edges, TopologyPage, routes, Grid ⇄ Topology toggle

**Files:**
- Create: `web/src/topo/LinkEdge.tsx`, `web/src/topo/TopologyPage.tsx`
- Delete: `web/src/pages/TopologyPage.tsx` (placeholder — the real page lives in `web/src/topo/`)
- Modify: `web/src/App.tsx` (routes), `web/src/pages/OverviewPage.tsx` (context row with the toggle)
- Test: `web/src/__tests__/topoedge.test.tsx`; existing `pages.test.tsx` must keep passing (it may reference the placeholder — if it asserts `topology-page`, the new page keeps that testid)

**Interfaces:**
- Consumes: Tasks 2/3 exports; `ToggleGroup`; wouter (`useLocation`, `useParams`, `Route`); `useActiveSession`, `useReviewStore` range; `healthForHosts`.
- Produces (Tasks 5/6 rely on): `TopologyPage` mounted at `/topology` AND `/topology/:elementId` keeping testid `topology-page`; `view-toggle` ToggleGroup on both OverviewPage and TopologyPage (options grid|topology); edge testids `topo-link-<edgeId>` (a `<g>` wrapper), impair pill `topo-impair-<edgeId>`; `topo-fit` button; `topo-warnings` paragraph (rendered only when warnings exist); `topo-breadcrumb` nav on the intra view; `onSelectEdge(edge: TopoEdge)` wiring point left as a no-op stub for Task 5 (`// Task 5 wires the inspector here`).

Edge visual spec (binding): provenance styles — implicit `stroke #9ca3af, strokeWidth 1.5, solid`; declared `stroke #4b5563 (dark: #9ca3af), strokeWidth 2, solid`; dynamic `same stroke as declared, strokeDasharray "7 4"`; local `stroke #d1d5db (dark: #374151), strokeWidth 1.5, solid`; reports-for `stroke #9ca3af, strokeWidth 1.5, strokeDasharray "2 5"`. Parallel fan-out: bezier `curvature = (parallelIndex - (groupSize - 1) / 2) * 0.35` — pass `groupSize` via edge data (Task 4 computes per-pair group sizes when mapping `TopoEdge[]` to React Flow edges). Impair marker: a small pill at the bezier label point, text `impair · <hostId>`.

- [ ] **Step 1: Write the failing edge test**

Create `web/src/__tests__/topoedge.test.tsx` — LinkEdge is testable via React Flow's `<svg>` context requirements; keep it structural (render the component's inner builder, not a ReactFlow mount):

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { edgeStyle } from "../topo/LinkEdge";

afterEach(cleanup);

describe("edgeStyle", () => {
  it("maps provenance to distinct strokes", () => {
    expect(edgeStyle("implicit", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    expect(edgeStyle("declared", false).strokeWidth).toBe(2);
    const styles = ["implicit", "declared", "dynamic", "local", "reports-for"].map((p) =>
      JSON.stringify(edgeStyle(p as Parameters<typeof edgeStyle>[0], false)),
    );
    expect(new Set(styles).size).toBe(5);
  });

  it("selected state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth as number,
    );
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/topoedge.test.tsx`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement LinkEdge**

Create `web/src/topo/LinkEdge.tsx`:

```tsx
// Custom edge: provenance-styled bezier with parallel fan-out and an impair
// pill. Wrapped in <g data-testid> so Playwright can click/assert edges —
// BaseEdge's own prop passthrough is not part of our contract.
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@xyflow/react";

import type { TopoEdge } from "../data/topology";

type Provenance = TopoEdge["provenance"];

export function edgeStyle(
  provenance: Provenance,
  selected: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string } {
  const base = (() => {
    switch (provenance) {
      case "declared":
        return { stroke: "#4b5563", strokeWidth: 2 };
      case "dynamic":
        return { stroke: "#4b5563", strokeWidth: 2, strokeDasharray: "7 4" };
      case "local":
        return { stroke: "#d1d5db", strokeWidth: 1.5 };
      case "reports-for":
        return { stroke: "#9ca3af", strokeWidth: 1.5, strokeDasharray: "2 5" };
      default:
        return { stroke: "#9ca3af", strokeWidth: 1.5 };
    }
  })();
  return selected ? { ...base, strokeWidth: base.strokeWidth + 1.5 } : base;
}

export interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  [key: string]: unknown;
}

export function LinkEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition, selected } =
    props;
  const data = props.data as unknown as LinkEdgeData;
  const { edge, groupSize } = data;
  const curvature = (edge.parallelIndex - (groupSize - 1) / 2) * 0.35;
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    curvature: 0.25 + Math.abs(curvature),
  });
  return (
    <g data-testid={`topo-link-${edge.id}`}>
      <BaseEdge id={id} path={path} style={edgeStyle(edge.provenance, selected ?? false)} />
      {edge.impair !== null && (
        <EdgeLabelRenderer>
          <span
            data-testid={`topo-impair-${edge.id}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
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

**Adaptation protocol:** bezier curvature only bends one way — if two parallel edges with symmetric indices render overlapping (verify visually in Task 6's browser lane, or reason from the math), the sanctioned adaptation is signed offsets via `getBezierPath`'s curvature sign or an offset-path approach, keeping `edgeStyle` and all testids byte-identical. Version-typing drift in `EdgeProps` generics is adaptable (typing plumbing only), disclosed in the report.

- [ ] **Step 4: Implement the page + routes + toggle**

Create `web/src/topo/TopologyPage.tsx`:

```tsx
// Topology view (spec §10): inter-element map at /topology, intra view at
// /topology/:elementId. React Flow supplies pan/zoom; positions come from
// the deterministic layered layout; the review store's range drives health,
// so narrowing the range re-derives the cascade live.
import "@xyflow/react/dist/style.css";

import { useMemo, useState } from "react";
import {
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { Link, useLocation, useParams } from "wouter";

import { healthForHosts } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { buildTopoGraph, deriveReachability, type TopoEdge } from "../data/topology";
import { ToggleGroup } from "../ui/ToggleGroup";
import { LinkEdge } from "./LinkEdge";
import { layoutTopo } from "./layout";
import { ElementNode, HostNode, LocalNode } from "./nodes";

const nodeTypes = {
  local: LocalNode,
  element: ElementNode,
  host: HostNode,
};
const edgeTypes = { link: LinkEdge };

function FitButton() {
  const { fitView } = useReactFlow();
  return (
    <button
      type="button"
      data-testid="topo-fit"
      onClick={() => fitView({ padding: 0.2 })}
      className="cursor-pointer rounded-md border border-gray-200 px-2 py-1 text-xs text-gray-500
        hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-900"
    >
      Fit
    </button>
  );
}

export function TopologyPage() {
  const params = useParams<{ elementId?: string }>();
  const [, navigate] = useLocation();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const [sources, setSources] = useState(false);
  const expand = params.elementId;

  const graph = useMemo(() => {
    if (!session) return null;
    const { effective, warnings } = deriveReachability(session, healthForHosts(session, range));
    const g = buildTopoGraph(session, effective, { expand, sources });
    return { ...g, warnings: [...warnings, ...g.warnings] };
  }, [session, range, expand, sources]);

  const flow = useMemo(() => {
    if (!graph || !session) return { nodes: [] as Node[], edges: [] as Edge[] };
    const positions = layoutTopo(graph.nodes);
    const expandEl = session.elements.find((e) => e.id === expand);
    const physical = expandEl?.type === "physical";
    const nodes: Node[] = graph.nodes.map((n) => ({
      id: n.id,
      type: n.kind,
      position: positions.get(n.id) ?? { x: 0, y: 0 },
      data: n as unknown as Record<string, unknown>,
      draggable: false,
      connectable: false,
      ...(n.kind === "host" && physical && expandEl?.hostIds.includes(n.id)
        ? { data: { ...n, slotBadge: true } as unknown as Record<string, unknown> }
        : {}),
    }));
    const groupSizes = new Map<string, number>();
    for (const e of graph.edges) {
      const key = [e.source, e.target].sort().join("~");
      groupSizes.set(key, (groupSizes.get(key) ?? 0) + 1);
    }
    const edges: Edge[] = graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "link",
      data: { edge: e, groupSize: groupSizes.get([e.source, e.target].sort().join("~")) ?? 1 },
    }));
    return { nodes, edges };
  }, [graph, session, expand]);

  if (!session) return null;
  if (expand && !session.elementIds.has(expand)) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown element "{expand}" in this session. <Link href="/topology">Back to topology</Link>
      </main>
    );
  }

  const onSelectEdge = (_edge: TopoEdge): void => {
    // Task 5 wires the inspector here.
  };

  return (
    <main data-testid="topology-page" className="flex h-[calc(100vh-6.5rem)] flex-col gap-3 p-4">
      <div className="flex items-center gap-3">
        <ToggleGroup
          testId="view-toggle"
          label="View"
          selectedId="topology"
          onSelect={(id) => {
            if (id === "grid") navigate("/");
          }}
          options={[
            { id: "grid", label: "Grid" },
            { id: "topology", label: "Topology" },
          ]}
        />
        {expand && (
          <nav data-testid="topo-breadcrumb" className="text-sm text-gray-400">
            <Link href="/topology">Topology</Link> / {expand}
          </nav>
        )}
        <button
          type="button"
          data-testid="sources-toggle"
          aria-pressed={sources}
          onClick={() => setSources((v) => !v)}
          className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
            sources
              ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
              : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
          }`}
        >
          Sources
        </button>
        <ReactFlowProvider>
          <FitButton />
        </ReactFlowProvider>
      </div>
      {graph && graph.warnings.length > 0 && (
        <p data-testid="topo-warnings" className="text-xs text-status-error">
          {graph.warnings.join(" · ")}
        </p>
      )}
      <div className="min-h-0 grow rounded-lg border border-gray-200 dark:border-gray-800">
        <ReactFlowProvider>
          <ReactFlow
            nodes={flow.nodes}
            edges={flow.edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            minZoom={0.2}
            proOptions={{ hideAttribution: true }}
            onNodeClick={(_evt, node) => {
              const target = (node.data as { enterTarget?: string }).enterTarget;
              if (target) navigate(target);
            }}
            onEdgeClick={(_evt, edge) => {
              const data = edge.data as { edge?: TopoEdge } | undefined;
              if (data?.edge && data.edge.provenance !== "local") onSelectEdge(data.edge);
            }}
          >
            <Controls showInteractive={false} />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
    </main>
  );
}
```

**Known wart to fix during implementation, not after:** the `FitButton` above sits in a DIFFERENT `ReactFlowProvider` than the `<ReactFlow>` mount — `fitView` would act on nothing. Restructure so ONE `ReactFlowProvider` wraps both the toolbar (FitButton) and the canvas (move the provider up to wrap the whole `<main>` content, or render FitButton inside a `<Panel position="top-right">` child of `<ReactFlow>`). This is called out here so the implementer resolves it deliberately — the test for it is Task 6's fit-view spec, and the report must state which structure was chosen.

Modify `web/src/App.tsx`: replace the placeholder import (`web/src/pages/TopologyPage.tsx` is deleted) with `import { TopologyPage } from "./topo/TopologyPage";` and register BOTH routes before the catch-all:

```tsx
<Route path="/topology" component={TopologyPage} />
<Route path="/topology/:elementId" component={TopologyPage} />
```

Modify `web/src/pages/OverviewPage.tsx`: add the context row as the FIRST child inside `<main data-testid="overview-page">`:

```tsx
      <div className="flex items-center gap-3">
        <ToggleGroup
          testId="view-toggle"
          label="View"
          selectedId="grid"
          onSelect={(id) => {
            if (id === "topology") navigate("/topology");
          }}
          options={[
            { id: "grid", label: "Grid" },
            { id: "topology", label: "Topology" },
          ]}
        />
      </div>
```

with `import { useLocation } from "wouter";` → `const [, navigate] = useLocation();` and the `ToggleGroup` import added (wouter's `Link` import already exists; extend it).

- [ ] **Step 5: Verify green (full suite — overview/pages survivors matter)**

Run: `cd web && npx vitest run src/__tests__/topoedge.test.tsx src/__tests__/pages.test.tsx src/__tests__/overview.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build`
Expected: all PASS. If `pages.test.tsx` asserted the placeholder's copy, the page keeps testid `topology-page` — fix the page, never the test, unless the assertion pins the placeholder's literal prose (then STOP and report; the controller adjudicates). After the build: `bash scripts/check_airgap.sh` (or the make target that wraps it) must pass — this proves the `hideAttribution` fix.

- [ ] **Step 6: Commit**

```bash
git add web/src/topo/LinkEdge.tsx web/src/topo/TopologyPage.tsx web/src/App.tsx web/src/pages/OverviewPage.tsx web/src/__tests__/topoedge.test.tsx
git rm web/src/pages/TopologyPage.tsx
git commit -m "feat(web): topology page — React Flow map, provenance edges, view toggle

Inter-element map + intra view on one route pair; deterministic layout
feeds fixed positions; provenance-styled edges with parallel fan-out and
impair pills; Grid <-> Topology toggle on both overview and topology.

Assisted-by: Claude Fable 5"
```

---

### Task 5: Link inspector + Sources polish

**Files:**
- Create: `web/src/topo/LinkInspector.tsx`
- Modify: `web/src/topo/TopologyPage.tsx` (wire selection state → inspector)
- Test: `web/src/__tests__/linkinspector.test.tsx`

**Interfaces:**
- Consumes: `SlideOver` primitive; `LinkSnapshot`, `TopoEdge` (Task 2).
- Produces (Task 6 relies on): `LinkInspector` with props `{ edge: TopoEdge | null; onClose: () => void }` rendering into `SlideOver` with testid `link-inspector`; row testids `inspector-protocol`, `inspector-provenance`, `inspector-endpoints`, `inspector-impair` (only when impaired), `inspector-netem` (the reserved section); collapsed implicit edges render a `inspector-collapsed-note` line "N hop links — open the element to inspect individually".

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/linkinspector.test.tsx`:

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { LinkInspector } from "../topo/LinkInspector";

if (typeof CSS === "undefined" || !CSS.escape) {
  // react-aria portals call CSS.escape — same polyfill as ui.test.tsx.
  (globalThis as { CSS?: unknown }).CSS = {
    escape: (v: string) => v.replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`),
  };
}

afterEach(cleanup);

const link: LinkSnapshot = {
  id: "lnk-1",
  endpoints: [
    { host: "workers_w3", interface: "eth0", ip: "10.20.2.23" },
    { host: "db-01", interface: "eth0", ip: "10.20.3.31" },
  ],
  protocol: "udp",
  provenance: "declared",
  name: "metrics-udp",
  impair: "edge-gw",
};

function edgeWith(overrides: Partial<TopoEdge>): TopoEdge {
  return {
    id: "lnk-1",
    source: "workers",
    target: "db-01",
    provenance: "declared",
    link,
    impair: "edge-gw",
    parallelIndex: 0,
    ...overrides,
  };
}

describe("LinkInspector", () => {
  it("renders link facts, impair, and the reserved NetEm section", async () => {
    render(<LinkInspector edge={edgeWith({})} onClose={vi.fn()} />);
    const panel = await screen.findByTestId("link-inspector");
    expect(panel.textContent).toContain("metrics-udp");
    expect(screen.getByTestId("inspector-protocol").textContent).toContain("udp");
    expect(screen.getByTestId("inspector-provenance").textContent).toContain("declared");
    expect(screen.getByTestId("inspector-endpoints").textContent).toContain("workers_w3");
    expect(screen.getByTestId("inspector-endpoints").textContent).toContain("10.20.3.31");
    expect(screen.getByTestId("inspector-impair").textContent).toContain("edge-gw");
    expect(screen.getByTestId("inspector-netem").textContent).toContain("Configure — coming soon");
  });

  it("renders nothing when no edge is selected", () => {
    render(<LinkInspector edge={null} onClose={vi.fn()} />);
    expect(screen.queryByTestId("link-inspector")).toBeNull();
  });

  it("summarizes collapsed implicit bundles", async () => {
    const bundle = edgeWith({
      id: "implicit:chassis-a~edge-gw",
      provenance: "implicit",
      link: undefined,
      links: [link, { ...link, id: "lnk-2" }, { ...link, id: "lnk-3" }],
      impair: null,
    });
    render(<LinkInspector edge={bundle} onClose={vi.fn()} />);
    await screen.findByTestId("link-inspector");
    expect(screen.getByTestId("inspector-collapsed-note").textContent).toMatch(/3 hop links/);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/linkinspector.test.tsx`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `web/src/topo/LinkInspector.tsx`:

```tsx
// Right side-panel link inspector (spec §10): connectivity facts from the
// static snapshot + the reserved NetEm section. Marking NetEm "coming soon"
// is deliberate — the backend query/edit path is a later phase; the section
// existing NOW teaches the mental model that links are configurable objects.
import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { SlideOver } from "../ui/SlideOver";

function Row(props: { label: string; testId: string; children: React.ReactNode }) {
  return (
    <p data-testid={props.testId} className="text-sm">
      <span className="mr-2 inline-block w-24 text-xs text-gray-400 uppercase">{props.label}</span>
      {props.children}
    </p>
  );
}

function endpointText(link: LinkSnapshot): string {
  return link.endpoints
    .map((ep) => {
      const iface = ep.interface ? ` ${ep.interface}` : "";
      const addr = ep.ip ? ` · ${ep.ip}${ep.port != null ? `:${ep.port}` : ""}` : "";
      return `${ep.host}${iface}${addr}`;
    })
    .join("  ⇄  ");
}

export function LinkInspector(props: { edge: TopoEdge | null; onClose: () => void }) {
  const { edge, onClose } = props;
  if (edge === null) return null;
  const primary = edge.link ?? edge.links?.[0] ?? null;
  const title = primary?.name ?? primary?.id ?? edge.id;
  return (
    <SlideOver isOpen onClose={onClose} title={title} testId="link-inspector">
      {edge.links && edge.links.length > 1 && (
        <p data-testid="inspector-collapsed-note" className="text-xs text-gray-400">
          {edge.links.length} hop links — open the element to inspect individually.
        </p>
      )}
      {primary && (
        <div className="flex flex-col gap-2">
          <Row label="Protocol" testId="inspector-protocol">
            {primary.protocol ?? "—"}
          </Row>
          <Row label="Provenance" testId="inspector-provenance">
            {primary.provenance ?? "declared"}
          </Row>
          <Row label="Endpoints" testId="inspector-endpoints">
            {endpointText(primary)}
          </Row>
          {edge.impair !== null && (
            <Row label="Impair" testId="inspector-impair">
              in-path middlebox: {edge.impair}
            </Row>
          )}
        </div>
      )}
      <div
        data-testid="inspector-netem"
        className="mt-2 rounded-lg border border-dashed border-gray-200 p-3 text-xs text-gray-400
          dark:border-gray-800"
      >
        <p className="mb-1 font-semibold uppercase">NetEm</p>
        <p>delay / loss / jitter / rate — Configure — coming soon</p>
      </div>
    </SlideOver>
  );
}
```

Wire it in `web/src/topo/TopologyPage.tsx`: replace the Task-4 stub with state:

```tsx
const [selected, setSelected] = useState<TopoEdge | null>(null);
```

`onSelectEdge` becomes `setSelected`; reset selection on `expand`/`session` change (`useEffect(() => setSelected(null), [expand, session])` — the established biome-safe dependency shapes apply); render `<LinkInspector edge={selected} onClose={() => setSelected(null)} />` as the last child of `<main>`.

- [ ] **Step 4: Verify green**

Run: `cd web && npx vitest run src/__tests__/linkinspector.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/topo/LinkInspector.tsx web/src/topo/TopologyPage.tsx web/src/__tests__/linkinspector.test.tsx
git commit -m "feat(web): link inspector slide-over with reserved NetEm section

Assisted-by: Claude Fable 5"
```

---

### Task 6: Playwright behavior specs

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_review_shell.py` (append; existing 19 specs keep passing byte-unchanged)

**Interfaces:**
- Consumes: the testid contract from Tasks 3–5 and `_import_fixture` + `shell_dash`.

- [ ] **Step 1: `make web`** — rebuild dist. The air-gap greps MUST pass (proves `hideAttribution`); if they fail, STOP and report the URL — never allowlist.

- [ ] **Step 2: Append the specs**

```python
def test_topology_toggle_and_map(shell_dash, page):
    """Grid <-> Topology toggle (UX §6); map renders elements at hop depths
    rooted at local (UX §10)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.get_by_text("Topology", exact=True).click()
    page.locator('[data-testid="topology-page"]').wait_for()
    for node in ("local", "edge-gw", "chassis-a", "workers", "db-01", "mgmt-01"):
        assert page.locator(f'[data-testid="topo-node-{node}"]').count() == 1
    # Rollup segments on the chassis element node.
    assert page.locator('[data-testid="topo-node-chassis-a"] [data-status-segment]').count() == 3
    page.get_by_text("Grid", exact=True).click()
    page.locator('[data-testid="overview-page"]').wait_for()


def test_topology_drill_in_and_singleton(shell_dash, page):
    """Element enter -> intra view with slot badges; singleton goes straight
    to the host page."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-chassis-a"]').click()
    page.locator('[data-testid="topo-breadcrumb"]').wait_for()
    lc1 = page.locator('[data-testid="topo-node-chassis-a_lc1"]')
    lc1.wait_for()
    assert "slot 1" in lc1.inner_text()
    lc1.click()
    page.locator('[data-testid="subject-page"]').wait_for()
    # Singleton: db-01's element node lands on the host page directly.
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-db-01"]').click()
    page.locator('[data-testid="subject-page"]').wait_for()


def test_link_inspector_and_parallel_edges(shell_dash, page):
    """Links are first-class: declared edges select into the inspector; the
    workers~db pair fans out as two parallel edges; impair pill shows."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    assert page.locator('[data-testid^="topo-link-"]').count() >= 6
    impair = page.locator('[data-testid^="topo-impair-"]')
    assert impair.count() == 1
    assert "edge-gw" in impair.inner_text()
    # Click the impaired edge's path and inspect.
    marker_testid = impair.get_attribute("data-testid")
    edge_id = marker_testid.removeprefix("topo-impair-")
    page.locator(f'[data-testid="topo-link-{edge_id}"] path').first.click(force=True)
    panel = page.locator('[data-testid="link-inspector"]')
    panel.wait_for()
    assert "udp" in page.locator('[data-testid="inspector-protocol"]').inner_text()
    assert "coming soon" in page.locator('[data-testid="inspector-netem"]').inner_text()
    page.get_by_label("Close").click()
    panel.wait_for(state="detached")


def test_sources_overlay_toggles_reports_edges(shell_dash, page):
    """Sources overlay (UX §10): default off; toggling reveals the mgmt
    reports-for edge and toggling again removes it."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    reports = page.locator('[data-testid^="topo-link-reports:"]')
    assert reports.count() == 0
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 1"
    )
    page.locator('[data-testid="sources-toggle"]').click()
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid^=\"topo-link-reports:\"]').length === 0"
    )


def test_cascade_unreachable_vs_down(shell_dash, page):
    """Reachability cascade (spec headline): dead gateway renders down; the
    silent hosts behind it render unreachable, and narrowing the range to
    the healthy window clears both."""
    page.goto(shell_dash.url)
    _import_fixture(page, "cascade.json")
    page.goto(f"{shell_dash.url}#/topology")
    gw = page.locator('[data-testid="topo-node-gw-a"]')
    gw.wait_for()
    assert gw.get_attribute("data-status") == "down"
    rack = page.locator('[data-testid="topo-node-rack-a"]')
    assert rack.get_attribute("data-status") == "down" or True  # worst-status: see intra
    assert rack.locator('[data-status-segment="unreachable"]').count() == 2
    # Intra view: the member nodes carry unreachable themselves.
    rack.click()
    n1 = page.locator('[data-testid="topo-node-rack-a_n1"]')
    n1.wait_for()
    assert n1.get_attribute("data-status") == "unreachable"
    # Parallel rack pair fans out as two declared edges.
    assert page.locator('[data-testid^="topo-link-"][data-testid*="pair"], [data-testid^="topo-link-lnk"]').count() >= 0  # see NOTE below


def test_topology_pan_zoom_fit(shell_dash, page):
    """Pan/zoom smoke: dragging the pane moves the viewport; Fit restores."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topo-node-local"]').wait_for()
    viewport = page.locator(".react-flow__viewport")
    before = viewport.get_attribute("style")
    pane = page.locator(".react-flow__pane")
    box = pane.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 180, box["y"] + box["height"] / 2 + 60)
    page.mouse.up()
    page.wait_for_function(
        "(prev) => document.querySelector('.react-flow__viewport').getAttribute('style') !== prev",
        arg=before,
    )
    page.locator('[data-testid="topo-fit"]').click()
    page.locator('[data-testid="topo-node-local"]').wait_for()
```

**NOTE (mandatory resolution during implementation):** the two placeholder-ish assertions above are marked for the implementer because link IDs are generator-derived (`make_static_link_id`) and not knowable from this plan: (a) in `test_cascade_unreachable_vs_down`, replace the final `count() >= 0` line with a real assertion that exactly TWO declared edges exist between `rack-a_n1` and `rack-a_n2` — inspect `web/fixtures/cascade.json` for the two link ids and assert both `topo-link-<id>` testids are present; (b) in the same test, replace the `or True` rollup line with the actual worst-status value the element node carries (down beats unreachable in the severity order, so `data-status == "down"` — verify against the running DOM and pin it hard). A spec left with `>= 0` or `or True` is a spec that asserts nothing — the task reviewer will reject it.

Also register cascade.json's existence for the harness if the fixture directory is enumerated anywhere (check `shell_dash`/conftest — kitchen-sink is referenced by name, so likely nothing to do).

- [ ] **Step 3: `make dashboard`** — expected: 32 passed (26 previous + 6 new). Debug selector surprises against the actual DOM with scratch `inner_html()` dumps (headless); react-aria + established quirks are documented in the module docstring.

- [ ] **Step 4: Harness + ruff**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -q && uv run ruff format --check tests/e2e/monitor/dashboard/test_review_shell.py && uv run ruff check tests/e2e/monitor/dashboard/test_review_shell.py`
Expected: 12 passed; ruff clean (format the file first if needed).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "test(dashboard): topology behavior specs — map, drill-in, inspector, cascade

Assisted-by: Claude Fable 5"
```

---

### Task 7: Gates + ratchet

**Files:** possibly `web/vite.config.ts` (thresholds); fixes surfaced by gates.

- [ ] **Step 1:** `make coverage-hostless` — expected green (Python changes: generator + appended browser-marked specs, which this lane excludes).
- [ ] **Step 2:** `uv run nox -s lint typecheck` — expected green.
- [ ] **Step 3:** `make web` — drift gate + builds + air-gap ×2 green. Record the dist-size delta from @xyflow/react.
- [ ] **Step 4:** `cd web && npm run check && npm run typecheck && npm run test:coverage` — recalibrate thresholds to ~2–3 points below measured if drifted (they will move: the topo components are DOM-heavy with only structural RTL coverage); update the ratchet comment's baseline numbers precisely (exact old→new/measured values, never "~N points" prose).
- [ ] **Step 5:** `make dashboard` — one confirming run, 32 passed.
- [ ] **Step 6:** `make import-snapshot` — zero diff expected.
- [ ] **Step 7:** Commit any recalibration:

```bash
git add web/vite.config.ts
git commit -m "test(web): re-ratchet vitest coverage floor after the topology phase

Assisted-by: Claude Fable 5"
```

---

## Self-review notes (done at authoring time)

- **Spec coverage:** spec §Goal/§Decisions → Tasks 3/4 (React Flow, deterministic layout, attribution/air-gap); data-layer section → Task 2 (reachability incl. reporting-host override, cycle guard, collapse/individual edge rules, parallel indices, sources synthesis) + Task 1 (cascade fixture); UI section → Tasks 3/4/5 (nodes with glyph/rollup/slot badge, provenance edges + impair pill, page/routes/toggle/breadcrumb, inspector + NetEm stub, sources toggle); testing section → per-task vitest/RTL + Task 6 Playwright + Task 7 gates; follow-ups stay in the spec (none implemented here — YAGNI).
- **Deliberate deviations from spec text, called out:** (1) mgmt host NODES are always visible (they are lab hosts); the Sources toggle gates only the reports-for EDGES — §10's "reveals management hosts" is interpreted as revealing their source role, since hiding a real lab host would misrepresent the lab; flagged for Chris in the spec-review gap if he disagrees. (2) `MiniMap` was dropped (Controls + fit + deterministic layout suffice at lab scale; YAGNI — the spec listed it as optional chrome).
- **Known plan-level risks for implementers:** @xyflow/react API typing drift (adaptation protocol in Tasks 3/4 — typing plumbing adaptable, testids/behavior never); the Task-4 FitButton/provider wart is called out IN the task (deliberate: the fix shape depends on the installed version's Panel API); Task 6 has two mandatory placeholder resolutions (link ids are generator-derived) — the task text forbids leaving them vacuous.
- **Type consistency:** `TopoNode`/`TopoEdge`/`EffectiveStatus` (T2) consumed by T3 (`layoutTopo`, node components), T4 (page mapping, `LinkEdgeData { edge, groupSize }`), T5 (`LinkInspector { edge: TopoEdge | null }`); `edgeStyle(provenance, selected)` exported from LinkEdge (T4) and tested; testid grammar consistent across T3/T4/T5/T6 (`topo-node-<id>`, `topo-link-<edgeId>`, `topo-impair-<edgeId>`, `data-status`, `data-status-segment`).
- **Placeholder scan:** the only intentionally-open values are Task 6's two marked assertions, each with an explicit mandatory-resolution instruction and reviewer bait; everything else carries complete code.
