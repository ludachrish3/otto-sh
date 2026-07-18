# Makefile Python↔TS Quality/Test Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every quality/test aspect gets `<verb>-python`, `<verb>-ts`, and bare `<verb>` targets; the TS side gains knip and a merged (vitest + Playwright-e2e) gated coverage report so it meets the same standards as Python.

**Architecture:** Pure-Makefile renames land first (static family, format, guard-test/CI rewiring), then the e2e TS coverage plumbing (hidden sourcemaps → Chromium CDP collection in pytest → node-side conversion via monocart-coverage-reports → nyc merged gate), then the coverage/validate umbrella rewiring, then the docs sweep.

**Tech Stack:** GNU Make, ruff/ty (Python), Biome/tsc/vitest/knip/monocart-coverage-reports/nyc (TS), pytest + Playwright (CDP), Vite hidden sourcemaps.

**Spec:** `docs/superpowers/specs/2026-07-17-makefile-quality-parity-design.md`

## Global Constraints

- Never `from __future__ import annotations` (trips Sphinx nitpicky `-W`).
- npm deps are exact-pinned: always `npm install -D -E <pkg>` (repo convention — every version in package.json is exact).
- Never invoke release-flow tools via `uv run` (dirties uv.lock).
- After every task: `make lint-python` must pass; if the task touched Python sources, also `make typecheck-python` (ty runs only at nox/typecheck time — budget it now, not at the end).
- No heavy/parallel soak loads on the dev VM — run each gate once; never brute-force repeat.
- This is a worktree branch (`worktree-makefile-quality-parity`): self-commit with conventional prefix + `Assisted-by: Claude (Fable 5)` trailer.
- Do NOT edit files under `todo/` (Chris's own notes).
- The pre-topology TS coverage floor is 85/78/83/86 (statements/branches/functions/lines); the current vitest unit floor is 81/73/80/82 and STAYS as the unit-tier floor.
- Regression guards must be **proven red** against the pre-fix state before they count.
- Keep `make -n` safety: never put `$(MAKE)` on a recipe line of a target someone would dry-run (see the `release:`/`wheel-check:` comments in the Makefile) — the new artifact rules use prerequisite recursion, matching `wheel-check`'s pattern.

---

### Task 1: Static-family cutover (lint/typecheck/check/test-ts + CI + guard test)

**Files:**
- Modify: `Makefile` (lines 3, 270–311, 551–571 in the current file — the `.PHONY` list, the web quality lane block, and the Quality section at the bottom)
- Modify: `.github/workflows/ci.yml` (web-quality job, ~lines 122–147)
- Modify: `tests/unit/test_ci_web_gate.py` (rewrite pins to the new chain)

**Interfaces:**
- Produces make targets later tasks depend on: `lint-ts`, `typecheck-ts`, `check-python`, `check-ts`, `check`, `test-ts`, `coverage-ts-unit`, and `validate-ts` (transitional = `check-ts coverage-ts-unit`; Task 7 adds `coverage-ts`).
- Retires: `web-lint`, `web-format-check`, `web-biome`, `web-typecheck`, `web-coverage`, `web-check`, `web-test`. (`web-format` is retired in Task 3 with the format semantics change.)

- [ ] **Step 1: Plant a proven-red fixture for the weak-alias trap**

Create `web/src/_lint_trap.ts` (temporary, deleted in Step 6):

```ts
import { useState } from "react";
import React from "react";

export function trap() {
  return [React, useState];
}
```

Run: `cd web && npm run lint` (the OLD rules-only gate) — expected: PASSES (unsorted/unorganized imports are an assist, invisible to `biome lint`).
Run: `cd web && npm run check` — expected: FAILS (organize-imports assist). This is the trap the rename fixes; keep the file until the new target proves red on it.

- [ ] **Step 2: Rewrite the Makefile's static family**

In the web-lane block (current lines 273–311), delete the targets `web-lint`, `web-format-check`, `web-biome`, `web-typecheck`, `web-coverage`, `web-check`, and `web-test`. Keep `web-format` (Task 3 retires it). In their place (same location, so the web build/dev family stays grouped):

```make
# web/ quality lanes moved to the language-parity family (lint-ts /
# typecheck-ts / coverage-ts-unit / test-ts) in the Quality section below —
# one name per aspect, no web-* aliases. web-install/web/web-dev/web-clean
# stay here: they are artifact/dev targets, not language-parity gates.

test-ts: $(WEB_NODE_MODULES) ## (Dev) Run the web/ vitest suite once — no coverage, the fast TS loop. (Deliberately no test-python twin and no bare `test`: the fast Python lane is `coverage-unit`.)
	cd web && npm run test
```

Replace the Quality section (current lines 551–571) with:

```make
lint: lint-python lint-ts ## (Quality) Lint ALL code (Python + TS): sub-targets lint-python + lint-ts

lint-python: ## (Quality) Run ruff lint + format checks (part of check-python)
	uv run ruff check .
	uv run ruff format --check .

# `biome check` = lint rules + formatting + ASSIST actions (organize-imports).
# `biome lint` + `biome format` together are STRICTLY WEAKER: neither reports
# an assist action, so unsorted imports pass both and fail `biome check`. That
# gap sat on main undetected while CI hand-listed sub-targets — see
# tests/unit/test_ci_web_gate.py, which pins this chain. This target is the
# single authoritative Biome gate; there is deliberately NO weaker TS lint.
lint-ts: $(WEB_NODE_MODULES) ## (Quality) Lint web/: the authoritative Biome gate (rules + format + assists)
	cd web && npm run check

typecheck: typecheck-python typecheck-ts ## (Quality) Type-check ALL code (Python + TS): sub-targets typecheck-python + typecheck-ts

typecheck-python: ## (Quality) Run ty type checker
	uv run ty check

typecheck-ts: $(WEB_NODE_MODULES) ## (Quality) Type-check web/ with tsc --noEmit (no build)
	cd web && npm run typecheck

check: check-python check-ts ## (Quality) ALL static analysis (Python + TS): sub-targets check-python + check-ts

check-python: lint-python typecheck-python ## (Quality) All Python static analysis: ruff (lint+format) + ty

check-ts: lint-ts typecheck-ts ## (Quality) All TS static analysis: Biome (+ knip, Task 2) + tsc

coverage-ts-unit: $(WEB_NODE_MODULES) ## (Quality) Run the web/ vitest suite with v8 coverage and enforce the UNIT-tier floor (the TS analogue of coverage-hostless's reduced CI gate; the full merged gate is coverage-ts)
	cd web && npm run test:coverage
```

Update `validate-ts` (current line 191) to:

```make
validate-ts: check-ts coverage-ts-unit ## (Build & Release) TypeScript validation: Biome, tsc, vitest coverage floor
```

Update the `release` recipe's `$(MAKE) web-check` line (current line 139) to `$(MAKE) validate-ts`, and its `##` help text word `web-check` to `validate-ts`.

Update `.PHONY` (line 3): remove `web-test web-lint web-format-check web-biome web-typecheck web-coverage web-check`; add `test-ts coverage-ts-unit check check-python check-ts`. (`lint-ts`, `typecheck-ts`, `format-ts`, `coverage-ts`, `validate-ts` are already listed.)

Delete the now-orphaned `coverage-ts: web-coverage` alias at current line 311 — `coverage-ts` returns as a real target in Task 7. Temporarily point the `coverage` target's final line (current line 362) from `$(MAKE) web-coverage` to `$(MAKE) coverage-ts-unit` (Task 8 replaces this wiring entirely).

- [ ] **Step 3: Rewire CI's web-quality job**

In `.github/workflows/ci.yml`, replace the web-quality job's gate step and its comment block:

```yaml
  # web/ TypeScript quality gates — the TS analogue of the lint/typecheck jobs.
  # Node-only: no Python, no browsers, no dashboard build, so it runs fast and
  # in parallel. Both targets take the node_modules stamp as a prerequisite, so
  # they run npm ci themselves — no explicit install step.
  #
  # TWO GATES, NOT A RE-LISTED UMBRELLA: this job invokes `check-ts` (Biome +
  # knip + tsc) and `coverage-ts-unit` (vitest unit floor) — the two TS gates
  # that can run without browsers. It deliberately does NOT re-enumerate any
  # gate's INTERNALS: a CI job that copies a gate's steps is a second copy of
  # that gate, and it will drift (unsorted imports once sat on main this way).
  # The third TS gate — the merged e2e coverage report (`coverage-ts`) — needs
  # the Playwright browser lane and runs locally via `make coverage`/release,
  # mirroring how the Python 95-gate is local-only while CI gates hostless.
  # `tests/unit/test_ci_web_gate.py` pins this.
  web-quality:
    name: web-quality
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.0
      - name: Set up Node
        uses: actions/setup-node@v6.4.0
        with:
          node-version-file: .nvmrc
      - name: web/ static gates (Biome check + tsc) + vitest unit coverage floor
        run: make check-ts coverage-ts-unit
```

- [ ] **Step 4: Rewrite the guard test**

Replace the body of `tests/unit/test_ci_web_gate.py` (keep module-style docstring conventions; adjust wording):

```python
"""CI's web-quality job must invoke the TS gates, not re-list their internals.

The job used to hand-list `web-check`'s sub-targets and the list silently
drifted from the gate it was copying: `biome lint` + `biome format` do NOT
report Biome's ASSIST actions (organize-imports), so unsorted imports passed
CI while failing `biome check`. The web-check umbrella was later folded into
the language-parity family (spec 2026-07-17-makefile-quality-parity): the
job now calls `check-ts` (whose lint leg IS `biome check`) plus the vitest
unit floor `coverage-ts-unit`. These pins keep both the CI invocation and
the Makefile chain from drifting back to something weaker.
"""

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
_MAKEFILE = (_REPO / "Makefile").read_text()


def _web_quality_runs() -> list[str]:
    ci = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["web-quality"]["steps"]
    return [step["run"] for step in steps if "run" in step]


def test_ci_invokes_the_ts_gates_not_their_internals() -> None:
    runs = _web_quality_runs()
    assert runs == ["make check-ts coverage-ts-unit"], (
        "CI's web-quality job must invoke `make check-ts coverage-ts-unit` — "
        "the browserless TS gates — in ONE step, not re-list any gate's "
        f"internals (drift risk). Got: {runs!r}"
    )


def test_check_ts_chain_reaches_biome_check() -> None:
    """Pins the chain: check-ts -> lint-ts -> `npm run check` (biome check)."""
    check_ts = re.search(r"^check-ts:([^\n#]*)", _MAKEFILE, re.MULTILINE)
    assert check_ts, "no `check-ts` target in the Makefile"
    assert "lint-ts" in check_ts.group(1), (
        "`check-ts` no longer depends on `lint-ts`, so CI is not running the "
        "authoritative Biome gate"
    )
    lint_ts = re.search(r"^lint-ts:.*(?:\n\t.+)+", _MAKEFILE, re.MULTILINE)
    assert lint_ts, "no `lint-ts` target in the Makefile"
    assert "npm run check" in lint_ts.group(0), (
        "`lint-ts` must run `npm run check` (biome check = rules + format + "
        "assists); anything weaker reopens the organize-imports gap"
    )


def test_coverage_ts_unit_runs_the_vitest_floor() -> None:
    cov = re.search(r"^coverage-ts-unit:.*\n\t(.+)$", _MAKEFILE, re.MULTILINE)
    assert cov, "no `coverage-ts-unit` target in the Makefile"
    assert "npm run test:coverage" in cov.group(1), (
        "`coverage-ts-unit` must enforce the vitest unit-tier coverage floor"
    )
```

- [ ] **Step 5: Run the guard test — verify it fails against nothing / passes against the new state**

Run: `uv run pytest tests/unit/test_ci_web_gate.py -v` — expected: 3 PASS.
Proven-red check: temporarily change the ci.yml run line to `make web-check`, rerun — expected: FAIL on the first test. Revert.

- [ ] **Step 6: Prove the new lint-ts red on the planted trap, then remove it**

Run: `make lint-ts` — expected: FAIL naming `_lint_trap.ts` (assist finding). Then `rm web/src/_lint_trap.ts` and rerun `make lint-ts` — expected: PASS.

- [ ] **Step 7: Verify retirements and green tree**

Run: `make web-lint` — expected: `make: *** No rule to make target 'web-lint'`. Same for `web-check`, `web-biome`, `web-test`.
Run: `make check` — expected: ruff + ty + biome + tsc all green.
Run: `make lint-python && uv run pytest tests/unit/test_ci_web_gate.py tests/unit -q` — expected: green (full unit tier catches any conftest fallout).

- [ ] **Step 8: Commit**

```bash
git add Makefile .github/workflows/ci.yml tests/unit/test_ci_web_gate.py
git commit -m "refactor(make)!: language-parity static family (lint/typecheck/check, -python/-ts)

lint-ts is now the authoritative biome check (the old lint-ts alias ran
rules-only web-lint, strictly weaker); check-X = lint-X + typecheck-X;
web-* quality aliases retired; CI web-quality job and its guard test pin
the new chain.

Assisted-by: Claude (Fable 5)" 
```

---

### Task 2: knip — project-scope dead-code/unused-deps gate in lint-ts

**Files:**
- Create: `web/knip.json`
- Modify: `web/package.json` (devDependency + script)
- Modify: `Makefile` (`lint-ts` gains the knip leg)
- Modify: `tests/unit/test_ci_web_gate.py` (pin knip into the chain)

**Interfaces:**
- Produces: `npm run knip` and `lint-ts` = `npm run check` + `npm run knip`. Later tasks treat `lint-ts` as opaque.

- [ ] **Step 1: Install and configure**

```bash
cd web && npm install -D -E knip
```

Create `web/knip.json`:

```json
{
  "$schema": "./node_modules/knip/schema.json",
  "entry": ["src/main.tsx", "src/covreport/main.ts"],
  "project": ["src/**/*.{ts,tsx}"],
  "ignore": [
    "src/components/**",
    "src/styles/**",
    "src/utils/cx.ts",
    "src/utils/is-react-component.ts",
    "src/hooks/use-breakpoint.ts",
    "src/hooks/use-resize-observer.ts",
    "src/api/types.gen.ts",
    "src/api/export.gen.ts"
  ]
}
```

(The ignore list mirrors Biome's `files.includes` exclusions — vendored Untitled UI source and generated wire types; see `web/README.md`'s vendor-boundary rationale. knip auto-detects the vite/vitest plugins from package.json.)

Add to `web/package.json` scripts: `"knip": "knip"`.

- [ ] **Step 2: Triage the first run**

Run: `cd web && npm run knip`.
For every finding, decide per the keep-rules-enforced principle: real dead code → **delete the code**; a false positive with a nameable mechanism (e.g. a dependency referenced only from CSS, like `@fontsource-variable/inter` or the tailwind plugins) → add to `ignoreDependencies` in knip.json with a one-line comment in the commit message naming the mechanism. Do NOT blanket-ignore categories. If a finding is ambiguous, check usage with `grep -rn <name> web/src` before deciding.

Expected steady state: `npm run knip` exits 0.

- [ ] **Step 3: Proven-red check**

Append to any non-vendored file (e.g. `web/src/api/format.ts` or similar existing module):

```ts
export function _knipCanary() {
  return 42;
}
```

Run: `cd web && npm run knip` — expected: FAIL (unused export `_knipCanary`). Remove the canary; rerun — expected: PASS.

- [ ] **Step 4: Wire into lint-ts and pin**

`lint-ts` recipe becomes:

```make
lint-ts: $(WEB_NODE_MODULES) ## (Quality) Lint web/: the authoritative Biome gate (rules + format + assists) + knip (unused exports/files/deps)
	cd web && npm run check
	cd web && npm run knip
```

In `tests/unit/test_ci_web_gate.py::test_check_ts_chain_reaches_biome_check`, extend the lint-ts assertion:

```python
    assert "npm run knip" in lint_ts.group(0), (
        "`lint-ts` must also run knip — the project-scope unused-code parity "
        "for what ruff already does on the Python side"
    )
```

- [ ] **Step 5: Verify and commit**

Run: `make lint-ts && uv run pytest tests/unit/test_ci_web_gate.py -q && make lint-python` — expected: green.

```bash
git add web/knip.json web/package.json web/package-lock.json Makefile tests/unit/test_ci_web_gate.py
# plus any dead code deleted in Step 2
git commit -m "feat(web): knip dead-code/unused-deps gate, wired into lint-ts

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: `format` = all safe autofixes

**Files:**
- Modify: `Makefile` (format family, current lines 559–564; retire `web-format`)
- Modify: `web/package.json` (prune orphaned scripts)

- [ ] **Step 1: Rewrite the format family**

```make
format: format-python format-ts ## (Quality) Apply ALL safe autofixes (Python + TS): sub-targets format-python + format-ts

# "format" means: after this, everything auto-fixable that `make lint` gates
# is fixed — not merely reformatted. That is why the Python leg runs ruff's
# safe lint fixes before the formatter (fixes can need reformatting), and the
# TS leg runs `biome check --write` (biome format alone cannot apply assist
# actions like organize-imports, which lint-ts gates).
format-python: ## (Quality) Apply ruff safe lint autofixes + autoformat
	uv run ruff check --fix .
	uv run ruff format .

format-ts: $(WEB_NODE_MODULES) ## (Quality) Apply Biome fixes to web/: rules + format + assists (`biome check --write`)
	cd web && npm run check:fix
```

Delete the `web-format` target and remove it from `.PHONY`.

- [ ] **Step 2: Prune orphaned npm scripts**

In `web/package.json`, delete the `lint`, `format`, and `format:check` scripts (their only callers were the retired make targets; `check`/`check:fix` are the surviving Biome surface). Run `cd web && npm run knip` — knip must stay green (no script references broken).

- [ ] **Step 3: Verify behavior**

Plant `web/src/_fmt_trap.ts`:

```ts
import { useState } from "react";
import React from "react";

export const fmtTrap = () => [React, useState];
```

Keep it standalone and uncommitted (knip would rightly flag it):
Run: `make lint-ts` — expected FAIL (assists). Run: `make format-ts` then `cd web && npx biome check src/_fmt_trap.ts` — expected: organize-imports fixed (imports reordered in the file). Then `rm web/src/_fmt_trap.ts`.
Python leg: run `make format-python` on the clean tree — expected: "All checks passed" style output, zero diff (`git status --short` unchanged). This confirms --fix on a clean tree is a no-op, i.e. the behavior change only bites when there is something to fix.

- [ ] **Step 4: Gate and commit**

Run: `make check && make lint-python` — green; `git diff --stat` shows only Makefile + package.json.

```bash
git add Makefile web/package.json
git commit -m "refactor(make)!: format = all safe autofixes (ruff --fix + biome check --write)

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: Hidden sourcemaps + vitest JSON output

**Files:**
- Modify: `web/vite.config.ts` (build.sourcemap + coverage.reporter)
- Modify: `web/vite.covreport.config.ts` (build.sourcemap)

**Interfaces:**
- Produces: every built bundle ships `<asset>.js.map` beside it (no `sourceMappingURL` comment); `npm run test:coverage` emits `web/coverage/coverage-final.json` (istanbul JSON). Tasks 5–7 depend on both.

- [ ] **Step 1: Configure**

In `web/vite.config.ts`, inside the `build` options (add `build` section if the dashboard config lacks one — check first with `grep -n "build" web/vite.config.ts`):

```ts
  build: {
    // Hidden sourcemaps: emitted for the merged TS coverage gate
    // (make coverage-ts maps Chromium V8 coverage of THIS shipped bundle back
    // to web/src), never referenced from the bundle. They ride along in dist
    // and the wheel — that is the price of certifying the real artifact
    // instead of an instrumented second build.
    sourcemap: "hidden",
  },
```

Same `sourcemap: "hidden"` in `web/vite.covreport.config.ts`'s build options (it has a `build` section configuring the covreport output — extend it).

In `web/vite.config.ts` coverage options, change `reporter: ["text", "html"]` to `reporter: ["text", "html", "json"]` with a trailing comment: `// json feeds the merged gate (make coverage-ts)`.

- [ ] **Step 2: Build and verify the artifact contract**

Run: `make web` (rebuilds both bundles + runs the airgap/brand gates — they must stay green with maps present).
Verify:

```bash
ls src/otto/monitor/static/dist/assets/*.js.map src/otto/coverage/renderer/static/dist/covreport.js.map
grep -L sourceMappingURL src/otto/monitor/static/dist/assets/*.js   # every file listed (no reference)
```

Expected: maps exist; grep lists every .js (i.e. none contains the comment).

- [ ] **Step 3: Verify vitest JSON**

Run: `make coverage-ts-unit` then `ls web/coverage/coverage-final.json` — expected: file exists; spot-check a key is an absolute path under `web/src`:

```bash
python3 -c "import json; print(sorted(json.load(open('web/coverage/coverage-final.json')))[:3])"
```

- [ ] **Step 4: Commit**

```bash
git add web/vite.config.ts web/vite.covreport.config.ts
git commit -m "feat(web): hidden sourcemaps on both bundles + vitest json coverage output

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: Chromium CDP coverage collection in the browser e2e suites

**Files:**
- Create: `tests/_fixtures/_ts_coverage.py`
- Modify: `tests/e2e/monitor/dashboard/conftest.py` (register fixtures)
- Modify: `tests/e2e/cov/report_browser/conftest.py` (register fixtures)

**Interfaces:**
- Produces: after any chromium run of either suite, `reports/ts-e2e-cov/raw/cdp-<pid>-<hex>.json` files, each `{"result": [<CDP ScriptCoverage entries for our bundles>]}`. Task 6 consumes exactly this shape.

- [ ] **Step 1: Write the collection helper**

`tests/_fixtures/_ts_coverage.py`:

```python
"""Chromium V8 JS-coverage collection for the browser e2e suites.

Feeds the merged TS coverage gate (``make coverage-ts``): raw CDP precise-
coverage dumps land in ``reports/ts-e2e-cov/raw/`` and are converted to
istanbul JSON on the web side (``web/scripts/e2e_coverage_report.mjs``, via
the hidden sourcemaps built next to the dist bundles). Chromium-only by
design — coverage numbers are engine-independent, the same reason
``make coverage`` pins a single Python — and skipped for ``soak`` (per-call
CDP overhead on the SSE firehose is exactly what that test measures without).

Intra-test full navigations (``page.goto`` twice in one test) drop the first
page's V8 data — precise coverage reports only currently-loaded scripts.
Same-document hash navigation (the dashboard's routing) is unaffected, and
the suite-wide accumulation makes per-test loss statistically irrelevant;
do not add per-navigation flushing complexity for it.
"""

import json
import os
import uuid
from pathlib import Path

from playwright.sync_api import CDPSession, Page

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RAW_DIR = _REPO_ROOT / "reports" / "ts-e2e-cov" / "raw"


def start_ts_coverage(page: Page) -> CDPSession:
    """Begin precise V8 coverage on the page's main frame target."""
    client = page.context.new_cdp_session(page)
    client.send("Profiler.enable")
    client.send("Profiler.startPreciseCoverage", {"callCount": False, "detailed": True})
    return client


def collect_ts_coverage(client: CDPSession, sink: list[dict]) -> None:
    """Take the coverage snapshot and keep only our served bundles."""
    data = client.send("Profiler.takePreciseCoverage")
    client.send("Profiler.stopPreciseCoverage")
    for entry in data["result"]:
        url = entry.get("url", "")
        if "/assets/" in url or url.endswith("covreport.js"):
            sink.append(entry)


def write_ts_coverage(sink: list[dict]) -> None:
    """Persist one raw dump per pytest session (per xdist worker)."""
    if not sink:
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"cdp-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    out.write_text(json.dumps({"result": sink}))
```

- [ ] **Step 2: Register in both browser-suite conftests**

Read each conftest first; append (imports at top, fixtures at bottom, matching local style):

```python
from tests._fixtures._ts_coverage import (
    collect_ts_coverage,
    start_ts_coverage,
    write_ts_coverage,
)


@pytest.fixture(scope="session")
def _ts_coverage_sink():
    entries: list[dict] = []
    yield entries
    write_ts_coverage(entries)


@pytest.fixture(autouse=True)
def _ts_coverage(page, browser_name, request, _ts_coverage_sink):
    """Per-test V8 coverage; suite-wide accumulation. See _ts_coverage.py."""
    if browser_name != "chromium" or request.node.get_closest_marker("soak"):
        yield
        return
    client = start_ts_coverage(page)
    yield
    collect_ts_coverage(client, _ts_coverage_sink)
```

(If the conftests import fixtures via a different established mechanism — e.g. `pytest_plugins` or re-export from `tests/_fixtures/__init__` — follow that pattern instead; the fixture bodies are identical.)

- [ ] **Step 3: Prove collection works (and no-ops off-chromium)**

```bash
rm -rf reports/ts-e2e-cov
uv run pytest tests/e2e/monitor/dashboard -m "browser and not soak" --browser chromium -n 1 --no-cov -k "review_shell" -x -q
ls reports/ts-e2e-cov/raw/            # expected: one cdp-*.json
python3 - <<'EOF'
import json, glob
d = json.load(open(glob.glob("reports/ts-e2e-cov/raw/*.json")[0]))
urls = {e["url"] for e in d["result"]}
assert any("/assets/" in u for u in urls), urls
print("ok:", len(d["result"]), "entries")
EOF
rm -rf reports/ts-e2e-cov
uv run pytest tests/e2e/monitor/dashboard -m "browser and not soak" --browser firefox -n 1 --no-cov -k "review_shell" -x -q
ls reports/ts-e2e-cov 2>&1            # expected: No such file or directory (no-op off-chromium)
```

- [ ] **Step 4: Full-lane + static gates**

Run: `make dashboard` (chromium lane end-to-end with the fixture live) — expected: green, raw dump present.
Run: `make lint-python && make typecheck-python` — expected: green (new Python module).

- [ ] **Step 5: Commit**

```bash
git add tests/_fixtures/_ts_coverage.py tests/e2e/monitor/dashboard/conftest.py tests/e2e/cov/report_browser/conftest.py
git commit -m "feat(tests): chromium CDP TS-coverage collection in the browser e2e suites

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: Conversion to istanbul JSON + Makefile artifact plumbing

**Files:**
- Create: `web/scripts/e2e_coverage_report.mjs`
- Modify: `web/package.json` (monocart dep + script)
- Modify: `Makefile` (raw stamp + artifact rules; dashboard recipe additions)

**Interfaces:**
- Consumes: Task 5's raw dumps; Task 4's dist + `.map` files.
- Produces: `reports/ts-e2e-cov/istanbul/coverage-final.json` (istanbul JSON keyed by the same absolute `/…/web/src/…` paths vitest uses) and the make variables/rules `TS_E2E_RAW_STAMP`, `TS_E2E_COV`. Task 7 consumes both.

- [ ] **Step 1: Install monocart-coverage-reports**

```bash
cd web && npm install -D -E monocart-coverage-reports
```

- [ ] **Step 2: Write the converter**

`web/scripts/e2e_coverage_report.mjs`:

```js
// Converts the raw Chromium CDP precise-coverage dumps written by the browser
// e2e suites (tests/_fixtures/_ts_coverage.py -> reports/ts-e2e-cov/raw/)
// into istanbul JSON under reports/ts-e2e-cov/istanbul/, resolving served
// URLs back to web/src through the HIDDEN sourcemaps that `make web` builds
// beside each dist file. A missing .map is a hard error: it means
// build.sourcemap regressed and the merged TS gate would silently lose its
// e2e leg — the gate must fail loudly instead.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CoverageReport } from "monocart-coverage-reports";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const rawDir = resolve(repo, "reports/ts-e2e-cov/raw");
const outDir = resolve(repo, "reports/ts-e2e-cov/istanbul");
const dists = [
  resolve(repo, "src/otto/monitor/static/dist"),
  resolve(repo, "src/otto/coverage/renderer/static/dist"),
];

// Vendored Untitled UI source + generated wire types: excluded from coverage
// the same way Biome/vitest/knip exclude them (web/README.md, vendor
// boundary). Bootstrap entrypoints (main.tsx, covreport/main.ts) are
// deliberately INCLUDED here — the e2e leg is precisely what exercises them
// (vitest excludes them for the opposite reason).
const EXCLUDED = new Set([
  "src/utils/cx.ts",
  "src/utils/is-react-component.ts",
  "src/hooks/use-breakpoint.ts",
  "src/hooks/use-resize-observer.ts",
]);
const excluded = (p) =>
  !p.startsWith("src/") ||
  p.includes(".test.") ||
  /^src\/(components|styles)\//.test(p) ||
  /^src\/api\/(types|export)\.gen\.ts$/.test(p) ||
  EXCLUDED.has(p);

function distFileFor(url) {
  const path = new URL(url).pathname;
  for (const dist of dists) {
    const candidate = resolve(dist, `.${path}`);
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`e2e_coverage_report: no dist file serves ${url}`);
}

const dumps = existsSync(rawDir)
  ? readdirSync(rawDir).filter((f) => f.endsWith(".json"))
  : [];
if (!dumps.length) {
  throw new Error(
    "e2e_coverage_report: no raw dumps in reports/ts-e2e-cov/raw — run `make dashboard` (chromium) first",
  );
}

const entries = [];
for (const f of dumps) {
  const dump = JSON.parse(readFileSync(resolve(rawDir, f), "utf8"));
  for (const entry of dump.result) {
    const file = distFileFor(entry.url);
    const map = `${file}.map`;
    if (!existsSync(map)) {
      throw new Error(
        `e2e_coverage_report: missing hidden sourcemap ${map} — build.sourcemap regressed?`,
      );
    }
    entries.push({
      ...entry,
      source: readFileSync(file, "utf8"),
      sourceMap: JSON.parse(readFileSync(map, "utf8")),
    });
  }
}

const report = new CoverageReport({
  name: "otto web e2e coverage",
  outputDir: outDir,
  reports: [["json", { file: "coverage-final.json" }]],
  sourceFilter: (sourcePath) => !excluded(sourcePath),
  // Key the istanbul JSON by the same absolute paths vitest's
  // coverage-final.json uses, so nyc merges them as one file set.
  sourcePath: (filePath) => resolve(repo, "web", filePath),
});
await report.add(entries);
await report.generate();
console.log(`e2e_coverage_report: wrote ${resolve(outDir, "coverage-final.json")}`);
```

Add to `web/package.json` scripts: `"e2e:coverage-report": "node scripts/e2e_coverage_report.mjs"`.

API caveat (not a placeholder — an adaptation point): `sourceFilter`'s argument shape and `sourcePath`'s callback signature must be verified against the installed version's README (`web/node_modules/monocart-coverage-reports/README.md`). If the resolved source paths arrive with a `webpack://`/`vite` prefix or as `../../src/...`, normalize inside `sourcePath` until Step 4's key-alignment check passes. The exclusion SEMANTICS above are fixed; only the path plumbing may need adjusting.

- [ ] **Step 3: Makefile plumbing**

Below the `DASHBOARD_DIST`/`COVREPORT_DIST` block, add:

```make
# Merged-TS-coverage inputs. The browser lane (dashboard) dumps raw Chromium
# V8 coverage (tests/_fixtures/_ts_coverage.py); its recipe touches the raw
# stamp. The istanbul artifact is source-stamped like DASHBOARD_DIST: a cold
# or stale `make coverage-ts` re-runs the (chromium) browser lane itself —
# honest, if heavy; the fast no-coverage loop is `make test-ts`. Prerequisite
# recursion (not $(MAKE) in a dry-runnable recipe) per the wheel-check note.
TS_E2E_RAW_STAMP := reports/ts-e2e-cov/raw/.stamp
TS_E2E_COV := reports/ts-e2e-cov/istanbul/coverage-final.json
BROWSER_TEST_SRCS := $(shell find tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -name '*.py') tests/_fixtures/_ts_coverage.py

$(TS_E2E_RAW_STAMP): $(WEB_SRCS) $(BROWSER_TEST_SRCS)
	$(MAKE) dashboard

$(TS_E2E_COV): $(TS_E2E_RAW_STAMP) $(WEB_NODE_MODULES) web/scripts/e2e_coverage_report.mjs
	cd web && npm run e2e:coverage-report
```

Edit the `dashboard` recipe: FIRST line `@rm -rf reports/ts-e2e-cov/raw` (stale dumps from an older bundle must not pollute), LAST line `@mkdir -p reports/ts-e2e-cov/raw && touch $(TS_E2E_RAW_STAMP)`. (dashboard-soak gets neither — its fixture no-ops.)

- [ ] **Step 4: Key-alignment verification (the merge's load-bearing invariant)**

```bash
rm -rf reports/ts-e2e-cov
make dashboard
cd web && npm run e2e:coverage-report && cd ..
python3 - <<'EOF'
import json
e2e = set(json.load(open("reports/ts-e2e-cov/istanbul/coverage-final.json")))
vit = set(json.load(open("web/coverage/coverage-final.json")))
sample = sorted(e2e)[:3]
assert all(p.startswith("/") and "/web/src/" in p for p in sample), sample
overlap = e2e & vit
print(f"e2e files={len(e2e)} vitest files={len(vit)} overlap={len(overlap)}")
assert overlap, "no common file keys — sourcePath normalization is wrong"
assert any(p.endswith("TopologyPage.tsx") for p in e2e), "TopologyPage missing from e2e leg"
EOF
```

(Run `make coverage-ts-unit` first if `web/coverage/coverage-final.json` is missing.) Expected: nonzero overlap and TopologyPage present. If the assertions fail, fix `sourcePath` in the converter — do not weaken the assertions.

- [ ] **Step 5: Gates + commit**

Run: `make lint-python` (Makefile-only Python impact: none, but cheap) and `make lint-ts` (Biome sees the new .mjs; fix any findings properly).

```bash
git add web/scripts/e2e_coverage_report.mjs web/package.json web/package-lock.json Makefile
git commit -m "feat(web): convert e2e CDP dumps to istanbul JSON (monocart) + make artifact plumbing

Assisted-by: Claude (Fable 5)"
```

---

### Task 7: `coverage-ts` — the merged, gated TS report

**Files:**
- Modify: `web/package.json` (nyc dep + merged-gate script)
- Modify: `Makefile` (real `coverage-ts` target)
- Modify: `web/vite.config.ts` (threshold comment rewrite — numbers unchanged)

**Interfaces:**
- Consumes: `TS_E2E_COV` (Task 6), vitest JSON (Task 4), `coverage-ts-unit` (Task 1).
- Produces: `make coverage-ts` — the full TS coverage gate. Task 8 wires it into `coverage`/`validate-ts`.

- [ ] **Step 1: Install nyc and add the merged-gate script**

```bash
cd web && npm install -D -E nyc
```

`web/package.json` scripts (thresholds start at the pre-topology floor; Step 3 adjusts upward from measurement):

```json
"coverage:merged": "nyc report --temp-dir ../reports/ts-cov/final --report-dir ../reports/ts-cov/html --reporter text --reporter html --check-coverage --statements 85 --branches 78 --functions 83 --lines 86"
```

- [ ] **Step 2: The Makefile target**

In the Quality section, after `coverage-ts-unit`:

```make
# The FULL TS coverage gate: vitest (unit) + the Playwright e2e leg, merged
# into ONE istanbul report and gated at the merged floor. The vitest-only
# floor (coverage-ts-unit, enforced inside vite.config.ts) is the reduced
# browserless tier CI runs — the exact analogue of coverage-hostless's 90 vs
# the full gate's 95 on the Python side.
coverage-ts: $(TS_E2E_COV) ## (Quality) Merged TS coverage gate: vitest + browser-e2e legs, one report, one floor (see also coverage-ts-unit)
	cd web && npm run test:coverage
	rm -rf reports/ts-cov/final && mkdir -p reports/ts-cov/final
	cp web/coverage/coverage-final.json reports/ts-cov/final/vitest.json
	cp $(TS_E2E_COV) reports/ts-cov/final/e2e.json
	cd web && npm run coverage:merged
```

Add `coverage-ts` to `.PHONY` if it was removed in Task 1.

- [ ] **Step 3: Measure, then set the floor (gate must be able to fail)**

```bash
make coverage-ts   # first run: may FAIL check-coverage if merged < 85/78/83/86
```

Record the four merged numbers from nyc's table. Then:
- If every number ≥ its pre-topology floor (85/78/83/86): set each threshold in `coverage:merged` to `max(pre-topology, floor(measured) - 2)`.
- If any number is BELOW its pre-topology floor: STOP. The spec's premise (the drop was e2e-covered code) is wrong for that axis — investigate which files drag it and report to Chris before proceeding. Do not lower the floor to pass.

Proven-red: temporarily set `--statements 99`, run `make coverage-ts` — expected FAIL from nyc check-coverage. Restore. Rerun — expected PASS.

TopologyPage check (the motivating file):

```bash
python3 - <<'EOF'
import json
cov = json.load(open("reports/ts-cov/final/e2e.json"))
topo = [v for k, v in cov.items() if k.endswith("TopologyPage.tsx")]
assert topo, "TopologyPage.tsx not in merged e2e leg"
hit = sum(1 for c in topo[0]["s"].values() if c > 0)
total = len(topo[0]["s"])
print(f"TopologyPage statements covered: {hit}/{total}")
assert hit > 0
EOF
```

- [ ] **Step 4: Rewrite the vite.config.ts threshold comment**

Replace the lowered-thresholds comment block (current lines ~85–95) above `thresholds:` with:

```ts
      // UNIT-TIER floor (browserless; what CI's web-quality job gates via
      // `make coverage-ts-unit`). The FULL floor lives in the merged gate
      // (`make coverage-ts`, web/package.json's coverage:merged): it folds in
      // the Playwright e2e leg, which is where TopologyPage.tsx and the
      // bootstrap entrypoints are exercised — the reason these numbers sit
      // below the merged gate's (the vitest leg alone cannot see e2e-only
      // coverage). Raise these only from measured vitest-only output.
```

Numbers stay 81/73/80/82.

- [ ] **Step 5: Gates + commit**

Run: `make coverage-ts && make lint-ts && uv run pytest tests/unit/test_ci_web_gate.py -q` — green.

```bash
git add web/package.json web/package-lock.json web/vite.config.ts Makefile
git commit -m "feat(make): coverage-ts — merged vitest+e2e TS coverage, gated at the restored floor

Assisted-by: Claude (Fable 5)"
```

---

### Task 8: coverage-python + umbrella/pipeline rewiring

**Files:**
- Modify: `Makefile` (`coverage` family, `validate-python`, `validate-ts`, `COVERAGE_TARGET`, `release`, `.PHONY`)

**Interfaces:**
- Consumes: `coverage-ts` (Task 7), `check-python` (Task 1).
- Produces: `coverage-python`, rewired `coverage`/`validate*`/`release`. Task 9 sweeps docs.

- [ ] **Step 1: Split the coverage umbrella**

Replace the current `coverage:` target (dashboard prereq + pytest + coverage-ts-unit bolt-on) with:

```make
coverage-python: dashboard ## Run the full Python suite (all tiers, pinned Python) and enforce the 95 gate; the browser (Playwright) suite runs first as its own process via the `dashboard` prerequisite — its coverage data is folded in via --cov-append. Requires lab VMs (+ `make browsers` once). JUnit XML lands in reports/junit/coverage-python/.
	$(TIMEOUT_CMD) uv run pytest -m "not stability and not browser" --cov-append --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage-python)

coverage: coverage-python coverage-ts ## Run BOTH language coverage gates: coverage-python (full pytest, 95 floor) + coverage-ts (merged vitest+e2e floor). The dashboard browser lane runs exactly once — coverage-python triggers it, and coverage-ts's artifact stamp sees it fresh.
```

(Keep the long comment block that precedes the old `coverage` target — it documents the browser/marker interplay and still applies; move it above `coverage-python`.)

Change `COVERAGE_TARGET ?= coverage` (line 24) to `COVERAGE_TARGET ?= coverage-python` and update its comment (validate-python's coverage leg is Python-only now; `ci` still overrides with `coverage-hostless`).

- [ ] **Step 2: Umbrellas**

```make
validate-python: ## (Build & Release) Python validation (clean-dist, static checks, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) check-python \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

validate-ts: check-ts coverage-ts ## (Build & Release) TypeScript validation: Biome+knip, tsc, merged coverage gate (unit floor runs inside it via test:coverage; CI's browserless slice is check-ts + coverage-ts-unit)
```

`validate` and `all` stay as-is (their bodies already compose the renamed pieces).

- [ ] **Step 3: Release order**

In the `release` recipe, the `$(MAKE) validate-ts` line (from Task 1) moves to AFTER `$(MAKE) dashboard-all` — the all-engine browser run has just refreshed the raw-coverage stamp, so validate-ts's merged gate reuses it instead of re-running a chromium lane:

```make
	@$(MAKE) clean-dist \
		&& $(MAKE) web-install \
		&& $(MAKE) check-python \
		&& $(MAKE) docs \
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox \
		&& $(MAKE) web \
		&& $(MAKE) dashboard-all \
		&& $(MAKE) validate-ts \
		&& $(MAKE) profile \
		&& NEW_VERSION=...   # (rest of the recipe unchanged)
```

Also update the `release` target's `##` help text: "Python lint+typecheck" → "Python static checks (check-python)", "full TS gate (web-check)" → "full TS gate (validate-ts, incl. merged coverage)".

- [ ] **Step 4: Verify the once-only dashboard invariant**

```bash
rm -rf reports/ts-e2e-cov
make -n coverage 2>/dev/null | grep -c "pytest tests/e2e/monitor/dashboard"
```

Expected: `make -n` shows the plan; then run `make coverage` for real and confirm in the output that the dashboard pytest banner appears exactly once. (This is the full local gate — lab VMs must be up; it also proves the whole chain end-to-end.)

- [ ] **Step 5: Commit**

```bash
git add Makefile
git commit -m "refactor(make)!: coverage = coverage-python + coverage-ts; validate/release rewired

Assisted-by: Claude (Fable 5)"
```

---

### Task 9: Docs sweep, section banners, retired-name purge, final gate

**Files:**
- Modify: `docs/contributing.md` (web-lanes table, ~lines 155–175)
- Modify: `docs/guide/monitor.md` (line ~340: `make web-test` → `make test-ts`)
- Modify: `Makefile` (section banner comments; `help` target sanity)

- [ ] **Step 1: Rewrite the contributing.md web-lanes table**

Replace the table at docs/contributing.md:159–171 with:

```markdown
| Aspect       | Python                    | TS                          | Both              |
| ------------ | ------------------------- | --------------------------- | ----------------- |
| Lint         | `make lint-python`        | `make lint-ts` (Biome check + knip) | `make lint`       |
| Type-check   | `make typecheck-python`   | `make typecheck-ts`         | `make typecheck`  |
| All static   | `make check-python`       | `make check-ts`             | `make check`      |
| Autofix      | `make format-python`      | `make format-ts`            | `make format`     |
| Fast tests   | `make coverage-unit`      | `make test-ts`              | —                 |
| Coverage gate| `make coverage-python`    | `make coverage-ts` (merged vitest+e2e; unit floor: `coverage-ts-unit`) | `make coverage` |
| Everything   | `make validate-python`    | `make validate-ts`          | `make validate`   |
```

Adjust the surrounding prose to match (the paragraph after the table describes the umbrella relationships; keep the tier/marker axes doc unchanged).

- [ ] **Step 2: Sweep remaining references**

```bash
grep -rn "web-lint\|web-format\|web-biome\|web-typecheck\|web-coverage\|web-check\|web-test" \
  --include='*.md' --include='*.yml' --include='*.py' --include='Makefile' . \
  | grep -v node_modules | grep -v _build | grep -v "^./todo/" | grep -v docs/superpowers
```

Expected after fixes: zero hits outside `todo/` (Chris's notes — leave) and the spec/plan documents (historical record — leave). Fix `docs/guide/monitor.md`'s `make web-test` → `make test-ts` and anything else the grep surfaces.

- [ ] **Step 3: Makefile section banners**

Add banner comments (comment-only change) delimiting the families, in file order:

```make
# ═══ Build & Release pipeline ═══════════════════════════════════════════════
# ═══ Test & Coverage (Python tiers + TS legs) ═══════════════════════════════
# ═══ Quality: static analysis + autofix ═════════════════════════════════════
# ═══ Docs ═══════════════════════════════════════════════════════════════════
# ═══ Lab ════════════════════════════════════════════════════════════════════
# ═══ Dev environment ════════════════════════════════════════════════════════
```

Then run `make help` and eyeball: every new target appears under its `(Quality)`/`(Dev)`/`(Build & Release)` category; no stale names in the Testing header lines (update the `coverage-*` line's wording: bare `coverage` = both languages).

- [ ] **Step 4: Full final gate**

```bash
make check                    # all static, both languages
make coverage                 # full Python 95-gate + merged TS gate (lab VMs up)
uv run nox -s dashboard       # three-engine browser matrix — the fixture must be engine-safe
make docs                     # clean Sphinx build (docs edits above)
uv run pytest tests/unit -q   # unit tier incl. the rewritten guard test
```

All green before the effort is called done. (`nox -s dashboard` is mandatory: the browser lane changed and bare pytest runs chromium only.)

- [ ] **Step 5: Commit**

```bash
git add docs/contributing.md docs/guide/monitor.md Makefile
git commit -m "docs(make): language-parity target tables + Makefile section banners; retire web-* names

Assisted-by: Claude (Fable 5)"
```
