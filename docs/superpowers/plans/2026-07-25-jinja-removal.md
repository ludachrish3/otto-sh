# Jinja Removal + `--report`→`--dir` + Coverage Docs Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the retired Jinja coverage-report lane (renderer, templates, covreport bundle, jinja2 dependency), rename `otto cov report --report` to `--dir`, close out the Plan C pickup list (gate hardening + covapp polish), and expand the coverage docs: one screenshot per SPA page kind and an architecture restructure into subpages covering coverage types, data merging, and manual-coverage tracking.

**Architecture:** Pure-subtraction first (Python lane, then web lane, then packaging), each deletion step leaving the tree green; then the user-visible CLI rename with its docs sweep; then the small covapp hardening items; finally the docs expansion, which must describe the post-deletion world. Spec: `docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md` §11 (Migration), with the pickup list from Plan C's final review.

**Tech Stack:** Python 3.10+ / Typer / pytest; uv_build packaging; Vite 8 (Rolldown) + vitest for `web/`; Sphinx (`-W`, myst) docs; Playwright for docs media.

## Global Constraints

- **Behavior preservation:** deleting the Jinja lane must not change a single byte of the SPA report that `SpaRenderer` emits. The two relocated/dropped test intents are: `test_load_tolerates_absent_excluded_lines_key` → relocated to `tests/unit/cov/test_model.py` (Task 1); the ticket-in-run-chip-tooltip pin → intentionally dropped (design moved ticket surfacing to the Runs page in Plan C) and called out in Task 1's commit message. Everything else already has SPA-lane equivalents (verified in recon).
- **CLI flag exact values (Task 5):** `--dir` / `-d`, default `Path("./cov_report")` unchanged, help text exactly `"Where to place the generated coverage report."`. The old `--report` / `-r` disappears — no back-compat alias.
- **Never** add `from __future__ import annotations` (breaks Sphinx nitpicky `-W`).
- **Import budget:** never raise a cap. Deleting jinja2 must not grow any surface (it can only shrink or hold).
- **uv.lock** changes only via `uv lock` after the pyproject edit — never edited by hand, never dirtied via `uv run` side effects.
- **Docs gate = CLEAN rebuild:** `uv run sphinx-build -E -a -W -b html docs/ docs/_build/html` (the exact Makefile recipe). Incremental sphinx `-W` misses broken `{doc}`/`:class:` refs — always `-E -a`.
- **`pytest` does not build the web dist** — only `make web` does. Any browser-test verification must confirm the bundle is fresh first.
- **Dev-VM rules:** scoped pytest only, mid-task; browser runs chromium-only and serial (`-n 1` semantics — the suites already serialize via their xdist group); ONE full `make coverage` gate at plan end, run by the coordinator in the main session, never by an implementer.
- **Do NOT rename** the `covreport` xdist serial-group label (`tests/e2e/conftest.py:57`, pinned by `tests/unit/test_browser_group_policy.py::test_covreport_serial_group_matches_historical_name`) — it is a historical JUnit-classname stability contract, not a reference to the deleted lane.
- **Do NOT edit** historical records: `CHANGELOG.md` entries, anything under `docs/superpowers/specs/` or `docs/superpowers/plans/` (other than this file's checkboxes). They describe what was true when written.
- Commits in this worktree: conventional prefix + `Assisted-by: Claude Fable 5` trailer.

## File Structure (what exists after this plan)

- `src/otto/coverage/renderer/` — `__init__.py` (docstring only), `spa_renderer.py`, `spa_data.py`, `static/covapp/` (gitignored build output). `html_renderer.py`, `templates/`, `static/report.css` are gone.
- `web/` — two Vite lanes: `vite.config.ts` (dashboard) + `vite.covapp.config.ts`. `web/src/covreport/` and `vite.covreport.config.ts` are gone.
- `docs/architecture/subsystems/coverage/` — `index.md`, `types.md`, `merging.md`, `manual.md`, `renderer.md` (replaces the single `coverage.md`).
- `docs/_static/generated/` — gains `coverage-file.png`, `coverage-runs.png` next to the existing `coverage-report.png` (build-time generated, not committed).

---

### Task 1: Delete the Python Jinja lane

**Files:**
- Delete: `src/otto/coverage/renderer/html_renderer.py`, `src/otto/coverage/renderer/templates/index.html`, `src/otto/coverage/renderer/templates/file.html`, `src/otto/coverage/renderer/templates/_legend.html`, `src/otto/coverage/renderer/static/report.css`, `tests/unit/cov/test_renderer.py`, `tests/unit/cov/test_html_renderer_dist.py`, `tests/unit/cov/test_html_renderer_prefix.py`
- Modify: `src/otto/coverage/renderer/__init__.py`, `src/otto/coverage/reporter.py` (line ~444 comment), `src/otto/coverage/renderer/spa_data.py` (comment sweep), `pyproject.toml` (line 43), `scripts/import_budget.py` (lines 38, 56, 59, 61-62), `docs/getting-started.md` (line 179), `docs/api/coverage/renderer.rst`, `tests/unit/cov/test_model.py` (add one relocated test)
- Regenerate: `uv.lock` (via `uv lock`)

**Interfaces:**
- Consumes: nothing from other tasks (first task).
- Produces: `otto.coverage.renderer.__init__` becomes import-free (docstring only). Callers keep importing `from otto.coverage.renderer.spa_renderer import SpaRenderer` — no import path changes anywhere.

- [ ] **Step 1: Relocate the misfiled store-model test (write it first, watch it pass in its new home)**

`tests/unit/cov/test_renderer.py::TestExcludedLinesPersisted::test_load_tolerates_absent_excluded_lines_key` is a pure `CoverageStore.load()` back-compat test that never touches `HtmlRenderer` — it must survive the file deletion. Add it to `tests/unit/cov/test_model.py` inside the existing `class TestCoverageStore:` (match that file's import style — it already imports `CoverageStore`; use a module-level `import json` only if the file doesn't have one):

```python
    def test_load_tolerates_absent_excluded_lines_key(self, tmp_path):
        """A v4 store.json with no excluded_lines key loads to an empty set."""
        store_json = tmp_path / "store.json"
        store_json.write_text(
            json.dumps(
                {
                    "format": 4,
                    "tier_order": ["system"],
                    "files": [{"path": "/x/f.c", "lines": {}}],
                }
            )
        )
        reloaded = CoverageStore.load(store_json)
        (frec,) = list(reloaded.files())
        assert frec.excluded_lines == set()
```

Run: `uv run pytest tests/unit/cov/test_model.py -q` — expected: all pass including the new test.

- [ ] **Step 2: Delete the lane**

```bash
git rm src/otto/coverage/renderer/html_renderer.py
git rm -r src/otto/coverage/renderer/templates
git rm src/otto/coverage/renderer/static/report.css
git rm tests/unit/cov/test_renderer.py tests/unit/cov/test_html_renderer_dist.py tests/unit/cov/test_html_renderer_prefix.py
```

- [ ] **Step 3: Rewrite `src/otto/coverage/renderer/__init__.py`** (currently `from .html_renderer import HtmlRenderer` + `__all__ = ["HtmlRenderer"]` — a hard break the moment Step 2 lands, because importing `spa_renderer` triggers the package `__init__`). Replace the whole file with:

```python
"""Coverage renderer: the covapp SPA report emitted from a ``CoverageStore``.

The renderer is :class:`~otto.coverage.renderer.spa_renderer.SpaRenderer`
(imported from its submodule directly — this package deliberately re-exports
nothing, so importing it stays free for the import-budget surfaces).
"""
```

- [ ] **Step 4: Repoint `docs/api/coverage/renderer.rst`** — it currently autodocs the deleted module. Replace the automodule body:

```rst
coverage.renderer
=================

.. automodule:: otto.coverage.renderer.spa_renderer

.. automodule:: otto.coverage.renderer.spa_data
```

- [ ] **Step 5: Comment sweep in the survivors** — `grep -n "HtmlRenderer\|html_renderer\|Plan D\|jinja" src/otto/coverage/ -ri` and rewrite each hit so no comment refers to a module that no longer exists:
  - `spa_data.py:15` — module docstring has a `:class:`~otto.coverage.renderer.html_renderer.HtmlRenderer`` cross-ref (would break sphinx now that spa_data is autodoc'd). Reword to plain prose ("the retired Jinja renderer").
  - `spa_data.py:39-40` — the `TIER_LABELS` comment ("Copied from HtmlRenderer (not imported — that module dies in Plan D)"). The dict is now the sole definition; reword to: `# Pretty labels for the conventional tier names. Tiers without an entry here render with their raw name title-cased.`
  - `spa_data.py:66, 86, 281, 332, 347` — behavior-comparison comments naming HtmlRenderer: keep the behavioral fact, reword the attribution to "the retired Jinja renderer" where the historical contrast still explains a decision, or drop the clause where it no longer does.
  - `reporter.py:444` — comment "rationale as HtmlRenderer's own deferred jinja2 import, now": reword to state the import-budget rationale directly without the dead reference.
  - Leave test-file comments (`tests/e2e/cov/test_coverage_e2e.py`, `tests/unit/cov/test_spa_renderer.py`, etc.) that say "the retired HtmlRenderer" — those are accurate historical framing.

- [ ] **Step 6: Drop the jinja2 dependency**
  - `pyproject.toml` line 43: delete `"jinja2>=3.1.0",` from `[project] dependencies`.
  - Run `uv lock` (jinja2 stays in the lock as a transitive dev dependency of the sphinx toolchain — that is expected; only `otto-sh`'s own requires-dist entry disappears).
  - `scripts/import_budget.py`: `_ALL_HEAVY = ("fastapi", "uvicorn", "starlette", "pytest")` (drop `"jinja2"`); monitor surface deny `("pytest",)`; test surface deny `("fastapi", "uvicorn", "starlette")`; delete the `# cov templates the HTML report, so jinja2 is allowed here.` comment above the cov surface (the cov deny tuple itself is already jinja2-free — unchanged).
  - `docs/getting-started.md` line 179: delete the dependency-table row `| \`jinja2\` | 3.1.0 | HTML templating for coverage reports |`.

- [ ] **Step 7: Run the scoped gates**

```bash
uv run pytest tests/unit/cov tests/unit/import_budget -q
uv run nox -s lint
uv run nox -s typecheck
```

Expected: all green; import-budget snapshots unchanged (jinja2 was never on a help surface — it was import-deferred).

- [ ] **Step 8: Clean docs build** (the renderer.rst repoint + spa_data docstring edit are sphinx-facing; `make web` first because the docs build boots the real frontends for media capture):

```bash
make web
uv run sphinx-build -E -a -W -b html docs/ docs/_build/html
```

Expected: zero warnings. (The covreport lane still exists at this point — `make web` still builds all three bundles until Task 2.)

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(cov)!: delete the retired Jinja render lane (Python side)" \
  -m "html_renderer.py, its templates, report.css, and the jinja2 dependency are gone; SpaRenderer (Plan C) has been the only wired renderer since 7610d09b. The renderer package __init__ re-exports nothing. Relocated the one non-renderer test that lived in test_renderer.py (store.load excluded_lines back-compat) to test_model.py. The old ticket-in-run-chip-tooltip pin is intentionally dropped: ticket surfacing moved to the Runs page (search + detail) in the SPA design." \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 2: Delete the covreport web lane + build plumbing

**Files:**
- Delete: `web/src/covreport/main.ts`, `web/src/covreport/sort.ts`, `web/src/covreport/sort.test.ts`, `web/vite.covreport.config.ts`
- Modify: `web/package.json` (line 10), `web/tsconfig.json` (line 21), `web/vite.config.ts` (line 90), `Makefile` (multiple sites, listed below), `.gitignore` (line 55), `web/scripts/e2e_coverage_report.mjs`, `Vagrantfile` (line 396 comment)

**Interfaces:**
- Consumes: Task 1 (tree already free of Python-side references).
- Produces: `make web` builds exactly two bundles (dashboard + covapp); `$(COVREPORT_DIST)` no longer exists as a Make variable; `src/otto/coverage/renderer/static/dist/` is never created again.

- [ ] **Step 1: Delete the files**

```bash
git rm -r web/src/covreport
git rm web/vite.covreport.config.ts
```

- [ ] **Step 2: Web config plumbing**
  - `web/package.json`: delete the `"build:covreport": "vite build --config vite.covreport.config.ts",` script.
  - `web/tsconfig.json` include array: drop `"vite.covreport.config.ts"`.
  - `web/vite.config.ts` line 90: drop `"src/covreport/main.ts",` from the vitest `coverage.exclude` array.

- [ ] **Step 3: Makefile sweep** — every `COVREPORT_DIST`/covreport site:
  - `web:` target: delete the `scripts/build_web_no_warnings.sh build:covreport` line and the `scripts/check_airgap.sh src/otto/coverage/renderer/static/dist` line; update the `##` help text to "Build the web/ React dashboard + the covapp SPA (vite) into their static dist dirs, then gate both against absolute http(s) URLs … and against a resolved-brand-color regression …" (keep the parenthetical references as they are).
  - `web-clean:` target: delete `rm -rf src/otto/coverage/renderer/static/dist`; help text → "(monitor dashboard + covapp)".
  - Line ~470: delete `COVREPORT_DIST := src/otto/coverage/renderer/static/dist/covreport.js`.
  - `WEB_SRCS`: drop the `web/vite.covreport.config.ts \` line.
  - Grouped rule: `$(DASHBOARD_DIST) $(COVAPP_DIST) &: $(WEB_SRCS) $(WEB_NODE_MODULES)` (drop `$(COVREPORT_DIST)`).
  - `dashboard:` target prereqs → `$(DASHBOARD_DIST) $(COVAPP_DIST)` — **note the swap, not just a drop**: the target runs `tests/e2e/cov/report_browser` (the SPA suite), so it must depend on the covapp bundle; today's `$(COVREPORT_DIST)` prereq is a Plan-C-era leftover that only worked because the grouped rule built all three together.
  - `dashboard-soak:` target prereqs → `$(DASHBOARD_DIST)` only (the soak runs one monitor test).
  - `docs/_build/html/index.html:` rule prereqs → `$(SPHINX_SRCS) $(DASHBOARD_DIST) $(COVAPP_DIST)` — same swap-not-drop reasoning: `capture_docs_media.py` photographs the SPA report, so a fresh worktree's docs build must trigger the covapp build.
  - `wheel-check:` delete the middle assertion block (the `otto/coverage/renderer/static/dist/` count check + its FAIL/OK echos, currently lines ~358-364). The monitor block and the covapp `index.html` block stay.
- [ ] **Step 4: Remaining references**
  - `.gitignore`: delete the `src/otto/coverage/renderer/static/dist/` line.
  - `web/scripts/e2e_coverage_report.mjs`: delete the `resolve(repo, "src/otto/coverage/renderer/static/dist"),` entry from the `dists` array; reword the three comments that name covreport (lines ~28, ~72-74, ~148) to describe only the dashboard + covapp bundles.
  - `Vagrantfile` line 396 comment: "…for the web/ toolchain: dashboard/covreport" → "…dashboard/covapp".
  - `scripts/build_web_no_warnings.sh` line 16 usage comment: "(e.g. build, build:covreport)" → "(e.g. build, build:covapp)". (The grep widening is Task 3 — do not do it here.)

- [ ] **Step 5: Verify the build lane end to end**

```bash
rm -rf src/otto/coverage/renderer/static/dist
make web
test ! -d src/otto/coverage/renderer/static/dist && echo "no covreport dist — OK"
make test-ts
uv run nox -s lint
```

Expected: `make web` builds dashboard + covapp only, both airgap/brand gates pass; the deleted sort.test.ts doesn't fail vitest (file gone); lint clean. Also `grep -rn "covreport" Makefile web/ --include="*.ts" --include="*.json" --include="*.mjs"` returns nothing.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(web): delete the covreport bundle lane" \
  -m "The vanilla-TS table-sorter bundle existed only for the Jinja templates deleted in the previous commit. make web now builds two bundles (dashboard + covapp); the dashboard and docs targets' stale COVREPORT_DIST prereqs are swapped to COVAPP_DIST — the bundle those targets actually consume." \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 3: Gate hardening — TS-coverage sink guard + Rolldown warning grep

**Files:**
- Modify: `tests/_fixtures/_ts_coverage.py`, `scripts/build_web_no_warnings.sh`

**Interfaces:**
- Consumes: Task 2 (covreport bundle no longer exists, so its URL leaves the filter).
- Produces: `collect_ts_coverage(client, sink)` raises `RuntimeError` when an armed snapshot matches zero bundle URLs (signature unchanged).

- [ ] **Step 1: `tests/_fixtures/_ts_coverage.py`** — three edits in one file:
  - Filter (line 58): `if "/assets/" in url or url.endswith("covapp.js"):`
  - Docstring of `collect_ts_coverage` (lines 40-53): rewrite for two bundle shapes — the dashboard's hashed `.../dist/assets/index-*.js` and the covapp SPA's unhashed `.../dist/covapp.js` (both `file://` and served-CSP). Keep the closing rationale sentence about naming exact bundle files rather than widening to "anything under dist/".
  - Loud empty-sink guard, per-test, at the end of `collect_ts_coverage` (this is the Plan-C lesson: a filter that stops matching sinks coverage to zero *silently* and the merged gate still passes):

```python
def collect_ts_coverage(client: CDPSession, sink: list[dict]) -> None:
    data = client.send("Profiler.takePreciseCoverage")
    client.send("Profiler.stopPreciseCoverage")
    matched = False
    for entry in data["result"]:
        url = entry.get("url", "")
        if "/assets/" in url or url.endswith("covapp.js"):
            sink.append(entry)
            matched = True
    if not matched:
        seen = sorted({e.get("url", "") for e in data["result"]})[:10]
        raise RuntimeError(
            "ts-coverage: collection was armed and a snapshot taken, but no "
            "script URL matched the bundle filter — the filter and the built "
            f"bundles have drifted. URLs seen: {seen}"
        )
```

Every armed chromium browser test loads exactly one of our bundles (that is what makes it a browser test), so a zero-match snapshot is always a broken filter, not a legitimate state. The raise happens in fixture teardown → the test errors loudly.

- [ ] **Step 2: Prove the guard can fire** (temporarily change `"covapp.js"` to `"covapp.js.NOPE"` in the filter, run one armed test, watch it error with the RuntimeError, revert). This is the proven-red requirement for a new guard:

```bash
OTTO_TS_COVERAGE=1 uv run pytest tests/e2e/cov/report_browser/test_spa_csp.py -q --browser chromium 2>&1 | tail -5
# expect: errors mentioning "ts-coverage: collection was armed"
# revert the sabotage, then:
OTTO_TS_COVERAGE=1 uv run pytest tests/e2e/cov/report_browser -q --browser chromium
rm -rf reports/ts-e2e-cov/raw   # honor make's rm-and-stamp protocol: ad-hoc armed runs must not leave dumps
```

Expected: sabotaged run errors on every test; clean run 37/37. `make web` first if the bundle is stale (pytest does not build it).

- [ ] **Step 3: Widen the warning grep in `scripts/build_web_no_warnings.sh`** — Rolldown (Vite 8) emits warnings as ` WARN ` lines, not `(!)`; the current grep is blind to them (bit Plan C: an ignored-option warning sailed through). Replace the grep and message:

```bash
if grep -qE '\(!\)| WARN ' "$LOG"; then
    echo "" >&2
    echo "build_web_no_warnings: the build emitted warning(s) above — matched" >&2
    echo "'(!)' (vite/rollup) or ' WARN ' (rolldown) — during \`npm run $SCRIPT\`;" >&2
    echo "warnings are errors here. Fix the cause (for chunk-size: the budget" >&2
    echo "lives in web/vite.config.ts's chunkSizeWarningLimit and raising it" >&2
    echo "is a reviewed decision)." >&2
    exit 1
fi
```

Also update the header comment (lines 2-7) to mention both marker formats. Verify both directions: `make web` still passes; `echo "12:34:56  WARN  something" | grep -qE '\(!\)| WARN ' && echo catches`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "fix(tests): fail loudly when TS-coverage collection matches no bundle; catch Rolldown WARN lines" \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 4: Exclude sourcemaps from the wheel

**Files:**
- Modify: `pyproject.toml` (`[tool.uv.build-backend]` + its NOTE comment), `Makefile` (`wheel-check:` target)

**Interfaces:**
- Consumes: Task 2 (wheel-check no longer asserts the covreport dist).
- Produces: wheels never contain `*.map`; `make wheel-check` pins it.

- [ ] **Step 1: Add the exclude**

```toml
[tool.uv.build-backend]
module-name = "otto"
wheel-exclude = ["**/*.map"]
```

Amend the NOTE comment below it: after the "do not add a `wheel-exclude`/`source-exclude` pattern that would match `static/dist/**`, or air-gapped installs break silently" sentence, add: "The one deliberate, narrow exception is `wheel-exclude = [\"**/*.map\"]`: both bundles build with `sourcemap: \"hidden\"` (no `sourceMappingURL` comment, so nothing ever requests a map at runtime); the maps exist on disk solely for the merged TS-coverage fold, and SpaRenderer already strips them from every emitted report. Without this, a wheel built after `make web` ships ~5 MB of dead sourcemaps."

- [ ] **Step 2: Extend `wheel-check`** — append a `.map` absence assertion after the covapp `index.html` block (Makefile `$$` escaping):

```make
	@if unzip -l dist/*.whl | grep -q '\.map$$'; then \
		echo "wheel-check: FAIL — sourcemap (*.map) files embedded in the wheel; the wheel-exclude in pyproject.toml [tool.uv.build-backend] should strip them." >&2; \
		exit 1; \
	fi; \
	echo "wheel-check: OK — no *.map files in the wheel."
```

- [ ] **Step 3: Verify red-then-green** — first prove the assertion catches (comment out the `wheel-exclude` line, `make wheel-check`, expect the new FAIL; restore), then run clean:

```bash
make wheel-check
```

Expected: monitor-dist block OK, covapp index.html OK, no-maps OK. If `**/*.map` doesn't strip the maps (glob-anchoring differences between uv_build versions), try `["*.map"]` — the empirical gate is wheel-check itself.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "build: exclude hidden sourcemaps from the wheel" \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 5: CLI `--report` → `--dir` (+ reporter param rename, docs/todo sweep)

**Files:**
- Modify: `src/otto/cli/cov.py`, `src/otto/coverage/reporter.py`, `tests/unit/cli/test_cov.py` (lines 247, 477, 496, 520), `tests/e2e/cov/test_coverage_e2e.py` (lines 6, 181, 190, 708, 737), `tests/repo1/tests/test_coverage_product.py` (line 11), `tests/repo3/tests/test_embedded_coverage.py` (line 14), `docs/guide/coverage.md` (lines 450, 567, 596, 604, 682, 798, 847), `docs/guide/cli-reference.md` (lines 383, 413, 414), `todo/TODO.md` (line 51 — delete the item), `todo/gcno_mismatch_error.md` (line 17)

**Interfaces:**
- Consumes: nothing (independent of Tasks 1-4).
- Produces: `run_coverage_report(cov_dirs, output_dir, ...)` — second positional param renamed from `report_dir`. Zero keyword call sites exist (verified: `grep -rn "report_dir=" src/ tests/` → none, excluding `cov_report_dir`). `suite/run.py` calls positionally — untouched.

- [ ] **Step 1: The flag** — `src/otto/cli/cov.py` `report()` command:

```python
    report_dir: Annotated[
        Path,
        typer.Option(
            "--dir",
            "-d",
            help="Where to place the generated coverage report.",
        ),
    ] = Path("./cov_report"),
```

The Python param name stays `report_dir` deliberately: the command already has a positional `output_dirs` (the run dirs), and a local `output_dir` one letter away from it is a readability hazard. The flag string is the user contract; `-d` replaces `-r` (free on this command; matches the new name).

- [ ] **Step 2: Module docstring** (`cov.py` top) — usage line → `otto cov report RUN_DIR1 [RUN_DIR2 ...] --dir ./my_report`; the option block → ```` ``--dir PATH`` ```` / "Where to place the generated coverage report (default: ``./cov_report``)." (This docstring IS `docs/api/cli/cov.rst` — it renders verbatim.)

- [ ] **Step 3: Reporter param rename** — in `src/otto/coverage/reporter.py`, rename the `report_dir` parameter to `output_dir` in `run_coverage_report`, `_run_legacy_report`, and `_run_collection_report` (signatures, body uses, and their docstrings/Args blocks). `CoverageReporter` already uses `output_dir` — this closes the naming seam end to end: flag `--dir` → CLI `report_dir` local → library `output_dir`.

- [ ] **Step 4: Sweep every `--report` literal** — the four test-file sites and four docs/todo sites listed under **Files** (`-r` sites too, if any tests use the short form). `todo/TODO.md` line 51 (the "should be changed to --dir" item) is DONE — delete the line. `todo/gcno_mismatch_error.md:17`'s example command gets `--dir`. In `docs/guide/coverage.md` line 604 and `docs/guide/cli-reference.md` line 383, the option-table rows become `` `--dir, -d PATH` `` with the new help text.

- [ ] **Step 5: Verify**

```bash
uv run pytest tests/unit/cli/test_cov.py tests/unit/cov/test_pipeline.py tests/unit/cov/test_report_config.py -q
uv run pytest tests/e2e/cov/test_coverage_e2e.py --collect-only -q | tail -3
uv run nox -s lint && uv run nox -s typecheck
grep -rn -- "--report" src/ tests/ docs/guide/ todo/ Makefile scripts/ && echo "RESIDUAL FOUND" || echo "clean"
```

Expected: unit suites green; e2e collects (live-bed execution deferred to the bed, not this task); grep clean (hits under `docs/superpowers/` are historical and excluded from the sweep).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(cov)!: rename otto cov report --report/-r to --dir/-d" \
  -m "Closes the long-standing TODO. The reporter-layer report_dir params rename to output_dir to match CoverageReporter; all call sites are positional so only signatures change." \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 6: covapp polish (Plan C pickup items)

**Files:**
- Modify: `web/src/covapp/pages/DirectoryPage.tsx`, `web/src/covapp/pages/RunsPage.tsx`, `web/src/covapp/chrome/StatsCard.tsx`, `web/src/covapp/stats.ts`, `web/src/covapp/types.ts`, `web/src/covapp/data.ts`
- Test: `web/src/covapp/data.test.ts` (extend), existing page tests must stay green unchanged

**Interfaces:**
- Consumes: nothing.
- Produces: `stats.ts` exports `PCT_TEXT: Record<PctClass, string>`; `LineJson` gains `ticket?: string`.

- [ ] **Step 1: One `useHashLocation`** — `DirectoryPage.tsx` line 10 imports `{ useHashLocation } from "wouter/use-hash-location"` (raw wouter), bypassing the app's own query-stripping hook. Switch to the local hook (`import { useHashLocation } from "../focus";` — merge into the existing `../focus` import if one exists). Signature is drop-in (`[path, navigate]`); DirectoryPage only uses `navigate`.

- [ ] **Step 2: Hoist `PCT_TEXT`** — add to `stats.ts` (below `pctClass`):

```ts
/** Text-color class per pct bucket — the single copy (was triplicated across
 * DirectoryPage/RunsPage/StatsCard). */
export const PCT_TEXT: Record<PctClass, string> = {
  "pct-high": "text-success-primary",
  "pct-mid": "text-warning-primary",
  "pct-low": "text-error-primary",
  "pct-na": "text-quaternary",
};
```

Delete the local copies: `DirectoryPage.tsx:121-126` (`PCT_TEXT`), `RunsPage.tsx:47-52` (`PCT_TEXT`, and its lines 43-46 "kept as a local copy" comment), `StatsCard.tsx:43-49` (`PCT_COLOR` — rename its usages to `PCT_TEXT`). Import from `../stats` / `./` as each file's path requires.

- [ ] **Step 3: Contract mirror** — `types.ts` `LineJson`: add after `stale_run?`:

```ts
  /** Reserved per-line ticket slot (store v4) — emitted only when the
   * Python side has a ticket for the line; no producer fills it yet. */
  ticket?: string;
```

(`spa_data.py::_line_to_json` already emits `d["ticket"]` when `lr.ticket is not None` — the TS type was out of sync with the wire contract.)

- [ ] **Step 4: Encode the chunk URL** — `data.ts` line 117: `` script.src = `./cov_data/files/${encodeURIComponent(chunk)}.js`; `` — chunk ids come from `mangle_path` (slashes flattened) but other URL-reserved characters (`%`, `#`, `?`) in a real filename would survive mangling and truncate or corrupt the request URL.

- [ ] **Step 5: Test the encoding** — extend the existing `loadFileChunk` suite in `data.test.ts` with a chunk id containing reserved characters, using that file's existing script-element harness; the assertion:

```ts
    const chunk = "product_100%_ready#.c";
    // ... trigger loadFileChunk(chunk) per the file's existing pattern ...
    expect(script.getAttribute("src")).toBe(`./cov_data/files/${encodeURIComponent(chunk)}.js`);
```

Run: `cd web && npx vitest run src/covapp` — expected: new test fails before Step 4's change if written first (preferred order: test → red → fix → green), everything green after.

- [ ] **Step 6: Full TS gates + commit**

```bash
make lint-ts && make typecheck-ts && make test-ts
git add -A
git commit -m "refactor(covapp): single useHashLocation, shared PCT_TEXT, LineJson.ticket mirror, encoded chunk URLs" \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 7: Docs screenshots — one per SPA page kind

**Files:**
- Modify: `scripts/capture_docs_media.py` (`_capture_coverage_report`), `docs/guide/coverage.md` (embed the two new images)

**Interfaces:**
- Consumes: Task 2 (docs Makefile rule now depends on `$(COVAPP_DIST)`).
- Produces: `docs/_static/generated/coverage-file.png` and `coverage-runs.png` alongside the existing `coverage-report.png` (all build-time generated, not committed).

- [ ] **Step 1: Extend `_capture_coverage_report`** — it already renders the shared report fixture once into a tmp dir and shoots the directory page (`#/coverage`, waits on `[data-testid="tree-row-dir:product"]`, writes `coverage-report.png`). Reuse the SAME rendered report and page object for two more navigations:
  - **File page** → `coverage-file.png`: navigate to the annotated-source route for a fixture file that shows tier colors, a stale row, and branch pills (pick the file and the exact `#/...` route by reading how `tests/e2e/cov/report_browser/test_spa_file.py` navigates — it encodes the same fixture's paths); wait on the selector that suite uses for code rows before shooting.
  - **Runs page** → `coverage-runs.png`: navigate to the runs/contexts route (same method: mirror `test_spa_runs_focus.py`'s navigation); wait on its run-row selector.
  - Add both filenames to the module-level expected-outputs list (line ~71 area) so the freshness/`--mode` logic tracks them.
  - Full-page screenshots, same as the existing shot.

- [ ] **Step 2: Embed in the guide** — `docs/guide/coverage.md`:
  - The existing dir-page image (line ~19) keeps its place.
  - Add `![Annotated source view: winner-take-all row tinting, branch pills, and per-line run drilldowns](../_static/generated/coverage-file.png)` in the section that describes the per-file/annotated-source view.
  - Add `![Runs & contexts page: one row per context with per-host breakdowns and filters](../_static/generated/coverage-runs.png)` in the runs/contexts section.
  - (Find both sections by their headings; keep the guide's existing image style — plain myst image syntax as at line 19.)

- [ ] **Step 3: Verify**

```bash
uv run python scripts/capture_docs_media.py --mode force
ls -la docs/_static/generated/coverage-*.png
uv run sphinx-build -E -a -W -b html docs/ docs/_build/html
```

Expected: three coverage PNGs, each visibly the right page (open them — a NotFound screenshot passes `ls` but fails the point); clean docs build. Read each PNG to confirm the page content before committing.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(cov): screenshot every SPA page kind (directory, file, runs) in the guide" \
  -m "Assisted-by: Claude Fable 5"
```

---

### Task 8: Architecture restructure — coverage subpages (types, merging, manual, renderer)

**Files:**
- Delete: `docs/architecture/subsystems/coverage.md` (content redistributed, not lost)
- Create: `docs/architecture/subsystems/coverage/index.md`, `docs/architecture/subsystems/coverage/types.md`, `docs/architecture/subsystems/coverage/merging.md`, `docs/architecture/subsystems/coverage/manual.md`, `docs/architecture/subsystems/coverage/renderer.md`
- Modify: every cross-reference to the old doc path — known: `docs/architecture/index.rst` (toctree) and `docs/architecture/subsystems/execution.md:89` (`{doc}`../subsystems/coverage``); sweep with `grep -rn "subsystems/coverage" docs/ src/` for the rest (docstring `:doc:` refs included — they break `-W` silently on incremental builds, which is why the gate below is a clean build).

**Interfaces:**
- Consumes: Tasks 1-7 (content must describe the post-deletion, post-rename world: no Jinja, `--dir`, two bundles).
- Produces: the five-page structure other docs link to as `subsystems/coverage/index` (hub) and `subsystems/coverage/<facet>` (deep links).

The current 320-line `coverage.md` maps onto the new structure as follows (line ranges from the pre-task file):

| Current section | Lines | Destination |
|---|---|---|
| Intro + pipeline digraph + stage list | 1-55 | `index.md` |
| "Tiers and what is committed" | 56-86 | `types.md` (seed) |
| "Anchor resolution: two paths, one contract" | 87-167 | `manual.md` (seed) |
| "The store (v4)" | 168-219 | `types.md` |
| "The renderer" | 220-278 | `renderer.md` (minus the "retained but no longer wired" paragraph and the Jinja asides — that lane is now deleted; state it in one past-tense sentence) |
| "What is unique about `cov`" | 279-291 | `index.md` |
| "Where the code lives" | 292-320 | `index.md` (drop the `html_renderer` bullet) |

Beyond the moves, each facet page must actually COVER its facet (this is the point of the restructure — the current page is renderer-heavy and thin everywhere else). Write the new content from the code, not from memory or the specs; every claim should be traceable to a module named on the page. Match the existing page's voice: dense, factual, decision-rationale-first, myst markdown, `{doc}` cross-refs.

- [ ] **Step 1: Create the structure and move the mapped content** per the table. `index.md` starts with the current problem statement + digraph, then a short "how this section is organized" paragraph with the toctree:

```markdown
```{toctree}
:maxdepth: 1

types
merging
manual
renderer
```
```

- [ ] **Step 2: `types.md` — Coverage types.** Cover, deriving from `src/otto/coverage/store/model.py`, `tiers.py`, `report_config.py`, `spa_data.py`:
  - Tiers (`tier_order`, conventional `system`/`unit`/`manual`, tier colors, free-form extra tiers and how unlabeled tiers render).
  - Stat types: line / branch / decision — what each measures, `stat_types` being declarative (save-only), decision as the reserved gcno+DWARF slot rendering "no data".
  - Line states and precedence: covered-by-tier (winner-take-all by `tier_order`), `stale`, `aging`, excluded (`LCOV_EXCL`), uncoverable (no `LineRecord`); where each is computed vs stored.
  - Thresholds: `[coverage.report] high/medium`, defaults 80/70, data-driven bucketing end to end (store → `IndexPayload.thresholds` → `pctClass`).
  - The store v4 section (moved here): format pinning, `RunRecord.host`, per-line ticket slot, `excluded_lines`.

- [ ] **Step 3: `merging.md` — How coverage data is merged.** Derive from `reporter.py` (`CoverageReporter.run` stages), `merge/`, `capture/`:
  - The collection pipeline in order: `.gcda` fetch → lcov merge (per-host toolchains) → captures load → unit harvest → **manual folds LAST** (and why the order is a correctness rule, not a convenience).
  - Run accumulation: `LineRecord.run_hits` keyed by run id; per-host per-line breakdown DERIVED by grouping over `RunRecord.host`, never stored (one capture == one host == one run).
  - Supersede: same `(tier, label, host)` replaces, never accumulates (double-count rationale); the superseded capture leaves the runs table.
  - Overlapping captures on one line: no double-count, per-run traceability preserved.
  - What "merged" means across tiers vs within a tier (tiers never merge into each other — precedence, not addition).

- [ ] **Step 4: `manual.md` — Manual coverage tracking.** Seed with the moved anchor-resolution section; add, deriving from `capture/`, `validity.py`, `gitio*.py`:
  - The committed store (`.otto/coverage/manual/`) — what a manual capture records and why it's proof tied to a commit.
  - The validity engine: re-anchoring on every report; the revocation policy as implemented (`git diff -M -w --ignore-cr-at-eol` — whitespace/EOL never revoke, encoding-only DOES revoke as a documented limitation, renames followed exactly as far as `-M` tracks, splits/copies re-prove).
  - The two anchor paths (tree-diff vs blob fallback) — already moved here; tie into revert-resurrection (blob fast-path regaining credits) and loud degradation to stale (GC'd base, shallow clones).
  - Aging (`max_age`, the aging state) and dirty-remap (`✎ remapped` and what uncertainty it encodes).
- [ ] **Step 5: `renderer.md`** — the moved section, cleaned: SPA delivery model (classic IIFE, file:// + CSP), data contract (`cov_data/index.js` + per-file chunks, stamp guard), Python-side rollups, the one-sentence history note that the Jinja renderer was deleted after the SPA shipped.

- [ ] **Step 6: Re-point every cross-reference** — the two known sites plus a full sweep:

```bash
grep -rn "subsystems/coverage" docs/ src/ --include="*.md" --include="*.rst" --include="*.py"
```

Old deep links to the single page become links to the specific facet page where the anchor now lives (e.g. execution.md's pipeline reference → `{doc}`../subsystems/coverage/index``).

- [ ] **Step 7: Verify — clean build only** (incremental `-W` provably misses broken refs):

```bash
uv run sphinx-build -E -a -W -b html docs/ docs/_build/html
```

Expected: zero warnings, and the built `docs/_build/html/architecture/subsystems/coverage/index.html` exists with working toctree links.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "docs(arch): split coverage architecture into facet subpages (types, merging, manual, renderer)" \
  -m "Assisted-by: Claude Fable 5"
```

---

## Final gates (coordinator, main session — not a task)

1. Whole-branch fable review (this plan's diff vs main).
2. `make coverage` — the one full run, in the main session.
3. `make wheel-check` and the clean docs build if not already green from the last task.
4. Re-check `git merge-base main HEAD` — main can move mid-plan; rebase + re-gate if it did.
