# Coverage-Report Frontend Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the coverage HTML report's frontend to the monitor dashboard's
testing standard — TypeScript built by vite, Vitest unit tests, a Playwright
browser suite pinning today's behavior — add an `otto cov report --prefix`
display-root option (genhtml `--prefix` analogue), and add a build-time
screenshot of the report GUI to the coverage guide page.

**Architecture:** The 53-line vanilla `report.js` (click-to-sort) becomes a
TypeScript vite *lib-mode* entry in the existing `web/` workspace, emitting a
fixed-name `covreport.js` into `src/otto/coverage/renderer/static/dist/`
(gitignored, wheel-embedded — same lifecycle as the monitor's dist). A shared
Python fixture renders a deterministic two-tier report that both the new
Playwright suite (`tests/e2e/cov/report_browser/`) and the docs-media pipeline
(`scripts/capture_docs_media.py`) consume. This is phase 1 of
`docs/superpowers/specs/2026-07-03-branch-clause-mapping-design.md` — pin the
existing report; add **no** new report features (no dark mode, no clause UI).

**Tech Stack:** TypeScript 6 / vite 8 (lib mode, IIFE) / Vitest 4 + jsdom /
pytest-playwright (Chromium) / Jinja2 templates (unchanged rendering) /
GNU make lanes.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-03-branch-clause-mapping-design.md`
  (phase 1 only; phases 2–3 are DEFERRED — do not implement any clause
  mapping).
- **No new Python dependencies.** No React in the covreport entry — plain
  TS + DOM.
- **Pin, don't improve** (one approved exception): the TS port must
  reproduce `report.js` behavior exactly (same class names
  `sortable`/`num`/`sort-asc`/`sort-desc`, same `data-sort` precedence,
  same NaN→`-Infinity` numeric fallback). The report has no dark theme —
  do not add one. The ONLY new feature in this effort is `--prefix`
  (Task 5), explicitly approved in review; it is display-only — links,
  store keys, and coverage numbers are untouched by it.
- `src/otto/coverage/renderer/static/dist/` is **never committed**
  (gitignored) and must be embedded in the wheel (uv_build embeds all of
  `src/otto/**`; `wheel-check` asserts it).
- Air-gap: built assets must contain no absolute `http(s)` URLs
  (`scripts/check_airgap.sh`).
- The Jinja template references the **fixed** filename
  `static/dist/covreport.js` — the vite build must emit exactly that name
  (no content hashing).
- Python style: NO `from __future__ import annotations` (breaks the Sphinx
  nitpicky gate); real 3.10+ annotations, module-top imports unless a memory
  or neighboring code says lazy; ruff `select=ALL` strictness — after any
  `ruff format`, re-run `ruff check .`.
- Test tiers: browser tests carry `pytest.mark.hostless`,
  `pytest.mark.browser`, and `pytest.mark.xdist_group("covreport")` and run
  single-process in the `make dashboard` lane; they must NOT run in plain
  `pytest tests/unit` lanes.
- Worktree setup: run `uv sync`, `make web-install`, and `make browsers`
  before the browser/build tasks; `ty` runs only at the `make typecheck`
  gate.
- Commits in the worktree embed the trailer line `Assisted-by: Claude
  Fable 5` (the prepare-commit-msg hook cannot prompt without /dev/tty).

## File Structure

```text
web/
  vite.covreport.config.ts        NEW  lib-mode build → renderer static dist
  package.json                    MOD  adds build:covreport script
  tsconfig.json                   MOD  include gains vite.covreport.config.ts
  src/covreport/sort.ts           NEW  TS port of report.js (exported, testable)
  src/covreport/main.ts           NEW  entry point (wires initReportPage)
  src/covreport/sort.test.ts      NEW  Vitest suite
src/otto/coverage/renderer/
  templates/index.html            MOD  script src → static/dist/covreport.js
  static/report.js                DEL  replaced by the TS build
  html_renderer.py                MOD  _copy_static warns when dist missing;
                                       prefix param + prefix-aware _display_path
src/otto/coverage/reporter.py     MOD  prefix threaded CLI → renderer
src/otto/cli/cov.py               MOD  otto cov report --prefix option
tests/
  _fixtures/_report_fixture.py    NEW  deterministic fixture report builder
  _fixtures/_browser_guard.py     NEW  extracted -m guard (shared w/ dashboard)
  e2e/monitor/dashboard/conftest.py MOD  uses the extracted guard
  e2e/cov/report_browser/__init__.py NEW
  e2e/cov/report_browser/conftest.py NEW  dist guard + rendered-report fixture
  e2e/cov/report_browser/test_report_index.py NEW
  e2e/cov/report_browser/test_report_file.py  NEW
  unit/cov/test_report_fixture.py NEW  fixture renders hermetically
  unit/cov/test_html_renderer_dist.py NEW  dist-missing warning
  unit/cov/test_html_renderer_prefix.py NEW  --prefix display stripping
  unit/cli/test_cov.py            MOD  --prefix reaches run_coverage_report
scripts/capture_docs_media.py     MOD  + coverage-report.png artifact
docs/guide/coverage.md            MOD  screenshot embed
Makefile                          MOD  web / web-clean / wheel-check / dashboard
.gitignore                        MOD  + src/otto/coverage/renderer/static/dist/
```

---

### Task 1: TypeScript sorter with Vitest suite

**Files:**
- Create: `web/src/covreport/sort.ts`
- Create: `web/src/covreport/main.ts`
- Create: `web/src/covreport/sort.test.ts`

**Interfaces:**
- Consumes: nothing (self-contained DOM module).
- Produces: `initReportPage(root?: Document): void`, `attachSort(table:
  HTMLTableElement): void`, `makeComparator(idx: number, asc: boolean,
  numeric: boolean)`, `cellValue(row: HTMLTableRowElement, idx: number):
  string` — Task 2 builds `main.ts` into `covreport.js`; Task 8's browser
  tests exercise the built behavior.

- [ ] **Step 1: Write the failing Vitest suite**

Create `web/src/covreport/sort.test.ts`:

```ts
// Pins the exact behavior of the legacy static/report.js sorter that this
// module replaces: class-name contract (sortable/num/sort-asc/sort-desc),
// data-sort precedence over cell text, and NaN -> -Infinity numeric fallback.
import { describe, expect, it } from "vitest";

import { attachSort, cellValue, initReportPage, makeComparator } from "./sort";

function renderTable(rowsHtml: string, tableClass = "files-table"): HTMLTableElement {
  document.body.innerHTML = `
    <table class="${tableClass}">
      <thead><tr>
        <th class="sortable">File</th>
        <th class="sortable num">Line %</th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
  return document.querySelector("table") as HTMLTableElement;
}

const ROWS = `
  <tr><td>b.c</td><td data-sort="9.5">9.5%</td></tr>
  <tr><td>a.c</td><td data-sort="100.0">100.0%</td></tr>
  <tr><td>c.c</td><td>&mdash;</td></tr>`;

function column(table: HTMLTableElement, idx: number): string[] {
  return Array.from(table.tBodies[0].rows).map((r) => (r.cells[idx].textContent ?? "").trim());
}

function header(table: HTMLTableElement, idx: number): HTMLTableCellElement {
  return table.querySelectorAll<HTMLTableCellElement>("thead th")[idx];
}

describe("cellValue", () => {
  it("prefers data-sort over cell text", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 1)).toBe("9.5");
  });

  it("falls back to trimmed textContent without data-sort", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 0)).toBe("b.c");
  });

  it("returns empty string for a missing cell", () => {
    const table = renderTable(ROWS);
    expect(cellValue(table.tBodies[0].rows[0], 99)).toBe("");
  });
});

describe("makeComparator", () => {
  it("treats non-numeric values as -Infinity in numeric mode", () => {
    const table = renderTable(ROWS);
    const rows = Array.from(table.tBodies[0].rows);
    rows.sort(makeComparator(1, true, true));
    // ascending: the dash row (no data-sort, NaN) sorts first
    expect(rows.map((r) => r.cells[0].textContent)).toEqual(["c.c", "b.c", "a.c"]);
  });
});

describe("attachSort", () => {
  it("sorts text columns ascending on first click and marks sort-asc", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    expect(column(table, 0)).toEqual(["a.c", "b.c", "c.c"]);
    expect(header(table, 0).classList.contains("sort-asc")).toBe(true);
  });

  it("re-clicking flips to descending and swaps the marker class", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    header(table, 0).click();
    expect(column(table, 0)).toEqual(["c.c", "b.c", "a.c"]);
    expect(header(table, 0).classList.contains("sort-desc")).toBe(true);
    expect(header(table, 0).classList.contains("sort-asc")).toBe(false);
  });

  it("sorts num columns numerically via data-sort (9.5 before 100.0)", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 1).click();
    expect(column(table, 0)).toEqual(["c.c", "b.c", "a.c"]);
    header(table, 1).click();
    expect(column(table, 0)).toEqual(["a.c", "b.c", "c.c"]);
  });

  it("clicking a second column clears the first column's marker", () => {
    const table = renderTable(ROWS);
    attachSort(table);
    header(table, 0).click();
    header(table, 1).click();
    expect(header(table, 0).classList.contains("sort-asc")).toBe(false);
    expect(header(table, 1).classList.contains("sort-asc")).toBe(true);
  });
});

describe("initReportPage", () => {
  it("wires only .files-table tables", () => {
    document.body.innerHTML = `
      <table class="summary-table">
        <thead><tr><th class="sortable">X</th></tr></thead>
        <tbody><tr><td>2</td></tr><tr><td>1</td></tr></tbody>
      </table>`;
    initReportPage(document);
    const table = document.querySelector("table") as HTMLTableElement;
    (table.querySelector("th") as HTMLTableCellElement).click();
    // untouched: no sort marker, original row order preserved
    expect(table.querySelector("th.sort-asc")).toBeNull();
    expect(column(table, 0)).toEqual(["2", "1"]);
  });
});
```

- [ ] **Step 2: Run the suite to verify it fails**

Run: `cd web && npx vitest run src/covreport/sort.test.ts`
Expected: FAIL — `Cannot find module './sort'` (or equivalent resolve error).

- [ ] **Step 3: Implement the sorter (faithful port of report.js)**

Create `web/src/covreport/sort.ts`:

```ts
// Otto coverage report — click-to-sort for the index's files table.
//
// Faithful TypeScript port of the legacy static/report.js: the class-name
// contract (sortable/num on <th>, sort-asc/sort-desc as the active marker)
// and the data-sort-over-textContent precedence are pinned by sort.test.ts
// and by the Playwright suite in tests/e2e/cov/report_browser/.

export function cellValue(row: HTMLTableRowElement, idx: number): string {
  const cell = row.cells[idx];
  if (!cell) return "";
  const raw = cell.getAttribute("data-sort");
  if (raw !== null) return raw;
  return (cell.textContent ?? "").trim();
}

export function makeComparator(
  idx: number,
  asc: boolean,
  numeric: boolean,
): (a: HTMLTableRowElement, b: HTMLTableRowElement) => number {
  return (a, b) => {
    const av = cellValue(a, idx);
    const bv = cellValue(b, idx);
    if (numeric) {
      let an = Number.parseFloat(av);
      let bn = Number.parseFloat(bv);
      if (Number.isNaN(an)) an = Number.NEGATIVE_INFINITY;
      if (Number.isNaN(bn)) bn = Number.NEGATIVE_INFINITY;
      return asc ? an - bn : bn - an;
    }
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  };
}

export function attachSort(table: HTMLTableElement): void {
  const headers = table.querySelectorAll<HTMLTableCellElement>("thead th.sortable");
  headers.forEach((th, idx) => {
    th.addEventListener("click", () => {
      const tbody = table.tBodies[0];
      if (!tbody) return;
      const rows = Array.from(tbody.rows);
      const asc = !th.classList.contains("sort-asc");
      headers.forEach((h) => {
        h.classList.remove("sort-asc");
        h.classList.remove("sort-desc");
      });
      th.classList.add(asc ? "sort-asc" : "sort-desc");
      const numeric = th.classList.contains("num");
      rows.sort(makeComparator(idx, asc, numeric));
      rows.forEach((r) => tbody.appendChild(r));
    });
  });
}

export function initReportPage(root: Document = document): void {
  root.querySelectorAll<HTMLTableElement>("table.files-table").forEach(attachSort);
}
```

Create `web/src/covreport/main.ts`:

```ts
// Entry point for the covreport bundle (built by vite.covreport.config.ts).
// The Jinja template loads it with `defer`, so the DOM is parsed before this
// runs — the readyState guard covers non-deferred manual loads too.
import { initReportPage } from "./sort";

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => initReportPage());
} else {
  initReportPage();
}
```

- [ ] **Step 4: Run the suite to verify it passes**

Run: `cd web && npx vitest run src/covreport/sort.test.ts`
Expected: PASS (9 tests).

Also run the whole web suite to prove no regression:
`cd web && npm run test` — Expected: PASS, existing monitor tests included.

- [ ] **Step 5: Commit**

```bash
git add web/src/covreport/sort.ts web/src/covreport/main.ts web/src/covreport/sort.test.ts
git commit -m "feat(web): TypeScript port of the coverage-report sorter with Vitest pins

Assisted-by: Claude Fable 5"
```

---

### Task 2: covreport vite build

**Files:**
- Create: `web/vite.covreport.config.ts`
- Modify: `web/package.json` (scripts)
- Modify: `web/tsconfig.json` (include)
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `web/src/covreport/main.ts` (Task 1).
- Produces: `src/otto/coverage/renderer/static/dist/covreport.js` (IIFE,
  fixed name) via `npm run build:covreport` — Tasks 3, 4, 7 depend on this
  artifact existing at exactly this path.

- [ ] **Step 1: Write the vite config**

Create `web/vite.covreport.config.ts`:

```ts
import { defineConfig } from "vite";

// The coverage report is Jinja-generated static HTML (see
// src/otto/coverage/renderer/templates/index.html): its <script> tag
// references the FIXED path static/dist/covreport.js, so this build must
// emit exactly that filename — lib mode with a fileName override, no content
// hashing. IIFE so it runs as a classic deferred script, including off
// file:// URLs (reports are opened straight from disk, no server).
export default defineConfig({
  build: {
    outDir: "../src/otto/coverage/renderer/static/dist",
    emptyOutDir: true,
    lib: {
      entry: "src/covreport/main.ts",
      name: "ottoCovReport",
      formats: ["iife"],
      fileName: () => "covreport.js",
    },
  },
});
```

- [ ] **Step 2: Wire package.json and tsconfig**

In `web/package.json` `"scripts"`, after the `"build"` entry add:

```json
    "build:covreport": "vite build --config vite.covreport.config.ts",
```

In `web/tsconfig.json`, change the include line to:

```json
  "include": ["src", "vite.config.ts", "vite.covreport.config.ts"]
```

Append to `.gitignore` (next to the existing
`src/otto/monitor/static/dist/` line):

```text
src/otto/coverage/renderer/static/dist/
```

- [ ] **Step 3: Build and verify the artifact**

Run: `cd web && npm run build:covreport`
Expected: vite reports one chunk written to
`../src/otto/coverage/renderer/static/dist/covreport.js`.

Run: `grep -c "files-table" ../src/otto/coverage/renderer/static/dist/covreport.js` (from `web/`)
Expected: `1` or more (the selector made it into the bundle).

Run: `git status --short src/otto/coverage/renderer/static/`
Expected: no output — dist is ignored.

Run: `cd web && npm run build && npm run build:covreport`
Expected: both builds pass — `tsc` type-checks the new files in the same
pass (they live under `src/`).

- [ ] **Step 4: Commit**

```bash
git add web/vite.covreport.config.ts web/package.json web/tsconfig.json .gitignore
git commit -m "build(web): covreport vite lib build into the renderer's static dist

Assisted-by: Claude Fable 5"
```

---

### Task 3: renderer cutover — template swap, delete report.js, dist warning

**Files:**
- Modify: `src/otto/coverage/renderer/templates/index.html:13`
- Delete: `src/otto/coverage/renderer/static/report.js`
- Modify: `src/otto/coverage/renderer/html_renderer.py:482-485` (`_copy_static`)
- Test: `tests/unit/cov/test_html_renderer_dist.py`

**Interfaces:**
- Consumes: the dist artifact path from Task 2.
- Produces: reports whose index.html loads `static/dist/covreport.js`;
  a `logger.warning` (logger name `otto.coverage.renderer.html_renderer`)
  when the dist is missing at render time. Task 8's browser tests and Task 7's
  conftest guard rely on the template path; nothing else consumes the warning.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cov/test_html_renderer_dist.py`:

```python
"""The renderer's static assets now include a built JS bundle (make web).

A dist-less checkout must still render a usable (static) report, but say
loudly that interactivity is missing — silence here would look like a bug
in the report, not a build-step omission.
"""

from pathlib import Path

import pytest

from otto.coverage.renderer import html_renderer
from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import CoverageStore


def _render_empty(tmp_path: Path) -> Path:
    out = tmp_path / "report"
    HtmlRenderer(out).render(CoverageStore(tier_order=["system"]))
    return out


def test_index_references_built_bundle(tmp_path):
    """The template must load the vite-built bundle, not the deleted report.js."""
    out = _render_empty(tmp_path)
    index = (out / "index.html").read_text()
    assert "static/dist/covreport.js" in index
    assert "static/report.js" not in index


def test_missing_dist_warns_and_still_renders(tmp_path, monkeypatch, caplog):
    """No dist (checkout without `make web`): render succeeds, warning names the fix."""
    bare_static = tmp_path / "static_src"
    bare_static.mkdir()
    (bare_static / "report.css").write_text("body {}")
    monkeypatch.setattr(html_renderer, "STATIC_DIR", bare_static)

    with caplog.at_level("WARNING"):
        out = _render_empty(tmp_path)

    assert (out / "index.html").exists()
    assert any("make web" in r.message for r in caplog.records)


def test_present_dist_copies_bundle_and_does_not_warn(tmp_path, monkeypatch, caplog):
    fake_static = tmp_path / "static_src"
    (fake_static / "dist").mkdir(parents=True)
    (fake_static / "report.css").write_text("body {}")
    (fake_static / "dist" / "covreport.js").write_text("// bundle")
    monkeypatch.setattr(html_renderer, "STATIC_DIR", fake_static)

    with caplog.at_level("WARNING"):
        out = _render_empty(tmp_path)

    assert (out / "static" / "dist" / "covreport.js").read_text() == "// bundle"
    assert not [r for r in caplog.records if "make web" in r.message]
```

- [ ] **Step 2: Run tests to verify failures**

Run: `uv run --no-sync pytest tests/unit/cov/test_html_renderer_dist.py -v`
Expected: `test_index_references_built_bundle` FAILS (template still says
`static/report.js`); `test_missing_dist_warns_and_still_renders` FAILS (no
warning emitted).

- [ ] **Step 3: Implement the cutover**

In `src/otto/coverage/renderer/templates/index.html`, change line 13:

```html
  <script src="static/dist/covreport.js" defer></script>
```

Delete the legacy file:

```bash
git rm src/otto/coverage/renderer/static/report.js
```

In `src/otto/coverage/renderer/html_renderer.py`, replace `_copy_static`:

```python
    def _copy_static(self) -> None:
        static_dst = self.output_dir / "static"
        if STATIC_DIR.exists():
            shutil.copytree(str(STATIC_DIR), str(static_dst), dirs_exist_ok=True)
        if not (STATIC_DIR / "dist" / "covreport.js").exists():
            # The sorter bundle is built by `make web` (vite), not committed.
            # The report is still readable without it, so degrade — but say
            # exactly what is missing and how to get it.
            logger.warning(
                "Coverage report interactivity (covreport.js) is missing — "
                "the report renders read-only. Run `make web` to build the "
                "frontend assets."
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/cov/test_html_renderer_dist.py -v`
Expected: 3 PASS.

Run the neighboring renderer/cov unit tests for regressions:
`uv run --no-sync pytest tests/unit/cov tests/unit/cli/test_cov.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/coverage/renderer/templates/index.html src/otto/coverage/renderer/html_renderer.py tests/unit/cov/test_html_renderer_dist.py
git commit -m "feat(cov)!: report sorter served from the vite-built covreport bundle

The legacy static/report.js is deleted; index.html loads
static/dist/covreport.js (built by make web), and a dist-less render
warns that interactivity is missing instead of failing silently.

Assisted-by: Claude Fable 5"
```

---

### Task 4: Makefile build lanes and wheel guard

**Files:**
- Modify: `Makefile` (`web`, `web-clean`, `wheel-check` targets)

**Interfaces:**
- Consumes: `npm run build:covreport` (Task 2).
- Produces: `make web` builds + air-gap-checks BOTH dists; `make wheel-check`
  asserts both are wheel-embedded. Task 9's docs flow and CI rely on
  `make web` covering the covreport bundle.

- [ ] **Step 1: Extend the `web` target**

In the `web:` recipe, after `cd web && npm run build` and before
`scripts/check_airgap.sh`, the recipe becomes:

```makefile
web: ## (Build & Release) Build the web/ React dashboard + the covreport bundle (vite) into their static dist dirs, then gate both against absolute http(s) URLs (air-gap requirement — labs have no network access, see scripts/check_airgap.sh)
	# Regenerate web/src/api/types.gen.ts from the live pydantic models and fail
	# BEFORE the vite build if the committed file has drifted — a stale wire
	# contract should be caught by its own diff, not surface later as a build
	# or runtime type error with no clue which model changed.
	scripts/gen_web_types.sh
	git diff --exit-code web/src/api/types.gen.ts
	cd web && npm run build
	cd web && npm run build:covreport
	scripts/check_airgap.sh
	scripts/check_airgap.sh src/otto/coverage/renderer/static/dist
```

- [ ] **Step 2: Extend `web-clean`**

```makefile
web-clean: ## (Dev) Remove the built web/ dist outputs (monitor dashboard + covreport)
	rm -rf src/otto/monitor/static/dist
	rm -rf src/otto/coverage/renderer/static/dist
```

- [ ] **Step 3: Extend `wheel-check`**

In the `wheel-check` recipe, after the existing monitor-dist assertion
block, add a covreport assertion with the same shape:

```makefile
	@count=$$(unzip -l dist/*.whl | grep -c "otto/coverage/renderer/static/dist/" || true); \
	if [ "$$count" -eq 0 ]; then \
		echo "wheel-check: FAIL — no otto/coverage/renderer/static/dist/ entries in dist/*.whl; an air-gapped install would ship the coverage report without its frontend." >&2; \
		exit 1; \
	fi; \
	echo "wheel-check: OK — $$count otto/coverage/renderer/static/dist/ entries embedded."
```

(Match the exact `if/echo` formatting of the existing monitor block when
editing — copy it and change the path and message.)

- [ ] **Step 4: Verify the lanes**

Run: `make web`
Expected: both vite builds run; both `check_airgap` invocations print `OK`.

Run: `make web-clean && ls src/otto/coverage/renderer/static/`
Expected: no `dist/` present; then re-run `make web` to restore it.

Run: `make -n wheel-check`
Expected: dry-run prints both assertion blocks, exits 0 (dry-run safety —
the target's comment demands `make -n` stays safe).

- [ ] **Step 5: Commit**

```bash
git add Makefile
git commit -m "build: web lane builds + air-gap-checks the covreport dist; wheel-check asserts embedding

Assisted-by: Claude Fable 5"
```

---

### Task 5: `--prefix` display-root option (approved feature)

**Files:**
- Modify: `src/otto/coverage/renderer/html_renderer.py` (`__init__`,
  `_display_path`)
- Modify: `src/otto/coverage/reporter.py` (`CoverageReporter.__init__`,
  the `HtmlRenderer(` construction in `run()`, `run_coverage_report`
  signature and every `CoverageReporter(` construction site inside it —
  find them with `grep -n "CoverageReporter(" src/otto/coverage/reporter.py`)
- Modify: `src/otto/cli/cov.py` (`report()` option + forwarding)
- Test: `tests/unit/cov/test_html_renderer_prefix.py`
- Test: `tests/unit/cli/test_cov.py` (one new test)

**Interfaces:**
- Consumes: nothing new.
- Produces: `HtmlRenderer(..., prefix: Path | None = None)`;
  `CoverageReporter(..., prefix: Path | None = None)` (keyword-only);
  `run_coverage_report(..., prefix: Path | None = None)` (keyword-only);
  CLI `otto cov report --prefix PATH`. Task 6's fixture passes
  `prefix=` to `HtmlRenderer`; Task 8's browser pins assert the stripped
  display paths.

Display-only semantics (the `genhtml --prefix` analogue): a file whose
path is under the prefix *displays* relative to it; everything else —
file-page links (`_file_link`), store keys, coverage numbers — is
untouched. Non-matching files display unchanged. Stripping is
path-component-aware (`Path.relative_to`, never string `startswith`, so
`/opt/foo` cannot match `/opt/foobar`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cov/test_html_renderer_prefix.py`:

```python
"""--prefix strips a leading directory from DISPLAYED paths only (the
genhtml --prefix analogue): links and store keys stay full-path,
non-matching files display unchanged, no prefix means today's verbatim
display."""

from pathlib import Path

from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import CoverageStore, FileRecord, LineRecord


def _store_with(*paths: Path) -> CoverageStore:
    store = CoverageStore(tier_order=["system"])
    for path in paths:
        rec = FileRecord(path=path)
        line = LineRecord(line_number=1)
        line.hits.add("system", 1)
        rec.lines[1] = line
        store.merge_file(rec)
    return store


def _write_src(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("int one(void) { return 1; }\n")
    return path


def test_prefix_strips_displayed_paths(tmp_path):
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path).render(_store_with(src))
    index = (out / "index.html").read_text()
    assert "product/main.c" in index
    assert str(tmp_path) not in index


def test_file_outside_prefix_displays_unchanged(tmp_path):
    inside = _write_src(tmp_path / "repo" / "a.c")
    outside = _write_src(tmp_path / "elsewhere" / "b.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path / "repo").render(_store_with(inside, outside))
    index = (out / "index.html").read_text()
    assert ">a.c<" in index or "a.c" in index  # stripped
    assert str(tmp_path / "elsewhere" / "b.c") in index  # verbatim


def test_no_prefix_keeps_verbatim_display(tmp_path):
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out).render(_store_with(src))
    assert str(src) in (out / "index.html").read_text()


def test_prefix_does_not_change_file_links(tmp_path):
    """Links stay keyed on the full path — only the label is stripped."""
    src = _write_src(tmp_path / "product" / "main.c")
    out = tmp_path / "report"
    HtmlRenderer(out, prefix=tmp_path).render(_store_with(src))
    mangled = str(src).replace("/", "_").lstrip("_")
    assert (out / "files" / f"{mangled}.html").exists()
```

Add to the `TestCovReport*` area of `tests/unit/cli/test_cov.py`
(mirror the module's existing `patch.object(cov_module, ...)` idiom and
fixtures; `AsyncMock` because `report()` awaits it via `asyncio.run`):

```python
    def test_prefix_option_forwards_to_reporter(self, cov_dir):
        with patch.object(
            cov_module, "run_coverage_report", new=AsyncMock(return_value=None)
        ) as rcr:
            runner.invoke(cov_app, ["report", str(cov_dir), "--prefix", "/repo"])
        assert rcr.call_args.kwargs["prefix"] == Path("/repo")
```

- [ ] **Step 2: Run tests to verify failures**

Run: `uv run --no-sync pytest tests/unit/cov/test_html_renderer_prefix.py tests/unit/cli/test_cov.py -v -k "prefix"`
Expected: FAIL — `HtmlRenderer` has no `prefix` parameter; CLI rejects
`--prefix` as an unknown option.

- [ ] **Step 3: Implement the threading**

`src/otto/coverage/renderer/html_renderer.py` — add the keyword-only
parameter and store it (alongside `extra_markers`):

```python
    def __init__(
        self,
        output_dir: Path,
        templates_dir: Path = TEMPLATES_DIR,
        project_name: str = "Coverage Report",
        *,
        extra_markers: list[str] | None = None,
        prefix: Path | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.project_name = project_name
        self.extra_markers: list[str] = list(extra_markers or [])
        self.prefix = prefix
```

(keep the rest of `__init__` unchanged) and document it in the class
docstring's Args:

```text
        prefix: Strip this leading directory from file paths *shown* in
            the report (display only, like ``genhtml --prefix``).  Files
            outside the prefix display unchanged; links and store keys
            always use the full path.
```

Replace the `_display_path` staticmethod with an instance method (call
sites already say `self._display_path(...)`):

```python
    def _display_path(self, record: FileRecord) -> str:
        if self.prefix is not None:
            try:
                return str(record.path.relative_to(self.prefix))
            except ValueError:
                return str(record.path)
        return str(record.path)
```

`src/otto/coverage/reporter.py` — `CoverageReporter.__init__` gains a
keyword-only `prefix: Path | None = None` (stored as `self.prefix`, and
added to the class docstring Args with the same wording as the renderer);
the `HtmlRenderer(` construction in `run()` gains `prefix=self.prefix`;
`run_coverage_report` gains keyword-only `prefix: Path | None = None`
and forwards `prefix=prefix` at every `CoverageReporter(` construction
site it reaches (grep — both the legacy path and the collection-model
path must forward it).

`src/otto/cli/cov.py` — add to `report()`'s parameters (after
`project_name`):

```python
    prefix: Annotated[
        Path | None,
        typer.Option(
            "--prefix",
            help=(
                "Strip this leading directory from file paths shown in "
                "the report (display only, like genhtml --prefix). Files "
                "outside the prefix display unchanged."
            ),
        ),
    ] = None,
```

and forward `prefix=prefix` in the `run_coverage_report(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/cov/test_html_renderer_prefix.py tests/unit/cli/test_cov.py tests/unit/cov -q`
Expected: all PASS (new tests plus no regressions in the cov suites).

- [ ] **Step 5: Commit**

```bash
git add src/otto/coverage/renderer/html_renderer.py src/otto/coverage/reporter.py src/otto/cli/cov.py tests/unit/cov/test_html_renderer_prefix.py tests/unit/cli/test_cov.py
git commit -m "feat(cov): otto cov report --prefix strips a display root from report paths

Display-only genhtml --prefix analogue: labels under the prefix render
relative, links/store keys/numbers unchanged.

Assisted-by: Claude Fable 5"
```

---

### Task 6: shared fixture report builder

**Files:**
- Create: `tests/_fixtures/_report_fixture.py`
- Test: `tests/unit/cov/test_report_fixture.py`

**Interfaces:**
- Consumes: `HtmlRenderer` with the `prefix` parameter (Task 5);
  `CoverageStore`/`FileRecord`/`LineRecord`/`LineHits`/`BranchHits`
  (existing store model).
- Produces: `build_fixture_report(base_dir: Path) -> Path` — returns the
  rendered report directory. Tasks 7–9 import it from
  `tests._fixtures._report_fixture`. Displayed paths are always the
  deterministic `product/main.c` / `product/utils.c` (the builder renders
  with `prefix=base_dir`), so browser tests and the docs screenshot can
  assert/show exact path strings.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/cov/test_report_fixture.py`:

```python
"""The fixture report is consumed by the browser suite AND the docs-media
screenshot — it must render hermetically and deterministically."""

from pathlib import Path

from tests._fixtures._report_fixture import build_fixture_report


def test_fixture_report_renders(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    index = (report_dir / "index.html").read_text()
    assert "otto example product" in index
    assert "main.c" in index
    assert "System %" in index and "Unit %" in index
    file_pages = list((report_dir / "files").glob("*.html"))
    assert len(file_pages) == 2
    assert (report_dir / "static" / "report.css").exists()


def test_fixture_report_has_branch_pills(tmp_path):
    report_dir = build_fixture_report(tmp_path)
    # _file_link mangles only path SEPARATORS to "_" — "product/main.c"
    # becomes "..._product_main.c.html" (the basename keeps its dot).
    main_page = next(p for p in (report_dir / "files").glob("*main.c.html"))
    html = main_page.read_text()
    assert "branch-taken" in html
    assert "branch-not-taken" in html


def test_display_paths_are_short_and_deterministic(tmp_path):
    """The builder renders with prefix=base_dir — the screenshot and the
    browser pins both rely on the exact strings product/main.c|utils.c."""
    report_dir = build_fixture_report(tmp_path)
    index = (report_dir / "index.html").read_text()
    assert "product/main.c" in index
    assert "product/utils.c" in index
    assert str(tmp_path) not in index
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --no-sync pytest tests/unit/cov/test_report_fixture.py -v`
Expected: FAIL — `ModuleNotFoundError: tests._fixtures._report_fixture`.

- [ ] **Step 3: Implement the builder**

Create `tests/_fixtures/_report_fixture.py`:

```python
"""Deterministic coverage-report fixture.

One builder shared by the report browser suite
(tests/e2e/cov/report_browser/) and the docs-media screenshot
(scripts/capture_docs_media.py), so the pixels users see in the guide are
produced by the exact HTML the browser tests pin.

Two tiers (system, unit), two files, every pill state the renderer knows:
branch-taken, branch-not-taken, branch-unreachable — plus a fully covered
file and a partially covered one so sorting has something to reorder.
Rendered with ``prefix=base_dir`` so displayed paths are the deterministic
``product/main.c`` / ``product/utils.c`` regardless of the tmp dir.
"""

from pathlib import Path

from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    LineRecord,
)

_MAIN_C = """\
#include <stdio.h>

int checked_add(int a, int b) {
    if (a > 0 && b > 0) {
        return a + b;
    }
    return 0;
}

int main(void) {
    printf("%d\\n", checked_add(20, 22));
    return 0;
}
"""

_UTILS_C = """\
int double_it(int x) {
    return x * 2;
}

int never_called(int x) {
    return x - 1;
}
"""


def _line(number: int, hits: dict[str, int]) -> LineRecord:
    rec = LineRecord(line_number=number)
    for tier, n in hits.items():
        rec.hits.add(tier, n)
    return rec


def _branch(block: int, branch: int, hits: dict[str, int], *, reachable: bool) -> BranchHits:
    bh = BranchHits(block=block, branch=branch)
    for tier, n in hits.items():
        bh.hits.add(tier, n)
    for tier in ("system", "unit"):
        bh.set_reachable(tier, reachable)
    return bh


def build_fixture_report(base_dir: Path) -> Path:
    """Write sample sources under *base_dir* and render the report; return its dir.

    FileRecords carry absolute paths (the renderer reads the sources from
    them); ``prefix=base_dir`` makes the *displayed* paths the short
    ``product/...`` form — same strings in the browser pins and the docs
    screenshot.
    """
    src_dir = base_dir / "product"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "main.c").write_text(_MAIN_C)
    (src_dir / "utils.c").write_text(_UTILS_C)

    store = CoverageStore(tier_order=["system", "unit"])

    main_rec = FileRecord(path=src_dir / "main.c")
    for lineno, hits in [
        (3, {"system": 4, "unit": 12}),
        (4, {"system": 4, "unit": 12}),
        (5, {"system": 4, "unit": 8}),
        (7, {"unit": 4}),
        (10, {"system": 4}),
        (11, {"system": 4}),
        (12, {"system": 4}),
    ]:
        main_rec.lines[lineno] = _line(lineno, hits)
    # The `if (a > 0 && b > 0)` line: one taken pair-half, one never-taken,
    # one unreachable — all three pill classes on one line.
    main_rec.lines[4].branches = [
        _branch(0, 0, {"system": 4, "unit": 8}, reachable=True),
        _branch(0, 1, {}, reachable=True),
        _branch(0, 2, {}, reachable=False),
    ]
    store.merge_file(main_rec)

    utils_rec = FileRecord(path=src_dir / "utils.c")
    utils_rec.lines[2] = _line(2, {"unit": 6})
    utils_rec.lines[6] = _line(6, {})
    store.merge_file(utils_rec)

    report_dir = base_dir / "report"
    HtmlRenderer(
        report_dir, project_name="otto example product", prefix=base_dir
    ).render(store)
    return report_dir
```

Note: `FileRecord.lines` is the `dict[int, LineRecord]` field from
`src/otto/coverage/store/model.py` (see `get_or_create_line`); direct
assignment keeps the fixture deterministic and explicit.

- [ ] **Step 4: Run to verify passes**

Run: `uv run --no-sync pytest tests/unit/cov/test_report_fixture.py -v`
Expected: 3 PASS. If the `*main.c.html` glob finds nothing, check the
mangling in `HtmlRenderer._file_link` (only path separators become `_`;
the basename keeps its dot) and fix the test's glob, not the renderer.

- [ ] **Step 5: Commit**

```bash
git add tests/_fixtures/_report_fixture.py tests/unit/cov/test_report_fixture.py
git commit -m "test(cov): deterministic fixture report shared by browser suite and docs media

Assisted-by: Claude Fable 5"
```

---

### Task 7: browser-guard extraction and report_browser conftest

**Files:**
- Create: `tests/_fixtures/_browser_guard.py`
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (use the extracted guard)
- Create: `tests/e2e/cov/report_browser/__init__.py` (empty)
- Create: `tests/e2e/cov/report_browser/conftest.py`

**Interfaces:**
- Consumes: `build_fixture_report` (Task 6); the dist artifact path (Task 2).
- Produces: `browser_tests_could_run(config) -> bool` in `_browser_guard`;
  a `report_dir: Path` session fixture (rendered fixture report) for
  Task 8's tests, which navigate with
  `(report_dir / "index.html").as_uri()`.

- [ ] **Step 1: Extract the -m guard helper**

Create `tests/_fixtures/_browser_guard.py` by MOVING the
`_BROWSER_TEST_MARKERS` constant and `_browser_tests_could_run` function
verbatim from `tests/e2e/monitor/dashboard/conftest.py` (keep their
docstrings), renamed public:

```python
"""Shared session guard for browser-marked suites.

Both browser suites (monitor dashboard, coverage report) need the same
pytest_configure-time question answered: "could a browser-marked item
survive this session's -m filter?" — evaluated from config alone, before
collection exists, with pytest's own expression engine. See the dashboard
conftest for the full design rationale (xdist constraints, historic-hook
semantics); it moved here unchanged when the coverage-report suite arrived.
"""

import pytest
from _pytest.mark.expression import Expression

BROWSER_TEST_MARKERS = frozenset({"browser", "hostless", "xdist_group"})


def browser_tests_could_run(config: pytest.Config) -> bool:
    """Would a browser-marked item survive this session's ``-m`` filter?"""
    markexpr = config.option.markexpr
    if not markexpr:
        return True

    def matches(name: str, **_kwargs: object) -> bool:
        return name in BROWSER_TEST_MARKERS

    return Expression.compile(markexpr).evaluate(matches)
```

In `tests/e2e/monitor/dashboard/conftest.py`: delete the local
`_BROWSER_TEST_MARKERS` and `_browser_tests_could_run` (keep the big
design docstring on `pytest_configure`, adding one line noting the helper
now lives in `tests/_fixtures/_browser_guard.py`), import
`from tests._fixtures._browser_guard import browser_tests_could_run`, and
call the imported name in `pytest_configure`.

- [ ] **Step 2: Verify the dashboard suite still guards**

Run: `uv run --no-sync pytest tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: PASS (the non-browser module runs; import chain is intact).

- [ ] **Step 3: Write the report_browser conftest**

Create `tests/e2e/cov/report_browser/__init__.py` (empty file), then
`tests/e2e/cov/report_browser/conftest.py`:

```python
"""Coverage-report browser suite fixtures: a rendered fixture report on disk.

The report is static HTML opened via file:// — no server. The suite pins
the REAL rendered page (templates + report.css + the vite-built
covreport.js), so it needs the actual build: the session guard mirrors the
dashboard suite's (same rationale, see that conftest), pointing at
`make web` when the covreport bundle is missing.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

import otto.coverage.renderer as renderer_pkg
from tests._fixtures._browser_guard import browser_tests_could_run
from tests._fixtures._report_fixture import build_fixture_report

_COVREPORT_BUNDLE = Path(renderer_pkg.__file__).parent / "static" / "dist" / "covreport.js"


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast with one clear message if the covreport bundle is missing."""
    if not browser_tests_could_run(config):
        return
    if not _COVREPORT_BUNDLE.exists():
        pytest.exit(
            f"coverage-report browser tests need the built frontend bundle "
            f"({_COVREPORT_BUNDLE}); run `make web` first.",
            returncode=1,
        )


@pytest.fixture(scope="session")
def report_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One rendered fixture report per session (tests only read/click it)."""
    base = tmp_path_factory.mktemp("cov_report_fixture")
    yield build_fixture_report(base)
```

- [ ] **Step 4: Verify collection**

Run: `uv run --no-sync pytest tests/e2e/cov/report_browser -m browser --collect-only -q`
Expected: `no tests ran` / empty collection (no test files yet) with NO
errors — and if the dist is missing, the clean `make web` exit message.

- [ ] **Step 5: Commit**

```bash
git add tests/_fixtures/_browser_guard.py tests/e2e/monitor/dashboard/conftest.py tests/e2e/cov/report_browser/__init__.py tests/e2e/cov/report_browser/conftest.py
git commit -m "test(cov): report_browser suite scaffolding with shared browser guard

Assisted-by: Claude Fable 5"
```

---

### Task 8: Playwright pins for the rendered report

**Files:**
- Create: `tests/e2e/cov/report_browser/test_report_index.py`
- Create: `tests/e2e/cov/report_browser/test_report_file.py`
- Modify: `Makefile` (`dashboard` target: add the suite path)

**Interfaces:**
- Consumes: `report_dir` session fixture (Task 7); pytest-playwright's
  `page` fixture (already a dev dependency); deterministic display paths
  `product/main.c` / `product/utils.c` (Tasks 5–6).
- Produces: the browser pins CI runs; nothing downstream consumes them.

- [ ] **Step 1: Write the index-page pins**

Create `tests/e2e/cov/report_browser/test_report_index.py`:

```python
"""Pins the report index in a real browser: the built JS actually loads and
sorts, tier columns render, and no page errors fire. These assertions are
the cutover guard — a broken covreport.js path would pass string-level
tests and fail only here."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("covreport"),
]


def _open_index(page: Page, report_dir: Path) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto((report_dir / "index.html").as_uri())
    expect(page.locator("table.files-table")).to_be_visible()
    return errors


def _file_column(page: Page) -> list[str]:
    return page.locator("table.files-table tbody tr td:first-child").all_inner_texts()


def test_index_renders_without_page_errors(page: Page, report_dir: Path) -> None:
    errors = _open_index(page, report_dir)
    expect(page.locator("h1")).to_have_text("otto example product")
    assert errors == []


def test_tier_columns_render(page: Page, report_dir: Path) -> None:
    _open_index(page, report_dir)
    headers = page.locator("table.files-table thead th").all_inner_texts()
    assert any("System %" in h for h in headers)
    assert any("Unit %" in h for h in headers)


def test_click_sorts_files_and_marks_header(page: Page, report_dir: Path) -> None:
    """The real built bundle sorts the real table — the JS-loads pin.

    Display paths are deterministic (`--prefix` via the fixture), so the
    assertions are exact strings, not order relations."""
    _open_index(page, report_dir)
    file_header = page.locator("table.files-table thead th", has_text="File")
    file_header.click()
    assert _file_column(page) == ["product/main.c", "product/utils.c"]
    assert "sort-asc" in (file_header.get_attribute("class") or "")

    file_header.click()
    assert _file_column(page) == ["product/utils.c", "product/main.c"]
    assert "sort-desc" in (file_header.get_attribute("class") or "")


def test_numeric_sort_uses_data_sort(page: Page, report_dir: Path) -> None:
    """Line % sorts numerically: utils.c (partial) before main.c (full) asc."""
    _open_index(page, report_dir)
    page.locator("table.files-table thead th", has_text="Line %").click()
    assert _file_column(page) == ["product/utils.c", "product/main.c"]
```

Note: class assertions read `get_attribute("class")` directly —
Playwright's `to_have_class` matches the FULL class string (or regex),
which is brittle against neighboring classes; match whichever idiom
`tests/e2e/monitor/dashboard/` already uses if it differs.

- [ ] **Step 2: Write the file-page pins**

Create `tests/e2e/cov/report_browser/test_report_file.py`:

```python
"""Pins the annotated-source page: pill classes for every branch state,
per-line row classes, and the breadcrumb back to the index."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("covreport"),
]


def _open_main_page(page: Page, report_dir: Path) -> None:
    main_page = next((report_dir / "files").glob("*main.c.html"))
    page.goto(main_page.as_uri())
    expect(page.locator("table.source-table")).to_be_visible()


def test_branch_pills_render_all_states(page: Page, report_dir: Path) -> None:
    _open_main_page(page, report_dir)
    expect(page.locator(".branch-taken").first).to_be_visible()
    expect(page.locator(".branch-not-taken").first).to_be_visible()
    expect(page.locator(".branch-unreachable").first).to_be_visible()


def test_pill_tooltip_names_block_and_branch(page: Page, report_dir: Path) -> None:
    _open_main_page(page, report_dir)
    title = page.locator(".branch-taken").first.get_attribute("title")
    assert title is not None and "block=0" in title


def test_breadcrumb_returns_to_index(page: Page, report_dir: Path) -> None:
    _open_main_page(page, report_dir)
    page.locator(".breadcrumb a").click()
    expect(page.locator("table.files-table")).to_be_visible()
```

Note: the pill tooltip markup lives in `file.html` /
`HtmlRenderer._build_branch` (`tip` joins `block=... branch=...`); if the
attribute is not `title`, read the template and match the real attribute —
update the test, not the renderer (this suite pins, it does not change).

- [ ] **Step 3: Run the suite (expect failures only if pins mismatch reality)**

Run: `make web` (ensure the bundle exists), then:
`uv run --no-sync pytest tests/e2e/cov/report_browser -m browser -n 1 -v`
Expected: 7 PASS. Any failure here means a pin doesn't match the real
rendered page — inspect the fixture HTML under the test's tmp dir and fix
the TEST to match reality (never the renderer).

- [ ] **Step 4: Fold the suite into the browser lane**

In the `Makefile` `dashboard` target, extend the pytest path list:

```makefile
dashboard: ## Run the browser e2e suites (monitor dashboard + coverage report; needs `make browsers` once). Writes coverage data for `coverage` to append. JUnit XML in reports/junit/dashboard/.
	$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -m browser -n 1 --cov-report= --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard)
```

Run: `make dashboard`
Expected: dashboard tests + the 7 new report tests all PASS in one process.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/cov/report_browser/test_report_index.py tests/e2e/cov/report_browser/test_report_file.py Makefile
git commit -m "test(cov): Playwright pins for the rendered coverage report

Assisted-by: Claude Fable 5"
```

---

### Task 9: docs screenshot via the media pipeline

**Files:**
- Modify: `scripts/capture_docs_media.py`
- Modify: `docs/guide/coverage.md`

**Interfaces:**
- Consumes: `build_fixture_report` (Task 6 — displayed paths already
  pretty via `prefix`, no chdir needed); the existing capture flow
  (`_capture`, `ARTIFACTS`, `_STAMP_INPUTS`, `_write_placeholders`,
  `main`).
- Produces: `docs/_static/generated/coverage-report.png` at docs build
  time; the guide page references it.

- [ ] **Step 1: Extend the capture script**

In `scripts/capture_docs_media.py`:

1. Extend the promise list:

```python
ARTIFACTS = ["dashboard-live.png", "dashboard-live.webm", "coverage-report.png"]
```

2. Extend `_STAMP_INPUTS` (the media must regenerate when the report
   frontend or its fixture changes):

```python
_STAMP_INPUTS = [
    Path(__file__).resolve(),
    REPO_ROOT / "tests" / "_fixtures" / "_dashboard_harness.py",
    REPO_ROOT / "tests" / "_fixtures" / "_fake_collector.py",
    REPO_ROOT / "tests" / "_fixtures" / "_report_fixture.py",
    REPO_ROOT / "src" / "otto" / "monitor",
    REPO_ROOT / "src" / "otto" / "coverage" / "renderer",
]
```

3. In `_write_placeholders`, add alongside the dashboard placeholders:

```python
    (OUT_DIR / "coverage-report.png").write_bytes(_PLACEHOLDER_PNG)
```

4. Add the capture helper (the `tempfile` import goes at the top with
   the existing imports). The fixture renders with `prefix=` internally,
   so displayed paths are already the short `product/main.c` form — no
   cwd manipulation anywhere:

```python
def _capture_coverage_report(browser) -> None:  # noqa: ANN001 — playwright import is deferred
    from tests._fixtures._report_fixture import build_fixture_report

    with tempfile.TemporaryDirectory(prefix="otto-docs-cov-") as tmp:
        report_dir = build_fixture_report(Path(tmp))
        page = browser.new_page(viewport=_VIEWPORT)
        page.goto((report_dir / "index.html").as_uri())
        page.wait_for_selector("table.files-table")
        page.screenshot(path=OUT_DIR / "coverage-report.png", full_page=True)
        page.close()
```

5. In `_capture`, after the dashboard still-shot block (`page.close()`)
   and before the video-context block, insert:

```python
        # Still shot of the coverage HTML report (same fixture the
        # report_browser Playwright suite pins).
        _capture_coverage_report(browser)
```

- [ ] **Step 2: Verify the capture runs**

Run: `uv run --no-sync python scripts/capture_docs_media.py --mode force`
Expected: prints `captured dashboard-live.png, dashboard-live.webm,
coverage-report.png in ...s`; the PNG exists and is >20 KB:
`ls -la docs/_static/generated/coverage-report.png`

Run placeholder mode too:
`OTTO_DOCS_MEDIA=placeholder uv run --no-sync python scripts/capture_docs_media.py`
Expected: placeholder message; `coverage-report.png` exists (tiny).
Then restore real media: `uv run --no-sync python scripts/capture_docs_media.py --mode force`

- [ ] **Step 3: Embed the screenshot in the guide**

In `docs/guide/coverage.md`, directly after the opening paragraph's
numbered command list (after the `otto cov report` item, before the
`## Prerequisites` heading), insert:

```markdown
![The multi-tier coverage report: summary, legend, and a sortable per-file
table with per-tier percentage columns](../_static/generated/coverage-report.png)

*The screenshot is generated from the live report renderer at docs build
time by `scripts/capture_docs_media.py` — the same pipeline that captures
the monitor dashboard — so it can never drift from what `otto cov report`
actually produces.*
```

- [ ] **Step 4: Build the docs and verify**

Run: `make docs`
Expected: build succeeds with 0 warnings; then confirm the image landed in
the built HTML:
`grep -l "coverage-report.png" docs/_build/html/guide/coverage.html`
(adjust the build dir if `make docs` reports a different output path —
check the `docs-html` target).

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_docs_media.py docs/guide/coverage.md
git commit -m "docs(cov): build-time screenshot of the coverage report in the guide

Assisted-by: Claude Fable 5"
```

---

### Task 10: full-gate verification

**Files:** none (verification only; fix-forward anything it surfaces).

- [ ] **Step 1: Lint**

Run: `make lint`
Expected: `ruff check` and `ruff format --check` both clean. If format
rewrites anything, re-run `ruff check .` afterwards (format is not
lint-neutral in this repo).

- [ ] **Step 2: Typecheck**

Run: `make typecheck`
Expected: `ty` all checks passed.

- [ ] **Step 3: Web suite**

Run: `make web-test && make web`
Expected: vitest green (monitor + covreport tests); both builds + both
air-gap checks pass.

- [ ] **Step 4: Full coverage gate**

Run: `make coverage`
Expected: full suite green including the `dashboard` prerequisite (which
now runs the report_browser pins); coverage ≥ 94%.

- [ ] **Step 5: Docs gate**

Run: `make docs`
Expected: 0 warnings; doctests pass.

- [ ] **Step 6: Import budget**

Run: `uv run --no-sync pytest tests/unit/import_budget -q`
Expected: PASS — this effort adds no Python imports; if a snapshot drifted,
something imported eagerly that should not have.

- [ ] **Step 7: Commit any gate-driven fixes**

```bash
git status --short
# commit fixes individually with conventional messages + the Assisted-by trailer
```
