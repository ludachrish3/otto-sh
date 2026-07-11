# Monitor Views (Plan 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the monitor's data views on the Plan-2 shell: derived health, the fleet grid, the synced ECharts per-subject chart stack with series tree + source filtering, log-event tables, and the events slide-over — all against the committed fixtures, review-mode only.

**Architecture:** Pure derivation modules first (`data/health.ts`, `data/seriesTree.ts` — vitest-heavy), then a thin ECharts layer (tree-shaken `echarts/core`, pure option builders, direct instance management per the UX spec §5 proposal — no `echarts-for-react`), then the three view surfaces replace/extend the Plan-2 scaffolds. Events are **display-only** (overlay + slide-over): marking/editing needs the backend API and stays with the live-hookup phase, matching UX spec §12's read-only review semantics.

**Tech Stack:** echarts (new dep, tree-shaken, canvas renderer), existing react-aria primitives + Tailwind v4 tokens, zustand review store, wouter.

**Specs:** UX `docs/superpowers/specs/2026-07-05-monitor-untitled-ui-redesign-design.md` (§5 charts, §8 grid, §9 per-subject, §11 events, §13 states); contract `docs/superpowers/specs/2026-07-10-monitor-export-format-and-dummy-data-phase-design.md` (§6 derived health). Follow-ups folded in from `todo/monitor-ui-scaffold-followups.md`: #1 (densify meta — Task 1), #2 (`clampRange`/`formatSpan` consumed — Tasks 1/4), #4 (status casing: **decided, keep "Historical"** title-case in the app bar; the review bar's HISTORICAL badge carries the spec literal — record in the plan, no code change), #5 (`/host/:id` for elements: **kept** — Chris is not renaming element/board/slot and URL vocabulary churn isn't worth it now; revisit if it ever confuses), #6 (housekeeping — Task 1).

## Global Constraints

- **NEVER run `make coverage`** — dev-host swap-thrash issue stands. Gates: scoped `npx vitest run <file>`, `npm run check`/`typecheck`, `make dashboard` (browser lane), `make coverage-hostless` + `make web` only in Task 9.
- Worktree: `/home/vagrant/otto-sh/.claude/worktrees/monitor-ui-scaffold`, branch `worktree-monitor-ui-scaffold` (continue on tip `435a561`).
- Commit style: conventional prefix + `Assisted-by: Claude Fable 5` trailer embedded in `-m`; explicit `git add` per file; never `git add -u`.
- `npm run check:fix && npm run check && npm run typecheck` before every commit (biome format is not lint-neutral).
- Vitest conventions (established, copy them): no `test.globals` → explicit `afterEach(cleanup)`; `CSS.escape` polyfill in any file touching react-aria portals; fixtures via `readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../fixtures/<name>.json"), "utf-8")`; reset `useReviewStore.setState({...})` in `afterEach`.
- Playwright: data-testid contract only; react-aria quirks — options via `get_by_role("option", ...)`, radio presets via `get_by_text` scoped to the group, hidden `<select>` mirror exists.
- **Dataviz rules (encoded in Task 3, binding for all chart code):** categorical slots assigned in FIXED order by entity, never cycled — a 9th series never gets a generated color (charts render ≤8 series + an overflow notice); color follows the entity, so filtering/unchecking never repaints survivors; one y-axis per chart, never dual-axis; 2px lines, no point symbols (hover emphasis only), hairline grid, axis/labels in text tokens never series colors; crosshair+tooltip on by default; status colors (ok/error) are reserved and never used as series colors.
- Air-gap unchanged: echarts is a bundled npm dep; no new external URLs (dist grep gate will verify in `make web`).
- Timestamps: all internal times epoch ms (`parseTs`); `datetime-local` values are LOCAL wall-clock.

## Palette (validated — do not tweak values without re-running the validator)

Categorical series palette, adopted from the dataviz reference instance and **validated 2026-07-11 with `validate_palette.js` against otto's real surfaces** (light `#ffffff`: all checks pass, contrast WARN on slots 2/3/7 → relief obligation satisfied by the always-visible labeled series tree + tooltips + log tables; dark `#030712`: all checks pass ≥3:1, CVD floor-band 10.3 → same relief). Slot ORDER is the CVD-safety mechanism — never reorder:

| Slot | Light | Dark | | Slot | Light | Dark |
|---|---|---|---|---|---|---|
| 1 | `#2a78d6` | `#3987e5` | | 5 | `#4a3aa7` | `#9085e9` |
| 2 | `#1baf7a` | `#199e70` | | 6 | `#e34948` | `#e66767` |
| 3 | `#eda100` | `#c98500` | | 7 | `#e87ba4` | `#d55181` |
| 4 | `#008300` | `#008300` | | 8 | `#eb6834` | `#d95926` |

Health status tokens (reserved, UI-consumed → `@theme`): `--color-status-ok: #2f9e6e` (otto's live green), `--color-status-error: #d03b3b`. Brand violet stays UI-accent-only — never a series color (it is also the default event color; keeping it out of the series palette keeps event markers distinguishable).

---

### Task 1: Densify meta + clamp custom ranges + housekeeping

**Files:**
- Modify: `web/src/data/exportDoc.ts` (meta type + normalization, drop `chart_map` cast)
- Modify: `web/src/shell/ReviewBar.tsx` (clamp custom apply)
- Modify: `web/vite.config.ts` (ratchet comment wording only)
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (delete dead `_run_isolated`)
- Test: `web/src/__tests__/exportdoc.test.ts`, `web/src/__tests__/reviewbar.test.tsx` (append)

**Interfaces:**
- Consumes: existing `NormalizedSession`, `clampRange`, `sessionBounds` from `exportDoc.ts`.
- Produces (Tasks 2/3/5/6 rely on): `NormalizedMeta { interval: number | null; charts: ChartSpecRecord[]; tabs: TabSpecRecord[] }`; `NormalizedSession.meta: NormalizedMeta` — **dense, never undefined members**.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/__tests__/exportdoc.test.ts`:

```ts
describe("meta densification", () => {
  const base = {
    format: 1,
    sessions: [
      {
        id: "s1",
        start: "2026-07-01T08:00:00Z",
        end: "2026-07-01T09:00:00Z",
        meta: { interval: 15.0 }, // present but PARTIAL: no charts/tabs keys
      },
    ],
  };

  it("densifies a present-but-partial meta (follow-up #1)", () => {
    const { sessions } = parseExportDocument(JSON.stringify(base));
    expect(sessions[0].meta.interval).toBe(15.0);
    expect(sessions[0].meta.charts).toEqual([]);
    expect(sessions[0].meta.tabs).toEqual([]);
  });

  it("densifies an absent meta", () => {
    const doc = structuredClone(base);
    delete (doc.sessions[0] as Record<string, unknown>).meta;
    const { sessions } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].meta).toEqual({ interval: null, charts: [], tabs: [] });
  });
});
```

Append to `web/src/__tests__/reviewbar.test.tsx` (inside the existing describe, reusing its render/import helpers):

```ts
it("clamps a custom range that exceeds the session bounds (follow-up #2)", async () => {
  await renderWithKitchenSink(); // the file's existing setup helper
  const session = useReviewStore.getState().sessions[0];
  // Type a window starting a day early and ending a day late, then Apply.
  fireEvent.change(screen.getByTestId("range-from").querySelector("input") as HTMLInputElement, {
    target: { value: msToLocalInput(session.startMs - 86_400_000) },
  });
  fireEvent.change(screen.getByTestId("range-to").querySelector("input") as HTMLInputElement, {
    target: { value: msToLocalInput(session.endMs + 86_400_000) },
  });
  fireEvent.click(screen.getByTestId("range-apply"));
  const range = useReviewStore.getState().range;
  expect(range).not.toBeNull();
  // datetime-local has minute precision — clamp must land exactly on bounds.
  expect(range?.from).toBeGreaterThanOrEqual(session.startMs);
  expect(range?.to).toBeLessThanOrEqual(session.endMs);
});
```

(Adapt the helper name to the file's actual one — it renders `<ReviewBar/>` after importing `kitchen-sink.json`; `msToLocalInput` import from `../data/time`, `fireEvent`/`screen` already imported there. If `range-from`'s testid sits on the `<input>` itself rather than a wrapper, drop the `querySelector`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/exportdoc.test.ts src/__tests__/reviewbar.test.tsx`
Expected: the two meta tests FAIL (`meta.charts` undefined on partial meta); the clamp test FAILS (range.from is a day before startMs).

- [ ] **Step 3: Implement**

In `web/src/data/exportDoc.ts` — extend the generated-type import and add the dense type:

```ts
import type {
  ChartSpecRecord,
  ElementRecord,
  HostSnapshot,
  LinkSnapshot,
  MetricRecord,
  MonitorHistoricalExportDocument,
  SessionRecord,
  TabSpecRecord,
} from "../api/export.gen";
```

```ts
/** Dense presentation meta: normalized ONCE at the boundary (follow-up #1) —
 * downstream code iterates charts/tabs unconditionally, never `?? []`. */
export interface NormalizedMeta {
  interval: number | null;
  charts: ChartSpecRecord[];
  tabs: TabSpecRecord[];
}
```

In `NormalizedSession`, replace `meta: NonNullable<SessionRecord["meta"]>;` with `meta: NormalizedMeta;`. In `normalizeSession`, replace the `meta:` and `chartMap:` lines with:

```ts
    meta: {
      interval: raw.meta?.interval ?? null,
      charts: raw.meta?.charts ?? [],
      tabs: raw.meta?.tabs ?? [],
    },
    // ...
    chartMap: raw.chart_map ?? {},
```

In `web/src/shell/ReviewBar.tsx`: add `clampRange` to the `../data/exportDoc` import and clamp on apply:

```ts
  const applyCustom = () => {
    const fromMs = localInputToMs(from);
    const toMs = localInputToMs(to);
    if (fromMs !== null && toMs !== null && fromMs < toMs) {
      setRange(clampRange({ from: fromMs, to: toMs }, bounds));
    }
  };
```

In `web/vite.config.ts`, fix the imprecise ratchet comment line (replace the sentence containing "~15-18 points across the board" with: `// raised after the shell rebuild: stmts +16.2, branches +24.5, funcs +13.4, lines +15.8.`).

In `tests/e2e/monitor/dashboard/conftest.py`, delete the dead `_run_isolated` helper (function + its docstring; zero callers — verified at the Plan-2 final review).

- [ ] **Step 4: Verify green + full web suite**

Run: `cd web && npx vitest run src/__tests__/exportdoc.test.ts src/__tests__/reviewbar.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck`
Expected: all green. Also: `uv run ruff format --check tests/e2e/monitor/dashboard/conftest.py && uv run ruff check tests/e2e/monitor/dashboard/conftest.py` clean, and `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -q` still 12 passed.

- [ ] **Step 5: Commit**

```bash
git add web/src/data/exportDoc.ts web/src/shell/ReviewBar.tsx web/vite.config.ts tests/e2e/monitor/dashboard/conftest.py web/src/__tests__/exportdoc.test.ts web/src/__tests__/reviewbar.test.tsx
git commit -m "refactor(web): densify session meta + clamp custom ranges

Follow-ups #1/#2/#6 from todo/monitor-ui-scaffold-followups.md: dense
NormalizedMeta at the parse boundary, clampRange consumed by ReviewBar,
dead _run_isolated removed, ratchet comment precision.

Assisted-by: Claude Fable 5"
```

---

### Task 2: Derived health module (pure)

**Files:**
- Create: `web/src/data/health.ts`
- Test: `web/src/__tests__/health.test.ts`

**Interfaces:**
- Consumes: `NormalizedSession`, `TimeRange`, `DerivedElement` from `./exportDoc`; `parseTs` from `./time`.
- Produces (Tasks 4/6 rely on): `HEALTH_K = 3`; `type HealthStatus = "ok" | "down" | "no-data" | "unknown"`; `interface SubjectHealth { status: HealthStatus; lastSeenMs: number | null; outageMs: number }`; `healthForHosts(session, range): Map<string, SubjectHealth>`; `elementRollup(element, healths): SubjectHealth[]` (slot-then-id member order); `headlineFor(session, hostId, range): { text: string; chartKey: string } | null`.

Semantics (contract spec §6, binding): status is **last-known within the selected range**; evaluation point = `min(range.to ?? session.endMs, session.endMs)`; cadence per host = fastest `ChartSpec.interval` among the charts that host reports (label → `chartMap` → chart label → spec), falling back to `meta.interval`; down when gap > `HEALTH_K × cadence`; hosts with metric series but zero in-range samples → `no-data`; hosts with NO metric series at all (log-only or silent) → `unknown` — no health claim; cadence unresolvable (no intervals anywhere) → `unknown`. Headline: CPU-preferred (`unit === "%"` and `/cpu/i.test(label)`), else first `meta.charts`-order chart with in-range samples; text `"34% cpu"` / `"7212 rpm fan"` (value, unit — `%` unspaced, others spaced — then the `ChartSpec.chart` key).

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/health.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { elementRollup, HEALTH_K, headlineFor, healthForHosts } from "../data/health";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

const MIN = 60_000;

describe("healthForHosts against kitchen-sink", () => {
  it("everything is ok at full range (outage host recovered)", () => {
    const healths = healthForHosts(kitchen, null);
    for (const h of kitchen.lab.hosts) {
      expect(healths.get(h.id)?.status, h.id).toBe("ok");
    }
  });

  it("workers_w2 is down when the range ends inside its outage window", () => {
    // Outage: 60m→80m from session start. End the range at +70m.
    const range = { from: kitchen.startMs, to: kitchen.startMs + 70 * MIN };
    const h = healthForHosts(kitchen, range).get("workers_w2");
    expect(h?.status).toBe("down");
    // Last sample just before 60m; outage ≈ 10m (one cadence tick of slack).
    expect(h?.outageMs).toBeGreaterThanOrEqual(9 * MIN);
    expect(h?.outageMs).toBeLessThanOrEqual(11 * MIN);
  });

  it("other workers stay ok in that same window", () => {
    const range = { from: kitchen.startMs, to: kitchen.startMs + 70 * MIN };
    expect(healthForHosts(kitchen, range).get("workers_w1")?.status).toBe("ok");
  });

  it("a range before a host's data yields no-data", () => {
    // 1-minute sliver before the first cadence tick lands only start samples;
    // use a window entirely before the session for the strict case.
    const range = { from: kitchen.startMs - 10 * MIN, to: kitchen.startMs - 5 * MIN };
    expect(healthForHosts(kitchen, range).get("db-01")?.status).toBe("no-data");
  });
});

describe("healthForHosts synthetic edge cases", () => {
  function synthetic(metrics: object[], logEvents: object[] = []) {
    return parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "a", element: "a", ip: "10.0.0.1" },
                { id: "b", element: "b", ip: "10.0.0.2" },
              ],
            },
            meta: {
              interval: 30.0,
              charts: [
                { label: "CPU %", y_title: "CPU %", unit: "%", command: "x", chart: "cpu" },
              ],
            },
            chart_map: { "CPU %": "CPU %" },
            metrics,
            log_events: logEvents,
          },
        ],
      }),
    ).sessions[0];
  }

  it("a log-only host is unknown (no health claim)", () => {
    const s = synthetic(
      [{ timestamp: "2026-07-01T08:59:30Z", host: "a", label: "CPU %", value: 1 }],
      [{ timestamp: "2026-07-01T08:30:00Z", host: "b", tab: "kernel", fields: { m: "x" } }],
    );
    expect(healthForHosts(s, null).get("b")?.status).toBe("unknown");
  });

  it("down threshold is K x cadence exactly", () => {
    // Last sample 08:30; session ends 09:00 → gap 30m; cadence 30s → down.
    const s = synthetic([
      { timestamp: "2026-07-01T08:30:00Z", host: "a", label: "CPU %", value: 1 },
    ]);
    const h = healthForHosts(s, null).get("a");
    expect(HEALTH_K).toBe(3);
    expect(h?.status).toBe("down");
    expect(h?.outageMs).toBe(30 * MIN);
  });
});

describe("headlineFor", () => {
  it("prefers CPU and formats percent unspaced", () => {
    const head = headlineFor(kitchen, "chassis-a_lc1", null);
    expect(head?.chartKey).toBe("cpu");
    expect(head?.text).toMatch(/^\d+% cpu$/);
  });

  it("falls back to the first chart with data when CPU is absent", () => {
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: { hosts: [{ id: "a", element: "a", ip: "10.0.0.1" }] },
            meta: {
              interval: 60.0,
              charts: [
                { label: "Fan RPM", y_title: "Fan RPM", unit: "rpm", command: "x", chart: "fan" },
              ],
            },
            chart_map: { "Fan RPM": "Fan RPM" },
            metrics: [{ timestamp: "2026-07-01T08:59:00Z", host: "a", label: "Fan RPM", value: 7212.4 }],
          },
        ],
      }),
    ).sessions[0];
    expect(headlineFor(s, "a", null)?.text).toBe("7212 rpm fan");
  });

  it("returns null for a host with no in-range samples", () => {
    const range = { from: kitchen.startMs - 10 * MIN, to: kitchen.startMs - 5 * MIN };
    expect(headlineFor(kitchen, "db-01", range)).toBeNull();
  });
});

describe("elementRollup", () => {
  it("orders members by slot then id and reflects their health", () => {
    const healths = healthForHosts(kitchen, null);
    const chassis = kitchen.elements.find((e) => e.id === "chassis-a");
    expect(chassis).toBeDefined();
    const rollup = elementRollup(chassis!, healths);
    expect(rollup).toHaveLength(3);
    expect(rollup.every((h) => h.status === "ok")).toBe(true);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/health.test.ts`
Expected: FAIL — cannot resolve `../data/health`.

- [ ] **Step 3: Implement**

Create `web/src/data/health.ts`:

```ts
// Derived health (contract spec 2026-07-10 §6): a pure function of
// (samples, selected range, cadences). Nothing is stored — "last known
// status" means last known WITHIN THE SELECTED RANGE, so narrowing the
// range re-evaluates it. The same functions will drive live mode's
// unreachable-dimming at the hookup phase.
import type { DerivedElement, NormalizedSession, TimeRange } from "./exportDoc";
import { parseTs } from "./time";

/** Down when the gap past the last sample exceeds K x the host's cadence. */
export const HEALTH_K = 3;

export type HealthStatus = "ok" | "down" | "no-data" | "unknown";

export interface SubjectHealth {
  status: HealthStatus;
  lastSeenMs: number | null;
  outageMs: number;
}

export interface Headline {
  text: string;
  chartKey: string;
}

/** Fastest collection cadence (ms) among the charts this host reports,
 * via label -> chartMap -> spec.interval, falling back to the global
 * interval. null = unresolvable (no intervals anywhere). */
function cadenceMs(session: NormalizedSession, labels: Set<string>): number | null {
  const chartLabels = new Set([...labels].map((l) => session.chartMap[l] ?? l));
  const intervals = session.meta.charts
    .filter((c) => chartLabels.has(c.label))
    .map((c) => c.interval ?? session.meta.interval)
    .filter((v): v is number => v != null);
  if (intervals.length) return Math.min(...intervals) * 1000;
  return session.meta.interval != null ? session.meta.interval * 1000 : null;
}

export function healthForHosts(
  session: NormalizedSession,
  range: TimeRange | null,
): Map<string, SubjectHealth> {
  const evalFrom = range?.from ?? session.startMs;
  const evalTo = Math.min(range?.to ?? session.endMs, session.endMs);

  // One pass: per-host last in-range sample + the labels each host reports
  // anywhere in the session (cadence must not depend on the range).
  const lastSeen = new Map<string, number>();
  const labelsByHost = new Map<string, Set<string>>();
  for (const m of session.metrics) {
    if (!session.hostIds.has(m.host ?? "")) continue; // element-targeted rows
    const host = m.host as string;
    let labels = labelsByHost.get(host);
    if (!labels) {
      labels = new Set();
      labelsByHost.set(host, labels);
    }
    labels.add(m.label);
    const ts = parseTs(m.timestamp);
    if (ts >= evalFrom && ts <= evalTo && ts > (lastSeen.get(host) ?? -Infinity)) {
      lastSeen.set(host, ts);
    }
  }

  const out = new Map<string, SubjectHealth>();
  for (const host of session.lab.hosts) {
    const labels = labelsByHost.get(host.id);
    if (!labels) {
      // No metric series at all in this session: log-only or silent.
      // No health claim either way (spec: absence of logs proves nothing).
      out.set(host.id, { status: "unknown", lastSeenMs: null, outageMs: 0 });
      continue;
    }
    const last = lastSeen.get(host.id) ?? null;
    if (last === null) {
      out.set(host.id, { status: "no-data", lastSeenMs: null, outageMs: 0 });
      continue;
    }
    const cadence = cadenceMs(session, labels);
    if (cadence === null) {
      out.set(host.id, { status: "unknown", lastSeenMs: last, outageMs: 0 });
      continue;
    }
    const gap = evalTo - last;
    const down = gap > HEALTH_K * cadence;
    out.set(host.id, {
      status: down ? "down" : "ok",
      lastSeenMs: last,
      outageMs: down ? gap : 0,
    });
  }
  return out;
}

/** Member healths in slot-then-id order — the segmented rollup bar's input
 * and the element's health indicator everywhere (UX spec §8). */
export function elementRollup(
  element: DerivedElement,
  healths: Map<string, SubjectHealth>,
  session?: NormalizedSession,
): SubjectHealth[] {
  const bySlot = (id: string): number => {
    const host = session?.lab.hosts.find((h) => h.id === id);
    return host?.slot ?? Number.POSITIVE_INFINITY;
  };
  return [...element.hostIds]
    .sort((a, b) => bySlot(a) - bySlot(b) || a.localeCompare(b))
    .map((id) => healths.get(id) ?? { status: "unknown", lastSeenMs: null, outageMs: 0 });
}

function formatValue(value: number): string {
  return value >= 10 ? String(Math.round(value)) : String(Math.round(value * 10) / 10);
}

/** Labeled headline metric for a host tile (UX spec §8): CPU-preferred,
 * else the first meta-order chart with in-range samples. The LABEL matters
 * because of the fallback — "34% cpu", "7212 rpm fan". */
export function headlineFor(
  session: NormalizedSession,
  hostId: string,
  range: TimeRange | null,
): Headline | null {
  const evalFrom = range?.from ?? session.startMs;
  const evalTo = Math.min(range?.to ?? session.endMs, session.endMs);
  const specs = [...session.meta.charts].sort((a, b) => {
    const cpuA = a.unit === "%" && /cpu/i.test(a.label) ? 0 : 1;
    const cpuB = b.unit === "%" && /cpu/i.test(b.label) ? 0 : 1;
    return cpuA - cpuB;
  });
  for (const spec of specs) {
    let best: { ts: number; value: number } | null = null;
    for (const m of session.metrics) {
      if (m.host !== hostId) continue;
      if ((session.chartMap[m.label] ?? m.label) !== spec.label) continue;
      const ts = parseTs(m.timestamp);
      if (ts < evalFrom || ts > evalTo) continue;
      if (!best || ts > best.ts) best = { ts, value: m.value };
    }
    if (best) {
      const unit = spec.unit === "%" ? "%" : ` ${spec.unit}`;
      return { text: `${formatValue(best.value)}${unit} ${spec.chart}`, chartKey: spec.chart };
    }
  }
  return null;
}
```

- [ ] **Step 4: Verify green**

Run: `cd web && npx vitest run src/__tests__/health.test.ts && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS. If the kitchen-sink outage-window assertion fails, READ the failure values first — the fixture's outage is 60–80 min at 15/30 s cadences; the tolerance in the test (9–11 min) already absorbs one tick of slack. Do not widen tolerances without understanding the arithmetic.

- [ ] **Step 5: Commit**

```bash
git add web/src/data/health.ts web/src/__tests__/health.test.ts
git commit -m "feat(web): derived health — range-scoped status, outage, headline

Pure (samples, range, cadences) -> status per contract spec §6; K=3;
log-only hosts make no health claim; headline is CPU-preferred with
labeled fallback.

Assisted-by: Claude Fable 5"
```

---

### Task 3: ECharts foundation — palette, theme bridge, option builders, ChartPanel

**Files:**
- Modify: `web/package.json` (+ `echarts`, exact pin via `npm install -E echarts`)
- Modify: `web/src/app.css` (`@theme` gains `--color-status-ok`/`--color-status-error`)
- Create: `web/src/charts/palette.ts`, `web/src/charts/echarts.ts`, `web/src/charts/options.ts`, `web/src/charts/useIsDark.ts`, `web/src/charts/ChartPanel.tsx`
- Test: `web/src/__tests__/chartoptions.test.ts`, `web/src/__tests__/chartpanel.test.tsx`

**Interfaces:**
- Consumes: `TimeRange` from `../data/exportDoc`; `NormalizedSession["events"]` rows; theme `dark` class on `<html>`.
- Produces (Task 6 relies on):
  - `SERIES_LIGHT: readonly string[]`, `SERIES_DARK: readonly string[]` (8 slots each), `MAX_SERIES_PER_CHART = 8` (palette.ts)
  - `chartTheme(dark: boolean): ChartTheme` where `ChartTheme = { ink; muted; grid; axis; surface; series: readonly string[] }` (options.ts)
  - `interface SeriesInput { key: string; name: string; slot: number; points: [number, number][] }`
  - `interface EventMarker { id: number; label: string; color: string; fromMs: number; toMs: number | null }`
  - `eventMarkers(events, window): EventMarker[]` (window-overlap filter; wire rows in, ms out)
  - `buildStackOption(args: { unit: string; yTitle: string; series: SeriesInput[]; window: TimeRange; events: EventMarker[]; theme: ChartTheme }): Record<string, unknown>`
  - `zoomToRange(startPct: number, endPct: number, window: TimeRange): TimeRange`
  - `ChartPanel` component: `{ option: Record<string, unknown>; groupId: string; window: TimeRange; onZoom?: (r: TimeRange) => void; testId?: string }`
  - `useIsDark(): boolean` (MutationObserver on `<html>` class — charts re-render on theme toggle)

- [ ] **Step 1: Install the dep**

Run: `cd web && npm install -E echarts`
Record the resolved version. Verify `git diff package.json` shows only the one exact-pinned dependency line (+ lockfile).

- [ ] **Step 2: Write the failing tests**

Create `web/src/__tests__/chartoptions.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { MAX_SERIES_PER_CHART, SERIES_DARK, SERIES_LIGHT } from "../charts/palette";
import { buildStackOption, chartTheme, eventMarkers, zoomToRange } from "../charts/options";

const WINDOW = { from: 1_000_000, to: 2_000_000 };
const theme = chartTheme(false);

function series(slot: number) {
  return { key: `s${slot}`, name: `series ${slot}`, slot, points: [[1_500_000, 42]] as [number, number][] };
}

describe("palette", () => {
  it("has exactly 8 fixed slots per mode", () => {
    expect(SERIES_LIGHT).toHaveLength(8);
    expect(SERIES_DARK).toHaveLength(8);
    expect(MAX_SERIES_PER_CHART).toBe(8);
  });
});

describe("buildStackOption", () => {
  it("binds color to the entity slot, not the render position", () => {
    // Series 0 filtered out: series 2 must KEEP slot-2's color (never repaint
    // survivors — dataviz rule).
    const opt = buildStackOption({
      unit: "%", yTitle: "CPU %", series: [series(2), series(5)],
      window: WINDOW, events: [], theme,
    }) as { series: { itemStyle: { color: string }; lineStyle: { width: number } }[] };
    expect(opt.series[0].itemStyle.color).toBe(theme.series[2]);
    expect(opt.series[1].itemStyle.color).toBe(theme.series[5]);
    expect(opt.series[0].lineStyle.width).toBe(2);
  });

  it("pins the x axis to the window regardless of data extent", () => {
    const opt = buildStackOption({
      unit: "", yTitle: "y", series: [series(0)], window: WINDOW, events: [], theme,
    }) as { xAxis: { min: number; max: number; type: string } };
    expect(opt.xAxis.type).toBe("time");
    expect(opt.xAxis.min).toBe(WINDOW.from);
    expect(opt.xAxis.max).toBe(WINDOW.to);
  });

  it("attaches event markers to the first series only", () => {
    const events = [
      { id: 1, label: "point", color: "#7c5cff", fromMs: 1_200_000, toMs: null },
      { id: 2, label: "span", color: "#ff6b6b", fromMs: 1_300_000, toMs: 1_400_000 },
    ];
    const opt = buildStackOption({
      unit: "", yTitle: "y", series: [series(0), series(1)], window: WINDOW, events, theme,
    }) as { series: Record<string, unknown>[] };
    expect(opt.series[0].markLine).toBeDefined();
    expect(opt.series[0].markArea).toBeDefined();
    expect(opt.series[1].markLine).toBeUndefined();
    const line = opt.series[0].markLine as { data: { xAxis: number }[] };
    expect(line.data[0].xAxis).toBe(1_200_000);
    const area = opt.series[0].markArea as { data: [{ xAxis: number }, { xAxis: number }][] };
    expect(area.data[0][0].xAxis).toBe(1_300_000);
  });

  it("uses text tokens for axis labels, never series colors", () => {
    const opt = buildStackOption({
      unit: "", yTitle: "y", series: [series(0)], window: WINDOW, events: [], theme,
    }) as { xAxis: { axisLabel: { color: string } } };
    expect(opt.xAxis.axisLabel.color).toBe(theme.muted);
    expect(SERIES_LIGHT).not.toContain(opt.xAxis.axisLabel.color);
  });
});

describe("eventMarkers", () => {
  it("filters to window overlap and converts to ms", () => {
    const rows = [
      { id: 1, timestamp: "1970-01-01T00:20:00Z", label: "in", color: "#111111" },
      { id: 2, timestamp: "1970-01-01T02:00:00Z", label: "out", color: "#222222" },
      {
        id: 3,
        timestamp: "1970-01-01T00:10:00Z",
        end_timestamp: "1970-01-01T00:25:00Z",
        label: "span-straddles",
        color: "#333333",
      },
    ];
    const marks = eventMarkers(rows, WINDOW); // 1_000_000..2_000_000 ms
    expect(marks.map((m) => m.id)).toEqual([1, 3]);
    expect(marks[1].toMs).toBe(1_500_000);
  });
});

describe("zoomToRange", () => {
  it("maps percentages onto the window", () => {
    expect(zoomToRange(25, 75, WINDOW)).toEqual({ from: 1_250_000, to: 1_750_000 });
  });
});
```

Create `web/src/__tests__/chartpanel.test.tsx`:

```tsx
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const instances: FakeChart[] = [];

class FakeChart {
  group = "";
  disposed = false;
  options: unknown[] = [];
  handlers = new Map<string, (e: unknown) => void>();
  setOption(opt: unknown) {
    this.options.push(opt);
  }
  on(event: string, cb: (e: unknown) => void) {
    this.handlers.set(event, cb);
  }
  resize() {}
  dispose() {
    this.disposed = true;
  }
}

vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => {
      const c = new FakeChart();
      instances.push(c);
      return c;
    },
    connect: vi.fn(),
  },
}));

import { ChartPanel } from "../charts/ChartPanel";

const WINDOW = { from: 1_000_000, to: 2_000_000 };

describe("ChartPanel lifecycle", () => {
  beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("inits with the group, applies options, disposes on unmount", () => {
    const { rerender, unmount } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    expect(instances).toHaveLength(1);
    expect(instances[0].group).toBe("g");
    expect(instances[0].options).toEqual([{ a: 1 }]);
    rerender(<ChartPanel option={{ a: 2 }} groupId="g" window={WINDOW} testId="chart-panel-x" />);
    expect(instances[0].options).toEqual([{ a: 1 }, { a: 2 }]);
    unmount();
    expect(instances[0].disposed).toBe(true);
  });

  it("debounces datazoom into an onZoom range", () => {
    const onZoom = vi.fn();
    render(<ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} />);
    instances[0].handlers.get("datazoom")?.({ start: 25, end: 75 });
    expect(onZoom).not.toHaveBeenCalled(); // debounced
    vi.advanceTimersByTime(250);
    expect(onZoom).toHaveBeenCalledWith({ from: 1_250_000, to: 1_750_000 });
  });

  it("suppresses sub-second no-op zooms", () => {
    const onZoom = vi.fn();
    render(<ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} />);
    instances[0].handlers.get("datazoom")?.({ start: 0, end: 100 });
    vi.advanceTimersByTime(250);
    expect(onZoom).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 3: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/chartoptions.test.ts src/__tests__/chartpanel.test.tsx`
Expected: FAIL — modules don't exist.

- [ ] **Step 4: Implement**

`web/src/app.css` — append inside the existing `@theme` block:

```css
  /* Health (status family — reserved, never used as series colors). */
  --color-status-ok: #2f9e6e;
  --color-status-error: #d03b3b;
```

Create `web/src/charts/palette.ts`:

```ts
// Categorical series palette — dataviz-skill reference instance, VALIDATED
// 2026-07-11 with validate_palette.js against otto's real surfaces:
//   light #ffffff: PASS (contrast WARN slots 2/3/7 -> relief = the labeled
//   series tree + tooltips, always present in the subject view)
//   dark  #030712: PASS, all >= 3:1 (CVD floor-band -> same relief)
// Slot ORDER is the CVD-safety mechanism (maximizes worst adjacent-pair
// ΔE) — NEVER reorder, NEVER cycle. A 9th series is never a generated
// color: charts render at most MAX_SERIES_PER_CHART series plus an
// overflow notice. Brand violet is deliberately absent (UI accent + the
// default event color; series must not impersonate either).

export const SERIES_LIGHT = [
  "#2a78d6", // 1 blue
  "#1baf7a", // 2 aqua
  "#eda100", // 3 yellow
  "#008300", // 4 green
  "#4a3aa7", // 5 violet-deep
  "#e34948", // 6 red
  "#e87ba4", // 7 magenta
  "#eb6834", // 8 orange
] as const;

export const SERIES_DARK = [
  "#3987e5",
  "#199e70",
  "#c98500",
  "#008300",
  "#9085e9",
  "#e66767",
  "#d55181",
  "#d95926",
] as const;

export const MAX_SERIES_PER_CHART = 8;
```

Create `web/src/charts/echarts.ts`:

```ts
// Tree-shaken echarts core (UX spec §5): canvas renderer, line charts,
// and exactly the components the review stack uses. Direct instance
// management (the spec's confirmed choice) — no echarts-for-react.
import { LineChart } from "echarts/charts";
import {
  DataZoomInsideComponent,
  GridComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import * as echartsCore from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

echartsCore.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomInsideComponent,
  MarkLineComponent,
  MarkAreaComponent,
  CanvasRenderer,
]);

export const echarts = echartsCore;
```

Create `web/src/charts/options.ts`:

```ts
// Pure ECharts option builders — the plotly.ts idiom carried over: plain
// objects out, no echarts import, fully unit-testable. Dataviz mark specs
// are encoded here: 2px lines, no point symbols (hover emphasis only),
// hairline recessive grid, axis text in muted ink (never series colors),
// one y-axis per chart, crosshair tooltip on by default.
import type { NormalizedSession, TimeRange } from "../data/exportDoc";
import { parseTs } from "../data/time";
import { SERIES_DARK, SERIES_LIGHT } from "./palette";

export interface ChartTheme {
  ink: string;
  muted: string;
  grid: string;
  axis: string;
  surface: string;
  series: readonly string[];
}

/** Tailwind gray scale values, inlined: charts render to canvas and
 * cannot consume CSS classes. Keep in sync with app.css's body colors. */
export function chartTheme(dark: boolean): ChartTheme {
  return dark
    ? { ink: "#f3f4f6", muted: "#9ca3af", grid: "#1f2937", axis: "#374151", surface: "#030712", series: SERIES_DARK }
    : { ink: "#111827", muted: "#6b7280", grid: "#e5e7eb", axis: "#d1d5db", surface: "#ffffff", series: SERIES_LIGHT };
}

export interface SeriesInput {
  key: string;
  name: string;
  /** Entity-bound palette slot from the UNFILTERED tree — color follows
   * the entity; filtering must never repaint survivors. */
  slot: number;
  points: [number, number][];
}

export interface EventMarker {
  id: number;
  label: string;
  color: string;
  fromMs: number;
  toMs: number | null;
}

/** Window-overlap filter over the session's wire event rows. */
export function eventMarkers(
  events: NormalizedSession["events"],
  window: TimeRange,
): EventMarker[] {
  const out: EventMarker[] = [];
  for (const ev of events) {
    const fromMs = parseTs(ev.timestamp);
    const toMs = ev.end_timestamp != null ? parseTs(ev.end_timestamp) : null;
    const overlaps = toMs === null
      ? fromMs >= window.from && fromMs <= window.to
      : fromMs <= window.to && toMs >= window.from;
    if (!overlaps) continue;
    out.push({
      id: ev.id ?? out.length,
      label: ev.label ?? "",
      color: ev.color ?? "#7c5cff",
      fromMs,
      toMs,
    });
  }
  return out;
}

export function zoomToRange(startPct: number, endPct: number, window: TimeRange): TimeRange {
  const span = window.to - window.from;
  return {
    from: Math.round(window.from + (startPct / 100) * span),
    to: Math.round(window.from + (endPct / 100) * span),
  };
}

export function buildStackOption(args: {
  unit: string;
  yTitle: string;
  series: SeriesInput[];
  window: TimeRange;
  events: EventMarker[];
  theme: ChartTheme;
}): Record<string, unknown> {
  const { unit, yTitle, series, window, events, theme } = args;
  const markLine = {
    symbol: "none",
    animation: false,
    label: { formatter: "{b}", color: theme.muted, fontSize: 10 },
    data: events
      .filter((e) => e.toMs === null)
      .map((e) => ({ xAxis: e.fromMs, name: e.label, lineStyle: { color: e.color, type: "dashed", width: 1 } })),
  };
  const markArea = {
    silent: true,
    animation: false,
    data: events
      .filter((e) => e.toMs !== null)
      .map((e) => [
        { xAxis: e.fromMs, name: e.label, itemStyle: { color: e.color, opacity: 0.12 } },
        { xAxis: e.toMs as number },
      ]),
  };
  return {
    animation: false,
    grid: { left: 56, right: 16, top: 28, bottom: 28 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: theme.axis } },
      backgroundColor: theme.surface,
      borderColor: theme.grid,
      textStyle: { color: theme.ink, fontSize: 12 },
      valueFormatter: (v: unknown) => `${typeof v === "number" ? Math.round(v * 100) / 100 : v}${unit ? ` ${unit}` : ""}`,
    },
    xAxis: {
      type: "time",
      min: window.from,
      max: window.to,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: { color: theme.muted, fontSize: 10, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      name: yTitle,
      nameTextStyle: { color: theme.muted, fontSize: 10, align: "left" },
      axisLabel: { color: theme.muted, fontSize: 10 },
      splitLine: { lineStyle: { color: theme.grid, width: 1 } },
    },
    dataZoom: [{ type: "inside", filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: false }],
    series: series.map((s, i) => ({
      id: s.key,
      name: s.name,
      type: "line",
      showSymbol: false,
      sampling: "lttb",
      emphasis: { focus: "series", itemStyle: { borderWidth: 2 } },
      lineStyle: { width: 2 },
      itemStyle: { color: theme.series[s.slot % theme.series.length] },
      data: s.points,
      ...(i === 0 && (markLine.data.length || markArea.data.length)
        ? { markLine, markArea }
        : {}),
    })),
  };
}
```

(The `slot % length` is a type-level guard only — Task 6 never passes slot ≥ 8; the overflow rule truncates first.)

Create `web/src/charts/useIsDark.ts`:

```ts
// Charts render to canvas and cannot follow CSS `dark:` variants — this
// hook observes the <html> class that theme.ts toggles so chart options
// rebuild on theme changes.
import { useEffect, useState } from "react";

export function useIsDark(): boolean {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  useEffect(() => {
    const observer = new MutationObserver(() => {
      setDark(document.documentElement.classList.contains("dark"));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return dark;
}
```

Create `web/src/charts/ChartPanel.tsx`:

```tsx
// Direct ECharts instance management (UX spec §5): init/setOption/resize/
// dispose against a ref'd div. Instances join `groupId` so echarts.connect
// syncs the axisPointer crosshair across the whole stack. Zoom gestures
// (inside dataZoom) are debounced and surfaced as an absolute TimeRange —
// the review store's range is the single source of truth, so the zoomed
// window round-trips through the store and every chart (and the review
// bar's inputs) follows.
import { useEffect, useRef } from "react";

import type { TimeRange } from "../data/exportDoc";
import { echarts } from "./echarts";
import { zoomToRange } from "./options";

const HEIGHT_PX = 280;
const ZOOM_DEBOUNCE_MS = 200;
const MIN_ZOOM_DELTA_MS = 1000;

interface EChartsLike {
  group: string;
  setOption: (option: Record<string, unknown>, opts?: Record<string, unknown>) => void;
  on: (event: string, handler: (e: unknown) => void) => void;
  resize: () => void;
  dispose: () => void;
}

export function ChartPanel(props: {
  option: Record<string, unknown>;
  groupId: string;
  window: TimeRange;
  onZoom?: (range: TimeRange) => void;
  testId?: string;
}) {
  const { option, groupId, window: win, onZoom, testId } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsLike | null>(null);
  const latest = useRef({ win, onZoom });
  latest.current = { win, onZoom };

  useEffect(() => {
    if (!el.current) return;
    const instance = echarts.init(el.current) as unknown as EChartsLike;
    instance.group = groupId;
    echarts.connect(groupId);
    let timer: ReturnType<typeof setTimeout> | undefined;
    instance.on("datazoom", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const evt = e as { start?: number; end?: number; batch?: { start: number; end: number }[] };
        const start = evt.batch?.[0]?.start ?? evt.start;
        const end = evt.batch?.[0]?.end ?? evt.end;
        if (start === undefined || end === undefined) return;
        const range = zoomToRange(start, end, latest.current.win);
        const noop =
          Math.abs(range.from - latest.current.win.from) < MIN_ZOOM_DELTA_MS &&
          Math.abs(range.to - latest.current.win.to) < MIN_ZOOM_DELTA_MS;
        if (!noop) latest.current.onZoom?.(range);
      }, ZOOM_DEBOUNCE_MS);
    });
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(el.current);
    chart.current = instance;
    return () => {
      clearTimeout(timer);
      ro.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [groupId]);

  useEffect(() => {
    chart.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return <div ref={el} data-testid={testId} style={{ height: HEIGHT_PX }} className="w-full" />;
}
```

(jsdom has no `ResizeObserver` — the chartpanel test's `vi.mock` replaces the echarts module, but `ResizeObserver` is constructed regardless: add `globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;` at the top of `chartpanel.test.tsx` if the environment lacks it.)

- [ ] **Step 5: Verify green + build**

Run: `cd web && npx vitest run src/__tests__/chartoptions.test.ts src/__tests__/chartpanel.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build`
Expected: all green; the vite build succeeds with the echarts chunk bundled (note the dist size delta in your report — tree-shaken core should stay well under the full 1MB echarts).

- [ ] **Step 6: Commit**

```bash
git add web/package.json web/package-lock.json web/src/app.css web/src/charts/palette.ts web/src/charts/echarts.ts web/src/charts/options.ts web/src/charts/useIsDark.ts web/src/charts/ChartPanel.tsx web/src/__tests__/chartoptions.test.ts web/src/__tests__/chartpanel.test.tsx
git commit -m "feat(web): echarts foundation — validated palette, pure builders, ChartPanel

Tree-shaken echarts/core + canvas; dataviz-validated 8-slot categorical
palette (fixed order, entity-bound slots); crosshair sync via group
connect; inside-zoom debounced into absolute ranges.

Assisted-by: Claude Fable 5"
```

---

### Task 4: Fleet grid

**Files:**
- Modify: `web/src/pages/OverviewPage.tsx` (full body replacement; keep `overview-page`, `element-section-<id>`, `subject-link-<id>` testids — Playwright depends on them)
- Test: `web/src/__tests__/overview.test.tsx` (new; `pages.test.tsx`'s existing overview assertions keep passing — they only assert sections/links)

**Interfaces:**
- Consumes: `healthForHosts`, `elementRollup`, `headlineFor`, `SubjectHealth` (Task 2); `formatSpan` from `../data/time` (follow-up #2 — consumed here); `useActiveSession`, `useReviewStore` range.
- Produces: testids `host-tile-<id>`, `health-rollup-<elementId>`, `headline-<hostId>` (Task 8's Playwright contract).

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/overview.test.tsx`:

```tsx
import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { OverviewPage } from "../pages/OverviewPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

function load(range: { from: number; to: number } | null = null) {
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  if (range) useReviewStore.getState().actions.setRange(range);
  return render(<OverviewPage />);
}

afterEach(() => {
  cleanup();
  useReviewStore.setState({
    sessions: [], rawDocument: null, sourceName: null, warnings: [],
    importError: null, activeSessionId: null, range: null,
  });
});

describe("fleet grid", () => {
  it("renders a tile per host with a labeled headline", () => {
    load();
    const tile = screen.getByTestId("host-tile-chassis-a_lc1");
    expect(tile).toBeTruthy();
    expect(screen.getByTestId("headline-chassis-a_lc1").textContent).toMatch(/% cpu$/);
  });

  it("shows down · duration when the range ends inside the outage", () => {
    const start = useReviewStoreStart();
    load({ from: start, to: start + 70 * MIN });
    expect(screen.getByTestId("host-tile-workers_w2").textContent).toMatch(/down · 10m/);
  });

  it("healthy tiles show no down text at full range", () => {
    load();
    expect(screen.getByTestId("host-tile-workers_w2").textContent).not.toMatch(/down ·/);
  });

  it("renders the element rollup with one segment per member", () => {
    load();
    const rollup = screen.getByTestId("health-rollup-chassis-a");
    expect(rollup.children).toHaveLength(3);
  });

  it("keeps the empty-chassis section with no tiles", () => {
    load();
    const section = screen.getByTestId("element-section-spare-chassis");
    expect(section.textContent).toMatch(/empty/);
    expect(section.querySelector("[data-testid^=host-tile-]")).toBeNull();
  });
});

function useReviewStoreStart(): number {
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  return useReviewStore.getState().sessions[0].startMs;
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/overview.test.tsx`
Expected: FAIL — no `host-tile-*` testids in the scaffold body.

- [ ] **Step 3: Implement**

Replace `web/src/pages/OverviewPage.tsx` entirely:

```tsx
// Fleet grid (UX spec §8): element sections with health-rollup bars and
// host status tiles — status dot · name · board·slot · labeled headline
// metric; down tiles show the outage duration instead. All health is
// derived, range-scoped (data/health.ts) — nothing here is stored state.
import { Link } from "wouter";

import { elementRollup, headlineFor, healthForHosts, type SubjectHealth } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatSpan } from "../data/time";

const DOT_CLASS: Record<SubjectHealth["status"], string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

const SEGMENT_CLASS: Record<SubjectHealth["status"], string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

export function OverviewPage() {
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  if (!session) return null;

  const healths = healthForHosts(session, range);
  const hostById = new Map(session.lab.hosts.map((h) => [h.id, h]));

  return (
    <main data-testid="overview-page" className="flex flex-col gap-6 p-4">
      {session.elements.map((el) => {
        const rollup = elementRollup(el, healths, session);
        const memberIds = [...el.hostIds].sort((a, b) => {
          const slotA = hostById.get(a)?.slot ?? Number.POSITIVE_INFINITY;
          const slotB = hostById.get(b)?.slot ?? Number.POSITIVE_INFINITY;
          return slotA - slotB || a.localeCompare(b);
        });
        return (
          <section key={el.id} data-testid={`element-section-${el.id}`}>
            <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold">
              <span aria-hidden>{el.type === "physical" ? "▦" : "▤"}</span>
              {el.id}
              <span className="font-normal text-gray-400">
                {el.hostIds.length} host{el.hostIds.length === 1 ? "" : "s"}
                {el.description ? ` · ${el.description}` : ""}
              </span>
            </h2>
            {rollup.length > 0 && (
              <div
                data-testid={`health-rollup-${el.id}`}
                className="mb-2 flex h-1.5 w-full max-w-md gap-px overflow-hidden rounded"
                title={rollupTitle(rollup)}
              >
                {rollup.map((h, i) => (
                  <span
                    // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional by design
                    key={i}
                    className={`min-w-1 flex-1 ${SEGMENT_CLASS[h.status]}`}
                  />
                ))}
              </div>
            )}
            <ul className="flex flex-wrap gap-2">
              {memberIds.map((hostId) => {
                const host = hostById.get(hostId);
                const health = healths.get(hostId) ?? {
                  status: "unknown" as const,
                  lastSeenMs: null,
                  outageMs: 0,
                };
                const headline = health.status === "ok" ? headlineFor(session, hostId, range) : null;
                return (
                  <li key={hostId}>
                    <Link
                      href={`/host/${hostId}`}
                      data-testid={`subject-link-${hostId}`}
                      className="block rounded-lg border border-gray-200 px-3 py-2 text-sm
                        hover:border-brand-500 dark:border-gray-800 dark:hover:border-brand-500"
                    >
                      <article data-testid={`host-tile-${hostId}`} className="flex min-w-36 flex-col gap-1">
                        <span className="flex items-center gap-2 font-medium">
                          <span
                            aria-hidden
                            title={health.status}
                            className={`h-2 w-2 rounded-full ${DOT_CLASS[health.status]}`}
                          />
                          {hostId}
                        </span>
                        <span className="text-xs text-gray-400">
                          {host?.board ?? "—"}
                          {host?.slot != null ? ` · slot ${host.slot}` : ""}
                        </span>
                        {health.status === "down" ? (
                          <span className="text-xs font-medium text-status-error">
                            down · {formatSpan(0, health.outageMs)}
                          </span>
                        ) : health.status === "ok" && headline ? (
                          <span
                            data-testid={`headline-${hostId}`}
                            className="text-xs text-gray-600 dark:text-gray-300"
                          >
                            {headline.text}
                          </span>
                        ) : (
                          <span className="text-xs text-gray-400">
                            {health.status === "no-data" ? "no data" : "—"}
                          </span>
                        )}
                      </article>
                    </Link>
                  </li>
                );
              })}
              {el.hostIds.length === 0 && (
                <li className="text-sm text-gray-400">empty — no hosts fitted</li>
              )}
            </ul>
          </section>
        );
      })}
    </main>
  );
}

function rollupTitle(rollup: SubjectHealth[]): string {
  const counts = new Map<string, number>();
  for (const h of rollup) counts.set(h.status, (counts.get(h.status) ?? 0) + 1);
  return [...counts.entries()].map(([status, n]) => `${n} ${status}`).join(" · ");
}
```

- [ ] **Step 4: Verify green (incl. existing pages tests)**

Run: `cd web && npx vitest run src/__tests__/overview.test.tsx src/__tests__/pages.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS — `pages.test.tsx`'s overview assertions (sections + subject links) must survive unchanged; if one fails, fix the page, not the test.

- [ ] **Step 5: Commit**

```bash
git add web/src/pages/OverviewPage.tsx web/src/__tests__/overview.test.tsx
git commit -m "feat(web): fleet grid — health tiles + element rollup bars

Range-scoped derived health drives status dots, down-duration tiles
(formatSpan consumed), labeled headline metrics, and per-element
segmented rollups; empty chassis renders as an empty section.

Assisted-by: Claude Fable 5"
```

---

### Task 5: Series tree model + filter panel

**Files:**
- Create: `web/src/data/seriesTree.ts`, `web/src/pages/SeriesPanel.tsx`
- Test: `web/src/__tests__/seriestree.test.ts`, `web/src/__tests__/seriespanel.test.tsx`

**Interfaces:**
- Consumes: `NormalizedSession`, `TimeRange`; `MAX_SERIES_PER_CHART` (Task 3).
- Produces (Task 6 relies on):
  - `interface SeriesNode { key: string; label: string; host: string; source: string | null; slot: number }`
  - `interface ChartNode { chartKey: string; chartLabel: string; unit: string; yTitle: string; series: SeriesNode[] }`
  - `buildSeriesTree(session, subjectId): ChartNode[]` — the UNFILTERED tree; slots assigned here, once, entity-bound (dataviz rule: filtering never repaints survivors)
  - `filterTree(tree, opts: { search: string; chips: Set<string> | null; source: string | null }): ChartNode[]`
  - `sourcesIn(tree): string[]`
  - `collectSeriesPoints(session, tree, checked: Set<string>, range): Map<string, [number, number][]>`
  - `SeriesPanel` component with testids `series-panel`, `series-search`, `chip-<chartKey>`, `chip-source-<source>`, `series-node-<key>` (checkbox `<input>`), source badges.

Tree semantics: for a **host** subject, one node per distinct label of that host's metrics (key = label, host = subjectId); for an **element** subject, element-targeted rows (`m.host === elementId`) become nodes named by label, and member-host rows become one node per (host, label) named by host id with key `${host}/${label}`. Nodes group by `chartMap[label] ?? label` → matching `meta.charts` spec (`chartKey = spec.chart`; missing spec synthesizes `{ chartKey: label, unit: "", yTitle: label }`). Within a chart, series sort element-target-first then host id then label; `slot = index` in that sorted FULL list.

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/seriestree.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree, collectSeriesPoints, filterTree, sourcesIn } from "../data/seriesTree";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

describe("buildSeriesTree — host subject", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");

  it("groups by chart with spec metadata", () => {
    const cpu = tree.find((c) => c.chartKey === "cpu");
    expect(cpu).toBeDefined();
    expect(cpu?.unit).toBe("%");
    expect(cpu?.series).toHaveLength(1);
    expect(cpu?.series[0].key).toBe("CPU %");
  });

  it("marks mgmt-sourced series with their source", () => {
    const psu = tree.find((c) => c.chartKey === "psu-temp");
    expect(psu?.series[0].source).toBe("mgmt-01");
  });

  it("assigns stable slots from the full tree", () => {
    for (const chart of tree) {
      chart.series.forEach((s, i) => expect(s.slot).toBe(i));
    }
  });
});

describe("buildSeriesTree — element subject", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a");

  it("includes the element-targeted series", () => {
    const ambient = tree.find((c) => c.chartKey === "ambient");
    expect(ambient?.series.some((s) => s.host === "chassis-a")).toBe(true);
  });

  it("includes member-host series named by host", () => {
    const cpu = tree.find((c) => c.chartKey === "cpu");
    expect(cpu?.series.map((s) => s.host)).toEqual([
      "chassis-a_lc1",
      "chassis-a_lc2",
      "chassis-a_sup",
    ]);
    expect(cpu?.series[0].key).toBe("chassis-a_lc1/CPU %");
  });
});

describe("filterTree + sourcesIn", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");

  it("search prunes by series and chart label, case-insensitive", () => {
    const hit = filterTree(tree, { search: "psu", chips: null, source: null });
    expect(hit.map((c) => c.chartKey)).toEqual(["psu-temp"]);
    expect(filterTree(tree, { search: "zzz", chips: null, source: null })).toEqual([]);
  });

  it("chips restrict to whole chart groups", () => {
    const hit = filterTree(tree, { search: "", chips: new Set(["cpu"]), source: null });
    expect(hit.map((c) => c.chartKey)).toEqual(["cpu"]);
  });

  it("source filter keeps only externally-sourced series", () => {
    const hit = filterTree(tree, { search: "", chips: null, source: "mgmt-01" });
    expect(hit.every((c) => c.series.every((s) => s.source === "mgmt-01"))).toBe(true);
    expect(hit.length).toBeGreaterThan(0);
  });

  it("filtering preserves original slots (no repaint)", () => {
    const psu = filterTree(tree, { search: "psu", chips: null, source: null })[0];
    const original = tree.find((c) => c.chartKey === "psu-temp");
    expect(psu.series[0].slot).toBe(original?.series[0].slot);
  });

  it("sourcesIn lists distinct external sources", () => {
    expect(sourcesIn(tree)).toEqual(["mgmt-01"]);
  });
});

describe("collectSeriesPoints", () => {
  it("returns in-range [ms, value] pairs for checked keys only", () => {
    const tree = buildSeriesTree(kitchen, "chassis-a_lc1");
    const range = { from: kitchen.startMs, to: kitchen.startMs + 10 * 60_000 };
    const points = collectSeriesPoints(kitchen, tree, new Set(["CPU %"]), range);
    expect([...points.keys()]).toEqual(["CPU %"]);
    const cpu = points.get("CPU %") ?? [];
    expect(cpu.length).toBeGreaterThan(10);
    expect(cpu.every(([ts]) => ts >= range.from && ts <= range.to)).toBe(true);
    expect(cpu).toEqual([...cpu].sort((a, b) => a[0] - b[0]));
  });
});
```

Create `web/src/__tests__/seriespanel.test.tsx`:

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree } from "../data/seriesTree";
import { SeriesPanel } from "../pages/SeriesPanel";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

afterEach(cleanup);

function renderPanel(overrides: Partial<Parameters<typeof SeriesPanel>[0]> = {}) {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");
  const props = {
    tree,
    checked: new Set(tree.flatMap((c) => c.series.map((s) => s.key))),
    onToggle: vi.fn(),
    search: "",
    onSearch: vi.fn(),
    chips: null as Set<string> | null,
    onChips: vi.fn(),
    source: null as string | null,
    onSource: vi.fn(),
    ...overrides,
  };
  render(<SeriesPanel {...props} />);
  return props;
}

describe("SeriesPanel", () => {
  it("renders a chip per chart and a source chip", () => {
    renderPanel();
    expect(screen.getByTestId("chip-cpu")).toBeTruthy();
    expect(screen.getByTestId("chip-source-mgmt-01")).toBeTruthy();
  });

  it("checkbox toggle reports the series key", () => {
    const props = renderPanel();
    fireEvent.click(screen.getByTestId("series-node-CPU %"));
    expect(props.onToggle).toHaveBeenCalledWith("CPU %");
  });

  it("search box reports input", () => {
    const props = renderPanel();
    fireEvent.change(
      screen.getByTestId("series-search").querySelector("input") as HTMLInputElement,
      { target: { value: "psu" } },
    );
    expect(props.onSearch).toHaveBeenCalledWith("psu");
  });

  it("shows a source badge on externally-sourced series", () => {
    renderPanel();
    const node = screen.getByTestId("series-node-PSU Temp °C").closest("li");
    expect(node?.textContent).toContain("mgmt-01");
  });

  it("hides the source chip row when no external sources exist", () => {
    const tree = buildSeriesTree(kitchen, "db-01");
    renderPanel({ tree, checked: new Set() });
    expect(screen.queryByTestId("chip-source-mgmt-01")).toBeNull();
  });
});
```

(If `series-search`'s testid lands on the `<input>` itself via `TextInput`'s prop forwarding, drop the `querySelector` — match `TextInput`'s actual DOM, this is the same adaptation `reviewbar.test.tsx` made.)

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/seriestree.test.ts src/__tests__/seriespanel.test.tsx`
Expected: FAIL — modules don't exist.

- [ ] **Step 3: Implement**

Create `web/src/data/seriesTree.ts`:

```ts
// The per-subject series model (UX spec §9): metric-first tree grouped by
// chart, with the subject x source axes as node metadata. Slots are
// assigned HERE, once, from the unfiltered tree — the palette follows the
// entity, so search/chips/checkbox filtering never recolors survivors.
import type { NormalizedSession, TimeRange } from "./exportDoc";
import { parseTs } from "./time";

export interface SeriesNode {
  key: string;
  label: string;
  host: string;
  source: string | null;
  slot: number;
}

export interface ChartNode {
  chartKey: string;
  chartLabel: string;
  unit: string;
  yTitle: string;
  series: SeriesNode[];
}

interface RawNode {
  key: string;
  label: string;
  host: string;
  source: string | null;
  elementTarget: boolean;
}

export function buildSeriesTree(session: NormalizedSession, subjectId: string): ChartNode[] {
  const isElement = session.elementIds.has(subjectId) && !session.hostIds.has(subjectId);
  const element = session.elements.find((e) => e.id === subjectId);
  const members = new Set(isElement ? (element?.hostIds ?? []) : []);

  // Distinct (host, label, source) triples relevant to this subject.
  const raw = new Map<string, RawNode>();
  for (const m of session.metrics) {
    const host = m.host ?? "";
    let node: RawNode | null = null;
    if (host === subjectId) {
      node = {
        key: m.label,
        label: m.label,
        host,
        source: m.source ?? null,
        elementTarget: isElement,
      };
    } else if (members.has(host)) {
      node = {
        key: `${host}/${m.label}`,
        label: m.label,
        host,
        source: m.source ?? null,
        elementTarget: false,
      };
    }
    if (node && !raw.has(node.key)) raw.set(node.key, node);
  }

  // Group by chart label -> spec.
  const groups = new Map<string, RawNode[]>();
  for (const node of raw.values()) {
    const chartLabel = session.chartMap[node.label] ?? node.label;
    const list = groups.get(chartLabel);
    if (list) list.push(node);
    else groups.set(chartLabel, [node]);
  }

  const out: ChartNode[] = [];
  for (const [chartLabel, nodes] of groups) {
    const spec = session.meta.charts.find((c) => c.label === chartLabel);
    nodes.sort((a, b) => {
      if (a.elementTarget !== b.elementTarget) return a.elementTarget ? -1 : 1;
      return a.host.localeCompare(b.host) || a.label.localeCompare(b.label);
    });
    out.push({
      chartKey: spec?.chart ?? chartLabel,
      chartLabel,
      unit: spec?.unit ?? "",
      yTitle: spec?.y_title ?? chartLabel,
      series: nodes.map((n, i) => ({
        key: n.key,
        label: n.label,
        host: n.host,
        source: n.source,
        slot: i,
      })),
    });
  }
  // Chart order: meta.charts order first, unknown charts after, by label.
  const orderOf = (key: string): number => {
    const idx = session.meta.charts.findIndex((c) => c.chart === key);
    return idx === -1 ? Number.POSITIVE_INFINITY : idx;
  };
  return out.sort(
    (a, b) => orderOf(a.chartKey) - orderOf(b.chartKey) || a.chartLabel.localeCompare(b.chartLabel),
  );
}

export function filterTree(
  tree: ChartNode[],
  opts: { search: string; chips: Set<string> | null; source: string | null },
): ChartNode[] {
  const needle = opts.search.trim().toLowerCase();
  const out: ChartNode[] = [];
  for (const chart of tree) {
    if (opts.chips && !opts.chips.has(chart.chartKey)) continue;
    const chartHit = needle === "" || chart.chartLabel.toLowerCase().includes(needle);
    const series = chart.series.filter((s) => {
      if (opts.source !== null && s.source !== opts.source) return false;
      if (chartHit) return true;
      return (
        s.label.toLowerCase().includes(needle) || s.host.toLowerCase().includes(needle)
      );
    });
    if (series.length) out.push({ ...chart, series });
  }
  return out;
}

export function sourcesIn(tree: ChartNode[]): string[] {
  const set = new Set<string>();
  for (const chart of tree) {
    for (const s of chart.series) if (s.source !== null) set.add(s.source);
  }
  return [...set].sort();
}

/** In-range [ms, value] point arrays for the checked series keys, one pass
 * over the session's metrics, time-sorted (fixture data is generated
 * sorted; the sort is a cheap invariant guard). */
export function collectSeriesPoints(
  session: NormalizedSession,
  tree: ChartNode[],
  checked: Set<string>,
  range: TimeRange | null,
): Map<string, [number, number][]> {
  const keyOf = new Map<string, string>(); // "host|label" -> node key
  for (const chart of tree) {
    for (const s of chart.series) {
      if (checked.has(s.key)) keyOf.set(`${s.host}|${s.label}`, s.key);
    }
  }
  const out = new Map<string, [number, number][]>();
  for (const m of session.metrics) {
    const key = keyOf.get(`${m.host ?? ""}|${m.label}`);
    if (key === undefined) continue;
    const ts = parseTs(m.timestamp);
    if (range && (ts < range.from || ts > range.to)) continue;
    const arr = out.get(key);
    if (arr) arr.push([ts, m.value]);
    else out.set(key, [[ts, m.value]]);
  }
  for (const arr of out.values()) arr.sort((a, b) => a[0] - b[0]);
  return out;
}
```

Create `web/src/pages/SeriesPanel.tsx`:

```tsx
// Left panel of the subject view (UX spec §9): search -> quick-filter
// chips (chart groups + Source) -> series tree with checkboxes. Fully
// controlled; selection state lives in SubjectPage.
import { Badge } from "../ui/Badge";
import { TextInput } from "../ui/TextInput";
import { sourcesIn, type ChartNode } from "../data/seriesTree";

export function SeriesPanel(props: {
  tree: ChartNode[];
  checked: Set<string>;
  onToggle: (key: string) => void;
  search: string;
  onSearch: (value: string) => void;
  chips: Set<string> | null;
  onChips: (chips: Set<string> | null) => void;
  source: string | null;
  onSource: (source: string | null) => void;
}) {
  const { tree, checked, onToggle, search, onSearch, chips, onChips, source, onSource } = props;
  const sources = sourcesIn(tree);

  const toggleChip = (chartKey: string) => {
    const next = new Set(chips ?? []);
    if (next.has(chartKey)) next.delete(chartKey);
    else next.add(chartKey);
    onChips(next.size === 0 ? null : next);
  };

  return (
    <aside
      data-testid="series-panel"
      className="flex w-64 shrink-0 flex-col gap-3 border-r border-gray-200 pr-4 dark:border-gray-800"
    >
      <TextInput label="Search" value={search} onChange={onSearch} testId="series-search" />
      <div className="flex flex-wrap gap-1.5">
        {tree.map((chart) => (
          <button
            key={chart.chartKey}
            type="button"
            data-testid={`chip-${chart.chartKey}`}
            onClick={() => toggleChip(chart.chartKey)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              chips?.has(chart.chartKey)
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            {chart.chartLabel}
          </button>
        ))}
        {sources.map((src) => (
          <button
            key={src}
            type="button"
            data-testid={`chip-source-${src}`}
            onClick={() => onSource(source === src ? null : src)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              source === src
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            src: {src}
          </button>
        ))}
      </div>
      <div className="flex flex-col gap-2 overflow-y-auto text-sm">
        {tree.map((chart) => (
          <div key={chart.chartKey}>
            <p className="mb-1 text-xs font-semibold text-gray-400 uppercase">
              {chart.chartLabel}
              {chart.series.length > 6 ? ` (${chart.series.length})` : ""}
            </p>
            <ul className="flex flex-col gap-0.5">
              {chart.series.map((s) => (
                <li key={s.key} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    data-testid={`series-node-${s.key}`}
                    checked={checked.has(s.key)}
                    onChange={() => onToggle(s.key)}
                    className="accent-brand-600"
                  />
                  <span className="truncate">{s.host === s.key ? s.label : s.host}</span>
                  {s.source !== null && <Badge>{s.source}</Badge>}
                </li>
              ))}
            </ul>
          </div>
        ))}
        {tree.length === 0 && <p className="text-xs text-gray-400">No series match.</p>}
      </div>
    </aside>
  );
}
```

(Import-order note: biome will sort the `../data/seriesTree` import above the ui imports — let `check:fix` do it.)

- [ ] **Step 4: Verify green**

Run: `cd web && npx vitest run src/__tests__/seriestree.test.ts src/__tests__/seriespanel.test.tsx && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src/data/seriesTree.ts web/src/pages/SeriesPanel.tsx web/src/__tests__/seriestree.test.ts web/src/__tests__/seriespanel.test.tsx
git commit -m "feat(web): series tree model + filter panel

Metric-first tree with subject x source axes; entity-bound palette slots
assigned once from the unfiltered tree; search/chips/source filters; the
labeled tree is the palette's relief mechanism.

Assisted-by: Claude Fable 5"
```

---

### Task 6: Subject page — synced chart stack + log tables

**Files:**
- Modify: `web/src/pages/SubjectPage.tsx` (full body replacement; KEEP testids `subject-page`, `subject-title`, `series-summary` — Playwright's range tests pin the summary format `"<n> series · <m> samples in range"` — and the `not-found` branch verbatim)
- Test: `web/src/__tests__/subjectpage.test.tsx` (new; existing `pages.test.tsx` subject assertions must keep passing)

**Interfaces:**
- Consumes: Tasks 2/3/5 exports — `buildSeriesTree`, `filterTree`, `collectSeriesPoints`, `MAX_SERIES_PER_CHART`, `ChartPanel`, `buildStackOption`, `chartTheme`, `eventMarkers`, `useIsDark`, `zoom→setRange` via `clampRange`; `groupRowsFromData`, `logKey`, `visibleRows`, `MAX_TABLE_ROWS` from `../logevents` (kept Plan-2 module — first consumer in the new UI).
- Produces: testids `chart-stack`, `chart-panel-<chartKey>`, `series-overflow-<chartKey>`, `log-table-<tabId>` (+ `log-filter-<tabId>`), used by Task 8.

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/subjectpage.test.tsx`:

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

const setOptions: Record<string, unknown>[] = [];
vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => ({
      group: "",
      setOption: (o: Record<string, unknown>) => setOptions.push(o),
      on: () => {},
      resize: () => {},
      dispose: () => {},
    }),
    connect: () => {},
  },
}));

import { useReviewStore } from "../data/reviewStore";
import { SubjectPage } from "../pages/SubjectPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: mockSubject }) };
});
let mockSubject = "chassis-a_lc1";

function load(subject: string) {
  mockSubject = subject;
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  return render(<SubjectPage />);
}

afterEach(() => {
  cleanup();
  setOptions.length = 0;
  useReviewStore.setState({
    sessions: [], rawDocument: null, sourceName: null, warnings: [],
    importError: null, activeSessionId: null, range: null,
  });
});

describe("SubjectPage chart stack", () => {
  it("renders one chart panel per chart group with data", () => {
    load("chassis-a_lc1");
    expect(screen.getByTestId("chart-stack")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-cpu")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-psu-temp")).toBeTruthy();
  });

  it("keeps the pinned series-summary format", () => {
    load("chassis-a_lc1");
    expect(screen.getByTestId("series-summary").textContent).toMatch(
      /^\d+ series · \d+ samples in range$/,
    );
  });

  it("unchecking a series removes it from the chart options", () => {
    load("chassis-a_lc1");
    const before = setOptions.length;
    fireEvent.click(screen.getByTestId("series-node-CPU %"));
    expect(setOptions.length).toBeGreaterThan(before);
    const last = setOptions[setOptions.length - 1] as { series: { id: string }[] };
    // The cpu chart re-rendered without its only series -> panel unmounts;
    // whichever option was applied last must not contain the cpu series id.
    expect(last.series.every((s) => s.id !== "CPU %")).toBe(true);
  });

  it("element subject renders member series", () => {
    load("chassis-a");
    expect(screen.getByTestId("chart-panel-cpu")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-ambient")).toBeTruthy();
  });

  it("unknown subject keeps the not-found branch", () => {
    load("ghost");
    expect(screen.getByTestId("not-found")).toBeTruthy();
  });
});

describe("SubjectPage log tables", () => {
  it("renders the kernel table for a host with rows and filters it", () => {
    load("db-01");
    const table = screen.getByTestId("log-table-kernel");
    const rowsBefore = table.querySelectorAll("tbody tr").length;
    expect(rowsBefore).toBeGreaterThan(0);
    fireEvent.change(
      screen.getByTestId("log-filter-kernel").querySelector("input") as HTMLInputElement,
      { target: { value: "definitely-not-present" } },
    );
    expect(screen.getByTestId("log-table-kernel").querySelectorAll("tbody tr")).toHaveLength(0);
  });

  it("renders no table for a host without rows", () => {
    load("workers_w1");
    expect(screen.queryByTestId("log-table-kernel")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/subjectpage.test.tsx`
Expected: FAIL — no `chart-stack` testid in the scaffold body.

- [ ] **Step 3: Implement**

Replace `web/src/pages/SubjectPage.tsx` entirely:

```tsx
// Per-subject view (UX spec §9): left = SeriesPanel (search/chips/tree),
// right = charts stacked on a shared time axis with one synced crosshair
// (echarts group connect) and brush/wheel zoom driving the SAME range the
// review bar owns. Events overlay every chart (markLine/markArea). Table
// tabs render log-event tables below the stack. Review is display-only;
// marking/editing arrives with the live hookup.
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "wouter";

import { ChartPanel } from "../charts/ChartPanel";
import { buildStackOption, chartTheme, eventMarkers, type SeriesInput } from "../charts/options";
import { MAX_SERIES_PER_CHART } from "../charts/palette";
import { useIsDark } from "../charts/useIsDark";
import {
  clampRange,
  metricsForSubject,
  sessionBounds,
  subjectKind,
} from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { buildSeriesTree, collectSeriesPoints, filterTree } from "../data/seriesTree";
import { groupRowsFromData, logKey, visibleRows } from "../logevents";
import { SeriesPanel } from "./SeriesPanel";

export function SubjectPage() {
  const params = useParams<{ id: string }>();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const setRange = useReviewStore((s) => s.actions.setRange);
  const dark = useIsDark();

  const id = params.id;
  const [search, setSearch] = useState("");
  const [chips, setChips] = useState<Set<string> | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());

  const tree = useMemo(
    () => (session ? buildSeriesTree(session, id) : []),
    [session, id],
  );

  // (Re)select everything whenever the subject or session changes.
  const treeKey = `${session?.id ?? ""}:${id}`;
  useEffect(() => {
    setChecked(new Set(tree.flatMap((c) => c.series.map((s) => s.key))));
    setSearch("");
    setChips(null);
    setSource(null);
    // biome-ignore lint/correctness/useExhaustiveDependencies: treeKey is the session+subject identity; tree derives from it
  }, [treeKey]);

  if (!session) return null;
  const kind = subjectKind(session, id);
  if (kind === null) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown subject "{id}" in this session. <Link href="/">Back to overview</Link>
      </main>
    );
  }

  const bounds = sessionBounds(session);
  const window_ = range ?? bounds;
  const theme = chartTheme(dark);
  const filtered = filterTree(tree, { search, chips, source });
  const points = collectSeriesPoints(session, tree, checked, range);
  const markers = eventMarkers(session.events, window_);

  const host = session.lab.hosts.find((h) => h.id === id);
  const metrics = metricsForSubject(session, id, range);
  const labels = [...new Set(metrics.map((m) => m.label))].sort();

  const toggle = (key: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Log tables: one per table tab, per row-holding host (the subject, or
  // members for an element subject).
  const tableHosts = kind === "element"
    ? (session.elements.find((e) => e.id === id)?.hostIds ?? [])
    : [id];
  const grouped = groupRowsFromData(session.logEvents);
  const tableTabs = session.meta.tabs.filter((t) => t.kind === "table");

  return (
    <main data-testid="subject-page" className="flex flex-col gap-4 p-4">
      <nav className="text-sm text-gray-400">
        <Link href="/">Fleet</Link> / {id}
      </nav>
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-gray-400">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
      </h1>
      <p data-testid="series-summary" className="text-sm text-gray-500 dark:text-gray-400">
        {labels.length} series · {metrics.length} samples in range
      </p>
      <div className="flex gap-4">
        <SeriesPanel
          tree={filterTree(tree, { search, chips, source })}
          checked={checked}
          onToggle={toggle}
          search={search}
          onSearch={setSearch}
          chips={chips}
          onChips={setChips}
          source={source}
          onSource={setSource}
        />
        <div data-testid="chart-stack" className="flex min-w-0 grow flex-col gap-4">
          {filtered.map((chart) => {
            const active = chart.series.filter((s) => checked.has(s.key));
            if (active.length === 0) return null;
            const shown = active.slice(0, MAX_SERIES_PER_CHART);
            const series: SeriesInput[] = shown
              .map((s) => ({
                key: s.key,
                name: s.host === s.key ? s.label : s.host,
                slot: s.slot,
                points: points.get(s.key) ?? [],
              }))
              .filter((s) => s.points.length > 0);
            if (series.length === 0) return null;
            return (
              <section key={chart.chartKey}>
                <h2 className="mb-1 text-sm font-medium text-gray-600 dark:text-gray-300">
                  {chart.chartLabel}
                </h2>
                <ChartPanel
                  option={buildStackOption({
                    unit: chart.unit,
                    yTitle: chart.yTitle,
                    series,
                    window: window_,
                    events: markers,
                    theme,
                  })}
                  groupId={`subject-${id}`}
                  window={window_}
                  onZoom={(r) => setRange(clampRange(r, bounds))}
                  testId={`chart-panel-${chart.chartKey}`}
                />
                {active.length > MAX_SERIES_PER_CHART && (
                  <p
                    data-testid={`series-overflow-${chart.chartKey}`}
                    className="mt-1 text-xs text-gray-400"
                  >
                    showing {MAX_SERIES_PER_CHART} of {active.length} — narrow the selection
                  </p>
                )}
              </section>
            );
          })}
          {filtered.length === 0 && (
            <p className="text-sm text-gray-400">No series match the current filters.</p>
          )}
        </div>
      </div>
      {tableTabs.map((tab) =>
        tableHosts.map((tableHost) => (
          <LogTable
            key={`${tab.id}:${tableHost}`}
            tabId={tab.id ?? ""}
            label={tab.label ?? tab.id ?? ""}
            hostLabel={kind === "element" ? tableHost : null}
            columns={tab.columns ?? []}
            rows={grouped[logKey(tableHost, tab.id ?? "")] ?? []}
          />
        )),
      )}
    </main>
  );
}

function LogTable(props: {
  tabId: string;
  label: string;
  hostLabel: string | null;
  columns: string[];
  rows: ReturnType<typeof groupRowsFromData>[string];
}) {
  const { tabId, label, hostLabel, columns, rows } = props;
  const [filter, setFilter] = useState("");
  if (rows.length === 0) return null;
  const visible = visibleRows(rows, filter);
  return (
    <section data-testid={`log-table-${tabId}`} className="max-w-3xl">
      <div className="mb-1 flex items-center gap-3">
        <h2 className="text-sm font-medium text-gray-600 dark:text-gray-300">
          {label}
          {hostLabel ? ` — ${hostLabel}` : ""}
        </h2>
        <span data-testid={`log-filter-${tabId}`}>
          <input
            type="text"
            placeholder="filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="rounded border border-gray-200 px-2 py-0.5 text-xs dark:border-gray-700
              dark:bg-gray-900"
          />
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-gray-400">
              <th className="py-1 pr-3 font-medium">time</th>
              {columns.map((c) => (
                <th key={c} className="py-1 pr-3 font-medium">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are static snapshots
              <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                <td className="py-1 pr-3 text-gray-400">
                  {new Date(row.timestamp).toLocaleTimeString()}
                </td>
                {columns.map((c) => (
                  <td key={c} className="py-1 pr-3">
                    {row.fields[c] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
```

Type note: `groupRowsFromData` consumes `LogEventRow[]` (from `api/client.ts`) whose rows carry `timestamp/host/tab/fields`; `session.logEvents` rows are the generated `LogEventRecord` shape with optional `host`/`tab`. Bridge at the call site if tsc complains: `groupRowsFromData(session.logEvents.map((r) => ({ timestamp: r.timestamp, host: r.host ?? "", tab: r.tab ?? "", fields: r.fields ?? {} })))` — do NOT edit the kept `logevents.ts`.

- [ ] **Step 4: Verify green (incl. survivors)**

Run: `cd web && npx vitest run src/__tests__/subjectpage.test.tsx src/__tests__/pages.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build`
Expected: all PASS — `pages.test.tsx` subject assertions (`subject-title`, `series-summary` format, not-found) must survive unchanged.

- [ ] **Step 5: Commit**

```bash
git add web/src/pages/SubjectPage.tsx web/src/__tests__/subjectpage.test.tsx
git commit -m "feat(web): subject page — synced ECharts stack + log tables

Filter panel + entity-slotted series over a shared window; group-connected
crosshair; zoom clamps into the review range (single source of truth);
events overlay every chart; table tabs render via kept logevents helpers.

Assisted-by: Claude Fable 5"
```

---

### Task 7: Events slide-over

**Files:**
- Create: `web/src/ui/SlideOver.tsx`, `web/src/shell/EventsPanel.tsx`
- Modify: `web/src/shell/AppBar.tsx` (Events button, left of the status text)
- Test: `web/src/__tests__/events_panel.test.tsx`, extend `web/src/__tests__/ui.test.tsx` (SlideOver)

**Interfaces:**
- Consumes: `useActiveSession`, `useReviewStore` (`setRange`), `clampRange`, `sessionBounds`, `formatSpan`, `parseTs`; react-aria `Modal/ModalOverlay/Dialog/Heading`.
- Produces: `SlideOver` primitive `{ isOpen: boolean; onClose: () => void; title: string; children: ReactNode; testId?: string }`; testids `events-button`, `events-count`, `events-panel`, `event-row-<id>`.
- Jump semantics: clicking a row sets `range = clampRange({ from: ts − 15 min, to: (end ?? ts) + 15 min }, bounds)` and closes the panel.

- [ ] **Step 1: Write the failing tests**

Append to `web/src/__tests__/ui.test.tsx` (inside the file's existing describe conventions):

```tsx
describe("SlideOver", () => {
  it("renders children when open and calls onClose on dismiss", async () => {
    const onClose = vi.fn();
    render(
      <SlideOver isOpen onClose={onClose} title="Events" testId="events-panel">
        <p>content</p>
      </SlideOver>,
    );
    const panel = await screen.findByTestId("events-panel");
    expect(panel.textContent).toContain("content");
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders nothing when closed", () => {
    render(
      <SlideOver isOpen={false} onClose={() => {}} title="Events">
        <p>content</p>
      </SlideOver>,
    );
    expect(screen.queryByText("content")).toBeNull();
  });
});
```

(Add `SlideOver` to the file's ui imports.)

Create `web/src/__tests__/events_panel.test.tsx`:

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

if (typeof CSS === "undefined" || !CSS.escape) {
  // Same polyfill as ui.test.tsx — react-aria portals call CSS.escape.
  (globalThis as { CSS?: unknown }).CSS = {
    escape: (v: string) => v.replace(/[^a-zA-Z0-9_ -￿-]/g, (c) => `\\${c}`),
  };
}

import { useReviewStore } from "../data/reviewStore";
import { EventsPanel } from "../shell/EventsPanel";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

afterEach(() => {
  cleanup();
  useReviewStore.setState({
    sessions: [], rawDocument: null, sourceName: null, warnings: [],
    importError: null, activeSessionId: null, range: null,
  });
});

function load() {
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  const onClose = vi.fn();
  render(<EventsPanel isOpen onClose={onClose} />);
  return { onClose, session: useReviewStore.getState().sessions[0] };
}

describe("EventsPanel", () => {
  it("lists events newest-first with span durations", async () => {
    load();
    const panel = await screen.findByTestId("events-panel");
    const rows = panel.querySelectorAll("[data-testid^=event-row-]");
    expect(rows).toHaveLength(4);
    // Newest first: the log-capture span (start 90m) precedes stress (85m),
    // w2 lost (60m), config reload (20m).
    expect(rows[0].textContent).toContain("log capture");
    expect(rows[0].textContent).toContain("10m"); // 90->100m span
    expect(rows[3].textContent).toContain("config reload");
  });

  it("clicking a row jumps the range around the event and closes", async () => {
    const { onClose, session } = load();
    const row = await screen.findByTestId("event-row-2"); // stress run 85–95m
    fireEvent.click(row);
    const range = useReviewStore.getState().range;
    expect(range).not.toBeNull();
    expect(range?.from).toBe(session.startMs + 70 * MIN); // 85m − 15m
    expect(range?.to).toBe(session.startMs + 110 * MIN); // 95m + 15m
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the empty state without events", async () => {
    useReviewStore.getState().actions.importText(
      readFileSync(join(HERE, "../../fixtures/minimal.json"), "utf-8"),
      "minimal.json",
    );
    render(<EventsPanel isOpen onClose={() => {}} />);
    const panel = await screen.findByTestId("events-panel");
    expect(panel.textContent).toContain("No events in this session");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/__tests__/events_panel.test.tsx src/__tests__/ui.test.tsx`
Expected: FAIL — `SlideOver`/`EventsPanel` don't exist.

- [ ] **Step 3: Implement**

Create `web/src/ui/SlideOver.tsx`:

```tsx
// Right-anchored slide-over (UX spec §6: events review surface). Controlled;
// react-aria Modal handles focus trap, Escape, and overlay dismiss.
import type { ReactNode } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

export function SlideOver(props: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  testId?: string;
}) {
  const { isOpen, onClose, title, children, testId } = props;
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      isDismissable
      className="fixed inset-0 z-40 bg-black/30"
    >
      <Modal className="fixed inset-y-0 right-0 z-50 w-96 max-w-full">
        <Dialog
          data-testid={testId}
          className="flex h-full flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white
            p-4 outline-none dark:border-gray-800 dark:bg-gray-950"
        >
          <div className="flex items-center justify-between">
            <Heading slot="title" className="text-sm font-semibold">
              {title}
            </Heading>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="cursor-pointer rounded px-2 text-gray-400 hover:text-gray-600
                dark:hover:text-gray-200"
            >
              ✕
            </button>
          </div>
          {children}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
```

Create `web/src/shell/EventsPanel.tsx`:

```tsx
// Events slide-over (UX spec §11, review-mode subset): reverse-chron list;
// a row jumps the charts to its time (±15 min, clamped). Marking/editing
// needs the backend API — live-hookup phase.
import { clampRange, sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatSpan, parseTs } from "../data/time";
import { SlideOver } from "../ui/SlideOver";

const JUMP_PAD_MS = 15 * 60_000;

export function EventsPanel(props: { isOpen: boolean; onClose: () => void }) {
  const { isOpen, onClose } = props;
  const session = useActiveSession();
  const setRange = useReviewStore((s) => s.actions.setRange);
  if (!session) return null;

  const rows = session.events
    .map((ev) => ({
      id: ev.id ?? 0,
      label: ev.label ?? "",
      color: ev.color ?? "#7c5cff",
      source: ev.source ?? "manual",
      fromMs: parseTs(ev.timestamp),
      toMs: ev.end_timestamp != null ? parseTs(ev.end_timestamp) : null,
    }))
    .sort((a, b) => b.fromMs - a.fromMs);

  const jump = (fromMs: number, toMs: number | null) => {
    setRange(
      clampRange(
        { from: fromMs - JUMP_PAD_MS, to: (toMs ?? fromMs) + JUMP_PAD_MS },
        sessionBounds(session),
      ),
    );
    onClose();
  };

  return (
    <SlideOver isOpen={isOpen} onClose={onClose} title="Events" testId="events-panel">
      {rows.length === 0 && (
        <p className="text-sm text-gray-400">No events in this session.</p>
      )}
      <ul className="flex flex-col gap-1">
        {rows.map((ev) => (
          <li key={ev.id}>
            <button
              type="button"
              data-testid={`event-row-${ev.id}`}
              onClick={() => jump(ev.fromMs, ev.toMs)}
              className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5
                text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-900"
            >
              <span
                aria-hidden
                className="h-3 w-3 shrink-0 rounded-sm"
                style={{ backgroundColor: ev.color }}
              />
              <span className="min-w-0 grow truncate">{ev.label}</span>
              <span className="shrink-0 text-xs text-gray-400">
                {new Date(ev.fromMs).toLocaleTimeString()}
                {ev.toMs !== null ? ` · ${formatSpan(ev.fromMs, ev.toMs)}` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </SlideOver>
  );
}
```

Modify `web/src/shell/AppBar.tsx` — add the events trigger. New imports: `useState` already imported; add `useActiveSession` to the reviewStore import line, and `import { EventsPanel } from "./EventsPanel";`. Inside the component add:

```tsx
  const session = useActiveSession();
  const [eventsOpen, setEventsOpen] = useState(false);
```

and in the right-hand cluster, FIRST child (before the status text, keeping status text · dot · ⋯ hard-right per UX §7):

```tsx
        {session && session.events.length > 0 && (
          <button
            type="button"
            data-testid="events-button"
            onClick={() => setEventsOpen(true)}
            className="cursor-pointer rounded-md px-2 py-1 text-sm text-gray-500
              hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-900"
          >
            Events{" "}
            <span
              data-testid="events-count"
              className="rounded-full bg-gray-100 px-1.5 text-xs dark:bg-gray-800"
            >
              {session.events.length}
            </span>
          </button>
        )}
```

and render `<EventsPanel isOpen={eventsOpen} onClose={() => setEventsOpen(false)} />` as the last child inside the `<header>`'s right-hand cluster wrapper (after `OverflowMenu`).

- [ ] **Step 4: Verify green**

Run: `cd web && npx vitest run src/__tests__/events_panel.test.tsx src/__tests__/ui.test.tsx src/__tests__/shell.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck`
Expected: all PASS (shell tests must survive — the events button renders only with data + events, so empty-state tests are unaffected).

- [ ] **Step 5: Commit**

```bash
git add web/src/ui/SlideOver.tsx web/src/shell/EventsPanel.tsx web/src/shell/AppBar.tsx web/src/__tests__/events_panel.test.tsx web/src/__tests__/ui.test.tsx
git commit -m "feat(web): events slide-over — reverse-chron review + range jump

SlideOver primitive on react-aria Modal; rows jump the shared range
±15min clamped; display-only per review-mode semantics (marking is
live-hookup work).

Assisted-by: Claude Fable 5"
```

---

### Task 8: Playwright behavior specs

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_review_shell.py` (append; existing 12 specs must keep passing byte-unchanged)

**Interfaces:**
- Consumes: the testid contract from Tasks 4–7 (`host-tile-*`, `headline-*`, `health-rollup-*`, `chart-panel-*`, `series-panel`, `series-node-*`, `chip-*`, `chip-source-*`, `events-button`, `events-panel`, `event-row-*`, `log-table-kernel`, `log-filter-kernel`) and the existing `_import_fixture` helper + `shell_dash` fixture.

- [ ] **Step 1: Run `make web` (rebuild dist so the harness serves Tasks 4–7)**

Run: `make web`
Expected: green (drift gate, both builds, both air-gap greps — the echarts chunk must introduce no external URLs; if the airgap grep fails, STOP and report the URL rather than allowlisting anything without scrutiny).

- [ ] **Step 2: Append the specs**

```python
def test_grid_health_tiles_and_headline(shell_dash, page):
    """Fleet grid (UX §8): labeled headline at full range; down · duration
    when the selected range ends inside the outage window (health is
    last-known-within-range, so narrowing re-evaluates it)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    tile = page.locator('[data-testid="host-tile-chassis-a_lc1"]')
    tile.wait_for()
    assert re.search(r"% cpu", page.locator('[data-testid="headline-chassis-a_lc1"]').inner_text())
    w2 = page.locator('[data-testid="host-tile-workers_w2"]')
    assert "down ·" not in w2.inner_text()
    # Rollup bar: one segment per chassis member.
    assert page.locator('[data-testid="health-rollup-chassis-a"] > *').count() == 3

    # End the range inside workers_w2's 60-80min outage: derive +70min from
    # the pre-populated LOCAL from-input (same derivation the custom-range
    # spec uses — datetime-local is local wall-clock).
    start_raw = page.locator('[data-testid="range-from"] input').input_value()
    start = datetime.strptime(start_raw, "%Y-%m-%dT%H:%M")  # noqa: DTZ007 — naive local wall-clock by design
    page.locator('[data-testid="range-to"] input').fill(
        (start + timedelta(minutes=70)).strftime("%Y-%m-%dT%H:%M")
    )
    page.locator('[data-testid="range-apply"]').click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"host-tile-workers_w2\"]').innerText.includes('down ·')"
    )
    assert "down · 10m" in w2.inner_text()


def test_subject_charts_render_and_filter(shell_dash, page):
    """Per-subject stack (UX §9): canvases render per chart group; the
    series tree checkbox and chip filters narrow the stack."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').click()
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    assert page.locator('[data-testid="chart-stack"] canvas').count() >= 4
    # Uncheck the CPU series -> its (single-series) panel unmounts.
    page.locator('[data-testid="series-node-CPU %"]').click()
    page.locator('[data-testid="chart-panel-cpu"]').wait_for(state="detached")
    # Chip filter narrows to one group.
    page.locator('[data-testid="chip-mem"]').click()
    page.locator('[data-testid="chart-panel-psu-temp"]').wait_for(state="detached")
    assert page.locator('[data-testid="chart-stack"] canvas').count() == 1


def test_source_badges_and_source_filter(shell_dash, page):
    """Provenance (UX §9): mgmt-sourced series wear a badge; the source
    chip filters the tree to externally-sourced series only."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-chassis-a_lc1"]').click()
    panel = page.locator('[data-testid="series-panel"]')
    panel.wait_for()
    assert "mgmt-01" in panel.inner_text()
    before = page.locator('[data-testid^="series-node-"]').count()
    page.locator('[data-testid="chip-source-mgmt-01"]').click()
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-testid^=\"series-node-\"]').length < {before}"
    )
    # Only the two mgmt-sourced charts remain for this host.
    assert page.locator('[data-testid^="chart-panel-"]').count() == 2


def test_events_slide_over_jumps_range(shell_dash, page):
    """Events (UX §11 review subset): reverse-chron slide-over; a row jump
    re-scopes the shared range (review-bar inputs follow)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    assert page.locator('[data-testid="events-count"]').inner_text() == "4"
    before = page.locator('[data-testid="range-from"] input').input_value()
    page.locator('[data-testid="events-button"]').click()
    page.locator('[data-testid="events-panel"]').wait_for()
    rows = page.locator('[data-testid^="event-row-"]')
    assert rows.count() == 4
    assert "log capture" in rows.nth(0).inner_text()  # newest first
    page.locator('[data-testid="event-row-2"]').click()  # stress-run span
    page.locator('[data-testid="events-panel"]').wait_for(state="detached")
    page.wait_for_function(
        "(prev) => document.querySelector('[data-testid=\"range-from\"] input').value !== prev",
        arg=before,
    )


def test_log_table_renders_and_filters(shell_dash, page):
    """Table tabs: kernel log rows render for db-01 and filter down."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    table = page.locator('[data-testid="log-table-kernel"]')
    table.wait_for()
    rows_before = table.locator("tbody tr").count()
    assert rows_before > 0
    page.locator('[data-testid="log-filter-kernel"] input').fill("no-such-message-xyz")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"log-table-kernel\"]').querySelectorAll('tbody tr').length === 0"
    )


def test_element_subject_renders_member_series(shell_dash, page):
    """Element drill-in: /host/chassis-a stacks member + element-targeted
    series (ambient) as charts."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/host/chassis-a")
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.locator('[data-testid="chart-panel-ambient"] canvas').wait_for()


def test_theme_toggle_with_charts_open(shell_dash, page):
    """Theme flip re-renders open charts without error (canvas persists,
    dark class lands)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.locator('[data-testid="subject-link-db-01"]').click()
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
    page.locator('[data-testid="overflow-menu"]').click()
    page.locator('[data-testid="menu-theme"]').click()
    page.wait_for_function(
        "() => document.documentElement.classList.contains('dark') !== undefined"
    )
    page.locator('[data-testid="chart-panel-cpu"] canvas').wait_for()
```

Add to the module's imports (top of file, with the existing ones): `import re` and `from datetime import datetime, timedelta` (both may already be present from the custom-range spec — check first; `datetime`/`timedelta` are).

- [ ] **Step 3: Run the browser lane**

Run: `make dashboard`
Expected: all specs green — 19 previous (12 review-shell + 7 covreport) + 7 new = **26 passed**. Debug selector surprises against the actual DOM (`page.pause()` is unavailable headless — use `inner_html()` dumps in a scratch run); react-aria quirks and their fixes are documented in the module docstring and `.superpowers/sdd/progress.md`.

- [ ] **Step 4: Harness sanity**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -q && uv run ruff format --check tests/e2e/monitor/dashboard/test_review_shell.py && uv run ruff check tests/e2e/monitor/dashboard/test_review_shell.py`
Expected: 12 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "test(dashboard): behavior specs — grid health, chart stack, events, log tables

Assisted-by: Claude Fable 5"
```

---

### Task 9: Gates + ratchet

**Files:** possibly `web/vite.config.ts` (thresholds); fixes surfaced by gates.

- [ ] **Step 1:** `make coverage-hostless` — expected green (this plan's only Python change is the Task-1 conftest deletion). NEVER `make coverage`.
- [ ] **Step 2:** `uv run nox -s lint typecheck` — expected green.
- [ ] **Step 3:** `make web` — drift gate + builds + air-gap ×2 green. Note the dist-size delta from echarts in the report.
- [ ] **Step 4:** `cd web && npm run check && npm run typecheck && npm run test:coverage` — recalibrate thresholds to ~2–3% below measured if drifted in either direction (new chart/page components will move the needle); update the ratchet comment's baseline numbers precisely (no "~N points across the board" prose).
- [ ] **Step 5:** `make dashboard` — one confirming run, 26 passed.
- [ ] **Step 6:** `make import-snapshot` — zero diff expected.
- [ ] **Step 7:** Commit any recalibration/fixes (own conventional commits, trailer included), e.g.:

```bash
git add web/vite.config.ts
git commit -m "test(web): re-ratchet vitest coverage floor after the views phase

Assisted-by: Claude Fable 5"
```

---

## Self-review notes (done at authoring time)

- **Spec coverage:** UX §5 (ECharts, direct wrapper, axisPointer sync, brush→range) → Tasks 3/6; §8 (grid, rollup, tiles, headline+fallback, down·duration) → Tasks 2/4; §9 (panel, chips, tree, source badges, synced stack) → Tasks 5/6; §11 review subset (overlay + slide-over + jump) → Tasks 3/6/7 — marking/editing explicitly deferred to live hookup (§12 read-only review); §13 states — down/no-data covered, drilled-in-unreachable dimming is a LIVE-mode state, deferred with live hookup; contract §6 (health) → Task 2. Topology remains Plan 4.
- **Follow-ups:** #1 → T1; #2 (`clampRange` → T1, `formatSpan` → T4/T7); #4/#5 decided in the header (no code); #6 → T1; #3 (EmptyState import-error testid) deliberately NOT here — it belongs to the shell's own test file and is tracked in the todo file still.
- **Type consistency:** `NormalizedMeta` (T1) consumed by T2/T5 via `session.meta.charts/tabs` dense arrays; `SeriesInput.slot` ← `SeriesNode.slot` (T5→T6); `EventMarker` produced/consumed inside T3/T6; `ChartPanel` props match T6 usage; `elementRollup(element, healths, session)` signature matches T4's call.
- **Placeholder scan:** clean — every code step carries complete code; the two "adapt to the file's actual helper/DOM" notes mirror the established Plan-2 adaptation protocol rather than deferring content.
- **Known risks, called out for implementers:** echarts option typing is loose by design (`Record<string, unknown>` builders — the upstream `EChartsOption` type fights tsc strict for little gain); jsdom needs the `ResizeObserver` stub and the echarts module mock (provided verbatim); the kitchen-sink outage arithmetic in T2/T8 tests is derived from the fixture generator (60–80 min gap, 15/30 s cadences) — failures there mean real bugs, not tolerances to widen.
