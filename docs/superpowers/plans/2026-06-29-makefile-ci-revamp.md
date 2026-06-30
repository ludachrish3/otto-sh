# Makefile & CI Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the new linting and profiling gates into `make release` and CI, fix nox coverage-floor drift, and section the `make help` menu.

**Architecture:** Five focused edits — (1) make the import-budget caps gate from `make profile` via a shared `check_surface()` + `--check` flag in `scripts/import_budget.py`; (2) revamp the Makefile (release chain, profile recipe, sectioned help, collapsed docs); (3) align nox coverage floors with the make targets; (4) add a lint job to `ci.yml`; (5) add a lint job to `nightly.yml`. No production/library code changes — only the dev-tooling script, its test, the Makefile, the noxfile, and CI YAML.

**Tech Stack:** GNU Make, Python 3.10+ (`scripts/import_budget.py`, pytest), nox / nox_uv, GitHub Actions, ruff (lint), ty (typecheck).

## Global Constraints

- **Python floor 3.10** — use real 3.10+ annotations (`list[str]`, `int | None`). **Never** add `from __future__ import annotations` (trips otto's Sphinx-nitpicky docs gate).
- **Stage-only — never `git commit`.** End each task by `git add`-ing exactly the files it touched and recording the provided commit message for Chris to run by hand. (The repo's `prepare-commit-msg` hook needs `/dev/tty` and mis-tags agent commits.)
- **nox ↔ Makefile threshold contract:** the `*-unit` paths gate at **85%**, every full-suite path at **92%**. Keep `noxfile.py` in sync with the Makefile's `COVERAGE_THRESHOLD` (92) / `CI_COVERAGE_THRESHOLD` (85).
- **Help menu mechanism:** a target appears in `make help`'s "Other" sections **only** if its `## ` comment starts with a `(Section)` tag. Untagged `## ` lines (the test matrix) and tag-less targets are excluded by design.
- **The `help` recipe's `awk` program must stay on a single physical line.** A backslash-newline inside the single-quoted awk string is kept literally by the shell (single quotes suppress continuation), which corrupts the program. The existing recipe already follows this rule.
- Help output must render under the repo's color-free CI terminal (`NO_COLOR`); the ANSI codes are cosmetic only.

---

### Task 1: Make the import budget gate from the script (`check_surface()` + `--check`)

Today the cap / snapshot / denylist checks live only in `test_import_budget`. Extract them into one shared `check_surface()` consumed by both the test and a new `--check` flag, so `make profile` can fail on a regression without duplicating logic.

**Files:**
- Modify: `scripts/import_budget.py` (add `check_surface()`; extend `main()` with `--check`)
- Modify: `tests/unit/import_budget/test_import_budget.py:45-76` (delegate to `check_surface`; add focused tests)

**Interfaces:**
- Consumes: existing `Surface` (frozen dataclass with `.key/.argv/.deny/.cap`), `measure(argv) -> dict` (keys: `count`, `modules`, `otto_modules`, `non_stdlib_modules`), `read_snapshot(key) -> list[str]`, `SURFACES: list[Surface]`.
- Produces: `check_surface(surface: Surface, result: dict) -> list[str]` — returns human-readable violation strings (empty list = pass). New CLI flag `--check` makes `main()` return `1` on any violation.

- [ ] **Step 1: Write the failing tests for `check_surface`**

In `tests/unit/import_budget/test_import_budget.py`, add these two tests (after `test_surfaces_table_well_formed`):

```python
def test_check_surface_passes_for_real_measurement():
    surface = harness.SURFACES[0]  # import_otto
    result = harness.measure(surface.argv)
    assert harness.check_surface(surface, result) == []


def test_check_surface_flags_cap_violation():
    import dataclasses

    surface = harness.SURFACES[0]
    result = harness.measure(surface.argv)
    # Force the cap below the real count; the snapshot still matches, so only
    # the cap check fires.
    tight = dataclasses.replace(surface, cap=0)
    violations = harness.check_surface(tight, result)
    assert any("non-stdlib modules >" in v for v in violations)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py -k check_surface -v`
Expected: FAIL — `AttributeError: module 'import_budget' has no attribute 'check_surface'`.

- [ ] **Step 3: Implement `check_surface()` in `scripts/import_budget.py`**

Insert this function after `read_snapshot()` (around line 118, before `_run_hyperfine`):

```python
def check_surface(surface: Surface, result: dict) -> list[str]:
    """Return human-readable import-budget violations for a surface (empty = pass).

    Runs the three checks the unit test enforces, so the script (`--check`) and
    `tests/unit/import_budget/` share one source of truth:
      1. denylist     — heavy third-party stacks must be absent
      2. count cap     — non-stdlib module count must not exceed surface.cap
      3. golden snapshot — the otto-owned module set must match exactly
    """
    violations: list[str] = []

    leaked = [d for d in surface.deny if d in result["modules"]]
    if leaked:
        violations.append(f"`{surface.key}`: heavy modules leaked onto the path: {leaked}")

    if surface.cap is None:
        violations.append(f"`{surface.key}` has no cap set")
    else:
        non_stdlib = result["non_stdlib_modules"]
        if len(non_stdlib) > surface.cap:
            violations.append(
                f"`{surface.key}`: {len(non_stdlib)} non-stdlib modules > cap {surface.cap}. "
                f"If intentional, re-run `make import-snapshot` and raise the cap.\n"
                f"  non-stdlib modules: {non_stdlib}"
            )

    expected = read_snapshot(surface.key)
    if result["otto_modules"] != expected:
        violations.append(
            f"`{surface.key}`: otto module set changed. "
            f"If intentional, re-run `make import-snapshot` and review the diff.\n"
            f"  added:   {sorted(set(result['otto_modules']) - set(expected))}\n"
            f"  removed: {sorted(set(expected) - set(result['otto_modules']))}"
        )

    return violations
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py -k check_surface -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Refactor `test_import_budget` to delegate to `check_surface`**

Replace the body of `test_import_budget` (currently lines ~45-76, the three inline asserts) with the delegated form:

```python
@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure(surface.argv)
    violations = harness.check_surface(surface, result)
    assert not violations, "\n".join(violations)
```

- [ ] **Step 6: Run the full import-budget suite to verify the refactor is green**

Run: `uv run pytest tests/unit/import_budget/ -v`
Expected: PASS — every surface parametrization green, plus the two new `check_surface` tests.

- [ ] **Step 7: Add the `--check` flag to `main()`**

In `scripts/import_budget.py`, edit `main()`. Add the argument and the per-surface check + non-zero exit. The new lines are marked — keep the existing `--update`/`--hyperfine` behavior intact:

```python
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true", help="regenerate golden snapshots")
    ap.add_argument(
        "--check",
        action="store_true",
        help="enforce the import budget (caps + snapshots + denylist); exit non-zero on violation",
    )
    ap.add_argument("--hyperfine", action="store_true", help="also show wall-clock stats (manual)")
    args = ap.parse_args()

    # flush=True so these lines interleave correctly with hyperfine's (unbuffered)
    # subprocess output when stdout is piped/redirected (e.g. `make profile > log`).
    print(f"{'surface':14} {'total':>6} {'non_std':>7} {'otto':>5}  heavy_present", flush=True)
    failed = False
    for s in SURFACES:
        r = measure(s.argv)
        present = [d for d in s.deny if d in r["modules"]]
        non_std, otto = len(r["non_stdlib_modules"]), len(r["otto_modules"])
        print(f"{s.key:14} {r['count']:6d} {non_std:7d} {otto:5d}  {present}", flush=True)
        if args.update:
            write_snapshot(s.key, r["otto_modules"])
            print(f"  -> wrote {snapshot_path(s.key).relative_to(REPO_ROOT)} ({len(r['otto_modules'])} modules)", flush=True)
        if args.check:
            violations = check_surface(s, r)
            for v in violations:
                print(f"  FAIL {v}", flush=True)
            failed = failed or bool(violations)
        if args.hyperfine:
            _run_hyperfine(s)
    if failed:
        print("\nimport budget: FAILED — see FAIL lines above.", flush=True)
        return 1
    if args.check:
        print("\nimport budget: OK", flush=True)
    return 0
```

- [ ] **Step 8: Verify `--check` passes against the real tree and prints OK**

Run: `uv run python scripts/import_budget.py --check`
Expected: the per-surface table, then `import budget: OK`, exit code 0. Confirm the code: `echo $?` → `0`.

- [ ] **Step 9: Verify `--check` fails on a forced regression (smoke)**

Run: `uv run python -c "import runpy,sys; sys.argv=['x','--check']" ` is awkward; instead temporarily lower a cap and observe failure, then restore. Quick check:

```bash
uv run python - <<'PY'
import importlib.util, dataclasses, sys
spec = importlib.util.spec_from_file_location("ib", "scripts/import_budget.py")
ib = importlib.util.module_from_spec(spec); spec.loader.exec_module(ib)
s = ib.SURFACES[0]
r = ib.measure(s.argv)
print("real ok:", ib.check_surface(s, r) == [])
print("forced fail:", bool(ib.check_surface(dataclasses.replace(s, cap=0), r)))
PY
```

Expected: `real ok: True` and `forced fail: True`.

- [ ] **Step 10: Run lint + typecheck on the changed script**

Run: `uv run ruff check scripts/import_budget.py tests/unit/import_budget/ && uv run ruff format --check scripts/import_budget.py tests/unit/import_budget/ && uv run ty check`
Expected: all clean (no findings).

- [ ] **Step 11: Stage and record the commit message**

```bash
git add scripts/import_budget.py tests/unit/import_budget/test_import_budget.py
```

Commit message for Chris:

```
refactor(import-budget): centralize caps in check_surface() + add --check gate

Extract the denylist / count-cap / golden-snapshot checks from
test_import_budget into a shared check_surface(surface, result) in
scripts/import_budget.py. The test now delegates to it (one source of
truth), and a new `--check` flag makes the harness exit non-zero on any
violation — so `make profile` can gate on the import budget, not just the
test suite.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 2: Revamp the Makefile (release chain, profile gate, sectioned help, collapsed docs)

**Files:**
- Modify: `Makefile` (`release` recipe ~101-121; `profile` recipe ~158-159; `##` comments across the shown targets; `docs`/`docs-*`/`hyperfine` comments; `help` recipe ~306-316)

**Interfaces:**
- Consumes: `scripts/import_budget.py --check --hyperfine` (from Task 1).
- Produces: a `make help` whose "Other" targets are grouped into `Build & Release / Quality / Docs / Lab / Dev` sections via opt-in `## (Section) …` tags; a `release` chain that runs lint + profile; a `profile` target that gates.

- [ ] **Step 1: Add lint + profile to the `release` chain**

In the `release:` recipe, insert `lint` after `clean-dist` and `profile` after `nox`, and update its `## ` line with a `(Build & Release)` tag. Replace the recipe header + first chain lines:

```make
release: export PATH := $(VENV_BIN):$(PATH)
release: ## (Build & Release) lint, typecheck, docs, nox, profile, then changelog, bump, build dist (BUMP=patch|minor|major, default patch; or NEW_VERSION=X.Y.Z[rcN] for prereleases)
	@$(MAKE) clean-dist \
		&& $(MAKE) lint \
		&& $(MAKE) typecheck \
		&& $(MAKE) docs \
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox \
		&& $(MAKE) profile \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
```

(Leave the remaining `&& echo …` / changelog / bump / build lines below unchanged.)

- [ ] **Step 2: Make `profile` gate (run `--check`) and retag it**

Replace the `profile:` target:

```make
profile: hyperfine ## (Dev) Enforce the import budget (module-count caps + snapshots + denylist) + hyperfine wall-clock
	uv run python scripts/import_budget.py --check --hyperfine
```

- [ ] **Step 3: Add `(Section)` tags to the targets that should appear in help**

Edit only the `## ` text of each target below — prepend the tag, keep the rest of the description. Apply:

| Target | New `## ` text |
|---|---|
| `all` | `## (Build & Release) Run full pipeline against the dev VM (includes integration tests)` |
| `ci` | `## (Build & Release) Run pipeline without VM-dependent tests (used by GitHub Actions)` |
| `changelog` | `## (Build & Release) Regenerate CHANGELOG.md from conventional commit history (Unreleased only — does not touch released sections)` |
| `validate` | `## (Build & Release) Run validation (clean-dist, lint, typecheck, coverage, docs) without building dist` |
| `build` | `## (Build & Release) Build the project with uv` |
| `lint` | `## (Quality) Run ruff lint + format checks (part of validate/ci/all)` |
| `format` | `## (Quality) Apply ruff autoformat to the tree` |
| `typecheck` | `## (Quality) Run ty type checker (advisory during trial; not wired into all)` |
| `vm-health` | `## (Lab) Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.` |
| `qemu-restart` | `## (Lab) Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.` |
| `dev` | `## (Dev) Set up the dev environment (uv sync, git hooks, hyperfine)` |
| `schema` | `## (Dev) Generate JSON Schema for hosts.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)` |
| `import-snapshot` | `## (Dev) Regenerate import-budget golden snapshots + print per-surface counts (run after an intentional import change, then review the diff and update caps)` |
| `clean` | `## (Dev) Remove all generated artifacts` |

(`release` and `profile` were tagged in Steps 1-2.)

- [ ] **Step 4: Collapse the docs targets**

Retag `docs` and **remove** the `## ` comment from its sub-targets so they drop out of help (they remain valid targets). Apply these exact edits:

`docs:` line →
```make
docs: docs-lint docs-html doctest doctest-src ## (Docs) Build HTML docs + Sphinx & src doctests (sub-targets: docs-lint, docs-html, doctest, doctest-src, docs-inventories)
```

Strip the `## …` from each of these (keep the target + prerequisites, drop the trailing `## …`):
- `docs-lint:` → `docs-lint:` (was `## Fast doc lints …`)
- `docs-html: docs/_build/html/index.html` (was `… ## Build HTML docs only (warnings are errors)`)
- `docs-inventories:` → `docs-inventories:` (was `## Refresh vendored intersphinx inventories …`)
- `doctest:` → `doctest:` (was `## Run Sphinx doctests`)
- `doctest-src:` → `doctest-src:` (was `## Run docstring doctests in src/ …`)

Also strip the `## ` from `hyperfine` (internal sub-step that `dev`/`profile` auto-invoke):
- `hyperfine:` → `hyperfine:` (was `## Install the pinned hyperfine benchmark binary …`)

- [ ] **Step 5: Rewrite the `help` recipe's "Other targets" block as sections**

Replace the final `@grep … | awk …` block of the `help:` target with the single-physical-line awk below. **Keep the awk program on one line** (the `\` continuations below are only between the `awk` invocation and its file arg — the program string itself is unbroken):

```make
help: ## Show this help message
	@printf '\n\033[1mTesting\033[0m  (COUNT=N overrides iterations; omit the scope to run all environments)\n'
	@printf '  unit = no VMs (fast)  ·  unix = Linux VMs (incl. hops)  ·  embedded = Zephyr\n'
	@printf '  \033[36m%-31s\033[0m %s\n' 'nox-{unit,unix,embedded}'       'multi-Python matrix        (nox = all envs)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'coverage-{unit,unix,embedded}'  'pinned Python + coverage   (coverage = all, gated)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'stability-{unit,unix,embedded}' 'pinned pytest-repeat soak  (stability = all tiers)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'repeat'          'soak the full unit suite (pytest-repeat)'
	@awk 'BEGIN { FS=":.*?## "; n=split("Build & Release|Quality|Docs|Lab|Dev",order,"|") } /^[a-zA-Z_-]+:.*## \(/ { d=$$2; s=d; sub(/\).*/,"",s); sub(/^\(/,"",s); sub(/^\([^)]*\) */,"",d); items[s]=items[s] sprintf("  \033[36m%-16s\033[0m %s\n",$$1,d) } END { for(i=1;i<=n;i++) if(order[i] in items) printf "\n\033[1m%s\033[0m\n%s",order[i],items[order[i]] }' \
		$(MAKEFILE_LIST)
```

- [ ] **Step 6: Verify the help menu renders correctly**

Run: `NO_COLOR=1 make help`
Expected:
- A `Testing` block (unchanged), then five bold sections in order: `Build & Release`, `Quality`, `Docs`, `Lab`, `Dev`.
- `Build & Release` lists: `all, ci, changelog, validate, build, release` (order within a section follows file order).
- `Quality` lists `lint, format, typecheck`. `Docs` lists only `docs`. `Lab` lists `vm-health, qemu-restart`. `Dev` lists `dev, profile, schema, import-snapshot, clean`.
- **No** `docs-lint / docs-html / doctest / doctest-src / docs-inventories / hyperfine / clean-dist / help` lines appear.

If a section or target is missing, check that its `## ` text starts with exactly `(<Section>) ` and that the section name matches one of the five in the `split(...)`.

- [ ] **Step 7: Verify the gated profile and lint targets still run standalone**

Run: `make lint && make profile`
Expected: `lint` passes (ruff clean); `profile` prints the surface table, `import budget: OK`, then hyperfine benchmarks per surface, exit 0.

- [ ] **Step 8: Sanity-check the release recipe parses (dry-run, no bump)**

Run: `make -n release | head -20`
Expected: the expanded recipe shows `$(MAKE) clean-dist`, then `lint`, `typecheck`, `docs`, `nox`, `profile`, then the `NEW_VERSION=…` line — in that order. (This is a dry run; it does not bump or build.)

- [ ] **Step 9: Stage and record the commit message**

```bash
git add Makefile
```

Commit message for Chris:

```
build(make): gate profile + lint in release; section the help menu

`make release` now runs `lint` (after clean-dist) and `profile` (after
nox, before the bump) — the well-formed -> functional -> performant
ordering. `make profile` gates via `import_budget.py --check --hyperfine`.
`make help` groups the non-test targets into Build & Release / Quality /
Docs / Lab / Dev via opt-in `## (Section)` tags, and collapses the five
docs-* sub-targets (plus hyperfine) into the single `docs` entry.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 3: Align nox coverage floors with the make targets

**Files:**
- Modify: `noxfile.py:42-48` (`UNIT_TEST_ARGS`, 80 → 85) and `noxfile.py:99` (`tests_all`, 85 → 92)

**Interfaces:**
- Consumes: nothing new.
- Produces: nox `tests_unit` gates at 85% (matches `make coverage-unit`); `tests_all` gates at 92% (matches `make coverage`).

- [ ] **Step 1: Raise the `tests_unit` floor to 85 and document the contract**

Replace the `UNIT_TEST_ARGS` tuple:

```python
# Coverage floors mirror the Makefile: the *-unit paths gate at 85
# (CI_COVERAGE_THRESHOLD), every full-suite path at 92 (COVERAGE_THRESHOLD).
# Keep these in sync with the Makefile if either threshold moves.
UNIT_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded",
    "--cov-fail-under=85",
)
```

- [ ] **Step 2: Raise the `tests_all` floor to 92**

In `tests_all`, change the threshold and update its docstring line:

```python
    session.run("pytest", "--cov-fail-under=92", _junitxml(session, "nox"), *session.posargs)
```

Also update the docstring sentence in `tests_all` from `Coverage threshold matches ``make coverage`` (85%).` to `Coverage threshold matches ``make coverage`` (92%).`

- [ ] **Step 3: Verify the raised unit floor is achievable (no VMs needed)**

Run: `make coverage-unit`
Expected: PASS — the unit scope clears the 85% gate (measured ~88%). This is the same scope nox `tests_unit` runs, so it confirms the raised nox floor passes too.

- [ ] **Step 4: Verify the nox unit session itself passes at 85 under one Python**

Run: `uv run nox -s tests_unit-3.10`
Expected: PASS — session green, `Required test coverage of 85% reached`.

> Note: the full-suite 92% (`tests_all`) needs the Vagrant lab and is verified by Chris on the bed via `make nox` / `make release` pre-merge — do not run `tests_all` here.

- [ ] **Step 5: Stage and record the commit message**

```bash
git add noxfile.py
```

Commit message for Chris:

```
test(nox): align coverage floors with the make targets (unit 85, all 92)

nox tests_unit gated at 80 and tests_all at 85; both drifted from the
Makefile. Raise tests_unit to 85 (matches make coverage-unit) and
tests_all to 92 (matches make coverage). Only the *-unit paths carry the
85% floor; every full-suite path gates at 92%.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 4: Add a lint job to the main-branch CI (`ci.yml`)

**Files:**
- Modify: `.github/workflows/ci.yml` (add `lint` job; wire it into `report-failure`)

**Interfaces:**
- Consumes: `uv run nox -s lint` (existing nox session).
- Produces: a `lint` job that runs on every push/PR to `main`, mirroring `typecheck`/`docs`.

- [ ] **Step 1: Add the `lint` job**

Insert a `lint` job immediately before the `typecheck` job (after the `tests` job's block), mirroring the `docs` job's structure:

```yaml
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.0
      - uses: astral-sh/setup-uv@v8.2.0
        with:
          enable-cache: true
      - run: uv python install 3.10
      - run: uv run nox -s lint
```

- [ ] **Step 2: Wire `lint` into the failure-reporting gate**

In the `report-failure` job, add `lint` to `needs` and the issue body. Change:

```yaml
    needs: [tests, typecheck, docs]
```
to
```yaml
    needs: [lint, tests, typecheck, docs]
```

and change the issue-body line:

```yaml
            echo "Check the job logs to see which of tests / typecheck / docs failed."
```
to
```yaml
            echo "Check the job logs to see which of lint / tests / typecheck / docs failed."
```

- [ ] **Step 3: Validate the YAML structure**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml OK')"`
Expected: `ci.yml OK`. (If PyYAML is unavailable in the env, instead re-read the file and confirm the `lint` job is indented as a sibling of `tests`/`typecheck`/`docs` and `report-failure.needs` lists `lint` — GitHub validates fully on the next push.)

- [ ] **Step 4: Stage and record the commit message**

```bash
git add .github/workflows/ci.yml
```

Commit message for Chris:

```
ci: run ruff lint on every push/PR to main

Add a `lint` job (uv run nox -s lint) mirroring the typecheck/docs jobs,
and wire it into report-failure so a lint break on main opens the
tracking issue alongside tests/typecheck/docs.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

### Task 5: Add a lint job to the nightly CI (`nightly.yml`)

**Files:**
- Modify: `.github/workflows/nightly.yml` (add `lint` job; wire it into `report-failure`)

**Interfaces:**
- Consumes: `uv run nox -s lint`.
- Produces: a single (non-matrix) `lint` job in the nightly run.

- [ ] **Step 1: Add the `lint` job**

Insert a `lint` job as the first job under `jobs:` (before `stability-matrix`), matching the nightly step-naming style:

```yaml
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.0

      - name: Set up uv
        uses: astral-sh/setup-uv@v8.2.0
        with:
          enable-cache: true

      - name: Set up Python 3.10
        run: uv python install 3.10

      - name: Run lint
        run: uv run nox -s lint
```

- [ ] **Step 2: Wire `lint` into the failure-reporting gate**

In `report-failure`, add `lint` to `needs` and mention it in the body. Change:

```yaml
    needs: [stability-matrix, unit-matrix]
```
to
```yaml
    needs: [lint, stability-matrix, unit-matrix]
```

and change the body line:

```yaml
            echo "The nightly run failed (stability and/or nox unit matrix)."
```
to
```yaml
            echo "The nightly run failed (lint, stability, and/or nox unit matrix)."
```

- [ ] **Step 3: Validate the YAML structure**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/nightly.yml')); print('nightly.yml OK')"`
Expected: `nightly.yml OK`. (Fallback as in Task 4 Step 3 if PyYAML is absent.)

- [ ] **Step 4: Stage and record the commit message**

```bash
git add .github/workflows/nightly.yml
```

Commit message for Chris:

```
ci(nightly): add a ruff lint job

Add a single lint job (uv run nox -s lint) to the nightly run and wire it
into report-failure so a lint break surfaces in the nightly tracking
issue alongside the stability and unit matrices.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Self-Review

**Spec coverage:**
- TODO 1 (profiling + linting in `make release`) → Task 1 (gateable profile) + Task 2 Steps 1-2 (lint + profile in chain). ✓
- TODO 2 (nox 92%, *-unit 85%) → Task 3. ✓
- TODO 3 (help menu) → Task 2 Steps 3-6. ✓
- TODO 4 (lint in nightly + main CI) → Tasks 4 & 5. ✓
- Spec §1a (`check_surface` + `--check`, test delegation) → Task 1. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code and YAML block is complete and copy-paste-ready. ✓

**Type consistency:** `check_surface(surface: Surface, result: dict) -> list[str]` is defined in Task 1 Step 3 and consumed identically by the test (Step 5) and `main()` (Step 7). The `--check`/`--hyperfine` flags referenced by Task 2 Step 2 (`make profile`) exist after Task 1 Step 7. The `(Section)` tag strings used in Task 2 Steps 3-4 match the five names in the `split(...)` of Step 5 exactly. ✓
