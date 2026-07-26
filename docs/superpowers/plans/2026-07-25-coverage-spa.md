# Coverage SPA (Plan C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Jinja coverage report with a React SPA (`web/src/covapp/`) that renders from
Python-emitted classic-script data chunks, works at `file://` and behind Jenkins' minimal CSP, and
has functional + aesthetic parity with the otto monitor.

**Architecture:** A second Vite app in the existing `web/` workspace builds a **classic IIFE
bundle** (ES modules do not load over `file://`) into `src/otto/coverage/renderer/static/covapp/`.
A new Python `SpaRenderer` copies that prebuilt bundle into the report dir and emits
`cov_data/index.js` (tree + per-directory rollups + runs + config, precomputed in Python) plus one
lazy `cov_data/files/<mangled>.js` chunk per source file. `CoverageReporter.run` swaps
`HtmlRenderer` → `SpaRenderer`; the Jinja code stays in-tree (dead) until Plan D deletes it.

**Tech Stack:** React 19 + wouter (`useHashLocation`), react-aria-components (Tree), vendored
Untitled UI components, Tailwind 4 CSS-first tokens, Shiki (JS-regex engine, C/C++ grammars only),
vitest + Playwright (pytest side), Python stdlib emitter (no new Python deps).

**Spec:** `docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md` §2–§7, §10, §11.
**Approved layout reference (binding):** the three interactive mockups in
`docs/superpowers/specs/assets/2026-07-24-coverage-ui/` — `directory-page.html`,
`file-page.html`, `contexts-page.html`. They pin grid columns, class vocabulary, chip/pill
anatomy, and interaction. One deviation is authoritative: the spec (§4) moved the coverage key
into the ⋮ overflow menu on **every** page (the directory mockup's note about a floating key
panel is outdated; `file-page.html`/`contexts-page.html` show the final ⋮-menu placement).

## Global Constraints

- **Delivery (spec §2):** the emitted report must work from `file://` and any URL subpath. No ES
  module scripts, no inline `<script>`, no `eval`, no WASM, no network fetches (fonts bundled).
  All asset references relative (`./…`).
- **Documented minimal CSP (spec §2, exact string used by docs and the served test lane):**
  `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'`
- **Component sourcing (spec §5):** vendored UUI first (`@/components/...`), then existing
  `@/ui/**`, new `ui/**` components ONLY `TreeView` and `CodeView`. Never edit anything under
  `web/src/components/**` (vendored, byte-exact, drift-checked). covapp-local helpers (e.g. a
  small toast) live under `web/src/covapp/`, not `ui/`.
- **Dark mode:** reuse `web/src/theme.ts` verbatim (localStorage key `otto-theme`, single
  `.dark-mode` class on `<html>`, pre-paint module side effect). Semantic tokens only; no `dark:`
  variants in authored code.
- **Thresholds (spec §4):** `pct >= high → "pct-high"`, `pct >= medium → "pct-mid"`, else
  `"pct-low"`; values come from the report data (store v4 `thresholds`), never hard-coded.
- **Row precedence (verbatim from today):** excluded > highest-precedence tier with a hit
  (tier_order index 0 first) > aging > stale > uncovered; a line with no LineRecord is
  **uncoverable** (muted, never red).
- **store.json schema is FROZEN** — Plan B shipped v4; Plan C adds no store keys. The report
  stamp lives only in the emitted chunks.
- **CLI flags unchanged** — `--report` keeps its name (rename to `--dir` is Plan D). The
  empty-report contract (exit 1 naming searched locations) is untouched.
- **Jinja lane untouched except the one-line reporter swap** — `html_renderer.py`, templates,
  `report.css`, `web/src/covreport/` and its vite config all stay as-is (Plan D deletes them).
  Their unit tests keep passing because they construct `HtmlRenderer` directly.
- **Web gates:** every task that touches `web/` must pass `npm run check` (biome),
  `npm run knip`, `npm run typecheck` (tsc), and `npm run test` (vitest, console-guard active —
  any unexpected `console.warn/error` fails the test) from `web/`. Builds go through
  `scripts/build_web_no_warnings.sh` (any Vite warning, incl. chunk-size, is a hard failure).
- **Python gates:** `uv run ruff check` + `uv run ruff format --check` on touched files; scoped
  pytest per task; `from __future__ import annotations` is banned repo-wide.
- **Makefile naming:** quality targets follow the language-parity convention (`lint-ts`,
  `typecheck-ts`, …) — no new `web-*` aliases. Do not create another hand-kept copy of the
  browser marker expression `-m "browser and not soak"` (two copies exist: Makefile + noxfile
  `DASHBOARD_MARKER_EXPR`; reuse those).
- **Commits:** one per task minimum, conventional prefix, trailer `Assisted-by: Claude Fable 5`.
- **Data-chunk format constant:** `OTTO_COV_DATA_FORMAT = 1` (Python) ==
  `EXPECTED_DATA_FORMAT = 1` (TS). Bump both together or never.
- Known data limitation (document, don't fake): per-run **branch** contribution is not stored in
  v4 (`run_hits` is per-line only). The runs page renders Branch/Decision contribution as
  "not tracked per-run" (muted). Focus mode filters **line** stats only; branch cells show "—"
  while focused.

## File Structure

**Python (new):**
- `src/otto/coverage/renderer/spa_data.py` — pure data-chunk emitter (payload build + JS file
  writing). No Jinja, no jinja2 import.
- `src/otto/coverage/renderer/spa_renderer.py` — `SpaRenderer` (copy bundle + call emitter),
  duck-type compatible with `HtmlRenderer`.
- `src/otto/coverage/reporter.py` — one construction-site swap.

**Web (new):**
- `web/vite.covapp.config.ts` — covapp build (classic IIFE, relative base, outDir
  `../src/otto/coverage/renderer/static/covapp`, assets under `dist/`).
- `web/covapp.html` — entry HTML (renamed to `index.html` at build time).
- `web/src/covapp/` — `main.tsx`, `App.tsx`, `types.ts`, `data.ts`, `stats.ts`, `focus.ts`,
  `format.ts`, `Toast.tsx`, `chrome/AppShell.tsx`, `chrome/StatsCard.tsx`,
  `chrome/ShortcutsDialog.tsx`, `pages/DirectoryPage.tsx`, `pages/FilePage.tsx`,
  `pages/RunsPage.tsx`, `pages/GuardScreen.tsx`, `covapp.css`, plus co-located `*.test.ts(x)`.
- `web/src/ui/TreeView.tsx`, `web/src/ui/CodeView.tsx` (+ tests) — the two new shared components.

**Tests (Python side):**
- `tests/unit/cov/test_spa_data.py`, `tests/unit/cov/test_spa_renderer.py` — new.
- `tests/_fixtures/_report_fixture.py` — grows multi-host/stale/aging+remapped runs + excluded
  lines.
- `tests/e2e/cov/report_browser/` — conftest updated; `test_report_index.py`/`test_report_file.py`
  replaced by `test_spa_index.py`, `test_spa_file.py`, `test_spa_runs_focus.py`, `test_spa_csp.py`.
- `tests/_fixtures/_csp_server.py` — new threaded static server with CSP header.

**Build/CI:** `Makefile` (web lane, wheel-check, clean), `web/package.json` (script + shiki dep),
`.gitignore`, `noxfile.py` (dashboard session gains the report_browser path),
`web/scripts/e2e_coverage_report.mjs` (one dists entry), `scripts/capture_docs_media.py`.

**Docs:** `docs/guide/coverage.md`, `docs/architecture/subsystems/coverage.md`.

---

## The data contract (consumed by every task — copied into each task's Interfaces)

`cov_data/index.js` (classic script, loaded by `index.html` BEFORE the app bundle):

```js
window.__OTTO_COV__ = {/* JSON payload, keys below */};
```

```
IndexPayload = {
  "format": 1,                          // OTTO_COV_DATA_FORMAT
  "stamp": "20260725T140200Z-1a2b3c4d", // UTC time + uuid4().hex[:8]
  "generated_at": "2026-07-25 14:02 UTC",
  "otto_version": "0.7.5",
  "project_name": "otto example product",
  "tier_order": ["system", "unit"],     // precedence order, index 0 = highest
  "tier_labels": {"system": "System (e2e)", ...},
  "tier_colors": {"system": "green", ...},   // store.tier_colors resolved w/ renderer defaults
  "state_colors": {"uncovered": "#f4a9a8", "excluded": "grey", "stale": "violet", "aging": "tan"},
  "thresholds": {"high": 80.0, "medium": 70.0},
  "stat_types": ["line", "branch", "decision"],
  "runs": [RunJson, ...],               // RunRecord.to_dict() verbatim (id/tier/label/board/
                                        // host/labs/captured_at/tester/ticket/note/base_commit/
                                        // dirty_remap/aging)
  "run_contrib": {"<run_id>": {"lines": int, "revoked": int,
                               "files": [["display/path.c", int], ...]}},  // desc by count
  "total_lines": int,                   // repo-wide coverable line count
  "tree": DirNode,                      // root; name == project_name
}
DirNode  = {"name": str, "dirs": [DirNode...], "files": [FileNode...], "stats": Stats}
FileNode = {"name": str, "path": "display/rel/path.c", "chunk": "<mangled>", "stats": Stats}
Stats = {
  "lines":    {"total": int, "hit": int, "per_tier": {tier: int}},
  "branches": {"total": int, "hit": int, "per_tier": {tier: int}},
  "flags":    {"stale": int, "aging": int, "excluded": int},
  "ctx_lines": {"<run-label>": int},    // hit lines credited to any run with that label,
                                        // within this node — powers the focus filter
}
```

`cov_data/files/<mangled>.js` (classic script, injected on navigation):

```js
window.__OTTO_COV_FILE__({/* FileChunk */});
```

```
FileChunk = {
  "stamp": "<same as index>",           // mismatch => regenerate screen
  "chunk": "<mangled>",
  "path": "display/rel/path.c",
  "source": "<full text, read with errors='replace'>",
  "lines": {"<lineno>": LineJson, ...}, // may contain linenos past EOF (shrunk files)
  "excluded": [int, ...],               // sorted
}
LineJson = {"hits": {tier: int}, "branches": [BranchJson...], "state": "stale"|"aging"|null,
            "run": {"<run_id>": int}?, "stale_run": [int]?}      // optional keys as in store v4
BranchJson = {"block": int, "branch": int, "hits": {tier: int}, "reachable": {tier: bool}}
```

`<mangled>` = `str(record.path).replace("/", "_").replace("\\", "_").lstrip("_")` — the same
scheme `HtmlRenderer._file_link` uses (full canonical path, NOT the display path; prefix only
affects display strings, never keys/filenames).

Report dir layout produced by `SpaRenderer`:

```
<report>/index.html            <report>/dist/covapp.js   <report>/dist/covapp.css
<report>/cov_data/index.js     <report>/cov_data/files/<mangled>.js ...
<report>/store.json            (written by the reporter afterwards, as today)
```

The bundle asset dir is named `dist/` deliberately: `web/scripts/e2e_coverage_report.mjs`
resolves served/file URLs back to local files by the last `/dist/` path segment.

---

### Task 1: Python data-chunk emitter (`spa_data.py`)

**Files:**
- Create: `src/otto/coverage/renderer/spa_data.py`
- Test: `tests/unit/cov/test_spa_data.py`

**Interfaces:**
- Consumes: `CoverageStore` / `RunRecord` / `LineRecord` / `Thresholds` from
  `otto.coverage.store.model`; `scan_excluded_lines` from `otto.coverage.exclusions`;
  `STATE_COLORS`, `DEFAULT_TIER_COLORS` from `otto.coverage.colors`; `get_version` from
  `otto.version`.
- Produces (Task 2+8 rely on these exact names):
  - `OTTO_COV_DATA_FORMAT: int = 1`
  - `make_stamp() -> str`
  - `mangle_path(path: Path) -> str`
  - `build_index_payload(store, *, project_name: str, prefix: Path | None, stamp: str) -> dict`
  - `emit_chunks(store, output_dir: Path, *, project_name: str, prefix: Path | None, extra_markers: list[str] | None, stamp: str) -> None`
    — writes `cov_data/index.js` + every file chunk AND annotates each
    `FileRecord.excluded_lines` (so the reporter's later `store.save()` persists them, exactly
    like `HtmlRenderer` does today).

**Details that are policy, not preference:**
- Display path: `record.path.relative_to(prefix)` with `ValueError` → full path fallback
  (mirror `HtmlRenderer._display_path`).
- Tier labels: copy the small `TIER_LABELS` dict + title-case fallback from `html_renderer.py`
  into `spa_data.py` (do NOT import from `html_renderer` — that module dies in Plan D).
- Tier colors: `store.tier_colors.get(t) or DEFAULT_TIER_COLORS.get(t, "green")` (same
  fallback quirk as `HtmlRenderer._resolve_tier_colors`; keyed by name here since kind is
  unavailable — identical to today's effective behavior for store-populated tiers).
- Tree building: group files by display-path parts; per-node `Stats` aggregates: line
  total/hit (a line is hit if `lr.hits.is_hit()`), per-tier hit counts, branch totals (branch
  hit = any tier count > 0), flags (stale = lines with `state=="stale"`, aging likewise,
  excluded = `len(excluded_lines)` from the scan), and `ctx_lines` per run label (a line
  credits label L if any run id in `lr.run_hits` with hits > 0 belongs to a run labeled L).
  Lines past EOF still count in stats (out-of-range tolerance — pin it).
- `run_contrib`: per run id — `lines` = count of LineRecords with `run_hits[id] > 0`,
  `revoked` = count with `id in stale_runs`, `files` = per-display-path counts sorted desc.
- JS emission: `f"window.__OTTO_COV__ = {json.dumps(payload)};\n"` and
  `f"window.__OTTO_COV_FILE__({json.dumps(chunk)});\n"`. `json.dumps` default `ensure_ascii`
  keeps the files ASCII-safe; chunks are external `.js` files so no `</script>` escaping is
  needed.
- Source read: `record.path.read_text(errors="replace")`, `OSError` → warn + `source=""`
  (mirror `HtmlRenderer._render_file`).
- Exclusions: `scan_excluded_lines(source_text, extra_markers or None)`, then
  `record.excluded_lines = excluded_linenos`.

- [ ] **Step 1: Write failing tests** (`tests/unit/cov/test_spa_data.py`) — follow the style of
  `tests/unit/cov/test_renderer.py` (tmp_path stores built by hand). Cover at minimum:

```python
class TestIndexPayload:
    def test_format_and_stamp_and_config_keys(self, tmp_path): ...
        # build store w/ 2 tiers, thresholds Thresholds(90, 75); payload["format"] == 1,
        # payload["thresholds"] == {"high": 90.0, "medium": 75.0},
        # payload["stat_types"] == ["line", "branch", "decision"],
        # payload["state_colors"]["stale"] == "violet"

    def test_tree_rollup_and_ctx_lines(self, tmp_path): ...
        # two files a/x.c (2 hit lines by run "r1") and a/b/y.c (1 hit, 1 miss);
        # root.stats.lines == {total 4, hit 3}; dir "a" contains dir "b";
        # stats["ctx_lines"]["r1"] == 3 at root

    def test_run_contrib_lines_revoked_topfiles(self, tmp_path): ...
    def test_display_path_prefix_strip_and_fallback(self, tmp_path): ...
    def test_out_of_range_line_counts_without_crash(self, tmp_path): ...
        # LineRecord at 999 on a 3-line file: emit_chunks succeeds, chunk carries "999",
        # stats count it

class TestEmitChunks:
    def test_index_js_is_classic_assignment(self, tmp_path): ...
        # text startswith "window.__OTTO_COV__ = {" and endswith "};\n"
    def test_file_chunk_wraps_call_and_matches_store_line_json(self, tmp_path): ...
        # json.loads of the slice between "(" and ");" round-trips; "run"/"stale_run"
        # keys present/absent exactly as in store save()
    def test_excluded_lines_annotated_on_store(self, tmp_path): ...
        # LCOV_EXCL_LINE marker in source → record.excluded_lines populated after emit
```

- [ ] **Step 2:** `uv run pytest tests/unit/cov/test_spa_data.py -x -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `spa_data.py` per the contract above.
- [ ] **Step 4:** Tests pass; run `uv run pytest tests/unit/cov -q` (no regressions) and
  `uv run ruff check src/otto/coverage/renderer/spa_data.py tests/unit/cov/test_spa_data.py`.
- [ ] **Step 5:** Commit `feat(cov): SPA data-chunk emitter (cov_data/index.js + per-file chunks)`.

---

### Task 2: covapp scaffold — classic-script Vite build + data boot layer

**Files:**
- Create: `web/vite.covapp.config.ts`, `web/covapp.html`, `web/src/covapp/main.tsx`,
  `web/src/covapp/App.tsx`, `web/src/covapp/types.ts`, `web/src/covapp/data.ts`,
  `web/src/covapp/pages/GuardScreen.tsx`, `web/src/covapp/covapp.css`,
  `web/src/covapp/data.test.ts`
- Modify: `web/package.json` (scripts + `shiki` dep), `Makefile` (`web`/`web-clean`/dist vars),
  `.gitignore` (`src/otto/coverage/renderer/static/covapp/`)

**Interfaces:**
- Consumes: the data contract above; `web/src/theme.ts` (`loadTheme`/`applyTheme`).
- Produces (Tasks 3–7 rely on):
  - `types.ts`: TS mirrors of `IndexPayload`, `Stats`, `DirNode`, `FileNode`, `RunJson`,
    `FileChunk`, `LineJson`, `BranchJson` (exact field names above; `EXPECTED_DATA_FORMAT = 1`).
  - `data.ts`: `getIndex(): IndexPayload | null` (validated `window.__OTTO_COV__`),
    `dataGuard(): "ok" | "missing" | "format"`,
    `loadFileChunk(chunk: string): Promise<FileChunk>` — injects
    `<script src="./cov_data/files/<chunk>.js">`, resolves via the registered
    `window.__OTTO_COV_FILE__` callback, caches per chunk, rejects on `onerror` or
    stamp mismatch (`StampMismatchError`).
  - Route table (App.tsx): `#/coverage`, `#/coverage/<path…>`, `#/runs`, fallback NotFound —
    pages arrive as placeholders here; Tasks 4–6 fill them.

**Vite config — the load-bearing part (write exactly this shape):**

```ts
// web/vite.covapp.config.ts
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";

/** file:// cannot load ES modules; Jenkins CSP forbids inline scripts. Emit one classic
 *  IIFE script + strip module attributes from the HTML Vite generates. */
function classicScript(): Plugin {
  return {
    name: "otto-classic-script",
    enforce: "post",
    generateBundle(_opts, bundle) {
      const html = bundle["covapp.html"];
      if (html && html.type === "asset") {
        html.fileName = "index.html";
        html.source = String(html.source)
          .replaceAll(' type="module"', " defer")
          .replaceAll(' crossorigin', "");
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), classicScript()],
  base: "./",
  resolve: { alias: { "@": resolve(__dirname, "./src") } },
  build: {
    outDir: "../src/otto/coverage/renderer/static/covapp",
    emptyOutDir: true,
    sourcemap: "hidden",
    chunkSizeWarningLimit: 2_000, // bundle-size ceiling (spec §10) — warnings are build failures
    rollupOptions: {
      input: resolve(__dirname, "covapp.html"),
      output: {
        format: "iife",
        inlineDynamicImports: true,
        entryFileNames: "dist/covapp.js",
        assetFileNames: "dist/covapp.[ext]",
      },
    },
  },
});
```

`web/covapp.html` (no inline scripts; data chunk loads before the app):

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>otto coverage</title>
    <script src="./cov_data/index.js" defer></script>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/covapp/main.tsx"></script>
  </body>
</html>
```

(The `type="module"` tag is Vite's dev/build entry; `classicScript()` rewrites it to `defer` in
the emitted HTML. Script order is preserved: `cov_data/index.js` executes before
`dist/covapp.js` because both are `defer` and document order rules.)

**data.ts contract details:**
- `window.__OTTO_COV_FILE__` is registered once at module init; it looks up the pending
  resolver by `chunk` id (chunks self-identify — concurrent loads stay correct).
- Guard logic: `missing` when `window.__OTTO_COV__` is undefined (data script absent/failed);
  `format` when `payload.format !== EXPECTED_DATA_FORMAT`. `GuardScreen` renders a friendly
  "This report needs to be regenerated — run `otto cov report`" card
  (`data-testid="guard-screen"`, reason line varies: missing data / format / stamp mismatch).
  App.tsx renders GuardScreen instead of the router whenever the guard is not `"ok"`;
  `StampMismatchError` from a lazy chunk load routes the file page to the same screen.
- `main.tsx`: `applyTheme(loadTheme())` module side effect (pre-paint), then
  `createRoot(...).render(<StrictMode><App/></StrictMode>)`.
- `covapp.css`: `@import "../styles/theme.css";` first, then an `@theme` block overriding
  `--color-brand-*` with otto violet (`#7c5cff` at 500) — copy the override list from
  `web/src/app.css` so `scripts/check_brand_tokens.sh` passes against the covapp CSS.

**package.json / Makefile wiring:**
- `web/package.json` scripts: `"build:covapp": "vite build --config vite.covapp.config.ts"`;
  dependency: `"shiki"` (latest 3.x; used in Task 5 but installed here so the lockfile churns
  once).
- `Makefile`: add `COVAPP_DIST := src/otto/coverage/renderer/static/covapp/index.html`; extend
  the `web` target: `scripts/build_web_no_warnings.sh build:covapp`, then
  `scripts/check_airgap.sh src/otto/coverage/renderer/static/covapp` and
  `scripts/check_brand_tokens.sh src/otto/coverage/renderer/static/covapp`; add the covapp dir
  to `web-clean`; add `$(COVAPP_DIST)` to the grouped `&:` build rule alongside
  `$(DASHBOARD_DIST) $(COVREPORT_DIST)`.

- [ ] **Step 1: Failing vitest** — `web/src/covapp/data.test.ts`: guards (`missing`/`format`/ok),
  `loadFileChunk` resolve-by-callback + cache + stamp mismatch rejection (jsdom: fake the
  script injection by invoking `window.__OTTO_COV_FILE__` manually after intercepting
  `appendChild`).
- [ ] **Step 2:** `cd web && npx vitest run src/covapp` → FAIL.
- [ ] **Step 3:** Implement types.ts / data.ts / GuardScreen / App skeleton / main.tsx / css /
  configs; `npm install shiki` (lockfile).
- [ ] **Step 4:** vitest green; `npm run typecheck && npm run check && npm run knip` green
  (knip: covapp entry needs registering in `web/knip.json` `entry` if knip flags it; shiki to
  `ignoreDependencies` until Task 5 uses it). Add `"src/covapp/main.tsx"` to
  `vite.config.ts` `test.coverage.exclude` (bootstrap entrypoint, same policy as
  `src/main.tsx` / `src/covreport/main.ts`).
- [ ] **Step 5:** `make web` from repo root — builds all three lanes; verify
  `src/otto/coverage/renderer/static/covapp/index.html` exists, contains **no**
  `type="module"` and no inline `<script>` (`grep -c '<script' … == expected external refs`).
- [ ] **Step 6:** Manual smoke: emit chunks for a toy store into a tmp dir with Task 1's
  `emit_chunks` + copy the bundle, open `index.html` via `file://` (headless chromium ok) —
  guard screen renders when `cov_data/index.js` is deleted.
- [ ] **Step 7:** Commit `feat(web): covapp scaffold — classic-script Vite lane + data boot layer`.

---

### Task 3: Shared chrome — AppShell, StatsCard, thresholds/meta plumbing

**Files:**
- Create: `web/src/covapp/chrome/AppShell.tsx`, `web/src/covapp/chrome/StatsCard.tsx`,
  `web/src/covapp/chrome/ShortcutsDialog.tsx`, `web/src/covapp/stats.ts`,
  `web/src/covapp/format.ts`, `web/src/covapp/Toast.tsx`, tests
  (`stats.test.ts`, `StatsCard.test.tsx`, `AppShell.test.tsx`)
- Modify: `web/src/covapp/App.tsx` (wrap routes in AppShell)

**Interfaces:**
- Consumes: `types.ts`/`data.ts` (Task 2); vendored `Dropdown`
  (`@/components/base/dropdown/dropdown`), `ButtonUtility`
  (`@/components/base/buttons/button-utility`), `@/ui/Breadcrumbs`.
- Produces (Tasks 4–7 rely on):
  - `stats.ts`: `pct(hit, total): number | null`, `pctClass(p, thresholds): "pct-high" | "pct-mid" | "pct-low" | "pct-na"`, `fmtPct(p): string` (one decimal, `"—"` for null),
    `nodeStats(node): Stats`, `findNode(tree, segments): DirNode | FileNode | null`.
  - `AppShell` props: `{ crumbs: Crumb[], title: ReactNode, meta: ReactNode, stats: StatsCardProps, children }` — renders app bar (brand `⬡ otto coverage · <project_name>`,
    focus chip slot [wired in Task 7], theme toggle, ⋮ menu), header grid, page body.
  - ⋮ menu contents (mockup `file-page.html` lines 178–197 are the DOM reference): "Keyboard
    shortcuts" item (opens ShortcutsDialog), separator, [Focus context section added in
    Task 7], separator, coverage key as informational rows — tier swatches from
    `tier_colors`, state swatches from `state_colors`, branch pill legend.
  - `StatsCard` props: `{ scope: string, title: string, rows: TierStatRow[] }` where
    `TierStatRow = { key, label, dotColor?, line: [hit,total], branch: [hit,total], decision: [hit,total] | null }` — renders the tier × Line/Branch/Decision matrix + "All tiers" row;
    decision `null` → muted "no data" (`pct-na`).
- Testids to pin: `stats-card`, `stats-row-<tier>`, `stats-row-all`, `appbar-menu`,
  `menu-shortcuts`, `theme-toggle`, `page-meta`.
- `ShortcutsDialog` content is deliberately minimal (spec §12.4 defers bindings): it lists
  exactly the two bindings this plan implements — `?` opens the dialog, `Esc` closes it —
  rendered with `@/ui/Kbd`. Wire `?` via a plain `keydown` listener in AppShell (ignore when
  focus is in an input). Nothing else; no dead rows for unimplemented bindings.

Threshold coloring, `fmtPct`, and the matrix layout must match the mockups' `.pct/.frac/.hi/.mid/.lo/.na` semantics (Tailwind utility classes on semantic tokens; the mockup CSS is a
rendering reference, not literal CSS to copy).

- [ ] **Step 1: Failing vitest** — `stats.test.ts`: `pctClass` boundaries at high/medium exactly
  (`80 → pct-high`, `79.9 → pct-mid`, `70 → pct-mid`, `69.9 → pct-low`, null → `pct-na`) with
  thresholds from payload (NOT constants); `findNode` walks dirs/files, returns null for
  unknown. `StatsCard.test.tsx`: renders per-tier rows + All tiers, decision "no data" when
  null. `AppShell.test.tsx`: menu opens, shortcuts item present, key rows show tier labels.
- [ ] **Step 2:** vitest FAIL → implement → vitest green.
- [ ] **Step 3:** `npm run typecheck && npm run check && npm run knip && npm run test` green.
- [ ] **Step 4:** Commit `feat(web): covapp shared chrome — app bar, ⋮ menu with coverage key, stats card`.

---

### Task 4: `ui/TreeView` + Directory page

**Files:**
- Create: `web/src/ui/TreeView.tsx`, `web/src/ui/TreeView.test.tsx`,
  `web/src/covapp/pages/DirectoryPage.tsx`, `web/src/covapp/pages/DirectoryPage.test.tsx`
- Modify: `web/src/covapp/App.tsx` (route `#/coverage/*` → DirectoryPage when the path
  resolves to a dir via `findNode`, NotFound when it resolves to nothing; paths resolving to a
  FileNode keep the Task 2 placeholder until Task 5 swaps in FilePage)

**Interfaces:**
- Consumes: Task 3 (`stats.ts`, AppShell/StatsCard), Task 2 types; react-aria-components
  `Tree`/`TreeItem` (same import style as existing `ui/` components); vendored `Badge`.
- Produces: `TreeView` shared component —
  `TreeView<T>({ roots, getChildren, getRowId, columns, renderName, onNavigate, sort, defaultExpanded })`:
  generic expandable tree with a sortable column header row (numeric per sibling group) and
  grid-aligned stat cells; owned by `ui/` (react-aria Tree, token-styled), reusable outside
  covapp.
- DOM/layout reference: `directory-page.html` — grid
  `minmax(210px,1fr) 92px 74px 74px <64px × tier> 170px`; columns Name · Lines (hit/total) ·
  Line % (value + threshold minibar) · Branch % (same) · one % column per tier · flag badges
  (`N stale` / `N aging` / `N excl`). Chevron toggles expand; clicking a dir name drills
  (route + crumbs + stats card re-scope); clicking a file navigates to its file route. Root
  page appends the collapsed "Runs & captures" disclosure (`@/ui/Disclosure`) with the summary
  table (Run/Tier/Board/Labs/Date/Tester/Ticket/Remap — `✎ remapped` from `dirty_remap`).
- Testids: `tree-row-<name>`, `tree-col-<id>`, `runs-disclosure`.
- Directory meta line (AppShell `meta` prop): `<b>N</b> covered files · report generated
  <b>{generated_at}</b> · otto {otto_version}` (file count = FileNodes under the current node).

- [ ] **Step 1: Failing vitest** — TreeView: expands/collapses, sort toggles asc/desc and
  re-orders sibling groups numerically, onNavigate fires for name clicks only.
  DirectoryPage: renders rollup numbers from a fixture payload, drill re-scopes (wouter
  memory location), tier columns appear in `tier_order` order, flags render only when > 0,
  runs disclosure only at root.
- [ ] **Step 2:** FAIL → implement → green.
- [ ] **Step 3:** Full web gates green (`check`, `knip`, `typecheck`, `test`).
- [ ] **Step 4:** Commit `feat(web): TreeView ui component + covapp directory page`.

---

### Task 5: `ui/CodeView` + Shiki + File page

**Files:**
- Create: `web/src/ui/CodeView.tsx`, `web/src/ui/CodeView.test.tsx`,
  `web/src/covapp/highlight.ts`, `web/src/covapp/highlight.test.ts`,
  `web/src/covapp/pages/FilePage.tsx`, `web/src/covapp/pages/FilePage.test.tsx`

**Interfaces:**
- Consumes: Tasks 2–3; `shiki` — fine-grained imports only:
  `createHighlighterCore` from `shiki/core`, `createJavaScriptRegexEngine` from
  `shiki/engine/javascript`, grammars `shiki/langs/c.mjs` + `shiki/langs/cpp.mjs`, themes
  `shiki/themes/github-light.mjs` + `shiki/themes/github-dark.mjs`. **Never** the full
  `shiki` bundle entry (WASM/oniguruma is forbidden by the CSP/no-WASM rule and blows the
  size ceiling).
- Produces: `CodeView` shared component — props
  `{ lines: CodeLine[], language, header: ReactNode, columns: GutterCol[], renderExpansion? }`
  where `GutterCol = { id: string, width: string, header: ReactNode }` (widths compose the
  grid template) and `CodeLine = { number, html: string /* shiki-highlighted */, rowClass, cells: ReactNode[], expandable?: boolean }`; sticky column-header row; the reserved ticket gutter
  is a zero-width first grid column (`grid-template-columns` starts `0px 46px …`) —
  collapsed until per-ticket plumbing exists (spec non-goal).
- `highlight.ts`: `highlightLines(source: string, lang: "c" | "cpp" | "text"): Promise<string[]>`
  — dual-theme (`github-light` default color + `--shiki-dark` CSS vars; covapp.css applies
  `.dark-mode` override), returns per-line HTML; `langForPath(path)`:
  `.c/.h → c`, `.cpp/.cc/.cxx/.hpp/.hh → cpp`, else `text` (no highlight, escaped).
- Row semantics (mockup `file-page.html` is the DOM reference): grid
  `[0px ticket] 46px <40px × tier> 96px 1fr 66px` — line # · per-tier hit counts (`·` shown
  for 0 with muted style) · branch pills · source · runs expander. `rowClassFor(line, excluded, tierOrder)` implements the precedence constraint verbatim; classes
  `t-<tier>`, `s-unc`, `s-excl`, `s-stale`, `s-aging`, `""` (uncoverable). Branch pills:
  `B<n>` taken (green) / not-taken (red) / unreachable (struck-through, when every tier's
  `reachable` is false), `title` naming block/branch + per-tier hits. Context expander
  `▸ N` opens the inline run-chip panel: tier dot + run label + host pill (`run.host`,
  fallback `run.board`) + `× N`; revoked (id in `stale_run`) → struck "revoked"; aging run →
  "· aging" suffix. "Expand contexts" button in the card header toggles all. Header: file
  icon + name + language badge, **no copy button**. Meta line: lines · covered · report
  generated · otto version. The file page's StatsCard rows are computed from the chunk
  (per-tier line/branch hit-vs-total over `lines`; decision `null`), scope = display path,
  title "Coverage — this file".
- Testids: `code-row-<lineno>`, `ctx-panel-<lineno>`, `run-chip`, `host-pill`, `expand-contexts`,
  `branch-pill`.

- [ ] **Step 1: Failing vitest** — `rowClassFor` precedence table (excluded beats hit; tier 0
  beats tier 1; hit beats aging/stale; aging beats stale; no-record → uncoverable);
  `langForPath`; FilePage: renders rows from a fixture chunk (mock `loadFileChunk`), zero
  hit renders `·`, revoked chip struck + labeled, expand-all opens every panel, stamp
  mismatch → guard screen. CodeView: sticky header present, expansion renders.
- [ ] **Step 2:** FAIL → implement → green. (Shiki in jsdom: `createJavaScriptRegexEngine`
  works without WASM — no test shims needed.)
- [ ] **Step 3:** Full web gates + `make web` (bundle must stay under the 2 000 kB ceiling —
  the build fails loudly if Shiki was imported un-curated).
- [ ] **Step 4:** Commit `feat(web): CodeView ui component + covapp file page (Shiki, JS-regex engine)`.

---

### Task 6: Runs & contexts page

**Files:**
- Create: `web/src/covapp/pages/RunsPage.tsx`, `web/src/covapp/pages/RunsPage.test.tsx`,
  `web/src/covapp/contexts.ts`, `web/src/covapp/contexts.test.ts`
- Modify: `web/src/covapp/App.tsx` (`#/runs` route)

**Interfaces:**
- Consumes: Tasks 2–3; vendored `Badge` (status), vendored `Input` (search); the tier filter
  chips are hand-rolled buttons on tokens matching `contexts-page.html`'s `.chipbtn` anatomy
  (no vendored equivalent fits a pill-toggle row).
- Produces (Task 7 relies on): `contexts.ts` —
  `Context = { label, tier, runs: RunJson[], hosts: [host, lines][] , lines, revoked, files, status: "ok"|"aging"|"stale", remapped: boolean }`;
  `groupContexts(payload): Context[]` — group `runs` by `label`; per-host lines from
  `run_contrib[run.id].lines` (host = `run.host || run.board || "—"`); `lines`/`revoked`/
  `files` summed/merged across member runs; `status`: `stale` if `lines == 0 && revoked > 0`,
  else `aging` if any member run `.aging`, else `ok`; `remapped` = any `dirty_remap`.
- DOM reference: `contexts-page.html`. Row grid
  `minmax(190px,1.2fr) 92px minmax(120px,1fr) 96px 82px 110px 150px 90px`: Run · Tier chip ·
  host pills · Board · Labs · Date · Lines contributed (`n / total_lines` + tier-colored bar;
  stale → `n revoked`) · Status badge (+ `✎ remapped` line). Filters: All/tier chips +
  free-text search over label/host/ticket/board. Expanded detail (click row): Capture
  metadata grid (Run label, Hosts, Board, Labs, Captured, Tester (`tester["name"]`), Ticket,
  Base commit — append `→ HEAD (remapped)` when `dirty_remap` — Note); Per-host lines
  (pill · count · bar, scaled to max host); Contribution by type — Line row real,
  stale context → "`N` credits revoked — anchor unverifiable"; Branch and Decision rows render
  muted "not tracked per-run" (v4 data limitation, Global Constraints); Top files (up to 5,
  each linking `#/coverage/<path>`, count + bar); "Focus this context" button (dispatches
  Task 7's `setFocus(label)`; until Task 7 lands it routes through a no-op stub exported from
  `focus.ts` — create the stub file here with `setFocus`/`useFocus` signatures).
- Page meta: `<N> contexts · <M> hosts · report generated <ts>`. Crumbs: home + `runs`.
- Testids: `run-row-<label>`, `run-detail-<label>`, `tier-chip-<tier>`, `runs-search`,
  `focus-context-btn`, `contrib-branch-na`.

- [ ] **Step 1: Failing vitest** — `groupContexts`: multi-host grouping (2 runs, same label →
  1 context, 2 host pills, per-host lines from run_contrib), stale detection, aging, remap
  flag. RunsPage: filter chips narrow rows, search matches host and ticket, detail shows
  base commit with remap suffix, revoked line text, branch row shows not-tracked, top-file
  link href.
- [ ] **Step 2:** FAIL → implement → green; full web gates.
- [ ] **Step 3:** Commit `feat(web): covapp runs & contexts page`.

---

### Task 7: Context focus — report-wide filter

**Files:**
- Create/replace: `web/src/covapp/focus.ts` (real implementation over Task 6's stub),
  `web/src/covapp/focus.test.ts`
- Modify: `web/src/covapp/chrome/AppShell.tsx` (focus chip + ⋮ "Focus context" Select),
  `web/src/covapp/chrome/StatsCard.tsx` (focused variant), `pages/DirectoryPage.tsx`
  (tree % under focus), `pages/FilePage.tsx` (tint under focus), `pages/RunsPage.tsx`
  (Focus button wires to real setFocus); tests for each touched page.

**Interfaces:**
- Consumes: everything prior.
- Produces: `focus.ts` —
  `useFocus(): { focus: string | null, setFocus(label | null): void }`; state lives in the
  route query (`?ctx=<encodeURIComponent(label)>` — survives deep links) and mirrors to
  `localStorage` key `` `otto-cov:${stamp}:focus` `` (stamp-namespaced so reports on a shared
  CI origin don't fight). On boot: query param wins; else localStorage; writing updates both.
  Clearing (chip ✕ / "All contexts") removes the param + storage key.
- Focus semantics (spec §4): with focus label L —
  - StatsCard: single Context row — line = `ctx_lines[L]` at the current node over node line
    total; branch/decision "—"; scope line `focused: <L>`.
  - Directory tree: Line % and per-tier columns recompute from `ctx_lines[L]`
    (tier columns: the focused context's tier shows its value, other tiers 0); Branch %
    shows "—"; bars follow.
  - File page: rows tint by the focused context only — a line is "covered" iff one of L's
    member run ids appears in `line.run["<id>"] > 0`; everything else uncovered/neutral;
    hit-count cells show the focused run hits.
  - Chip: visible on every page (violet, tier dot + label + ✕, `data-testid="focus-chip"`);
    ⋮ menu "Focus context" section: "All contexts" + one item per context, ✓ on active
    (vendored Select or Dropdown items — match `contexts-page.html` menu anatomy).
  - Toast (covapp `Toast.tsx`) on pin/clear: "Focused <label>" / "Focus cleared".
- Testids: `focus-chip`, `focus-clear`, `menu-focus-<label>`, `menu-focus-all`.

- [ ] **Step 1: Failing vitest** — focus round-trip: setFocus writes query + storage; boot
  precedence (query over storage); stamp-namespaced key; StatsCard focused variant;
  DirectoryPage % recompute from `ctx_lines`; FilePage tint flips for non-member lines;
  clear restores all-contexts everywhere.
- [ ] **Step 2:** FAIL → implement → green; full web gates; `make web`.
- [ ] **Step 3:** Commit `feat(web): report-wide context focus (query + stamp-namespaced storage)`.

---

### Task 8: `SpaRenderer`, reporter swap, fixture upgrade, test/docs-media migration

**Files:**
- Create: `src/otto/coverage/renderer/spa_renderer.py`, `tests/unit/cov/test_spa_renderer.py`
- Modify: `src/otto/coverage/reporter.py` (swap construction site),
  `tests/_fixtures/_report_fixture.py` (fixture grows the §10 states),
  `tests/unit/cov/test_report_fixture.py` (re-pin),
  `tests/e2e/cov/report_browser/conftest.py` (guard on covapp bundle + stale-dist check),
  **replace** `tests/e2e/cov/report_browser/test_report_index.py` and `test_report_file.py`
  with `test_spa_index.py` + `test_spa_file.py` (port of today's 8 assertions to the SPA DOM),
  `scripts/capture_docs_media.py` (SPA selectors)
- Delete: nothing under `src/` (Jinja renderer/templates/covreport lanes stay until Plan D);
  the only removals are the two Jinja-era browser test files being replaced above.

**Interfaces:**
- Consumes: Task 1 (`emit_chunks`, `make_stamp`, `OTTO_COV_DATA_FORMAT`); Task 2's bundle
  layout (`static/covapp/index.html` + `dist/`).
- Produces: `SpaRenderer(output_dir, project_name="Coverage Report", *, extra_markers=None, prefix=None)` with `render(store) -> None`:
  1. mkdir output;
  2. copy `renderer/static/covapp/**` → output (`index.html`, `dist/`); if the bundle dir or
     its `index.html` is missing: `logger.warning(...)` naming `make web` and **continue**
     (exact parity with `HtmlRenderer._copy_static`'s degrade — hostless unit envs have no
     web dist);
  3. `emit_chunks(store, output, project_name=…, prefix=…, extra_markers=…, stamp=make_stamp())`
     (this annotates `excluded_lines` — the reporter still saves store.json AFTER render, so
     the ordering contract from Plan B holds).
- Reporter swap in `reporter.py` (the only production call-site change):

```python
from .renderer.spa_renderer import SpaRenderer
...
renderer = SpaRenderer(
    self.output_dir,
    project_name=self.project_name,
    extra_markers=self.extra_markers,
    prefix=self.prefix,
)
renderer.render(store)
```

**Fixture upgrade** (`tests/_fixtures/_report_fixture.py`) — keep the 2 files + 2 tiers +
`prefix=base_dir` display strings, add tier `manual`, and register (spec §10):
- two `system` runs sharing label `"nightly-full"` with hosts `"router-a"` / `"router-b"`
  (multi-host context), with `run_hits` on main.c lines;
- one `unit` run `"unit harvest"`, host `"ci-01"`;
- one `manual` run `"smoke-old"` fully revoked: a stale line (`state="stale"`,
  `stale_runs=[id]`, no live hits) → stale context;
- one `manual` run `"field bring-up"` with `aging=True`, `dirty_remap=True`,
  `base_commit="a1..." * pattern`, `ticket="FW-1188"`, `tester={"name": "M. Reyes"}` and an
  `aging` line (`state="aging"` + `run_hits`);
- one `LCOV_EXCL_LINE`-marked line in utils.c (excluded state renders).
Update `tests/unit/cov/test_report_fixture.py` pins to the new counts/labels.

**Browser test port** (behavior preserved, assertions relocated — spec §11):
- `test_spa_index.py`: boot without pageerrors (h1 == project name), tier columns render in
  order, click "Line %" header sorts numerically (assert row order flips), file name click
  routes to `#/coverage/product/main.c`, runs disclosure lists the fixture runs with `✎`.
- `test_spa_file.py`: open `#/coverage/product/main.c` via hash URL on `index.html`; branch
  pills all three states with block/branch tooltips; row precedence classes present
  (`t-system` on a covered line, `s-excl`, `s-stale`, `s-aging` on their fixture lines);
  breadcrumb returns to the directory page; context expander shows host pills.
- Shared `_pageerror_guard` fixture in the conftest: collects `pageerror` + `console`
  `error`-type messages for every test in the dir and asserts empty at teardown
  (autouse; the CSP lane in Task 10 reuses it).
- conftest `pytest_configure`: fail fast when `static/covapp/index.html` missing; add the
  dashboard suite's stale-dist check pattern (`web/src` newer than covapp `index.html` →
  `pytest.exit`).

**Docs media:** `_capture_coverage_report` — same fixture, `wait_for_selector` on
`[data-testid="tree-row-product"]` (SPA tree) instead of `table.files-table`; keep
1280×720 full-page screenshot name/path.

**Unit-pin relocation:** `test_spa_renderer.py` carries the intent of the Jinja-era pins that
must survive the swap at the Python level (the vitest suites cover the DOM level):
prefix strips display path but not chunk names; out-of-range LineRecord doesn't crash and
appears in the chunk; excluded lines round-trip to `store.json` via the reporter ordering;
missing bundle warns (`caplog` names `make web`) and still emits `cov_data/`; bundle copy
places `index.html` + `dist/` at report root; `store.json` still written by the reporter
after render (integration test through `run_coverage_report` with the settings-driven path,
mirroring `test_cov.py::test_settings_driven_collection_path` style).

- [ ] **Step 1:** Failing `tests/unit/cov/test_spa_renderer.py` (list above) → implement
  `spa_renderer.py` → green.
- [ ] **Step 2:** Swap the reporter call-site; run `uv run pytest tests/unit/cov tests/unit/cli/test_cov.py -q` — fix fallout (tests that asserted Jinja files exist from the
  reporter path, if any, move to constructing `HtmlRenderer` directly).
- [ ] **Step 3:** Fixture upgrade + re-pin `test_report_fixture.py`; `uv run pytest tests/unit/cov -q` green.
- [ ] **Step 4:** Replace the two browser test files; `make web` then
  `uv run pytest tests/e2e/cov/report_browser -m "browser and not soak" --browser chromium -q`
  green.
- [ ] **Step 5:** `uv run python scripts/capture_docs_media.py --mode force` succeeds; inspect
  `docs/_static/generated/coverage-report.png` is the SPA.
- [ ] **Step 6:** Commit `feat(cov): SpaRenderer — reporter emits the covapp SPA report`.

---

### Task 9: Browser e2e expansion — file:// lane over every UI state

**Files:**
- Create: `tests/e2e/cov/report_browser/test_spa_runs_focus.py`
- Modify: `tests/e2e/cov/report_browser/test_spa_index.py`, `test_spa_file.py` (additions only)

**Interfaces:** consumes the Task 8 fixture and testids pinned in Tasks 3–7.

Coverage to add (all against the real built bundle at `file://`, chromium default matrix;
markers `browser` + `hostless`, same as today):
- **Runs page:** `#/runs` renders one row per context (multi-host row shows 2 host pills);
  stale context shows `revoked` count + stale badge; aging+remapped shows `✎ remapped` and
  base commit `→ HEAD (remapped)` in detail; tier chip filter narrows; search by ticket hits
  `FW-1188`; top-file link routes to the file page.
- **Focus flow:** pin focus from a run detail → chip appears; directory page % changes
  (assert a specific cell's text differs from unfocused); file page: a line hit only by a
  non-focused run renders uncovered class; `?ctx=` appears in the URL; reload the same URL →
  focus persists; ✕ clears; localStorage key is stamp-namespaced (evaluate
  `Object.keys(localStorage)`).
- **Both themes:** toggle theme → `document.documentElement` gains `.dark-mode`; a token-driven
  color actually changes (spot-check one computed style); reload preserves via `otto-theme`.
- **Not-found route:** `#/coverage/does/not/exist` renders the not-found page with a working
  link to `#/coverage`.
- **Guard screen:** copy the report to a tmp dir, truncate `cov_data/index.js` to
  `window.__OTTO_COV__ = {"format": 999};` → guard screen testid visible, no pageerrors.
- **Stats card:** "All tiers" line pct text matches the fixture's known totals (compute the
  expected string in the test from fixture constants — no magic numbers).

- [ ] **Step 1:** Write the tests (they run against already-shipped behavior — this is the
  regression net, RED only where a bug is found; fix any bug it finds).
- [ ] **Step 2:** `make web && uv run pytest tests/e2e/cov/report_browser -m "browser and not soak" --browser chromium -n 2 -q` green.
- [ ] **Step 3:** One webkit spot-run of the new files:
  `uv run pytest tests/e2e/cov/report_browser -m "browser and not soak" --browser webkit -q`.
- [ ] **Step 4:** Commit `test(cov): browser e2e over runs/focus/themes/guard states (file:// lane)`.

---

### Task 10: Served CSP lane + CI wiring + wheel/bundle gates

**Files:**
- Create: `tests/_fixtures/_csp_server.py`, `tests/e2e/cov/report_browser/test_spa_csp.py`
- Modify: `noxfile.py` (dashboard session includes `tests/e2e/cov/report_browser`),
  `web/scripts/e2e_coverage_report.mjs` (add covapp dist to `dists`),
  `Makefile` (`wheel-check` asserts covapp `index.html` embedded)

**Interfaces:**
- Produces: `_csp_server.py` —

```python
"""Serve a directory over HTTP from a subpath, with the documented minimal CSP header."""
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'"
)
SUBPATH = "/job/artifacts"  # exercised depth — report must be path-agnostic

class _Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith(SUBPATH):
            path = path[len(SUBPATH):] or "/"
        return super().translate_path(path)
    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()
    def log_message(self, *args):  # keep pytest output clean
        pass

class CspReportServer:
    def __init__(self, directory):
        handler = partial(_Handler, directory=str(directory))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
    @property
    def url(self):
        return f"http://127.0.0.1:{self._httpd.server_port}{SUBPATH}/index.html"
    def start(self): self._thread.start(); return self
    def stop(self): self._httpd.shutdown(); self._httpd.server_close(); self._thread.join(timeout=5)
```

- `test_spa_csp.py`: session fixture serving the Task 8 `report_dir`; tests — app boots under
  CSP (tree row visible), zero console errors / pageerrors (**the** inline-script regression
  gate: a stray inline script surfaces as a CSP console error), navigation to a file page
  lazy-loads its chunk over HTTP (assert code rows render), `#/runs` renders. Markers:
  `browser` + `hostless`.
- `noxfile.py`: `dashboard` session's pytest args gain `"tests/e2e/cov/report_browser"`
  next to the monitor path (same `DASHBOARD_MARKER_EXPR` — no new expression copies). This
  puts the whole report suite (file:// + CSP lanes) into CI's per-engine dashboard jobs.
- `e2e_coverage_report.mjs`: append the covapp static dir to the `dists` array
  (`src/otto/coverage/renderer/static/covapp`) so served/file URLs ending in
  `/dist/covapp.js` resolve for the TS coverage fold.
- `Makefile` `wheel-check`: alongside the existing covreport-dist entry-count assertion, add
  `unzip -l dist/*.whl | grep -q "otto/coverage/renderer/static/covapp/index.html"`.

- [ ] **Step 1:** Failing `test_spa_csp.py` (server fixture first; watch it fail before the
  handler exists) → implement `_csp_server.py` → green under chromium.
- [ ] **Step 2:** nox/mjs/Makefile wiring; `uv run nox -s dashboard-chromium --no-install` (or
  the repo's equivalent invocation) proves the session now collects the report suite.
- [ ] **Step 3:** `make wheel-check` green (covapp embedded, airgap re-checked).
- [ ] **Step 4:** Commit `test(cov): served CSP lane, CI dashboard wiring, wheel/coverage-fold gates`.

---

### Task 11: Docs

**Files:**
- Modify: `docs/guide/coverage.md` (report section: SPA pages, routes, focus, runs page,
  thresholds cross-ref; new "Hosting the report in CI" subsection with the GitLab
  works-out-of-the-box note and the Jenkins `hudson.model.DirectoryBrowserSupport.CSP`
  snippet quoting the documented CSP string verbatim),
  `docs/architecture/subsystems/coverage.md` ("The renderer" section: SpaRenderer + data-chunk
  contract summary — index.js/per-file chunks/stamp guard; "Where the code lives" adds
  `otto.coverage.renderer.spa_data` / `spa_renderer` and `web/src/covapp/`; note the Jinja
  renderer is retained but no longer wired, removal tracked for the follow-up plan).

**Constraints:** no `:func:`/`:class:` xrefs to modules that aren't autodoc'd — use literals
(Plan B landmine); docs gate is a CLEAN rebuild: `rm -rf docs/_build/html && make docs` with
zero warnings.

- [ ] **Step 1:** Write both docs edits.
- [ ] **Step 2:** `rm -rf docs/_build/html && make docs` → zero warnings; screenshot embeds the
  Task 8 SPA capture.
- [ ] **Step 3:** Commit `docs(cov): SPA report guide + architecture contract, CI hosting/CSP`.

---

## Verification (whole plan, run in the MAIN session — never in a subagent)

1. `make web` — three lanes, airgap × 3, brand tokens × 2, warnings-as-errors.
2. `cd web && npm run check && npm run knip && npm run typecheck && npm run test` — all green.
3. `make coverage` — ONE full gate at the end of the plan (dev-VM rule), includes the browser
   suites via the `dashboard` target and the TS coverage fold.
4. `make wheel-check`.
5. Clean docs rebuild.
