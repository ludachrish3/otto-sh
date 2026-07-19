# Test-Suite Speedup Levers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut test wall-clock without reducing quality: pin the primary Python to 3.10, tier `make nox` (full suite on primary + hostless on the rest, old matrix preserved as `nox-full`), make the browser-suite xdist pinning an env-gated policy so CI can shard it, add a standing JUnit-durations tool, and measure whether any e2e module still pays un-amortized fixture setup.

**Architecture:** Four independent levers ordered by value. Lever 1 touches only version pins (noxfile sessions + 3 CI lines). Lever 2 recomposes the `nox` make target from existing nox sessions. Lever 3 moves the browser suites' hard-coded `xdist_group` marks into a root-conftest policy hook (per the test-guard scope rule: the pin protects machine-global RAM, so the policy lives at the root), gated by `OTTO_BROWSER_SHARD`. Lever 4 is a sibling of `scripts/junit_failures.py`. Lever 5 is measurement + a decision gate, not presumed rewrites — the obvious candidates already use module-scoped beds.

**Tech Stack:** GNU Make 4.3, nox + nox-uv, pytest + pytest-xdist (`--dist loadgroup`), pytest-playwright, GitHub Actions.

## Context (what was measured, 2026-07-18)

- `make nox` today = `tests_all` × 5 Pythons, serial: ~400–530s each ≈ 35–40 min. The dominant serial block in `make release`.
- The dashboard browser lane is parallel-safe by construction: every test binds `MonitorServer(port=0)` (`tests/_fixtures/_dashboard_harness.py:50`) and CDP coverage dumps are keyed `cdp-<pid>-<uuid>.json` (`tests/_fixtures/_ts_coverage.py:54`). The single `xdist_group("dashboard")` is a dev-VM RAM policy (plan 2026-07-02: "never parallel browsers on the dev VM"; the VM has 4 cores/3GB), not a correctness constraint.
- xdist_group pins are process-local; the lab testbed is machine-global. Parallel *processes* (make -j, concurrent nox sessions) would bypass every group pin — that is why lever 2 tiers rather than parallelizes, and why `.NOTPARALLEL:` stays.
- The dev venv is already Python 3.10.20; release/docs/lint CI jobs already install 3.10. The only 3.12 pins left: noxfile `tests_unit_repeat` + `dashboard` sessions, `ci.yml` unit-repeat job (2 lines), `ci.yml` dashboard job (1 line), `nightly.yml` dashboard job (1 line).
- Nightly is hostless-only (no lab VMs) — the full cross-version `tests_all` matrix exists ONLY locally, so it must stay reachable (`make nox-full`), not "move to nightly".
- Candidate fixture-widening modules already amortize: `test_link_impair_e2e.py` (module-scoped bed, module loop_scope), `test_tunnel_e2e.py` (module-scoped autouse), `test_interact_e2e.py` (class-scoped per param). Lever 5 measures before touching anything.

## Global Constraints

- Primary Python is **3.10** (Chris, 2026-07-18) — every single-interpreter pin in this plan uses it; 3.12–3.14 remain matrix members everywhere.
- Quality floor unchanged: no test deleted, no marker selection narrowed, no coverage floor lowered. The full matrix remains runnable on demand.
- Default browser-lane behavior on the dev VM must be byte-for-byte unchanged (serial, one worker, same group names in JUnit classnames).
- `nox -s lint` must be green after every task (repo rule; strict ruff config).
- Per-task pytest gates below are scoped; the whole-suite gates run once at the end (`make coverage`, the new `make nox`) — scoped green does not certify the repo (see docs/contributing.md).
- Worktree commits: conventional prefix + `Assisted-by:` trailer (check `git log` for the exact trailer format used by prior worktree commits and match it).
- No parallel/heavy load beyond sanctioned make targets on the dev VM; the shard probe in Task 3 is deliberately bounded (2 modules, `-n 2`, chromium only, `--no-cov`).

---

### Task 1: Pin single-version sessions to the primary Python (3.10)

**Files:**
- Modify: `noxfile.py` (module docstring example, JUNIT comment example, new `PRIMARY_PYTHON` constant, `tests_unit_repeat` + `dashboard` session decorators)
- Modify: `.github/workflows/ci.yml` (unit-repeat job: "Set up Python 3.12" name + install line; dashboard job: install line)
- Modify: `.github/workflows/nightly.yml` (dashboard job install line)

**Interfaces:**
- Produces: `PRIMARY_PYTHON = "3.10"` module constant in `noxfile.py` — Task 2's Makefile `NOX_PRIMARY := 3.10` mirrors it (hand-kept pair, same idiom as `DASHBOARD_MARKER_EXPR`; cross-reference both comments).
- Session IDs change: `tests_unit_repeat-3.12` → `tests_unit_repeat-3.10`, `dashboard-3.12(browser='…')` → `dashboard-3.10(browser='…')`. CI invokes both WITHOUT version suffixes (`nox -s tests_unit_repeat`, `nox -k <browser>`), so those invocations need no change — only the `uv python install` lines do.

- [ ] **Step 1: Add the constant and repoint the two sessions in `noxfile.py`**

Below `PYTHON_VERSIONS = [...]` add:

```python
# The single-interpreter ("pinned") Python for sessions that don't span the
# matrix. 3.10 — the oldest supported interpreter — deliberately: it is what
# the dev venv and the release/build CI jobs run, so single-version lanes
# exercise the floor rather than silently requiring something newer. The
# Makefile's NOX_PRIMARY mirrors this value (hand-kept pair — see
# DASHBOARD_MARKER_EXPR below for why Make can't read a Python constant).
PRIMARY_PYTHON = "3.10"
```

Change both decorators:

```python
@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_unit_repeat(session: nox.Session) -> None:
```

```python
@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
@nox.parametrize("browser", ["chromium", "firefox", "webkit"])
def dashboard(session: nox.Session, browser: str) -> None:
```

In the module docstring change the example `uv run nox -s tests_unit-3.12` → `uv run nox -s tests_unit-3.10`, and in the JUNIT_DIR comment change `tests_unit-3.12.xml` → `tests_unit-3.10.xml`. In the `tests_unit_repeat` docstring, "One Python is sufficient" stays — append "(the primary, 3.10)".

- [ ] **Step 2: Update the three CI install lines**

`.github/workflows/ci.yml`, unit-repeat job:

```yaml
      - name: Set up Python 3.10
        run: uv python install 3.10
```

`.github/workflows/ci.yml`, dashboard job (the bare `- run: uv python install 3.12` step) and `.github/workflows/nightly.yml` dashboard job (same step): change both to `uv python install 3.10`.

- [ ] **Step 3: Sweep for stragglers**

Run: `grep -rn "3\.12" noxfile.py .github/workflows/ Makefile docs/contributing.md | grep -v '"3.12"' | grep -vE "3\.10.*3\.12|3\.12.*3\.13"`
Expected: no hit that denotes a *pin* (matrix lists naming 3.12 as a member are correct and stay).

- [ ] **Step 4: Verify session resolution**

Run: `uv run nox -l | grep -E "tests_unit_repeat|dashboard"`
Expected: `tests_unit_repeat-3.10`, `dashboard-3.10(browser='chromium')` (+ firefox/webkit variants); no `-3.12` anywhere.

- [ ] **Step 5: Run the repointed leak guard once**

Run: `uv run nox -s tests_unit_repeat`
Expected: PASS (builds a 3.10 session venv, repeats tests/unit ×2 single-process). This is the only new-venv session cheap enough to run at task time; `dashboard-3.10` is exercised by CI and its make-lane twin runs at final gates.

- [ ] **Step 6: Lint gate + commit**

Run: `uv run nox -s lint`
Expected: PASS.

```bash
git add noxfile.py .github/workflows/ci.yml .github/workflows/nightly.yml
git commit  # chore(nox): pin single-version sessions to the primary Python (3.10)
```

---

### Task 2: Tier `make nox` — full suite on primary, hostless on the rest

**Files:**
- Modify: `Makefile` (new `NOX_PRIMARY`/`NOX_SECONDARY` vars + comment, rewritten `nox` recipe + help, new `nox-full` target, `.PHONY` list, `help` summary line for `nox-*`)
- Modify: `docs/contributing.md` (cross-Python matrix rows + command block)

**Interfaces:**
- Consumes: nox session IDs `tests_all-<ver>`, `tests_hostless-<ver>` (already exist, all versions).
- Produces: `make nox` = tiered lane (used by `make release` unchanged — the recipe keyed `$(MAKE) nox` picks up the new meaning); `make nox-full` = the pre-tiering full matrix.

- [ ] **Step 1: Rewrite the `nox` target and add `nox-full`**

Above the `nox:` rule add:

```make
# Primary interpreter for the tiered `nox` lane (mirrors noxfile.py's
# PRIMARY_PYTHON — hand-kept pair). `make nox` runs the FULL suite on the
# primary only; the other supported interpreters run the hostless selection
# (the exact slice CI gates on, per push, on all five versions already).
# Rationale: interpreter-sensitive regressions live overwhelmingly in the
# unit/hostless code paths, while the VM-backed tiers exercise otto↔testbed
# behavior that does not vary by interpreter — and cross-version parallelism
# is not an option here because xdist_group pins are process-local while the
# lab testbed is machine-global (two concurrent sessions would race the
# fixed tunnel/impair topologies). The complete cross-version matrix stays
# available as `make nox-full`; nightly cannot absorb it (hostless-only, no
# lab VMs), so run nox-full on demand when a release touches
# interpreter-sensitive integration surface.
NOX_PRIMARY := 3.10
NOX_SECONDARY := 3.11 3.12 3.13 3.14
```

Replace the `nox:` rule:

```make
nox: ## Run the full suite on the PRIMARY Python (3.10) + the hostless CI-gate slice on the others. Requires dev VM with Vagrant hosts up. Not used by CI. Full cross-version matrix: `make nox-full`. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/ + reports/junit/nox-hostless/.
	uv run nox -s tests_all-$(NOX_PRIMARY) $(foreach v,$(NOX_SECONDARY),tests_hostless-$(v)) -- --count=$(NOX_COUNT) --repeat-scope=session

nox-full: ## Run the FULL test suite (all environments) across ALL supported Pythons — the pre-tiering `make nox` (~5× its wall-clock). Requires dev VM with Vagrant hosts up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/.
	uv run nox -s tests_all -- --count=$(NOX_COUNT) --repeat-scope=session
```

Add `nox-full` to the `.PHONY` line (after `nox`). In the `help` recipe, change the `'nox-*'` summary line text from `'every suffix, all Pythons   (bare nox = full matrix)'` to `'every suffix, all Pythons   (bare nox = full on primary + hostless on rest; nox-full = full matrix)'`.

- [ ] **Step 2: Update `docs/contributing.md`**

Row at line ~317: change `` `make nox` (full) `` to `` `make nox` (full on 3.10 + hostless on the rest) / `make nox-full` (full, all Pythons) ``. In the command block at ~361-364: change the `make nox` line's comment to `# tiered matrix: full suite on 3.10, hostless on 3.11-3.14 (needs VMs)`, add a `make nox-full` line `# complete full-suite matrix, all Pythons (needs VMs; ~5x nox)`, and change the example `uv run nox -s tests_hostless-3.12` → `uv run nox -s tests_hostless-3.10`.

- [ ] **Step 3: Verify composition without running**

Run: `make -n nox | head -3`
Expected: one `uv run nox -s tests_all-3.10 tests_hostless-3.11 tests_hostless-3.12 tests_hostless-3.13 tests_hostless-3.14 -- --count=1 --repeat-scope=session` line.
Run: `make -n nox-full | head -3`
Expected: the old `uv run nox -s tests_all -- --count=1 --repeat-scope=session` line.

- [ ] **Step 4: Docs gate for the contributing.md edit**

Run: `uv run doc8 docs/ && uv run python scripts/lint_markdown_doctests.py docs/`
Expected: PASS (the full sphinx rebuild runs once at final gates).

- [ ] **Step 5: Lint gate + commit**

Run: `uv run nox -s lint`
Expected: PASS.

```bash
git add Makefile docs/contributing.md
git commit  # feat(make): tier `make nox` — full suite on the primary Python, hostless on the rest
```

The full `make nox` run happens once at final gates (it doubles as this task's end-to-end verification).

---

### Task 3: Env-gated browser-suite xdist grouping (`OTTO_BROWSER_SHARD`)

**Files:**
- Modify: `tests/conftest.py` (pure helper + stamping in the existing `pytest_collection_modifyitems`, comment block)
- Modify: every browser-marked module that carries a static `xdist_group` mark — enumerate with `grep -rln "xdist_group" tests/e2e/monitor/dashboard tests/e2e/cov/report_browser`; expected set: `test_access_key.py`, `test_live_shell.py`, `test_command_palette.py`, `test_topology_tunnels.py`, `test_topology_budget.py`, `test_review_shell.py`, `test_replay_soak.py` (dashboard), `test_report_index.py`, `test_report_file.py` (covreport) — plus any the grep adds. **KEEP `test_harness.py`'s mark** (it is NOT browser-marked; its explicit `xdist_group("dashboard")` pin predates this policy and explicit pins win — add a one-line comment there saying so).
- Modify: `Makefile` (the `-n 1` comment block above `coverage-python`, lines ~349-358)
- Modify: `.github/workflows/ci.yml` (dashboard job: `env: OTTO_BROWSER_SHARD: "1"` on the nox step), `.github/workflows/nightly.yml` (dashboard job: same)
- Test: `tests/unit/test_browser_group_policy.py` (new)

**Interfaces:**
- Consumes: the existing root-conftest `pytest_collection_modifyitems` (stamp BEFORE the frontload sort, same function — the hook already runs after deeper conftests per LIFO ordering).
- Produces: `_browser_group_key(nodeid: str, *, shard: bool) -> str` pure helper in `tests/conftest.py`; env contract `OTTO_BROWSER_SHARD == "1"` ⇒ per-file groups for the two audited suites.

- [ ] **Step 1: Write the failing unit test**

`tests/unit/test_browser_group_policy.py`:

```python
"""Pin the browser-suite xdist grouping policy (root conftest's pure helper).

Serial (default) mode must reproduce the historical group names exactly —
"dashboard" / "covreport" appear in JUnit classnames and the Makefile's
comments. Shard mode (OTTO_BROWSER_SHARD=1, CI only) splits the two audited
suites per-file so `--dist loadgroup` can spread modules across workers while
module-scoped fixtures still land on a single worker. Any browser suite NOT
in the audited map stays serial in BOTH modes — sharding is opt-in per suite,
after auditing it for parallel safety (port=0 servers, collision-free dumps).
"""

from tests.conftest import _browser_group_key


def test_dashboard_serial_group_matches_historical_name() -> None:
    nodeid = "tests/e2e/monitor/dashboard/test_review_shell.py::test_grid"
    assert _browser_group_key(nodeid, shard=False) == "dashboard"


def test_covreport_serial_group_matches_historical_name() -> None:
    nodeid = "tests/e2e/cov/report_browser/test_report_index.py::test_index"
    assert _browser_group_key(nodeid, shard=False) == "covreport"


def test_shard_mode_splits_a_suite_per_file() -> None:
    a = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t", shard=True)
    b = _browser_group_key("tests/e2e/monitor/dashboard/test_b.py::t", shard=True)
    assert a != b
    assert a.startswith("dashboard::")
    assert b.startswith("dashboard::")


def test_shard_mode_keeps_one_file_in_one_group() -> None:
    a = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t1", shard=True)
    b = _browser_group_key("tests/e2e/monitor/dashboard/test_a.py::t2[chromium]", shard=True)
    assert a == b


def test_unaudited_browser_suite_stays_serial_in_both_modes() -> None:
    nodeid = "tests/e2e/somewhere/test_new_browser_suite.py::t"
    assert _browser_group_key(nodeid, shard=True) == "browser-serial"
    assert _browser_group_key(nodeid, shard=False) == "browser-serial"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_browser_group_policy.py -p no:cacheprovider --no-cov -n 0`
Expected: FAIL — `ImportError: cannot import name '_browser_group_key'`.

- [ ] **Step 3: Implement the policy in `tests/conftest.py`**

Add near the frontload block (module level; `import os` already exists mid-file — top-of-file import of `os` is fine, it does not trigger the typer/rich color path that mid-file comment guards):

```python
# ── Browser-suite xdist grouping policy ────────────────────────────────────
# The two Playwright suites are parallel-safe BY CONSTRUCTION: every test
# binds its MonitorServer to port=0 (tests/_fixtures/_dashboard_harness.py)
# and CDP coverage dumps are keyed pid+uuid (tests/_fixtures/_ts_coverage.py).
# Their single-worker pinning is a resource POLICY — never parallel browsers
# on the 3GB dev VM (plan 2026-07-02) — not a correctness constraint, so it
# is stamped here (process-global policy ⇒ root conftest, per the module
# docstring's rule) instead of hard-coded per module. OTTO_BROWSER_SHARD=1
# (set only by CI's dashboard jobs, whose runners have the RAM) relaxes the
# pin to per-FILE groups: `--dist loadgroup` then spreads modules across
# workers while any module-scoped fixture still instantiates on one worker.
# Suites not in the map stay serial in both modes — sharding is opt-in per
# suite, after auditing it for parallel safety. An explicit xdist_group mark
# on a test/module always wins (e.g. dashboard/test_harness.py's non-browser
# wire-contract pins keep their historical group).
_BROWSER_SUITE_GROUPS: dict[str, str] = {
    "tests/e2e/monitor/dashboard/": "dashboard",
    "tests/e2e/cov/report_browser/": "covreport",
}


def _browser_group_key(nodeid: str, *, shard: bool) -> str:
    """Return the xdist_group name for a browser-marked item.

    Pure helper — no pytest dependency — so it can be imported and tested
    directly in ``tests/unit/test_browser_group_policy.py``.
    """
    path = nodeid.split("::", 1)[0]
    for prefix, group in _BROWSER_SUITE_GROUPS.items():
        if path.startswith(prefix):
            return f"{group}::{path}" if shard else group
    return "browser-serial"
```

Inside the existing `pytest_collection_modifyitems`, BEFORE the `items.sort(...)` line:

```python
    shard = os.environ.get("OTTO_BROWSER_SHARD") == "1"
    for item in items:
        if item.get_closest_marker("browser") is None:
            continue
        if item.get_closest_marker("xdist_group") is not None:
            continue  # explicit pins always win
        item.add_marker(pytest.mark.xdist_group(_browser_group_key(item.nodeid, shard=shard)))
```

(`import pytest` at top of conftest if not already there; `add_marker` prepends, so `get_closest_marker` — what xdist reads — sees it.)

- [ ] **Step 4: Remove the static marks from the browser-marked modules**

In each file from the grep set (NOT `test_harness.py`): delete the `pytest.mark.xdist_group("dashboard"),` / `pytest.mark.xdist_group("covreport"),` line from `pytestmark`. In `test_harness.py`, annotate its retained mark:

```python
pytestmark = [
    pytest.mark.hostless,
    # Explicit pin, kept deliberately: these are NOT browser tests, so the
    # root conftest's browser-group policy skips them; the historical group
    # keeps their distribution identical in the hostless/coverage lanes.
    pytest.mark.xdist_group("dashboard"),
]
```

- [ ] **Step 5: Run the unit test to verify it passes**

Run: `uv run pytest tests/unit/test_browser_group_policy.py tests/unit/test_frontload_ordering.py -p no:cacheprovider --no-cov -n 0`
Expected: PASS (both — the frontload test guards the same hook we edited).

- [ ] **Step 6: Prove serial default is byte-identical**

Run: `OTTO_TS_COVERAGE= uv run pytest tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -m "browser and not soak" --browser chromium -n 1 --no-cov --collect-only -q 2>/dev/null | tail -3` — then spot-check groups:
Run: `uv run pytest tests/e2e/monitor/dashboard/test_command_palette.py --browser chromium --no-cov -n 1 -m "browser and not soak" --setup-plan 2>/dev/null | head -5`
Expected: collection succeeds, same test count as before the change (compare against `git stash` baseline if in doubt). The authoritative serial-lane check is `make dashboard` at final gates.

- [ ] **Step 7: Bounded shard probe (mechanics only)**

Run: `OTTO_BROWSER_SHARD=1 uv run pytest tests/e2e/monitor/dashboard/test_command_palette.py tests/e2e/cov/report_browser -m "browser and not soak" --browser chromium -n 2 --no-cov -p no:cacheprovider -v 2>&1 | tail -15`
Expected: PASS, with `[gw0]` AND `[gw1]` both appearing (two files ⇒ two groups ⇒ both workers busy). Two chromium instances briefly — bounded, within dev-VM tolerance.

- [ ] **Step 8: Wire CI + update the Makefile comment**

`ci.yml` dashboard job, last step becomes:

```yaml
      - name: Run dashboard browser e2e (${{ matrix.browser }})
        env:
          # Per-file xdist groups for the audited browser suites (see
          # tests/conftest.py's policy block): the repo-wide `-n auto` then
          # shards modules across the runner's workers. Never set on the
          # 3GB dev VM — `make dashboard` stays -n 1 serial there.
          OTTO_BROWSER_SHARD: "1"
        run: uv run nox -k ${{ matrix.browser }}
```

`nightly.yml` dashboard job: add the same `env:` (with a `# see ci.yml's dashboard job` one-liner) to its nox step.

`Makefile` comment block above `coverage-python` (~line 351): replace the sentence `Runs -n 1 (all browser tests share one xdist_group anyway; extra workers would sit idle and emit "No data was collected" coverage warnings)` with: `Runs -n 1: on the dev VM the browser suites are pinned serial by the root conftest's grouping policy (OTTO_BROWSER_SHARD unset — see tests/conftest.py), so extra workers would sit idle and emit "No data was collected" coverage warnings.`

- [ ] **Step 9: Lint + typecheck gates, commit**

Run: `uv run nox -s lint && uv run ty check`
Expected: PASS.

```bash
git add tests/conftest.py tests/unit/test_browser_group_policy.py tests/e2e/monitor/dashboard tests/e2e/cov/report_browser Makefile .github/workflows/ci.yml .github/workflows/nightly.yml
git commit  # feat(tests): env-gated browser-suite xdist grouping — CI shards, dev VM stays serial
```

---

### Task 4: `scripts/junit_durations.py` — standing wall-clock ranking

**Files:**
- Create: `scripts/junit_durations.py`
- Test: `tests/unit/scripts/test_junit_durations.py` (new; mirror the conventions of the existing `tests/unit/scripts/` tests)

**Interfaces:**
- Produces: CLI `scripts/junit_durations.py [--top N] [--by-file] REPORTS...`; `iter_cases(xml_path: Path) -> Generator[tuple[str, str, float], None, None]` yielding `(classname, name, seconds)`; `main(argv: list[str] | None = None) -> int` (always 0 on parseable input — informational tool). Task 5 consumes the CLI.

- [ ] **Step 1: Write the failing test**

`tests/unit/scripts/test_junit_durations.py`:

```python
"""scripts/junit_durations.py — ranking and aggregation over JUnit XML."""

from pathlib import Path

from scripts.junit_durations import iter_cases, main

_XML = """\
<testsuites>
  <testsuite name="pytest" tests="3" time="9.0">
    <testcase classname="tests.e2e.test_slow" name="test_big" time="5.5"/>
    <testcase classname="tests.e2e.test_slow" name="test_mid" time="2.5"/>
    <testcase classname="tests.unit.test_fast" name="test_small" time="1.0"/>
  </testsuite>
</testsuites>
"""


def _write_report(tmp_path: Path) -> Path:
    report = tmp_path / "sample.xml"
    report.write_text(_XML)
    return report


def test_iter_cases_yields_every_testcase(tmp_path: Path) -> None:
    report = _write_report(tmp_path)
    cases = list(iter_cases(report))
    assert ("tests.e2e.test_slow", "test_big", 5.5) in cases
    assert len(cases) == 3


def test_main_ranks_slowest_first_and_honors_top(tmp_path: Path, capsys) -> None:
    report = _write_report(tmp_path)
    assert main(["--top", "2", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.index("test_big") < out.index("test_mid")
    assert "test_small" not in out


def test_by_file_aggregates_per_classname(tmp_path: Path, capsys) -> None:
    report = _write_report(tmp_path)
    assert main(["--by-file", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.index("tests.e2e.test_slow") < out.index("tests.unit.test_fast")
    assert "8.0" in out  # 5.5 + 2.5 summed for the slow module
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_junit_durations.py -p no:cacheprovider --no-cov -n 0`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.junit_durations'`.

- [ ] **Step 3: Implement**

`scripts/junit_durations.py` (sibling of `junit_failures.py` — same shebang, docstring shape, argparse idiom, `ET` + `# noqa: S314` justification):

```python
#!/usr/bin/env python3
"""Rank test durations from pytest JUnit XML reports.

The standing answer to "where does the suite's wall-clock go": point it at
one or more ``reports/junit/**/*.xml`` files and it prints the slowest test
cases across them (``--top``, default 25), or the per-module totals
(``--by-file``). Every make test target writes into its own subdirectory of
``reports/junit/``, so after any gate run the data is already on disk — no
extra instrumentation pass needed.

JUnit XML folds setup + call + teardown into one time per testcase. For a
phase split (is it the fixture or the test?), re-run the lane with pytest's
``--durations=N``, which lists the phases as separate rows.

Usage::

    scripts/junit_durations.py reports/junit/**/*.xml
    scripts/junit_durations.py --top 40 reports/junit/nox/tests_all-3.10.xml
    scripts/junit_durations.py --by-file reports/junit/nox/*.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Generator
from pathlib import Path


def iter_cases(xml_path: Path) -> Generator[tuple[str, str, float], None, None]:
    """Yield ``(classname, name, seconds)`` for each testcase in the report."""
    tree = ET.parse(xml_path)  # noqa: S314 — parses our own trusted JUnit output, not untrusted input
    for tc in tree.iter("testcase"):
        yield tc.get("classname") or "", tc.get("name") or "", float(tc.get("time") or 0.0)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, print the duration ranking, and return exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML file(s)")
    parser.add_argument(
        "--top", type=int, default=25, help="How many rows to print (default: 25)"
    )
    parser.add_argument(
        "--by-file",
        action="store_true",
        help="Aggregate per classname (module) instead of listing individual tests",
    )
    args = parser.parse_args(argv)

    cases: list[tuple[str, str, float]] = []
    for xml_path in args.reports:
        cases.extend(iter_cases(xml_path))

    if args.by_file:
        per_file: dict[str, float] = defaultdict(float)
        for classname, _, seconds in cases:
            per_file[classname] += seconds
        rows = [(total, classname) for classname, total in per_file.items()]
    else:
        rows = [(seconds, f"{classname}::{name}") for classname, name, seconds in cases]

    rows.sort(reverse=True)
    total = sum(seconds for seconds, _ in rows)
    print(f"=== {len(cases)} case(s) across {len(args.reports)} report(s), {total:.1f}s summed ===")
    for seconds, label in rows[: args.top]:
        print(f"  {seconds:8.1f}s  {label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_junit_durations.py -p no:cacheprovider --no-cov -n 0`
Expected: PASS (3 tests). Then a smoke on real data: `uv run python scripts/junit_durations.py --by-file reports/junit/nox/*.xml | head -8` from the MAIN checkout's reports if the worktree has none — output is informational either way.

- [ ] **Step 5: Lint gate + commit**

Run: `uv run nox -s lint && uv run ty check`
Expected: PASS.

```bash
git add scripts/junit_durations.py tests/unit/scripts/test_junit_durations.py
git commit  # feat(scripts): junit_durations.py — rank suite wall-clock from JUnit reports
```

---

### Task 5: Durations measurement + fixture-scope decision gate

**Files:**
- Modify: `docs/superpowers/plans/2026-07-18-test-suite-speedup-levers.md` (append an `## Outcome — Task 5 measurement` section with the findings table)
- Modify: only if the decision rule below fires — the flagged e2e module(s), following `tests/e2e/test_link_impair_e2e.py`'s module-scoped-bed + autouse-verifier pattern.

**Interfaces:**
- Consumes: `scripts/junit_durations.py` (Task 4) for the module ranking; pytest `--durations` for the phase split JUnit can't provide.

- [ ] **Step 1: Capture the phase-split measurement (one sanctioned-load run)**

Run: `OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest tests/e2e tests/integration -m "not stability and not browser" --no-cov -p no:cacheprovider --durations=60 --durations-min=1.0 $(: no junitxml — ad hoc) 2>&1 | tee /tmp/claude-1000/-home-vagrant-otto-sh/2f63b0f5-2095-4939-9b72-b68d9ce62614/scratchpad/durations-e2e.txt | tail -80`
Expected: PASS; the tail shows the `slowest 60 durations` table with separate `setup`/`call`/`teardown` rows.

- [ ] **Step 2: Apply the decision rule**

For each module represented in the durations table: sum its `setup` rows and its `call` rows. Flag the module ONLY if (a) setup ≥ 20% of the module's summed time AND (b) the setup is a per-test/per-class fixture whose product is reusable across the module's tests without hidden coupling. Known priors: `test_link_impair_e2e.py` and `test_tunnel_e2e.py` beds are already module-scoped; `test_interact_e2e.py` is class-scoped per ssh/telnet param (widening across params does not compose — do not flag it for that alone).

- [ ] **Step 3: Act on the rule**

If NO module is flagged (the expected outcome): append the findings table + "no widening warranted" with the numbers to this plan's `## Outcome` section, and stop — the deliverable is the recorded measurement.
If a module IS flagged: widen its expensive fixture to `scope="module"` following the `test_link_impair_e2e.py:212` idiom (module-scoped `pytest_asyncio.fixture(scope="module", loop_scope="module")` bed + `@pytest.fixture(scope="module", autouse=True)` verifier that asserts the bed is pristine between tests), run that module standalone AND twice in one process (`--count=2 --repeat-scope=module`) to prove no inter-test coupling, and re-run `uv run nox -s lint`.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-07-18-test-suite-speedup-levers.md  # + any widened module
git commit  # docs(plans): record e2e durations measurement + fixture-scope decision
```

---

### Task 6: Final whole-suite gates

- [ ] **Step 1: `make coverage`** — the per-task gate: chromium dashboard lane (serial default proven unchanged end-to-end) + full Python suite at the 95 floor + merged TS coverage. Expected: PASS, floors met.
- [ ] **Step 2: `make nox`** — the NEW tiered lane, once, end-to-end (`tests_all-3.10` + 4× hostless). Expected: PASS; confirm `reports/junit/nox/tests_all-3.10.xml` and `reports/junit/nox-hostless/tests_hostless-3.1{1..4}.xml` exist.
- [ ] **Step 3: `make docs`** — contributing.md changed; the docs gate needs a clean rebuild (incremental `-W` misses broken refs). Expected: PASS.
- [ ] **Step 4: Failure triage, if any** — `uv run python scripts/junit_failures.py reports/junit/**/*.xml` (and enjoy `junit_durations.py --by-file` on the fresh data for the plan's Outcome section).
- [ ] **Step 5: Commit any gate-driven fixes** (each with its own conventional-prefix message + trailer), then hand over: branch summary + squash-merge message draft for Chris.

## Self-review notes

- Spec coverage: lever 1 → Tasks 1+2; lever 2 → Task 3; lever 4 (tool) → Task 4 (deliberately ahead of the measurement it powers); lever 3 (fixture widening) → Task 5 as measure-then-decide. Lever 5 ("honorable mentions") is explicitly out of scope — the `--cov-append` skip idea is subtle enough to warrant its own brainstorm if wanted.
- Type consistency: `_browser_group_key(nodeid, *, shard)` used identically in Task 3's test and impl; `iter_cases`/`main` signatures match between Task 4's test and impl.
- The `make dashboard` serial default is certified twice: bounded collect/probe at Task 3, full lane inside `make coverage` at Task 6.

---

## Outcome — Task 5 measurement (2026-07-18)

Run: `tests/e2e tests/integration -m "not stability and not browser"`, 417
passed / 12 skipped in 95.95s wall (`-n auto`, dev VM), `--durations=60
--durations-min=1.0`. Full table in the session scratchpad; summary:

| Phase | Rows ≥1s | Where |
|---|---|---|
| call | 51 | real telnet/SSH/QEMU/browser work — the tests themselves |
| setup | 3 (11.4s total) | all `tests/e2e/cov/test_coverage_e2e.py` |
| teardown | 7 (~13s total) | docker_e2e per-test `compose down` + run_exec |

**Decision: no fixture widening warranted.**

- The three flagged setups are already amortized or deliberately distinct:
  `coverage_run` is `scope="module"` (5.43s paid once for ~30 tests); the
  polluted-build-tree and suite-runner setups build *different* scenarios by
  design and cannot share the clean run's state.
- The docker teardowns belong to up/down-idempotency tests whose per-test
  lifecycle IS the contract under test.
- The prior candidates (`test_link_impair_e2e`, `test_tunnel_e2e`,
  `test_interact_e2e`) are call-dominated; their beds were widened to
  module/class scope in earlier work.

## Deviations from the plan as written

1. **Task 2 gained a canary leg** (Chris, mid-execution): `make nox` runs
   `tests_all` on 3.10 (floor) AND 3.14 (newest), hostless on 3.11–3.13.
   With `filterwarnings=error` a warning only fails on versions that run the
   affected tier; import-time DeprecationWarnings are caught by every
   version's unit/hostless legs, but RUNTIME warnings in VM-backed paths are
   version-specific and surface on the newest interpreter first (the
   3.14-only asyncio resource-leak episode).
2. **Task 3's stamping moved from the root conftest to
   `tests/e2e/conftest.py`** — proven empirically: the root conftest
   registers at config load, so its `pytest_collection_modifyitems` runs
   AFTER pytest-xdist's worker plugin has annotated test ids, making a
   root-level stamp silently invisible to the loadgroup scheduler (same-file
   tests landed on different workers). Deeper conftests register during
   collection and run first. A warning note now lives at the root hook.
3. **`OTTO_BROWSER_SHARD` needed an ambient-env allowlist entry** — the root
   conftest strips unknown `OTTO_*` vars at import time (hermeticity guard),
   which ate the flag before collection; added alongside `OTTO_TS_COVERAGE`
   with its pin test updated.

## Addendum — Task 6 gates found a latent parallel-safety bug (2026-07-19)

The rebased branch's first tiered `make nox` failed both `tests_all` legs on
`TestSingleHopSsh::test_{echo,hostname}_through_hop` (30s pytest-timeout on
the SSH echo), reproducing a 4-of-5-session failure seen on main's own full
matrix the same day. Root cause, proven from the worker interleaving in five
independent failures: `tests_all` was the only nox session whose selection
(`-m "not browser"`) swept the bed-hostile stability tier into a parallel
run. `test_sigstop_wedge_uncertain_then_recovers` SIGSTOPs **tomato's sshd
listener** for tens of seconds; its `link_tunnels_e2e` group pin serializes
stability tests against each other but cannot stop OTHER workers from
opening fresh ssh connections to the wedged host — the hop tests' second leg
(carrot→tomato ssh) then times out, while telnet-hop tests pass through the
same window (telnetd is untouched). Pre-5c schedules dodged the overlap by
luck; the 5c/CPU-collapse test-count changes moved the wedge window onto the
hop slot deterministically (exclusive machine, 2/2 legs, same position).

Fix (this branch): `tests_all` selects `-m "not browser and not stability"`,
matching every other session and the Makefile's `coverage-python` (all of
which already excluded `stability`); the tier keeps its dedicated serial
lane (`make stability-tunnel`), where it owns the bed. Guard: G3 in
`tests/unit/test_tier_marker_invariants.py` — every negation-only (catch-all)
`-m` expression in `noxfile.py` must contain `not stability`; proven red
against the pre-fix noxfile. Side benefit: each full leg drops the ~3-minute
stability chain, further shrinking the tiered lane.

## Final gates (2026-07-19, rebased on main `e849fb6` incl. 5c)

- `make coverage`: GREEN — Python 95.43% vs the 95 floor (4212 passed);
  dashboard lane 64/64 on the fresh post-5c web dist; TS merged floors met.
- `make nox` (tiered): GREEN — `tests_all-3.10` and `tests_all-3.14` each
  4213 passed / ~2 min; `tests_hostless-3.1{1..3}` ~1 min each; all five
  JUnit files present. Whole lane ≈ 7–8 min vs 22 min measured the same day
  for the old 5×`tests_all` matrix on the same quiet machine.
- `make docs`: GREEN — clean rebuild (no prior `docs/_build` in the
  worktree), doctests included.
- `scripts/junit_failures.py reports/junit/**/*.xml`: 0 problems.
- `junit_durations.py --by-file` on the fresh lane: top modules are now the
  embedded console suite (~107s) and docker e2e (~55s); the stability chain
  no longer appears.
