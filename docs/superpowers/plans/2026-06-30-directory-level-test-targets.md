# Directory-level test targets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a directory-based, cumulative *level* axis (`unit` ⊆ `integration` ⊆ all) to the `make`/`nox` test grid, alongside the existing marker-based *resource* axis (`unix`/`embedded`), and give the e2e tier its own auto-stamped marker.

**Architecture:** Two orthogonal axes. **Level** targets select by *path* (`tests/unit`, `tests/unit tests/integration`, all dirs). **Resource** targets select by *marker* (unchanged). The CI no-testbed gate moves off the `unit` name to a new `tests_hostless`/`coverage-hostless` that selects `not integration and not embedded and not stability` over `tests/unit tests/e2e` — identical to today's `tests_unit` set, so coverage floors are unchanged and the other agent's future `hostless` e2e tests are auto-included.

**Tech Stack:** GNU Make, nox (`nox_uv` sessions), pytest (markers + path selection), GitHub Actions YAML.

**Spec:** [docs/superpowers/specs/2026-06-30-directory-level-test-targets-design.md](../specs/2026-06-30-directory-level-test-targets-design.md)

## Global Constraints

- **Do NOT self-commit in otto-sh.** The `prepare-commit-msg` hook needs `/dev/tty`; agent commits mis-tag the AI-assist trailer. Each task ends by **staging** (`git add <paths>`) and surfacing a **paste-able commit message** for Chris to run. Never run `git commit`.
- **CHANGELOG.md is auto-managed — do not touch it.**
- **Never `from __future__ import annotations`** — it trips otto's Sphinx-nitpicky docs gate. Use real 3.10+ annotations and module-top imports.
- **Keep the `integration` marker** (directory-auto-stamped). The new `e2e` marker is an orthogonal *level* tag; resource markers on individual e2e tests stay.
- **Coverage floors are unchanged:** CI no-testbed gate = 85 (nox) / 90 (make `CI_COVERAGE_THRESHOLD`); full suite = 92 (nox) / 94 (make `COVERAGE_THRESHOLD`).
- **nox `lint` = `ruff check .` + `ruff format --check .`.** After editing/adding Python files, run `ruff format` then re-run `ruff check` (format is not lint-neutral).
- **Worktree dev env** is already materialized via `uv sync`. Do not run heavy/parallel test loads on the dev VM; single no-VM passes only.
- **Pinned Python in the worktree is whatever `uv run` resolves;** the plan uses `-3.12` for single-Python nox spot-checks. The full matrix (`make nox`) and full-lab gate (`make coverage`) require VMs — defer those to the lab/Chris.

---

### Task 1: e2e auto-stamp marker

Give the e2e tier a directory-derived `e2e` marker, mirroring `tests/integration/conftest.py`. This is the one behavioral change and gets a TDD cycle via a new drift guard (G3) next to the existing G1/G2.

**Files:**
- Create: `tests/e2e/conftest.py`
- Modify: `pyproject.toml` (markers list, after line 187)
- Test: `tests/unit/test_tier_marker_invariants.py` (add G3)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `tests/e2e/conftest.py::pytest_collection_modifyitems(config, items)` that stamps the `e2e` marker on every item under `tests/e2e/`. The `e2e` marker is registered in `pyproject.toml`. Later tasks (Makefile/nox) rely only on the `e2e` tier being collectable as before — the new marker is additive.

- [ ] **Step 1: Write the failing drift guard (G3)**

Append to `tests/unit/test_tier_marker_invariants.py`:

```python
def test_e2e_conftest_autostamps_e2e():
    """G3: the e2e/ conftest stamps `e2e` by directory (mirrors G1)."""
    from tests.e2e import conftest as e2e

    e2e_root = Path(e2e.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(e2e_root / "configmodule" / "test_example.py")
    e2e.pytest_collection_modifyitems(config=None, items=[item])
    assert "e2e" in item.added
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_tier_marker_invariants.py::test_e2e_conftest_autostamps_e2e -v -p no:cacheprovider --no-cov`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` (there is no `tests/e2e/conftest.py` yet).

- [ ] **Step 3: Register the `e2e` marker**

In `pyproject.toml`, add this line to the `markers = [...]` list immediately after the `concurrency:` entry (line 187):

```toml
    "e2e: end-to-end-tier test, auto-stamped from tests/e2e/ (level axis; orthogonal to the integration/embedded resource markers)",
```

- [ ] **Step 4: Create the e2e conftest (mirrors `tests/integration/conftest.py`)**

Create `tests/e2e/conftest.py`:

```python
"""End-to-end tier conftest — auto-stamp the ``e2e`` marker from the path.

Mirrors ``tests/integration/conftest.py``: the ``tests/e2e/`` directory is the
single source of truth for the e2e tier (level axis). Resource markers
(``integration``/``embedded``) stay explicit on the e2e tests that need a
testbed, so this hook is additive — it only tags the tier.
"""

from pathlib import Path

_E2E_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``e2e`` marker to every test under this tree.

    Idempotent and additive — explicit ``integration``/``embedded`` resource
    markers on individual e2e tests are left untouched.
    """
    for item in items:
        if _E2E_ROOT in item.path.parents:
            item.add_marker("e2e")
```

- [ ] **Step 5: Run the drift guard to verify it passes**

Run: `uv run pytest tests/unit/test_tier_marker_invariants.py -v -p no:cacheprovider --no-cov`
Expected: PASS — G1, G2, and the new G3 all green.

- [ ] **Step 6: Verify the marker actually stamps under real collection, and nothing broke**

Run: `uv run pytest tests/e2e -m "e2e and not integration and not embedded" --collect-only -q -p no:cacheprovider --no-cov`
Expected: `completion_cache` test(s) are collected (they are e2e + no-testbed); no `PytestUnknownMarkWarning` (the marker is registered). The `integration`-marked e2e tests (docker_e2e, sprout cov) are deselected by `not integration`.

Run: `uv run pytest tests/e2e/configmodule/test_completion_cache.py -q -p no:cacheprovider --no-cov`
Expected: PASS — the new conftest did not disturb the existing no-testbed e2e test.

- [ ] **Step 7: Lint the new/edited Python**

Run: `uv run ruff format tests/e2e/conftest.py tests/unit/test_tier_marker_invariants.py && uv run ruff check tests/e2e/conftest.py tests/unit/test_tier_marker_invariants.py`
Expected: format leaves them unchanged (or reformats), then `check` passes.

- [ ] **Step 8: Stage and prepare the commit message (do NOT commit)**

```bash
git add pyproject.toml tests/e2e/conftest.py tests/unit/test_tier_marker_invariants.py
```
Paste-able message for Chris:
```
test(tier): auto-stamp the e2e marker from tests/e2e/ + G3 drift guard

Mirror tests/integration/conftest.py for the e2e tier: register an `e2e`
marker and stamp it by directory. Resource markers on individual e2e tests
are untouched. Adds G3 to test_tier_marker_invariants.py.
```

---

### Task 2: Makefile — level targets + CI gate rewire

Replace the marker-based `unit` selection with directory-based level targets, add `coverage-integration` and the new no-testbed `coverage-hostless` gate, and point `make ci` at the gate.

**Files:**
- Modify: `Makefile` — `.PHONY` (line 3), the `M_*` block (lines 56–65), `ci` (line 94), the `coverage*` targets (lines 166–176), the help block (lines 309–314).

**Interfaces:**
- Consumes: the `e2e` marker (Task 1) only indirectly (e2e tests still collect).
- Produces: make targets `coverage-unit` (= `pytest tests/unit`), `coverage-integration` (= `pytest tests/unit tests/integration`), `coverage-hostless` (= the CI gate). `make ci` now runs `coverage-hostless`.

- [ ] **Step 1: Replace the `M_*` marker block**

In `Makefile`, replace lines 56–65 (the comment block + `M_UNIT`/`M_UNIX`/`M_EMBEDDED`):

```makefile
# Two axes of test selection (see docs/contributing.md → Regression-test
# categories). Keep these in sync with noxfile.py.
#   Level (directory, cumulative) — selected by PATH, in the coverage-*/nox-*
#   targets below:
#     unit        — tests/unit
#     integration — tests/unit + tests/integration
#     (bare)      — all three tiers (tests/unit + tests/integration + tests/e2e)
#   Resource (marker, orthogonal) — selected by MARKER:
#     unix     — real telnet/SSH against the Linux Vagrant VMs (incl. multi-hop)
#     embedded — Zephyr/QEMU under the zephyr VM
#     hostless — needs no testbed at all (what CI gates on): tests/unit + the
#                no-VM e2e tests. Mirrors noxfile.py tests_hostless.
M_UNIX := integration and not embedded
M_EMBEDDED := embedded
M_HOSTLESS := not integration and not embedded and not stability
```

- [ ] **Step 2: Rewrite the `coverage*` targets**

Replace lines 166–176 (`coverage`, `coverage-unit`, `coverage-unix`, `coverage-embedded`) with:

```makefile
coverage: ## Run the full suite (all tiers, pinned Python) and enforce the coverage gate (excludes heavy `stability`). Requires lab VMs. JUnit XML lands in reports/junit/coverage/.
	$(TIMEOUT_CMD) uv run pytest -m "not stability" --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage)

coverage-unit: ## Run the unit level tier (tests/unit only; no testbed) with a coverage report (no gate — one tier can't meet the whole-repo floor). JUnit XML lands in reports/junit/coverage-unit/.
	$(TIMEOUT_CMD) uv run pytest tests/unit -m "not stability" $(call junitxml,coverage-unit)

coverage-integration: ## Run the unit + integration level tiers (tests/unit + tests/integration) with a coverage report (no gate). Requires the full lab. JUnit XML in reports/junit/coverage-integration/.
	$(TIMEOUT_CMD) uv run pytest tests/unit tests/integration -m "not stability" $(call junitxml,coverage-integration)

coverage-hostless: ## Run the no-testbed CI gate suite (tests/unit + no-VM e2e) and enforce the CI coverage gate. No VMs. JUnit XML lands in reports/junit/coverage-hostless/.
	$(TIMEOUT_CMD) uv run pytest tests/unit tests/e2e -m "$(M_HOSTLESS)" --cov-fail-under=$(CI_COVERAGE_THRESHOLD) $(call junitxml,coverage-hostless)

coverage-unix: ## Run the Unix-VM resource slice (incl. multi-hop) with a coverage report (no gate). Requires lab VMs. JUnit XML in reports/junit/coverage-unix/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_UNIX)" $(call junitxml,coverage-unix)

coverage-embedded: ## Run the embedded (Zephyr) resource slice with a coverage report (no gate). Requires Vagrant lab up. JUnit XML in reports/junit/coverage-embedded/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_EMBEDDED)" $(call junitxml,coverage-embedded)
```

- [ ] **Step 3: Point `make ci` at the no-testbed gate**

In `Makefile` line 94, change the coverage target:

```makefile
	@$(MAKE) validate COVERAGE_TARGET=coverage-hostless \
```
(was `COVERAGE_TARGET=coverage-unit`).

- [ ] **Step 4: Update `.PHONY`**

In `Makefile` line 3, add `coverage-integration coverage-hostless` to the `.PHONY` list (next to `coverage-unit coverage-unix coverage-embedded`):

```makefile
.PHONY: help all ci nox nox-unit nox-integration nox-unix nox-embedded validate clean-dist dev build coverage coverage-unit coverage-integration coverage-unix coverage-embedded coverage-hostless docs docs-lint docs-html docs-inventories doctest doctest-src typecheck lint format schema clean changelog release stability stability-unit stability-unix stability-embedded repeat vm-health qemu-restart import-snapshot hyperfine profile
```
(This also pre-adds `nox-integration` used by Task 3.)

- [ ] **Step 5: Update the help "Testing" block**

Replace lines 310–314 with:

```makefile
	@printf '  Level (by dir): unit < integration < (all)   Resource (by marker): unix · embedded · hostless(CI)\n'
	@printf '  \033[36m%-30s\033[0m %s\n' 'coverage-*'   'pinned Python + coverage   (unit,integration | unix,embedded,hostless)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'nox-*'        'multi-Python matrix        (unit,integration,unix,embedded; nox=all)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'stability-*'  'pinned pytest-repeat soak  (unit,unix,embedded; stability=all)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'repeat'       'soak the full unit suite (pytest-repeat)'
```

- [ ] **Step 6: Verify the unit level target runs**

Run: `make coverage-unit`
Expected: PASS — runs only `tests/unit` (~2200 tests, ~20s), prints a coverage report, no `--cov-fail-under` gate failure (report-only).

- [ ] **Step 7: Verify the no-testbed CI gate passes at its floor**

Run: `make coverage-hostless`
Expected: PASS — runs `tests/unit tests/e2e -m "not integration and not embedded and not stability"` (tests/unit + completion_cache), coverage ≥ 90 (`CI_COVERAGE_THRESHOLD`). This is the identical set to the former `coverage-unit`, which passed at 90.

- [ ] **Step 8: Verify the integration level target selects both tiers (no VMs needed for collection)**

Run: `uv run pytest tests/unit tests/integration -m "not stability" --collect-only -q -p no:cacheprovider --no-cov | tail -5`
Expected: collection succeeds and includes items from both `tests/unit/` and `tests/integration/` (do NOT run them — the integration tier needs the lab). Confirms `make coverage-integration` targets the right set.

- [ ] **Step 9: Stage and prepare the commit message (do NOT commit)**

```bash
git add Makefile
```
Paste-able message for Chris:
```
ci(make): add directory-level test targets; move CI gate to coverage-hostless

coverage-unit -> tests/unit only (level). New coverage-integration (unit +
integration) and coverage-hostless (no-testbed CI gate = the former
coverage-unit set). `make ci` now gates on coverage-hostless. Resource
targets (unix/embedded) unchanged.
```

---

### Task 3: noxfile — level sessions + hostless gate

Mirror the Makefile in nox: `tests_unit` becomes level-unit, add `tests_integration` and `tests_hostless`, and default the session list to the gate.

**Files:**
- Modify: `noxfile.py` — `nox.options.sessions` (line 40), the `UNIT_TEST_ARGS` block + `tests_unit` (lines 42–57).

**Interfaces:**
- Consumes: nothing.
- Produces: nox sessions `tests_unit` (= `pytest tests/unit`), `tests_integration` (= `pytest tests/unit tests/integration`), `tests_hostless` (= the no-testbed gate, `--cov-fail-under=85`). `nox.options.sessions` defaults to `tests_hostless`. Task 4 (workflows) consumes `tests_hostless`.

- [ ] **Step 1: Default the session list to the gate**

In `noxfile.py` line 40, change:

```python
nox.options.sessions = ["lint", "tests_hostless", "typecheck", "docs"]
```
(was `"tests_unit"`).

- [ ] **Step 2: Replace `UNIT_TEST_ARGS` + `tests_unit` with the three sessions**

Replace lines 42–57 (the `# Coverage floors…` comment, `UNIT_TEST_ARGS`, and the `tests_unit` session) with:

```python
# Coverage floors mirror the Makefile: the no-testbed CI gate (tests_hostless)
# gates at 85 (CI_COVERAGE_THRESHOLD); every full-suite path at 92
# (COVERAGE_THRESHOLD). Keep these in sync with the Makefile if either moves.
HOSTLESS_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded and not stability",
    "--cov-fail-under=85",
)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unit(session: nox.Session) -> None:
    """Run the unit *level* tier (tests/unit only; no testbed) under each Python."""
    session.run(
        "pytest",
        "tests/unit",
        "-m",
        "not stability",
        _junitxml(session, "nox-unit"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_integration(session: nox.Session) -> None:
    """Run the unit + integration *level* tiers (tests/unit + tests/integration).

    Cumulative directory-based level: needs the full lab (the integration tier
    includes the Linux-VM, Zephyr, and Docker tests). No coverage gate — a single
    environment exercises only a slice of otto.
    """
    session.run(
        "pytest",
        "tests/unit",
        "tests/integration",
        "-m",
        "not stability",
        _junitxml(session, "nox-integration"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_hostless(session: nox.Session) -> None:
    """Run the no-testbed set (tests/unit + no-VM e2e) — the CI gate.

    Identical selection to the former ``tests_unit``: every test that needs no
    testbed across ``tests/unit`` and ``tests/e2e``. This is what
    ``.github/workflows/ci.yml`` runs and what ``nox.options.sessions`` defaults
    to. Auto-includes any future no-testbed e2e test.
    """
    session.run("pytest", *HOSTLESS_TEST_ARGS, _junitxml(session, "nox-hostless"), *session.posargs)
```

- [ ] **Step 3: Verify the sessions exist**

Run: `uv run nox -l 2>&1 | grep -E "tests_(unit|integration|hostless|unix|embedded|all)"`
Expected: `tests_unit`, `tests_integration`, `tests_hostless`, `tests_unix`, `tests_embedded`, `tests_all` all listed (one entry per Python version).

- [ ] **Step 4: Verify level-unit runs tests/unit only**

Run: `uv run nox -s tests_unit-3.12`
Expected: PASS — runs `tests/unit` under 3.12, no VMs, no gate failure.

- [ ] **Step 5: Verify the hostless gate runs and gates**

Run: `uv run nox -s tests_hostless-3.12`
Expected: PASS — runs `tests/unit tests/e2e -m "not integration and not embedded and not stability"` under 3.12, coverage ≥ 85.

- [ ] **Step 6: Lint the noxfile**

Run: `uv run ruff format noxfile.py && uv run ruff check noxfile.py`
Expected: format leaves it clean (or reformats), `check` passes.

- [ ] **Step 7: Stage and prepare the commit message (do NOT commit)**

```bash
git add noxfile.py
```
Paste-able message for Chris:
```
ci(nox): add tests_integration + tests_hostless; tests_unit -> level tier

tests_unit now runs tests/unit only (level). New tests_integration (unit +
integration) and tests_hostless (the no-testbed CI gate; --cov-fail-under=85,
the former tests_unit set). Default session list -> tests_hostless so local
`nox` == CI.
```

---

### Task 4: GitHub workflows — repoint CI + nightly to the gate

CI runs `nox -s tests_unit` today; repoint it (and the nightly flake-flush matrix, which mirrors CI) to `tests_hostless`.

**Files:**
- Modify: `.github/workflows/ci.yml` (lines 27, 45)
- Modify: `.github/workflows/nightly.yml` (lines 78–81 comment, line 104)

**Interfaces:**
- Consumes: the `tests_hostless` nox session (Task 3). **Order: do Task 3 before this task** so the session exists.
- Produces: CI and nightly both gate/soak the no-testbed set.

- [ ] **Step 1: Repoint the CI job**

In `.github/workflows/ci.yml`:
- Line 27: `name: tests_unit-${{ matrix.python }}` → `name: tests_hostless-${{ matrix.python }}`
- Line 45: `run: uv run nox -s tests_unit-${{ matrix.python }}` → `run: uv run nox -s tests_hostless-${{ matrix.python }}`

- [ ] **Step 2: Repoint the nightly flake-flush matrix**

In `.github/workflows/nightly.yml`:
- Lines 78–81 comment — update the wording so it references the gate session. Replace:

```yaml
  # Re-run the full nox unit matrix several times under pytest-repeat to flush
  # flakes anywhere in the suite (not just the stability tests). Mirrors the CI
  # `tests_unit` job but loops each session with --count via pytest-repeat,
  # matching the repo's `make nox-unit COUNT=N` convention.
```
with:

```yaml
  # Re-run the no-testbed CI gate matrix several times under pytest-repeat to
  # flush flakes anywhere in the suite (not just the stability tests). Mirrors
  # the CI `tests_hostless` job but loops each session with --count via
  # pytest-repeat.
```
- Line 104: `uv run nox -s tests_unit-${{ matrix.python-version }} --` → `uv run nox -s tests_hostless-${{ matrix.python-version }} --`

- [ ] **Step 3: Verify no stale `tests_unit` references remain in the workflows**

Run: `grep -rn "tests_unit" .github/workflows/`
Expected: no matches (CI and nightly now reference `tests_hostless`). The `tests_unit` session still exists for `make nox-unit` / `scripts/stability_campaign.py`, which intentionally mirror the *make* grid (level-unit) — those are out of scope here.

- [ ] **Step 4: Sanity-check YAML validity**

Run: `uv run python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ('.github/workflows/ci.yml','.github/workflows/nightly.yml')]; print('yaml ok')"`
Expected: `yaml ok` (PyYAML is available via the dev env; if not, skip — the edits are single-token string swaps).

- [ ] **Step 5: Stage and prepare the commit message (do NOT commit)**

```bash
git add .github/workflows/ci.yml .github/workflows/nightly.yml
```
Paste-able message for Chris:
```
ci(workflows): gate CI + nightly on tests_hostless (no-testbed set)

tests_unit is now the level-unit tier; the no-testbed CI gate moved to
tests_hostless. Repoint ci.yml and the nightly flake-flush matrix. The set is
unchanged, so coverage and flake coverage are preserved; future hostless e2e
tests are auto-included.
```

---

### Task 5: Docs — contributing.md test-tier section

Document the two axes and the new targets.

**Files:**
- Modify: `docs/contributing.md` — intro + table (lines 253–265), the nox section text + code block (lines 289–298).

**Interfaces:**
- Consumes: the make/nox target names from Tasks 2–3.
- Produces: docs only.

- [ ] **Step 1: Update the intro + category table**

Replace lines 253–265 (from "Tests are partitioned…" through the table) with:

```markdown
Tests live in two orthogonal axes. **Level** is the directory a test lives in
(`tests/unit/` ⊆ `tests/integration/` ⊆ `tests/e2e/`) and is selected by *path*;
**resource** is what infrastructure a test needs (`integration` = Vagrant VMs,
`embedded` = Zephyr) and is selected by *marker*. Pick the target that matches
what you want to exercise:

| Category | How to run | VMs needed |
|----------|------------|------------|
| Unit tier only (level) | `make coverage-unit` (pinned) / `make nox-unit` (all Pythons) | none |
| Unit + integration tiers (level) | `make coverage-integration` / `make nox-integration` | full lab |
| No-testbed CI gate (tests/unit + no-VM e2e) | `make coverage-hostless` (pinned) / `uv run nox -s tests_hostless` (all Pythons) | none |
| Full coverage gate (all tiers, excludes `stability`) | `make coverage` | lab VMs |
| Unix VMs, incl. multi-hop (resource) | `make coverage-unix` / `make nox-unix` | test1/test2/test3 |
| Embedded / Zephyr (resource) | `make coverage-embedded` / `make nox-embedded` | zephyr VM |
| Multi-hop only | `uv run pytest -m hops` | three VMs |
| Stability / soak | `make stability` (or `stability-unit` / `stability-unix` / `stability-embedded`) | lab VMs (`-unit` needs none) |
| Everything (the dev-VM contract) | `make all` | lab VMs |
| Cross-Python matrix | `make nox-unit` (quick, no VMs) / `make nox` (full) | `nox` needs VMs |
```

- [ ] **Step 2: Update the nox section text + code block**

In `docs/contributing.md`, replace lines 289–298 (the "`make ci` runs the unit suite…" sentence through the closing ``` of the code block) with:

```markdown
`make ci` runs the no-testbed CI gate under one Python (whichever uv resolves by
default). To exercise the full matrix the way CI does — Python 3.10
through 3.14 — use `nox`:

```bash
make nox-unit                      # unit level tier across all Pythons (no VMs)
make nox-integration               # unit + integration tiers across all Pythons (full lab)
make nox                           # full matrix: all environments, all Pythons (needs VMs)
uv run nox -s tests_hostless-3.12  # the no-testbed CI gate under one Python
uv run nox -s tests_unit-3.14 -- -k test_session   # forward args to pytest
uv run nox --list                  # show every available session
```
```

- [ ] **Step 3: Verify the docs build/lint passes**

Run: `make docs`
Expected: PASS — `doc8`, the markdown-doctest linter, and the Sphinx nitpicky build all succeed. (If `make docs` is slow, at minimum run `uv run python scripts/lint_markdown_doctests.py docs/` and `uv run doc8 docs/contributing.md`.)

- [ ] **Step 4: Stage and prepare the commit message (do NOT commit)**

```bash
git add docs/contributing.md
```
Paste-able message for Chris:
```
docs(contributing): document the level + resource test axes and new targets

Explain directory-level tiers (unit ⊆ integration ⊆ all) vs resource markers
(unix/embedded), and the no-testbed CI gate (coverage-hostless / tests_hostless).
```

---

### Task 6: Final gate verification

No new code — confirm the runnable (no-VM) gate is green and hand Chris a consolidated summary.

**Files:** none.

**Interfaces:** consumes everything from Tasks 1–5.

- [ ] **Step 1: Run the no-VM gate that CI will run**

Run: `make coverage-hostless && make typecheck && make docs`
Expected: all PASS. (`coverage-hostless` is the exact CI gate; `typecheck` = `ty check`; `docs` = the Sphinx/doc8 gate.)

- [ ] **Step 2: Confirm the level targets behave**

Run: `make coverage-unit` (PASS, tests/unit only) and `uv run pytest tests/unit tests/integration -m "not stability" --collect-only -q -p no:cacheprovider --no-cov | tail -3` (collects both tiers).

- [ ] **Step 3: Note what still needs the lab**

`make coverage` (full gate, 94) and `make nox` (full matrix) require the Vagrant lab and are **not** runnable in this worktree without VMs — flag them for Chris to run on the dev VM/lab, or note they run in the normal `make all` contract.

- [ ] **Step 4: Surface the consolidated summary**

Report to Chris: the five staged commits (Tasks 1–5) with their paste-able messages, the worktree path/branch, and the lab-only gates still pending. Do NOT commit.

---

## Self-Review

**Spec coverage** (each spec decision → task):
- D1 two axes side-by-side → Tasks 2 (make), 3 (nox); resource targets left intact.
- D2 cumulative level on coverage-*/nox-* → Tasks 2, 3.
- D3 keep `integration` marker → unchanged (verified by G1 staying green in Task 1 Step 5).
- D4 e2e auto-stamp + register marker → Task 1.
- D5 stability untouched → no task touches `stability-*` (confirmed: Tasks 2–3 don't edit those recipes).
- D6 split CI gate from level-unit; ci.yml/nightly repoint → Tasks 3 (session), 4 (workflows).
- D7 gate = `tests/unit tests/e2e -m "not integration and not embedded and not stability"`, floors unchanged → Tasks 2 (`coverage-hostless`), 3 (`tests_hostless`); verified in Task 2 Step 7 / Task 3 Step 5.
- Ripple: contributing.md → Task 5; CHANGELOG untouched (constraint); `scripts/stability_campaign.py` intentionally left on `tests_unit` (mirrors the make grid / level-unit) — noted in Task 4 Step 3.

**Placeholder scan:** no TBD/TODO; every code/edit step shows verbatim content.

**Type/name consistency:** `tests_hostless`/`coverage-hostless`/`M_HOSTLESS`/`HOSTLESS_TEST_ARGS`/`tests_integration`/`coverage-integration`/`nox-integration` used consistently across Tasks 2–5; `_junitxml` group names (`nox-unit`, `nox-integration`, `nox-hostless`) match the JUnit-dir convention; the `e2e` marker name is identical in pyproject, conftest, and the G3 guard.
