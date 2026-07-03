# Monitor Phase 2: React + Vite + TypeScript Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the vanilla-JS dashboard with a React + Vite + TypeScript frontend that passes the Phase 0/1 pin suites unchanged, fixes the three known frontend bugs by design, and ships air-gap-safe inside the wheel.

**Architecture:** The Phase 0 Playwright suite (21 tests) is the ACCEPTANCE HARNESS: the React app renders the SAME DOM ids/classes/behaviors as the legacy dashboard, so those pins pass against either frontend and gate the cutover. The wire contract is consumed via TS types GENERATED from `monitor-meta.schema.json` (Phase 1's export). A zustand store is fed by the SSE client (external-event updates without prop plumbing; per-chart selective subscriptions). Plotly stays, as the npm `plotly.js-gl2d-dist-min` partial bundle (scattergl) behind a ~100-line wrapper keeping the `extendTraces` fast path. The server serves `static/dist/` when present, falling back to the legacy files until the cutover task deletes them.

**Tech Stack:** Node 24 LTS (`.nvmrc`), Vite, React 18+, TypeScript strict, zustand, vitest, json-schema-to-typescript, plotly.js-gl2d-dist-min. Python side unchanged except: dist-serving in `server.py`, `MonitorMeta.interval` addendum, packaging config.

**Spec:** `docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md` §3 + Phase 2. Phase-1 final-review carry-ins absorbed here: MonitorMeta global-interval decision (DECIDED: yes, additive `interval: float | None`), `build_schemas` monitor-meta drift assertion, jsonschema docstring/editor-schemas row.

## Global Constraints

- **DOM-parity contract (the load-bearing rule):** the React app reproduces the legacy dashboard's observable surface — element ids (`#host-select`, `#status-label`, `#status-dot`, `#pause-btn`, `#theme-btn`, `#export-btn`, `#event-label/-color/-dash/-btn`, `#span-*`, `#popover-*`, `#clear-events-btn`, `#tab-bar`, `#event-bar`), classes (`.tab-btn[data-tab]`, `.tab-panel`/`active`, `#tab-<id>` panels, `.tab-charts`, `.section-divider`, `.metric-plot`, `.expand-btn`, body `historical`/`light`/`plot-expanded`), Plotly div internals (`gd.data` traces with legacy `name`s, `gd.layout.annotations/shapes`), status texts (`Live`/`Paused`/`Historical`/`Disconnected`), `confirm()` on clear-events, and `localStorage['otto-theme']`. Reference: `src/otto/monitor/static/dashboard.js` + the pin files. Tests in `tests/e2e/monitor/dashboard/` are NOT edited except where a task explicitly sanctions it.
- Sanctioned pin evolutions, complete list: Task 4 adds `"interval"` to `META_KEYS` (top-level meta). Task 10 ADDS new pins (churn/height); Task 11 ADDS resize + WebKit pins; Task 12 ADDS the offline pin. No existing assertion changes anywhere.
- **Air-gap hard requirement:** no external URL is fetched at runtime; everything the dashboard loads ships in the wheel. Enforced by Task 3's grep gate + Task 12's offline Playwright pin.
- Node/npm exist ONLY in the web lane (`make web*`, CI dashboard job); `make coverage`/nox python sessions never require Node once `dist/` is built (or absent → legacy/hard-error per phase).
- Python rules as Phase 1: no `from __future__ import annotations`; module-top imports; `@override`; strict ruff (re-check after format); ty at `nox -s typecheck`.
- Worktree execution; fresh worktree needs `uv sync`; Chromium already installed VM-wide; WebKit installed in Task 11.
- Commit per task; hook `/dev/tty` error → never `--no-verify`, leave staged + report. Placement check after every commit: `git branch --show-current` + `git log --oneline -1` in the reply.
- Web code style: TypeScript strict, no `any` without a comment, ESLint not introduced (vite+tsc only — YAGNI); components small and single-purpose.

## File Structure

```text
.nvmrc                                  # "24"
web/package.json, web/package-lock.json, web/vite.config.ts, web/tsconfig.json
web/index.html                          # parity chrome skeleton (ids per contract)
web/src/main.tsx, web/src/App.tsx
web/src/api/types.gen.ts                # GENERATED from monitor-meta.schema.json (committed; drift-gated)
web/src/api/client.ts                   # REST calls  ·  web/src/api/sse.ts  # EventSource → store
web/src/store.ts                        # zustand: meta/series/events/ui state + reducers
web/src/retirement.ts                   # PID-trace retirement policy (pure, vitest'd)
web/src/components/{Header,TabBar,ChartGrid,ChartPanel,EventToolbar,EventPopover}.tsx
web/src/plotly.ts                       # thin wrapper over plotly.js-gl2d-dist-min
web/src/theme.ts                        # light/dark + localStorage
web/src/__tests__/*.test.ts             # vitest
scripts/check_airgap.sh                 # grep dist/ for external URLs
scripts/gen_web_types.sh                # make schema → json-schema-to-typescript
src/otto/monitor/server.py              # dist-preferred serving; post-cutover: dist-required
src/otto/monitor/collector.py + models/monitor.py   # MonitorMeta.interval addendum (Task 4)
src/otto/models/jsonschema.py           # docstring bullet (Task 4)
Makefile (web, web-dev, web-install), Vagrantfile (node provisioner), .github/workflows/ci.yml
DELETED at cutover: src/otto/monitor/static/{dashboard.js,dashboard.css,dashboard.html,plotly.min.js}
```

Task briefs are intentionally leaner than Phase 1's: where the legacy behavior is the spec, the brief says "mirror `dashboard.js` §X" and the pin suite adjudicates. The SDD review loop verifies parity per task.

---

### Task 1: Node bootstrap (VM + Vagrantfile + .nvmrc)

**Files:** Modify `Vagrantfile` (dev provisioners), Create `.nvmrc`.
- [ ] Install Node 24 LTS on THIS VM via NodeSource: `curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt-get install -y nodejs` (needs network; report versions).
- [ ] Add the same to the Vagrantfile dev provisioning (own `dev-node` shell provisioner block, matching house style; comment: web-lane only — Python gates never need it). `ruby -c Vagrantfile`.
- [ ] `.nvmrc` containing `24`.
- [ ] Verify `node --version` (v24.x), `npm --version`. Commit `build(web): Node 24 toolchain — VM provisioning + .nvmrc`.

### Task 2: web/ scaffold + dist-preferred serving

**Files:** Create `web/*` scaffold; Modify `Makefile`, `.gitignore`, `src/otto/monitor/server.py`; Test `tests/unit/monitor/test_server.py`.
**Interfaces produced:** `make web-install` (npm ci), `make web` (vite build → `src/otto/monitor/static/dist/`), `make web-dev` (vite dev server proxying `/api` + `/static` to a running otto monitor, port from `OTTO_MONITOR_URL` env or default localhost:8080 target documented); server serves `dist/index.html` + `/static/dist/*` when `dist/index.html` exists, else legacy files (unchanged behavior).
- [ ] Scaffold: `npm create vite@latest` equivalent committed by hand (react-ts template, pinned deps; NO CDN references in index.html — air-gap). `vite.config.ts`: `base: "/static/dist/"`, `build.outDir: ../src/otto/monitor/static/dist`, `emptyOutDir: true`.
- [ ] Placeholder `App` renders `<h1>Otto Monitor</h1>` + `#tab-bar` div (enough for a smoke).
- [ ] `server.py`: `_STATIC_DIR/dist/index.html` exists → `dashboard()` returns it and static mount covers dist (it already mounts `_STATIC_DIR`); else legacy. Unit tests: both branches (tmp dist fixture via monkeypatched `_STATIC_DIR`? — mirror test_server.py's route-testing style; a `dist_dir` fixture writing a marker index.html).
- [ ] `.gitignore`: `src/otto/monitor/static/dist/` + `web/node_modules/`.
- [ ] Verify: `make web-install && make web` builds; `uv run pytest tests/unit/monitor/test_server.py -q` green; **legacy pins still green** (`uv run pytest tests/e2e/monitor/dashboard -q` — dist absent in test env? dist now EXISTS after make web → server serves the placeholder → browser pins FAIL. Therefore: pins run against legacy until cutover ⇒ `make web` output must NOT leak into test runs: delete dist after the smoke (`rm -rf src/otto/monitor/static/dist`) and note that until Task 9, `make web` is run only inside web-lane tasks. Add a `web-clean` target.)
- [ ] Commit `build(web): Vite+React+TS scaffold; make web lanes; dist-preferred serving`.

### Task 3: Wheel embedding spike + air-gap grep gate

**Files:** Modify `pyproject.toml` (uv_build config as discovered), Create `scripts/check_airgap.sh`, Modify `Makefile`.
- [ ] EMPIRICAL spike (the spec assumed hatchling; the backend is `uv_build`): `make web`, then `uv build`, then `unzip -l dist/*.whl | grep monitor/static/dist` — determine whether uv_build includes the gitignored dist. Read uv_build's config reference (`[tool.uv.build-backend]` keys) as installed; configure whatever include mechanism it offers. Deliverable: wheel demonstrably contains `dist/index.html` + assets. Document the mechanism in a pyproject comment.
- [ ] `scripts/check_airgap.sh`: fail if `grep -rE "https?://" src/otto/monitor/static/dist --include='*.js' --include='*.css' --include='*.html'` matches anything outside an allowlist (sourcemap `//# sourceMappingURL` is relative; license-comment URLs allowed via `-v` patterns — keep the allowlist explicit and short).
- [ ] Makefile: `web` target runs the check after build; add `wheel-check` target (build wheel, assert dist inside, run check_airgap) wired into `release`'s flow (before `build`), NOT into `coverage`.
- [ ] `make web-clean` after verification (Task 2 rule). Commit `build(web): wheel embeds dist (uv_build); air-gap grep gate`.

### Task 4: TS contract codegen + MonitorMeta.interval addendum

**Files:** Create `scripts/gen_web_types.sh`, `web/src/api/types.gen.ts` (committed); Modify `src/otto/models/monitor.py`, `src/otto/monitor/collector.py`, `src/otto/models/jsonschema.py` (docstring bullet), `docs/guide/editor-schemas.md` (row), `tests/unit/models/test_jsonschema.py` (monitor-meta drift assertion), `tests/e2e/monitor/dashboard/test_harness.py` (**sanctioned: META_KEYS += "interval"**), unit tests.
- [ ] Backend: `MonitorMeta.interval: float | None = None` (global collection interval, seconds). Collector: new constructor kwarg `interval_hint: float | None = None`? NO — simplest truthful source: `run()` records `self._global_interval = interval.total_seconds()` before looping; `get_meta_model()` emits it (None before run/historical). Unit test: after a short run, meta["interval"] == the configured seconds; historical → None.
- [ ] Pin evolution with comment (the ONLY test-file edit): `META_KEYS = {"hosts", "live", "metrics", "tabs", "interval"}`.
- [ ] `build_schemas` drift assertion (`"monitor-meta" in docs`), jsonschema module-docstring bullet, editor-schemas guide row.
- [ ] Codegen: `scripts/gen_web_types.sh` = `uv run otto schema export --out schemas && cd web && npx json-schema-to-typescript ../schemas/monitor-meta.schema.json -o src/api/types.gen.ts` (pin the npm dev-dep). `types.gen.ts` is COMMITTED; drift gate = regen + `git diff --exit-code web/src/api/types.gen.ts` inside `make web`.
- [ ] Verify: monitor units + harness pins green; `make docs` exit 0. Commit `feat(monitor): MonitorMeta.interval + generated TS wire types with drift gate`.

### Task 5: Store, SSE client, chrome skeleton, vitest

**Files:** Create `web/src/{store.ts,api/client.ts,api/sse.ts,theme.ts,components/Header.tsx,components/TabBar.tsx}`, `web/src/__tests__/store.test.ts`; Modify `web/src/App.tsx`, `web/package.json` (vitest), `Makefile` (`web-test`).
**Store shape (zustand):** `{ meta, series: Record<key, Point[]>, events, chartMap, activeTab, selectedHost, paused, connection: 'connecting'|'live'|'historical'|'disconnected', spanStartId, actions: {applyMeta, applyData, metricMsg, eventMsg, eventUpdated, eventDeleted, select…} }` — reducers mirror `dashboard.js`'s SSE handlers §startSSE/appendMetricPoint (data appended to series even while paused; charts frozen by UI, not by data — pinned behavior).
- [ ] vitest wired (`npm run test` / `make web-test`); store reducer tests: metric append (incl. paused-still-appends), event add/update/delete, disconnect transition, host-scoped key resolution (`seriesKey` fallback logic from dashboard.js).
- [ ] Header/TabBar render parity ids; status text derives from `connection`+`paused` exactly as legacy (`Live/Paused/Historical/Disconnected`); theme.ts ports `otto-theme` localStorage + body.light.
- [ ] SSE client: EventSource on `/api/stream`, dispatches to store; onerror → disconnected + span abandonment (mirror dashboard.js §src.onerror).
- [ ] Verify: `make web-test` green; `make web && make web-clean` builds. Commit `feat(web): zustand store + SSE client + chrome skeleton with parity ids (vitest)`.

### Task 6: Charts — Plotly wrapper, ChartGrid, live append

**Files:** Create `web/src/plotly.ts`, `web/src/components/{ChartGrid,ChartPanel}.tsx`, vitest for grouping logic.
- [ ] `plotly.ts`: import `plotly.js-gl2d-dist-min`; typed helpers `newPlot/react/extendTraces/relayout`. ChartPanel: one Plotly div per chart group, className `metric-plot`, section-divider heading + `.expand-btn` (expand/collapse parity incl. body.plot-expanded + Escape); traces built exactly as `buildMetricTraces` (names, `proc/` prefix strip, hovertemplates, meta text); layout as `buildLayout` INITIALLY legacy-faithful (height math ports as-is; Task 10 replaces it) with shapes/annotations from events.
- [ ] ChartGrid: tabs → `#tab-<id>` panels + `.tab-charts` containers from meta (lazy init on tab activation, as legacy); live metric messages extend traces via `extendTraces` when initialized (mirror appendMetricPoint's placeholder/group logic — port `initSeriesFromData` + chart_map grouping into a pure TS module with vitest tests).
- [ ] Manual check via `make web-dev` against a live `otto monitor` if lab free — OPTIONAL; primary verification is Task 9's pin run.
- [ ] Commit `feat(web): Plotly wrapper + chart grid with live SSE append (parity)`.

### Task 7: Events UI parity

**Files:** Create `web/src/components/{EventToolbar,EventPopover}.tsx`; extend store.
- [ ] Mark event, span start/end (button text `Start event`/`End event` + `active` class), popover on `plotly_clickannotation` (position clamped to viewport; save/delete/cancel; outside-click close), clear-all via `confirm()` then DELETE fan-out — all mirroring dashboard.js §Event bar/§popover with the same ids.
- [ ] vitest: span state machine; popover reducer. Commit `feat(web): event toolbar/span/popover/clear parity`.

### Task 8: Historical, export, pause, theme, disconnect parity

**Files:** Modify web components; no test-file changes.
- [ ] Historical: `meta.live=false` → body.historical, immediate chart render (no host gate), host-select single `historical` option, pause disabled, status `Historical`.
- [ ] Live: charts deferred until host selection (placeholder option `Select host`), pause toggle (freeze charts only; resume full refresh), export anchor href, page-title unchanged (`Otto Monitor`), disconnect state per pins.
- [ ] Verify: `make web-test`; build clean. Commit `feat(web): historical/export/pause/disconnect/theme parity`.

### Task 9: CUTOVER — pins are the acceptance gate

**Files:** Modify `src/otto/monitor/server.py` (dist REQUIRED: helpful RuntimeError "run `make web`" when missing — replaces legacy fallback), `tests/e2e/monitor/dashboard/conftest.py` (session guard: build presence check with the same helpful message), DELETE the four legacy static files; Modify anything referencing them (`grep -rn "dashboard.js\|plotly.min.js\|dashboard.css\|dashboard.html"` across repo — update docs references in guide/architecture pages).
- [ ] `make web` (leave dist in place from here on — it's the served frontend now).
- [ ] **Acceptance:** `uv run pytest tests/e2e/monitor/dashboard -q` — ALL 21+ pins green against the React build, UNMODIFIED. Any failure = parity bug in Tasks 5-8: fix forward in the web code, never in the pins. Run 3x.
- [ ] Also `uv run pytest tests/unit/monitor -q` (server dist-required tests updated in this task — sanctioned).
- [ ] Commit `feat(web)!: cut over to the React dashboard; legacy static assets removed`.

### Task 10: Height-growth fix + trace retirement

**Files:** Create `web/src/retirement.ts` + vitest; Modify ChartPanel layout math; ADD Playwright pin `tests/e2e/monitor/dashboard/test_dashboard_regressions.py` (new file, hostless+browser+xdist_group markers).
**Policy (concrete):** chart div height CONSTANT (a fixed total, legacy CHART_AREA 160 + fixed top/bottom margins; legend capped to 2 rows worth of entries); per-process (`proc/*`) traces are RETIRED from the chart when their PID is absent from the latest K=3 consecutive ticks (store data retained for export); retired traces leave the legend. Non-proc series never retire.
- [ ] vitest: retirement transitions (appear/persist/retire/reappear), cap ordering (top by latest value when over legend budget).
- [ ] New Playwright pin: pump 100 churning fake PIDs via `live_dash` pushes, assert `.metric-plot` clientHeight CONSTANT (before == after) and legend entries ≤ budget. 3x flake check.
- [ ] Commit `fix(web): constant chart heights via PID-trace retirement (K=3) + legend cap`.

### Task 11: Resize + Safari fixes; WebKit lane

**Files:** ChartPanel (`responsive: true` + ResizeObserver relayout), CSS containment for the modebar; ADD pins in `test_dashboard_regressions.py`; Makefile/CI: `playwright install webkit` (+ `--with-deps` in CI); browsers target gains webkit.
- [ ] Resize pin: set viewport 1200→800, assert plot width follows container (Plotly `gd._fullLayout.width` or clientWidth) — chromium.
- [ ] Safari pin: same smoke + modebar-containment assertion (modebar bounding box within plot container) running under `--browser webkit` — parametrize or a dedicated webkit test (check pytest-playwright's browser parametrization; keep it one test marked to run on webkit via `--browser` in a dedicated make/nox invocation if per-test browser selection is awkward — document the chosen mechanism).
- [ ] VM: `uv run playwright install webkit` + `install-deps` (sudo apt fallout — report what it installs). Commit `fix(web): responsive resize + Safari modebar containment; WebKit pin lane`.

### Task 12: Air-gap offline pin + wheel finalization

**Files:** ADD offline pin (new test in test_dashboard_regressions.py using a browser context with `route` blocking all non-localhost); verify `make wheel-check` end-to-end now that dist is real.
- [ ] Offline pin: block every request whose host isn't 127.0.0.1, load dashboard, assert charts render (trace data present) and zero blocked-request violations recorded.
- [ ] `make wheel-check` green; `scripts/check_airgap.sh` green on the real bundle. Commit `test(web): offline render pin; wheel/air-gap gates green on real bundle`.

### Task 13: CI + docs

**Files:** `.github/workflows/ci.yml` (dashboard job: setup-node with node-version-file, `npm ci`, `make web` incl. type-drift + airgap gates, `make web-test`, then the browser suite; webkit install), `noxfile.py` dashboard session note (browser suite unchanged — Node steps live in CI job, document why), `docs/guide/monitor.md` (frontend development section: make web-dev workflow, parity contract note), `docs/contributing.md` (Node prerequisite for web lane), `docs/architecture/monitoring-and-coverage.md` refresh if it describes the old frontend.
- [ ] `make docs` exit 0; lint clean. Commit `ci/docs(web): dashboard job builds+tests the web lane; frontend dev guide`.

### Task 14: Full gates

- [ ] `make coverage` (full tier; dashboard lane now runs the React build), `nox -s typecheck`, `nox -s lint`, `make docs` (exit code), `make profile`, `make web-test`, `make wheel-check`. Fix-forward with `fix:` commits; report the gate table.

## Verification checklist (whole plan)

- Phase 0/1 pin suite green against the React frontend, unmodified except the two sanctioned additions lists.
- The three known bugs demonstrably fixed by pins: height constant under churn, resize follows viewport, WebKit modebar contained.
- `make wheel-check`: wheel contains dist; no external URLs; offline pin green.
- Full gate table green.

## Out of scope (Phase 3+)

New metric families (Phase 3); UX backlog (Phase 4: date picker, import/clear, collapse-all, `--historical`, default DB path); per-plot custom frequencies UI; anomaly detection; JS plugin API (never, per spec).
