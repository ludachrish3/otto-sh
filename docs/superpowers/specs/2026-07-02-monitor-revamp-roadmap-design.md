# otto monitor revamp — roadmap design

**Date:** 2026-07-02
**Status:** Approved in brainstorming session; roadmap-level spec. Each phase gets its own
spec → plan cycle before implementation.

## Motivation

`otto monitor` works but is at the edge of its current architecture:

- The frontend is one 790-line vanilla-JS file (`dashboard.js`) with a global mutable
  `state`, hand-rolled DOM sync, and zero tests. The UX backlog in `todo/TODO.md`
  (import/clear buttons, date picker, URL params, collapse, historical chrome) will push
  it past maintainability.
- The backend's `MetricCollector` (795 lines) holds five jobs: tick loop, in-memory
  store, SQLite persistence, event CRUD, and SSE pub/sub.
- Extension is per-host only (`register_host_parsers`); there is no project-level parser
  registration and no per-metric collection frequency.
- Built-in metrics are four parsers (top-CPU, memory %, disk %, load). The user guide
  already promises network metrics that do not exist.
- Known frontend bugs: chart height grows over time under process churn; plots don't
  resize with the window; Safari modebar overdraws the right edge (`todo/TODO.md:43`).

### Bug diagnosis: chart height growth

`buildLayout()` (`dashboard.js`) sets each chart div's height to
`CHART_AREA_HEIGHT + topMargin + bottomMargin`, where the bottom margin grows with
legend rows (one row per ~6 traces). The CPU chart creates one trace per *PID ever
seen* (`proc/<pid>` series accumulate in `mp.metrics` and are never retired when
processes exit), so PID churn grows the legend — and therefore the div — without bound.

## Decisions (made in this brainstorm)

| Decision | Choice |
|---|---|
| Scope | One roadmap spec; phases get individual specs/plans |
| Plugin model | Declarative, Python-only. "Plugin" = adding tabs/charts within the existing framework, backend-driven. No third-party JS, no JS plugin API. |
| Frontend stack | Vite + TypeScript + React |
| Chart library | Keep Plotly (npm partial bundle); switching is high regression risk for little gain |
| Testing | vitest (TS unit) + pytest-playwright (browser e2e); Selenium rejected |
| Build/packaging | Build at release/CI time; `dist/` not committed; Node is a dev/CI/release dependency |
| Air-gap | **Hard requirement:** the wheel is fully self-contained. No CDN or external web assets at runtime; everything the dashboard loads ships inside the wheel. |
| Metrics | All four families: network, disk I/O + swap, per-core CPU + processes, Zephyr/SNMP parity |

## 1. Declarative contract

One source of truth for what the dashboard shows: pydantic models replacing the ad-hoc
dicts `MetricCollector.get_meta()` builds today.

- `TabSpec` — tab id, label, order.
- `ChartSpec` — chart key, tab, y-axis title, unit, series grouping, collection interval.

`/api/meta` serves these models. The frontend's TypeScript types for the wire contract
are **generated from the pydantic JSON schema** at web-build time (reusing the existing
`otto schema export` infrastructure), so backend and frontend cannot silently drift.
The frontend renders purely from the contract and knows nothing about specific parsers.

Registration — first party and third party use the identical mechanism:

- `register_parsers([...])` — **new**, project-level: extends/overrides
  `DEFAULT_PARSERS` for all hosts (from init modules in `.otto/settings.toml`).
- `register_host_parsers(host_id, {...})` — existing, kept as the per-host override
  layer. Per-host beats project-level beats defaults.
- Collisions are loud, reusing the generic `Registry` dupe/did-you-mean machinery.

## 2. Backend decomposition

Split `otto/monitor/collector.py`, keeping the public `MetricCollector` API delegating
so consumers (`Suite.get_monitor_results()`, server routes) don't churn:

| New module | Responsibility |
|---|---|
| `store.py` | `MetricStore`: in-memory series, chart map, events, snapshot methods |
| `db.py` | `MetricDB`: aiosqlite persistence — schema, WAL/DELETE selection, flock guard |
| `broadcast.py` | SSE subscriber queues (`subscribe`/`unsubscribe`/`publish`) |
| `history.py` | JSON/SQLite historical loaders + export |
| `collector.py` | Tick-loop orchestration only |

Two parser-API changes ride along:

- **Parser API v2:** `parse(output, *, ctx: ParseContext)`. `ctx` carries `core_count`
  (removing the collector-mutates-parser wart documented in
  `todo/parser-core-count-via-parse-kwarg.md`) and is extensible without further
  signature breaks. Rate parsers hold previous-tick counters as instance state — safe
  because parser instances are per-host deep copies.
- **Per-parser `interval: float | None = None`** (None → global interval). The
  collector buckets commands by effective interval and runs one gather loop per bucket.
  This delivers the TODO's "out-of-band commands at custom frequencies" without a
  separate mechanism.

## 3. Frontend: React + Vite + TypeScript

New `web/` directory at repo root. Build output goes to
`src/otto/monitor/static/dist/` (gitignored; built by CI/release).

- **Components:** `App`, `Header` (host select, theme, pause, export, status),
  `TabBar`, `ChartGrid`, `ChartPanel` (thin Plotly wrapper), `EventToolbar`,
  `EventPopover`. A small state store is fed by the SSE client (zustand vs.
  `useReducer` decided at plan time).
- **Plotly:** npm dependency using a partial bundle (scattergl only). The 4.5 MB
  vendored `plotly.min.js` leaves the repo. `react-plotly.js` is stale — a ~100-line
  wrapper keeps the `Plotly.extendTraces` fast path for live appends.
- **Air-gap enforcement:** Vite bundles all JS/CSS/fonts locally; no external URLs.
  Two checks: (a) a build gate greps `dist/` for external `https://` references,
  (b) a Playwright test blocks all non-localhost requests and asserts the dashboard
  fully renders.
- **Wheel packaging:** the wheel build depends on the web build; `dist/` is
  force-included in the wheel (hatchling `artifacts`) even though gitignored.

Bugs fixed *by design*, each pinned by a test:

- **Height growth:** constant chart-div height; fixed legend row budget; per-process
  traces are retired after their PID is absent for K consecutive ticks (data retained
  in the store for export — only the live legend/trace set shrinks).
- **Window resize:** `responsive: true` + ResizeObserver.
- **Safari overdraw:** modebar containment via CSS overflow rules on the plot
  container, pinned by a Playwright-WebKit test.
- Historical mode hides live-only chrome (event toolbar, play/pause, export); export
  disabled while disconnected; page title includes the selected host; play/pause
  relocated.

Dev workflow: `make web` (production build), `make web-dev` (Vite dev server with HMR,
proxying `/api` to a running `otto monitor`), and `server.py` raises a helpful
"run make web" error when `dist/` is missing. Node version pinned via `.nvmrc`.

## 4. First-party metrics

| Family | Source | Notes |
|---|---|---|
| Network | `/proc/net/dev` deltas; `ss -s` | Per-interface rx/tx rates; established/time-wait socket counts. Its own Network tab; makes the guide's existing "network" promise true. |
| Disk I/O + swap | `/proc/diskstats` deltas; `free -b`; `/proc/pressure/*` | Read/write rates per device; swap %; PSI parsers return `{}` gracefully on kernels without PSI. |
| Per-core CPU + processes | `/proc/stat` deltas; `/proc/loadavg` | `/proc/stat` deltas are far cheaper than a second `top` run; process/thread counts already appear in loadavg output. |
| SNMP parity | HOST-RESOURCES-MIB / UCD-SNMP-MIB / ifTable | Standard OID map so Zephyr/embedded beds render the same tabs as Unix hosts. |

## 5. Testing

- **vitest** (in `web/`): store reducers, SSE message handling, series grouping,
  trace-retirement policy. Pure TS, no browser.
- **pytest-playwright** (`tests/e2e/monitor/`): a `FakeCollector` with scripted
  series/events drives a real `MonitorServer`; headless Chromium asserts initial
  render, tab switching, live append, event CRUD round-trip, pause, disconnect state,
  historical chrome, **chart height stays constant under 100 churning fake PIDs**,
  window resize, and the air-gap render (all non-localhost network blocked). A WebKit
  run pins the Safari modebar fix. Marked per the existing tier scheme (e2e dir +
  a browser resource marker).
- **CI:** a `web` nox session runs the Vite build + vitest; wheel build depends on the
  web build.

## 6. Phases

1. **Phase 0 — harness first.** `FakeCollector` + Playwright fixtures against the
   *current* dashboard; smoke-pin the behavior that must survive the port. Cheap
   insurance; ~90 % of the harness is reused forever.
2. **Phase 1 — backend contract.** Decomposition, pydantic meta models, parser API v2,
   project-level registration, per-parser intervals. The old frontend keeps working
   (meta stays shape-compatible).
3. **Phase 2 — React port.** Toolchain, 1:1 feature port + by-design bug fixes,
   vitest, expanded Playwright suite, vendored Plotly removed, air-gap gates.
4. **Phase 3 — metrics.** Four families + SNMP parity + docs.
5. **Phase 4 — UX backlog.** Import/clear-data buttons, date picker + URL params, full
   graph collapse, default DB at `xdir/monitor/otto.db`, `--historical` flag.

## Out of scope

- JS plugin API / third-party frontend code (explicit decision — no plans for
  frontend-side plugins).
- Switching chart libraries away from Plotly.
- Embedding Grafana (considered; rejected — a lab tool shouldn't drag in an external
  server and its provisioning story).
- Anomaly detection (stays on the TODO backlog; revisit after Phase 3 when there are
  more series to reason over).

## Open questions (resolved at per-phase plan time)

- State store: zustand vs. React context + `useReducer`.
- Trace-retirement K (ticks of PID absence before retirement) and legend row budget.
- Node version pin and how CI provisions it.
- Exact SNMP OID set per metric family.
