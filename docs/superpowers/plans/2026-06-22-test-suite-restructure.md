# Test-suite Restructure & Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure otto's tests into three honest tiers (`unit`/`integration`/`e2e`), consolidate fixtures/helpers under `tests/_fixtures/`, and remove genuine duplication — preserving every gate's selected test set, total/per-line/per-branch coverage, and every integration/e2e scenario.

**Architecture:** Tier (directory) and resource-need (marker) are orthogonal. Gate selection moves off the `tests/unit` *path* onto markers so relocating a file between tiers can never change what a gate runs. `integration` is auto-stamped from the `tests/integration/` directory; `embedded`/`hops`/`stability`/`concurrency` stay explicit. Coverage equivalence is proven against a captured baseline after every structural and dedup step.

**Tech Stack:** pytest (>=9), pytest-xdist (`-n auto --dist loadgroup`), pytest-asyncio, nox, uv, `ty` type checker. Spec: [docs/superpowers/specs/2026-06-22-test-suite-restructure-design.md](../specs/2026-06-22-test-suite-restructure-design.md).

## Global Constraints

- **Coverage stays equivalent.** Total, per-line, per-branch coverage equals baseline. Every gate selects the same set it does today. (Spec §1.1, §8.)
- **Integration/e2e scenarios preserved.** No host × term × transfer × backend × failure-mode tuple is lost. (Spec §1.2.)
- **Thoroughness over speed.** A test is removed only when *provably* redundant, with documented per-deletion evidence. (Spec §1.3, §8.3.)
- **Staged-only; Chris commits.** The `prepare-commit-msg` hook needs `/dev/tty` for AI-assist attribution — workers **stage** changes and **never run `git commit`**. Each task ends staged-clean; paste-able commit messages are provided at phase boundaries. (Spec §9; memory `feedback_no_self_commit`.)
- **Worktree:** execute in an isolated git worktree. A fresh worktree has no `.venv` — run `uv sync` once before any `ty`/sphinx/docs gate (does not dirty `uv.lock`). (memory `reference_worktree_uv_sync`.)
- **`git mv` for all relocations** to preserve blame on these churn-heavy files.
- **No `src/otto/` product changes.** This is a tests-only effort. (Spec §11.)
- **Per-task gate (runs against the REAL bed):** the worker runs the touched
  tier's tests directly — including **integration and e2e against the live
  Vagrant/QEMU lab** — plus `make coverage-unit` and `ty`. At each phase
  boundary the worker runs the full live-bed `make coverage` (+ `coverage-unix`
  / `coverage-embedded` where the phase touched those tiers) and checks parity
  against the Phase-0 real-bed baseline. The all-Pythons `make nox` matrix is
  the only deferred gate (heavy; final checklist).
- **Live-bed rules (still binding):** bed-unreachable must **FAIL loudly with a
  host-named error, never `skip`** (memory `feedback_never_skip_on_host_down`);
  **never power/reboot lab VMs** and don't `make qemu-restart` a wedged embedded
  bed without asking Chris first (memory `feedback_never_power_real_vms`); and
  **never SIGTERM/kill a live-bed run at a tight timeout** — let it finish or ask
  to recover, since killing wedges single-client consoles (memory
  `feedback_live_bed_timeout_kills_wedge`). Confirm the bed is up
  (`make vm-health`) before a live run rather than discovering it mid-suite.

---

## File Structure

**New files:**
- `tests/_fixtures/__init__.py` — package marker.
- `tests/_fixtures/labdata.py` — single source of truth for lab-data paths + host builders (`lab_data_path()`, `host_data()`, `make_host()`).
- `tests/_fixtures/paths.py` — centralized `custom_hosts` `sys.path` insert + `OTTO_SUT_DIRS` setup helpers.
- `tests/integration/conftest.py` — gains the `integration` auto-stamp hook (file already exists; hook is added).
- `tests/unit/test_tier_marker_invariants.py` — drift guards (G1 hook fires; G2 no VM marker under `unit/`).
- New tier dirs/files created by the moves in Phase 2 (e.g. `tests/e2e/...`, `tests/integration/host/test_unix_host_integration.py`).

**Relocated (via `git mv`):**
- `tests/lab_data/` → `tests/_fixtures/lab_data/`
- `tests/_loop_reaper.py` → `tests/_fixtures/_loop_reaper.py`
- `tests/integration/host/_console_lock.py` → `tests/_fixtures/_console_lock.py`
- `tests/mockrepo.py` → `tests/_fixtures/mockrepo.py`
- The Phase-2 tier moves (Spec §4).

**Modified:** `tests/conftest.py` (re-export helpers from `_fixtures`), `Makefile` (`M_UNIT`, `coverage-unit`, `repeat`, hosts.json path), `noxfile.py` (`UNIT_TEST_ARGS`), `pyproject.toml` (`testpaths`), `scripts/lab_health.py` (hosts.json path), and the importers listed per task.

**Unchanged:** `tests/repo1/`, `tests/repo2/`, `tests/repo3/`, `tests/custom_hosts/` (SUT repos stay put).

---

## Phase 0 — Baseline capture

### Task 0: Capture the equivalence baseline

**Files:**
- Create: `reports/restructure-baseline/` (git-ignored working artifacts; confirm `reports/` is in `.gitignore`).

**Interfaces:**
- Produces: `reports/restructure-baseline/collect-all.txt` (sorted node-ID list over all testpaths) and `reports/restructure-baseline/collect-novm.txt` (no-VM gate set), consumed by every later task's equivalence check.

- [ ] **Step 1: Confirm the worktree is on the base commit and synced**

Run: `uv sync`
Expected: resolves the locked dev env; `git status` clean except the spec/plan/todo already staged.

- [ ] **Step 2: Capture the full collected node-ID set (no execution)**

Run:
```bash
mkdir -p reports/restructure-baseline
uv run pytest --collect-only -q -p no:cacheprovider \
  tests/unit tests/integration \
  | grep '::' | sort > reports/restructure-baseline/collect-all.txt
wc -l reports/restructure-baseline/collect-all.txt
```
Expected: a non-empty sorted list (~the full suite). Record the line count — this is the conserved-count invariant.

- [ ] **Step 3: Capture the no-VM gate set**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider \
  tests/unit -m "not integration" \
  | grep '::' | sort > reports/restructure-baseline/collect-novm.txt
wc -l reports/restructure-baseline/collect-novm.txt
```
Expected: the set `coverage-unit` runs today. Record the count.

- [ ] **Step 4: Confirm the bed is up before the live baseline**

Run: `make vm-health`
Expected: every lab VM + Zephyr QEMU instance responds. If any host is down,
**stop and report it by name** — do not proceed to a live run that would
fail/skip silently, and do not power or restart anything without asking Chris.

- [ ] **Step 5: Capture the no-VM coverage baseline**

Run: `make coverage-unit`
Expected: PASS at the CI threshold. Save the printed coverage % and the
`reports/junit/coverage-unit/` + `htmlcov` totals as the no-VM coverage baseline.

- [ ] **Step 6: Capture the FULL real-bed coverage baseline**

Run: `make coverage`
Expected: PASS — the whole suite (`-m "not stability"`, all paths) against the
live Vagrant/QEMU bed. **Do not impose a tighter timeout than the Makefile's own
cap, and do not kill the run if a console is slow** — let it finish (a wedged
embedded console is recovered with `make qemu-restart` only after asking Chris).
Save the total / per-line / per-branch coverage and the printed % — this is the
**whole-suite parity reference** the per-task and phase-boundary checks compare
against.

- [ ] **Step 7: Capture the per-tier real-bed baselines**

Run: `make coverage-unix` then `make coverage-embedded`
Expected: both green; save each tier's coverage report. These let a later task
that touches only one tier re-verify that tier directly without the full run.

- [ ] **Step 8: Record the baselines**

Write all captured numbers (collect counts, `coverage-unit` %, full `coverage`
total/line/branch, `coverage-unix`, `coverage-embedded`) into
`reports/restructure-baseline/NOTES.md`.

- [ ] **Step 9: Stage**

Run: `git add reports/restructure-baseline/NOTES.md` (the `collect-*.txt` stay as
local working artifacts; do not stage if `reports/` is git-ignored).
No commit (Chris commits). Phase-0 artifacts are reference data, not a code change.

---

## Phase 1 — `tests/_fixtures/` consolidation + lab-data path helper

### Task 1: Create the `_fixtures` package and lab-data helper

**Files:**
- Create: `tests/_fixtures/__init__.py`, `tests/_fixtures/labdata.py`
- Relocate: `tests/lab_data/` → `tests/_fixtures/lab_data/`
- Modify: `tests/conftest.py` (delegate `host_data`/`make_host` + `_LAB_DATA` to the helper, re-export)

**Interfaces:**
- Produces: `tests._fixtures.labdata.lab_data_path() -> Path` (returns `tests/_fixtures/lab_data/tech1/hosts.json`), `host_data(ne: str) -> dict[str, Any]`, `make_host(ne: str, **kwargs) -> UnixHost`. Re-exported from `tests.conftest` so existing `from tests.conftest import host_data, make_host` keep working.

- [ ] **Step 1: Move the lab data directory**

Run:
```bash
git mv tests/lab_data tests/_fixtures/lab_data
touch tests/_fixtures/__init__.py
```
Expected: `tests/_fixtures/lab_data/tech1/hosts.json` and `.../tech2/hosts.json` exist; `tests/lab_data/` gone.

- [ ] **Step 2: Write the helper**

Create `tests/_fixtures/labdata.py`:
```python
"""Single source of truth for test lab-data paths and host builders.

Centralizes the lab JSON location so test modules never hand-roll
``Path(__file__).parents[N] / "lab_data" / ...`` arithmetic (which breaks
whenever a file moves to a different depth). Import from here (or via the
re-exports in :mod:`tests.conftest`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from otto.host.unix_host import UnixHost

_LAB_DATA_DIR = Path(__file__).parent / "lab_data"


def lab_data_dir() -> Path:
    """Directory holding the per-tech lab-data trees (``tech1``/``tech2``)."""
    return _LAB_DATA_DIR


def lab_data_path(tech: str = "tech1") -> Path:
    """Path to a tech's ``hosts.json`` (default the primary ``tech1`` lab)."""
    return _LAB_DATA_DIR / tech / "hosts.json"


def host_data(ne: str, tech: str = "tech1") -> dict[str, Any]:
    """Return the raw host dict for ``ne`` from the lab JSON."""
    hosts = json.loads(lab_data_path(tech).read_text())
    for host in hosts:
        if host["element"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {lab_data_path(tech)}")


def make_host(ne: str, **kwargs: Any) -> UnixHost:
    """Build a UnixHost from lab data with optional field overrides."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        **kwargs,
    )
```

- [ ] **Step 3: Delegate the root conftest to the helper (keep the public names)**

In `tests/conftest.py`, replace the `_LAB_DATA` constant + `host_data` + `make_host` bodies (currently ~lines 387–409) with a re-export, leaving the public names importable:
```python
from tests._fixtures.labdata import host_data, make_host, lab_data_path  # noqa: F401

# Back-compat alias for any module still importing the old private constant.
_LAB_DATA = lab_data_path()
```
Delete the now-duplicated function bodies. Do **not** change any other fixture.

- [ ] **Step 4: Verify collection is unchanged**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider tests/unit tests/integration \
  | grep '::' | sort > /tmp/collect-after-t1.txt
diff reports/restructure-baseline/collect-all.txt /tmp/collect-after-t1.txt && echo "IDENTICAL"
```
Expected: `IDENTICAL` (no test added/removed/renamed; only helper internals moved).

- [ ] **Step 5: Verify the no-VM gate still passes**

Run: `make coverage-unit`
Expected: PASS, same coverage % as the Task-0 baseline.

- [ ] **Step 6: Stage**

Run: `git add -A tests/_fixtures tests/conftest.py` (the `git mv` is already staged).
Confirm `git status` shows only the intended moves/edits. No commit.

### Task 2: Relocate the shared helper modules

**Files:**
- Relocate: `tests/_loop_reaper.py` → `tests/_fixtures/_loop_reaper.py`; `tests/integration/host/_console_lock.py` → `tests/_fixtures/_console_lock.py`; `tests/mockrepo.py` → `tests/_fixtures/mockrepo.py`
- Modify importers: `tests/conftest.py:31`, `tests/unit/test_loop_reaper.py:14`, `tests/integration/host/conftest.py:30`, `tests/unit/host/test_console_lock.py:8`, `tests/unit/configmodule/test_repo.py:8`

**Interfaces:**
- Consumes: `tests._fixtures` package from Task 1.
- Produces: `tests._fixtures._loop_reaper`, `tests._fixtures._console_lock`, `tests._fixtures.mockrepo` import paths.

- [ ] **Step 1: Move the modules**

Run:
```bash
git mv tests/_loop_reaper.py tests/_fixtures/_loop_reaper.py
git mv tests/integration/host/_console_lock.py tests/_fixtures/_console_lock.py
git mv tests/mockrepo.py tests/_fixtures/mockrepo.py
```

- [ ] **Step 2: Update the five importers**

Apply each edit exactly:
- `tests/conftest.py:31` — `from tests._loop_reaper import ...` → `from tests._fixtures._loop_reaper import classify_loop_origin, reap_or_raise`
- `tests/unit/test_loop_reaper.py:14` — `from tests._loop_reaper import (...)` → `from tests._fixtures._loop_reaper import (...)`
- `tests/integration/host/conftest.py:30` — `from tests.integration.host._console_lock import console_access` → `from tests._fixtures._console_lock import console_access`
- `tests/unit/host/test_console_lock.py:8` — same rewrite to `from tests._fixtures._console_lock import console_access`
- `tests/unit/configmodule/test_repo.py:8` — `from tests.mockrepo import MockRepo` → `from tests._fixtures.mockrepo import MockRepo`

- [ ] **Step 3: Grep for any missed references**

Run:
```bash
grep -rn -E "tests\._loop_reaper|tests\.mockrepo|tests\.integration\.host\._console_lock|import _console_lock" tests src
```
Expected: no hits (all rewritten). Comment-only path strings referencing the old locations may be updated for clarity but aren't load-bearing.

- [ ] **Step 4: Verify collection + no-VM gate**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider tests/unit tests/integration \
  | grep '::' | sort > /tmp/collect-after-t2.txt
diff reports/restructure-baseline/collect-all.txt /tmp/collect-after-t2.txt && echo "IDENTICAL"
make coverage-unit
```
Expected: `IDENTICAL`; `coverage-unit` PASS at baseline %.

- [ ] **Step 5: Stage** (`git add -A tests`; no commit).

### Task 3: Centralize the `custom_hosts` path + `OTTO_SUT_DIRS` setup

**Files:**
- Create: `tests/_fixtures/paths.py`
- Modify: `tests/integration/host/conftest.py:41-44`, `tests/unit/host/test_zephyr_inline_frame.py:21-24`, `tests/unit/host/test_custom_hosts_module.py:19-22` (custom_hosts insert); `tests/integration/conftest.py:13`, `tests/unit/cov/conftest.py:13` (`OTTO_SUT_DIRS`)

**Interfaces:**
- Produces: `tests._fixtures.paths.ensure_custom_hosts_on_path() -> None` and `tests._fixtures.paths.default_sut_dir() -> str` / `ensure_sut_dirs() -> None`.

- [ ] **Step 1: Write the helper**

Create `tests/_fixtures/paths.py`:
```python
"""Centralized test sys.path / env setup that was copy-pasted across conftests.

``ensure_custom_hosts_on_path`` makes the repo's shared ``custom_hosts`` package
importable (the third-party-style frame package SUT repos depend on).
``ensure_sut_dirs`` points OTTO_SUT_DIRS at the ``repo1`` fixture SUT — both must
run before any ``otto`` import, so call them at conftest import time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/  (this file lives at tests/_fixtures/paths.py)
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_CUSTOM_HOSTS = _TESTS_ROOT / "custom_hosts"
_REPO1 = _TESTS_ROOT / "repo1"


def ensure_custom_hosts_on_path() -> None:
    """Prepend the shared ``custom_hosts`` dir to ``sys.path`` (idempotent)."""
    p = str(_CUSTOM_HOSTS)
    if p not in sys.path:
        sys.path.insert(0, p)


def default_sut_dir() -> str:
    """Path to the ``repo1`` fixture SUT used by the cov/integration suites."""
    return str(_REPO1)


def ensure_sut_dirs() -> None:
    """Default ``OTTO_SUT_DIRS`` to ``repo1`` if unset (must precede otto import)."""
    os.environ.setdefault("OTTO_SUT_DIRS", default_sut_dir())
```
> `_TESTS_ROOT` is computed once *here*; the call sites stop doing their own depth arithmetic, so future moves don't re-break them (Spec §7.3).

- [ ] **Step 2: Replace the three `custom_hosts` inserts**

In each of `tests/integration/host/conftest.py`, `tests/unit/host/test_zephyr_inline_frame.py`, `tests/unit/host/test_custom_hosts_module.py`, replace the local `_CUSTOM_HOSTS = Path(...).parents[2] / "custom_hosts"` + `sys.path.insert` guard block with:
```python
from tests._fixtures.paths import ensure_custom_hosts_on_path

ensure_custom_hosts_on_path()
```
Keep the subsequent `from custom_hosts.zephyr_inline import ...` import lines unchanged (now resolvable via the helper).

- [ ] **Step 3: Replace the two `OTTO_SUT_DIRS` setups**

In `tests/integration/conftest.py:13` and `tests/unit/cov/conftest.py:13`, replace the `os.environ.setdefault('OTTO_SUT_DIRS', str(Path(__file__)...))` line with (kept at the same import-time position, before any otto import):
```python
from tests._fixtures.paths import ensure_sut_dirs

ensure_sut_dirs()
```
> Import order matters: `ensure_sut_dirs()` must run before the conftest imports anything from `otto` (configmodule reads `OTTO_SUT_DIRS` at import). Place the import+call at the very top, preserving the existing "must precede otto imports" comment.

- [ ] **Step 4: Verify collection + no-VM gate + import order**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider tests/unit tests/integration \
  | grep '::' | sort > /tmp/collect-after-t3.txt
diff reports/restructure-baseline/collect-all.txt /tmp/collect-after-t3.txt && echo "IDENTICAL"
make coverage-unit
uv run pytest tests/unit/cov -p no:cacheprovider -q   # exercises OTTO_SUT_DIRS path
```
Expected: `IDENTICAL`; both runs PASS (the cov suite proves `OTTO_SUT_DIRS` still resolves repo1).

- [ ] **Step 5: Stage** (`git add -A tests`; no commit).

### Task 4: Update non-test references to lab_data

**Files:**
- Modify: `Makefile:196`, `scripts/lab_health.py:43`

**Interfaces:** none (string path updates only).

- [ ] **Step 1: Update the Makefile hosts.json path**

In `Makefile:196`, change `tests/lab_data/tech1/hosts.json` → `tests/_fixtures/lab_data/tech1/hosts.json` (the `stability` target's `jq` ping loop).

- [ ] **Step 2: Update the lab_health script default**

In `scripts/lab_health.py:43`, change `DEFAULT_HOSTS = Path("tests/lab_data/tech1/hosts.json")` → `Path("tests/_fixtures/lab_data/tech1/hosts.json")` (and the doc comment at line 27).

- [ ] **Step 3: Grep for stragglers**

Run: `grep -rn "tests/lab_data" . --include='*.py' --include='Makefile' --include='*.toml' --include='*.cfg' --include='*.yml'`
Expected: no remaining `tests/lab_data` references (only `tests/_fixtures/lab_data`).

- [ ] **Step 4: Smoke-test the script path resolves**

Run: `uv run python -c "from pathlib import Path; p=Path('tests/_fixtures/lab_data/tech1/hosts.json'); assert p.exists(), p; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Stage** (`git add Makefile scripts/lab_health.py`; no commit).

**Phase 1 boundary.** The helpers back the embedded console-lock + cov suites, so
the worker runs the live-bed `make coverage-unix` and `make coverage-embedded`
(bed up; fail-don't-skip) and confirms each tier's coverage matches the Phase-0
baseline **before staging is handed off**. Then Chris commits — paste-able
message:
```
refactor(tests): consolidate lab_data + shared helpers under tests/_fixtures/

Move lab_data/ and the _loop_reaper/_console_lock/mockrepo helpers into a
tests/_fixtures/ package; add a single lab-data path helper (labdata.py) and a
paths.py for the custom_hosts sys.path + OTTO_SUT_DIRS setup, killing the
duplicated depth-arithmetic. Behavior-preserving: collect-only set identical,
coverage-unit + live-bed coverage-unix/embedded unchanged. Spec §7.
```

---

## Phase 2 — Tier restructure

### Task 5: Add the `integration` auto-stamp hook + drift guards

**Files:**
- Modify: `tests/integration/conftest.py` (add `pytest_collection_modifyitems`)
- Create: `tests/unit/test_tier_marker_invariants.py`

**Interfaces:**
- Produces: every item collected under `tests/integration/` carries the `integration` marker without an explicit decorator. Guards enforce the invariant.

- [ ] **Step 1: Write a failing guard for the auto-stamp hook**

Create `tests/unit/test_tier_marker_invariants.py`:
```python
"""Drift guards for the tier<->marker contract (Spec §5.3).

Run in the no-VM unit gate. G1 proves the integration/ auto-stamp hook fires;
G2 proves no VM-only marker leaks into the unit tier.
"""

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]          # tests/
_UNIT = _TESTS / "unit"

# Markers that mean "needs a VM" — must never appear on a unit-tier test.
_VM_MARKERS = {"integration", "embedded", "hops"}


def test_integration_conftest_autostamps_integration():
    """G1: the integration/ conftest stamps `integration` by directory."""
    from tests.integration import conftest as integ

    integ_root = Path(integ.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(integ_root / "host" / "test_example.py")
    integ.pytest_collection_modifyitems(config=None, items=[item])
    assert "integration" in item.added


def _module_and_decorator_markers(path: Path) -> set[str]:
    """Marker names referenced by decorators or module-level `pytestmark`."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        # @pytest.mark.<name>
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
            if getattr(node.value, "attr", None) == "mark":
                found.add(node.attr)
    return found


def test_unit_tier_has_no_vm_markers():
    """G2: no test file under tests/unit/ references a VM-only marker."""
    offenders: list[str] = []
    for path in _UNIT.rglob("test_*.py"):
        if _VM_MARKERS & _module_and_decorator_markers(path):
            offenders.append(str(path.relative_to(_TESTS)))
    assert not offenders, f"VM markers found under tests/unit/: {offenders}"
```

- [ ] **Step 2: Run G1 to verify it fails (hook not yet added)**

Run: `uv run pytest tests/unit/test_tier_marker_invariants.py::test_integration_conftest_autostamps_integration -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'pytest_collection_modifyitems'` (the integration conftest doesn't define it yet).

- [ ] **Step 3: Add the auto-stamp hook**

In `tests/integration/conftest.py`, add at the top (after imports) and a module-level helper:
```python
import pytest
from pathlib import Path

_INTEGRATION_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``integration`` marker to every test under this tree.

    The ``tests/integration/`` directory is the single source of truth for the
    integration tier (Spec §5.1): tests here drive the real Vagrant/QEMU bed via
    otto's Python API. Stamping the marker from the path lets the marker-based
    gates (``coverage-unix`` = ``-m "integration and not embedded"``, etc.)
    select this tree without each test repeating ``@pytest.mark.integration``.
    Idempotent and additive — explicit ``embedded``/``hops``/``stability`` stay.
    """
    for item in items:
        if _INTEGRATION_ROOT in item.path.parents:
            item.add_marker("integration")
```
> Note: `tests/integration/host/conftest.py` already defines its own `pytest_collection_modifyitems` (embedded xdist grouping). pytest runs **both** — conftest hooks compose, they don't override. Keep them separate.

- [ ] **Step 4: Run both guards to verify they pass**

Run: `uv run pytest tests/unit/test_tier_marker_invariants.py -v`
Expected: G1 PASS; G2 PASS (no VM markers under `tests/unit/` *yet* — they move out in Tasks 6–9; if G2 fails now it lists the files Tasks 6–9 will relocate, which is expected until those tasks land — run G2 last in the phase if so). To keep this task green standalone, temporarily `xfail` G2 with reason `"unit/ still holds VM tests until Tasks 6-9"` and remove the xfail in Task 10.

- [ ] **Step 5: Verify the integration tree is now auto-stamped (no set change yet)**

Run:
```bash
uv run pytest tests/integration --collect-only -q -p no:cacheprovider | grep -c '::'
uv run pytest tests/integration -m integration --collect-only -q -p no:cacheprovider | grep -c '::'
```
Expected: **equal counts** — every integration test is now selected by `-m integration` (proving the stamp fired for all of them). This is the Spec §5.4 pre-flight: if the counts differ, list the unstamped items and stop.

- [ ] **Step 6: Confirm marker-based gates are unaffected on the no-VM side**

Run: `make coverage-unit`
Expected: PASS at baseline % (the auto-stamp only adds `integration` to the integration tree, which the no-VM gate already excludes).

- [ ] **Step 7: Stage** (`git add tests/integration/conftest.py tests/unit/test_tier_marker_invariants.py`; no commit).

### Task 6: Clean whole-file moves (unit → integration)

**Files:**
- Relocate: `tests/unit/host/test_hop_integration.py` → `tests/integration/host/`; `tests/unit/host/test_session_stability_integration.py` → `tests/integration/host/`; `tests/unit/cov/test_coverage_pipeline.py` → `tests/integration/cov/`
- Modify each moved file: drop the now-redundant explicit `integration` marker; keep `hops`/`stability`/`timeout`.

**Interfaces:** none new (relocation + marker cleanup).

- [ ] **Step 1: Move the three files**

Run:
```bash
mkdir -p tests/integration/cov
git mv tests/unit/host/test_hop_integration.py tests/integration/host/test_hop_integration.py
git mv tests/unit/host/test_session_stability_integration.py tests/integration/host/test_session_stability_integration.py
git mv tests/unit/cov/test_coverage_pipeline.py tests/integration/cov/test_coverage_pipeline.py
touch tests/integration/cov/__init__.py 2>/dev/null || true
```
> Only add `__init__.py` if sibling integration dirs use them; match the existing convention (check `tests/integration/host/` — if no `__init__.py`, skip it).

- [ ] **Step 2: Drop the redundant `integration` marker in each**

In each moved file, remove only the `integration` entry from the module-level `pytestmark` (or the per-test `@pytest.mark.integration` decorator), leaving the type/heaviness markers:
- `test_hop_integration.py`: `pytestmark = [pytest.mark.timeout(30), pytest.mark.integration]` → `pytestmark = [pytest.mark.timeout(30)]` (keep any per-test `@pytest.mark.hops`).
- `test_session_stability_integration.py`: module `pytestmark` list → remove `pytest.mark.integration`, **keep** `pytest.mark.stability`.
- `test_coverage_pipeline.py`: remove its `integration` marker (module or per-test).

- [ ] **Step 3: Verify each moved file is still selected by its gates**

Run:
```bash
# auto-stamp restores `integration`; hops/stability preserved
uv run pytest tests/integration/host/test_hop_integration.py -m "integration and hops" --collect-only -q -p no:cacheprovider | grep -c '::'
uv run pytest tests/integration/host/test_session_stability_integration.py -m "integration and stability" --collect-only -q -p no:cacheprovider | grep -c '::'
uv run pytest tests/integration/cov/test_coverage_pipeline.py -m integration --collect-only -q -p no:cacheprovider | grep -c '::'
```
Expected: non-zero counts matching each file's test count — the auto-stamp re-supplies `integration`, and `hops`/`stability` survive.

- [ ] **Step 4: Verify the no-VM set is unchanged**

The three relocated files all carried `integration`, so they were never in the
no-VM set — moving them must leave it untouched:
```bash
uv run pytest --collect-only -q -p no:cacheprovider tests/unit -m "not integration" \
  | grep '::' | sort > /tmp/collect-novm-t6.txt
diff reports/restructure-baseline/collect-novm.txt /tmp/collect-novm-t6.txt && echo "NO-VM UNCHANGED"
# And the whole-suite count is conserved (moved, not lost):
uv run pytest --collect-only -q -p no:cacheprovider | grep -c '::'
```
Expected: `NO-VM UNCHANGED` (empty diff); all-paths count equals the Task-0 baseline.

- [ ] **Step 5: Stage** (`git add -A tests`; no commit).

### Task 7: Split `test_unix_host.py` (82 unit / 20 integration)

**Files:**
- Modify: `tests/unit/host/test_unix_host.py` (remove the 20 `@integration` methods)
- Create: `tests/integration/host/test_unix_host_integration.py` (the 20 moved methods)

**Interfaces:** none new.

- [ ] **Step 1: Identify the integration methods**

Run: `grep -nB1 "@pytest.mark.integration" tests/unit/host/test_unix_host.py`
Expected: exactly 20 decorated test functions. Record their names — these move; the other 82 stay.

- [ ] **Step 2: Create the integration file with the moved methods**

Create `tests/integration/host/test_unix_host_integration.py`: a module that imports the same fixtures/helpers the moved methods used (check the original's imports — `host_data`/`make_host` from `tests.conftest`, any module-level constants the moved methods reference) and contains the 20 functions verbatim, with the explicit `@pytest.mark.integration` decorator **removed** (auto-stamped by the integration/ dir). Keep any `@pytest.mark.hops`/`timeout` on individual methods.

- [ ] **Step 3: Remove the 20 methods from the unit file**

Delete those 20 functions (and any now-unused imports) from `tests/unit/host/test_unix_host.py`, leaving the 82 unit tests.

- [ ] **Step 4: Verify the split conserves every test**

Run:
```bash
uv run pytest tests/unit/host/test_unix_host.py tests/integration/host/test_unix_host_integration.py \
  --collect-only -q -p no:cacheprovider | grep -c '::'
```
Expected: **102** (82 + 20) — no test lost or duplicated.

- [ ] **Step 5: Verify marker routing**

Run:
```bash
uv run pytest tests/unit/host/test_unix_host.py -m "not integration and not embedded" --collect-only -q -p no:cacheprovider | grep -c '::'   # expect 82
uv run pytest tests/integration/host/test_unix_host_integration.py -m integration --collect-only -q -p no:cacheprovider | grep -c '::'        # expect 20
```
Expected: 82 and 20 respectively.

- [ ] **Step 6: Run BOTH halves (no-VM + integration against the bed)**

Run:
```bash
uv run pytest tests/unit/host/test_unix_host.py -p no:cacheprovider -q                         # 82 no-VM
uv run pytest tests/integration/host/test_unix_host_integration.py -p no:cacheprovider -q       # 20 live bed
```
Expected: 82 PASS and 20 PASS. The integration run uses the live Vagrant bed —
confirm it's up (`make vm-health`) first; on a bed failure, report the host by
name, don't skip. Together they prove the split preserved both halves' behavior,
not just collection.

- [ ] **Step 7: Stage** (`git add -A tests`; no commit).

### Task 8: Split `test_import_and_register.py` and `test_logger.py`

**Files:**
- Modify: `tests/unit/suite/test_import_and_register.py` (remove the 1 integration test); `tests/unit/logger/test_logger.py` (remove its integration test(s))
- Create: `tests/integration/suite/test_import_and_register_integration.py`; relocate the logger integration test(s) to `tests/integration/<subsystem>/`

**Interfaces:** none new.

- [ ] **Step 1: Locate the integration tests in each file**

Run:
```bash
grep -nB2 -E "mark\.integration" tests/unit/suite/test_import_and_register.py tests/unit/logger/test_logger.py
```
Expected: 1 integration test in `test_import_and_register.py`; identify the integration test(s) in `test_logger.py` (the audit flagged it; confirm count + names here).

- [ ] **Step 2: Extract the suite integration test**

Create `tests/integration/suite/test_import_and_register_integration.py` (and `tests/integration/suite/` dir) holding the single moved test verbatim, `@pytest.mark.integration` removed (auto-stamped), preserving its other markers and required imports. Remove it from the unit file (11 remain).

- [ ] **Step 3: Extract the logger integration test(s)**

Move the logger integration test(s) into `tests/integration/logger/test_logger_integration.py` (create the dir), decorator removed, imports preserved. Leave the no-VM logger tests in `tests/unit/logger/test_logger.py`.

- [ ] **Step 4: Verify conservation + routing**

Run:
```bash
uv run pytest tests/unit/suite/test_import_and_register.py tests/integration/suite/test_import_and_register_integration.py --collect-only -q -p no:cacheprovider | grep -c '::'   # expect 12
uv run pytest tests/unit/suite/test_import_and_register.py -m "not integration" --collect-only -q -p no:cacheprovider | grep -c '::'   # expect 11
uv run pytest tests/unit/logger/test_logger.py -m "not integration" -p no:cacheprovider -q   # no-VM logger tests PASS
```
Expected: 12 total, 11 unit for the suite file; logger no-VM tests PASS.

- [ ] **Step 5: Stage** (`git add -A tests`; no commit).

### Task 9: e2e tier moves

**Files:**
- Relocate (unmarked CLI, → `tests/e2e/`): `tests/unit/configmodule/test_completion_cache.py` → `tests/e2e/configmodule/`; `tests/unit/cov/test_coverage_e2e.py` → `tests/e2e/cov/`; `tests/unit/host/test_interact_e2e.py` → `tests/e2e/host/`; `tests/unit/suite/test_stability_e2e.py` → `tests/e2e/suite/`
- Relocate (keep marker, integration → e2e): `tests/integration/test_docker_e2e_cli.py` → `tests/e2e/docker/`; `tests/integration/test_embedded_coverage_e2e.py` → `tests/e2e/host/`

**Interfaces:** none new.

- [ ] **Step 1: Confirm the four CLI files are genuinely no-VM**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache.py tests/unit/cov/test_coverage_e2e.py tests/unit/host/test_interact_e2e.py tests/unit/suite/test_stability_e2e.py -p no:cacheprovider -q`
Expected: PASS with no Vagrant lab up (they pass in CI today → no VM). If any needs a VM, STOP and surface it (Spec §4.3 invariant — that would be a pre-existing latent bug, not something to paper over).

- [ ] **Step 2: Move the four unmarked CLI files (stay unmarked)**

Run:
```bash
mkdir -p tests/e2e/configmodule tests/e2e/cov tests/e2e/host tests/e2e/suite tests/e2e/docker
git mv tests/unit/configmodule/test_completion_cache.py tests/e2e/configmodule/test_completion_cache.py
git mv tests/unit/cov/test_coverage_e2e.py tests/e2e/cov/test_coverage_e2e.py
git mv tests/unit/host/test_interact_e2e.py tests/e2e/host/test_interact_e2e.py
git mv tests/unit/suite/test_stability_e2e.py tests/e2e/suite/test_stability_e2e.py
```
Do **not** add markers — they must keep running in the no-VM gate.

- [ ] **Step 3: Move the two VM-requiring e2e files (keep their markers)**

Run:
```bash
git mv tests/integration/test_docker_e2e_cli.py tests/e2e/docker/test_docker_e2e_cli.py
git mv tests/integration/test_embedded_coverage_e2e.py tests/e2e/host/test_embedded_coverage_e2e.py
```
> These leave `tests/integration/` so the auto-stamp no longer applies. They **must keep their explicit `@pytest.mark.integration` / `@pytest.mark.embedded`** (Spec §5.1, §4.3). Verify the decorators/`pytestmark` are present after the move; if `test_docker_e2e_cli.py` relied on the auto-stamp for `integration`, **add the explicit decorator back** now.

- [ ] **Step 4: Verify the VM e2e files keep their markers**

Run:
```bash
uv run pytest tests/e2e/docker/test_docker_e2e_cli.py -m integration --collect-only -q -p no:cacheprovider | grep -c '::'    # > 0
uv run pytest tests/e2e/host/test_embedded_coverage_e2e.py -m embedded --collect-only -q -p no:cacheprovider | grep -c '::'  # > 0
```
Expected: non-zero — both still selected by their resource gates.

- [ ] **Step 5: Verify the unmarked e2e files run in the no-VM gate via the new path scope**

Run: `uv run pytest tests/e2e -m "not integration and not embedded" -p no:cacheprovider -q`
Expected: the four CLI files PASS; the two VM files are deselected.

- [ ] **Step 6: Stage** (`git add -A tests`; no commit).

### Task 10: Flip the four path-based gates + finalize guards

**Files:**
- Modify: `pyproject.toml` (`testpaths`), `Makefile` (`M_UNIT`, `coverage-unit`, `repeat`, `repeat` doc), `noxfile.py` (`UNIT_TEST_ARGS`), `tests/unit/test_tier_marker_invariants.py` (remove the Task-5 `xfail` on G2)

**Interfaces:** the no-VM gates become `tests/unit tests/e2e -m "not integration and not embedded"`; `repeat` becomes the full local suite.

- [ ] **Step 1: Expand `testpaths`**

In `pyproject.toml` (currently lines 145–148):
```toml
testpaths = [
    "tests/unit",
    "tests/integration",
    "tests/e2e",
]
```

- [ ] **Step 2: Update `M_UNIT` and `coverage-unit`**

In `Makefile`:
- Line 51: `M_UNIT := not integration` → `M_UNIT := not integration and not embedded`
- Line 152: `... pytest tests/unit -m "$(M_UNIT)" ...` → `... pytest tests/unit tests/e2e -m "$(M_UNIT)" ...`

- [ ] **Step 3: Update `repeat` to the full local suite**

In `Makefile` (lines 215–220):
- Line 215 doc: `## Run the full unit suite (including integration) under pytest-repeat...` → `## Run the full local suite (unit + integration + e2e) under pytest-repeat. Local only; requires VMs. ...`
- Line 216: `... pytest tests/unit \` → `... pytest \` (drop the path arg; default `testpaths` now covers all three tiers).

- [ ] **Step 4: Update the nox unit args**

In `noxfile.py` (lines 42–47):
```python
UNIT_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded",
    "--cov-fail-under=80",
)
```

- [ ] **Step 5: Remove the Task-5 G2 xfail**

In `tests/unit/test_tier_marker_invariants.py`, remove the temporary `xfail` on `test_unit_tier_has_no_vm_markers` (all VM tests have now left `tests/unit/`).

- [ ] **Step 6: Prove the no-VM gate set is identical to baseline**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider tests/unit tests/e2e -m "not integration and not embedded" \
  | grep '::' > /tmp/novm-after.txt
# Normalize away the tier-dir prefix so relocations don't show as diffs:
sed -E 's#^tests/(unit|integration|e2e)/##' /tmp/novm-after.txt | sort > /tmp/novm-after-norm.txt
sed -E 's#^tests/(unit|integration|e2e)/##' reports/restructure-baseline/collect-novm.txt | sort > /tmp/novm-base-norm.txt
diff /tmp/novm-base-norm.txt /tmp/novm-after-norm.txt && echo "NO-VM SET IDENTICAL (modulo tier path)"
```
Expected: `NO-VM SET IDENTICAL` — same node identities, only their tier-dir prefix changed.

- [ ] **Step 7: Prove the whole collected set is conserved, and snapshot it for Phase 3**

Run:
```bash
uv run pytest --collect-only -q -p no:cacheprovider | grep -c '::'   # == Task-0 all-paths count
# Snapshot the post-restructure set as the Phase-3 reference (Phase 2 renamed
# the split files, so Phase 3 must diff against THIS, not the Phase-0 baseline):
uv run pytest --collect-only -q -p no:cacheprovider | grep '::' | sort \
  > reports/restructure-baseline/collect-post-phase2.txt
```
Expected: count equals the Task-0 all-paths count (the splits conserve count — 82+20=102 for `test_unix_host` — and every move conserves count). `collect-post-phase2.txt` is the exact-match reference for Phase 3.

- [ ] **Step 8: Run the no-VM gate + guards + type check**

Run:
```bash
make coverage-unit
uv run pytest tests/unit/test_tier_marker_invariants.py -v
uv sync && uv run ty check   # or `make typecheck`
```
Expected: `coverage-unit` PASS at baseline %; both guards PASS; `ty` clean.

- [ ] **Step 9: Run the FULL live-bed parity gate**

Confirm the bed is up (`make vm-health`), then run `make coverage` (whole suite,
all tiers, live bed). Don't impose a tighter timeout than the Makefile's cap or
kill a slow console. Assert the total / per-line / per-branch coverage equals the
Phase-0 full real-bed baseline (Task 0 Step 6), and that the marker-based gates
select the expected sets (the restructure must not move coverage at all).

- [ ] **Step 10: Stage** (`git add -A`; no commit).

**Phase 2 boundary — Chris commits.** The worker has already proven full live-bed
parity in Step 9. Paste-able message:
```
refactor(tests): split tests into unit/integration/e2e tiers (fable #5)

Directories now carry the tier (level); markers carry resource-need. Auto-stamp
`integration` from tests/integration/; drop redundant explicit decorators; keep
embedded/hops/stability/concurrency. Split mixed files (test_unix_host 82u/20i,
import_and_register, logger); move CLI e2e suites to tests/e2e/. Flip the four
path-based gates (coverage-unit, nox-unit, repeat, testpaths) to marker-based.
Add tier<->marker drift guards. Full live-bed coverage proven equal to baseline.
Spec §3-§6.
```
The all-Pythons `make nox` matrix stays deferred to the final checklist (heavy;
the restructure is Python-version-agnostic, so pinned-Python `make coverage`
parity is the load-bearing check here).

---

## Phase 3 — Dedup

> Each dedup tier escalates only after the prior is green. Every step re-proves
> the relevant coverage invariant against the **real bed** in-loop (the affected
> tests may be integration/embedded). Tier-3 removals are staged with their
> evidence (coverage diff + scenario citation); Chris commits each tier.

### Task 11: Tier-1 — fixture / helper / conftest collapse (coverage bit-identical)

**Files:**
- Modify: `tests/integration/conftest.py` (drop `_host_data`, the `carrot`/`tomato` fixtures), `tests/unit/cov/conftest.py` (dedupe `carrot`/`tomato`), `tests/conftest.py` (`host2`/`host3` factory)

**Interfaces:** behavior identical; only fixture/helper internals change.

- [ ] **Step 1: Replace `_host_data` with the shared helper**

In `tests/integration/conftest.py`, delete the local `_host_data` (lines ~87–92) and `_LAB_DATA` (line 24); rewrite the `carrot`/`tomato` fixtures to use the shared `make_host` (matching the DRY pattern already in `tests/unit/cov/conftest.py`):
```python
from tests.conftest import make_host  # re-exported from tests._fixtures.labdata

@pytest_asyncio.fixture
async def carrot():
    h = make_host("carrot", term="ssh", transfer="scp")
    yield h
    await h.close()

@pytest_asyncio.fixture
async def tomato():
    h = make_host("tomato", term="ssh", transfer="scp")
    yield h
    await h.close()
```

- [ ] **Step 2: Collapse `host2`/`host3` into one factory**

In `tests/conftest.py`, replace the near-identical `host2` (tomato) and `host3` (pepper) bodies with a shared helper they both call (preserving their distinct NE + the `host3` ssh→scp override), keeping the two fixture names so existing `indirect` parametrizations are untouched. Example:
```python
async def _term_host(ne: str, request, *, ssh_transfer: str | None = None):
    term = request.param
    kwargs: dict[str, str] = {"term": term}
    if term == "telnet":
        kwargs["transfer"] = "ftp"
    elif ssh_transfer and term == "ssh":
        kwargs["transfer"] = ssh_transfer
    h = make_host(ne, **kwargs)
    yield h
    await h.close()

@pytest_asyncio.fixture
async def host2(request):
    async for h in _term_host("tomato", request):
        yield h

@pytest_asyncio.fixture
async def host3(request):
    async for h in _term_host("pepper", request, ssh_transfer="scp"):
        yield h
```
> Verify the original `host2`/`host3` semantics exactly (term→transfer mapping) before collapsing; if the `host3` ssh-branch differs, preserve it precisely.

- [ ] **Step 2b: De-duplicate the `carrot`/`tomato` fixtures across conftests**

If `tests/unit/cov/conftest.py` and `tests/integration/conftest.py` now hold identical `carrot`/`tomato`, leave both **only if** they serve different test dirs (pytest fixtures are conftest-scoped). Do not hoist into the root conftest unless every consumer needs them — keep fixtures close to their users (YAGNI).

- [ ] **Step 3: Prove the collected set is unchanged (no test body moved/renamed)**

Tier-1 touches only fixtures/helpers, so the set must match the **post-Phase-2**
snapshot *exactly* (no normalization needed — nothing renamed since then):
```bash
uv run pytest --collect-only -q -p no:cacheprovider | grep '::' | sort > /tmp/collect-t11.txt
diff reports/restructure-baseline/collect-post-phase2.txt /tmp/collect-t11.txt && echo "SET IDENTICAL"
make coverage-unit
```
Expected: `SET IDENTICAL`; `coverage-unit` PASS at baseline % (fixtures changed, test functions did not → coverage unchanged).

- [ ] **Step 4: Run the live-bed integration gate (these fixtures back it)**

Confirm the bed is up, then run `make coverage-unix` (and `make coverage-embedded`
if the embedded fixtures were touched). Assert each tier's coverage equals the
Phase-0 baseline — the fixture refactor must not move integration coverage. Then
stage (`git add -A tests`; no commit).

### Task 12: Tier-2 — parametrize duplicate test bodies (coverage delta 0)

**Files:**
- Modify: the candidate clusters surfaced by the audit (e.g. term-variant duplicates in `tests/integration/host/test_hop_integration.py` / `test_hop.py`), one cluster per commit.

**Interfaces:** test IDs may change; the merged parametrization must yield the same case count + assertions.

- [ ] **Step 1: Pick one cluster and record its baseline**

Run (example for a hop cluster — substitute the real file):
```bash
uv run pytest tests/integration/host/test_hop_integration.py --collect-only -q -p no:cacheprovider | grep -c '::'
uv run pytest tests/integration/host/test_hop_integration.py --cov=otto --cov-report=term-missing -p no:cacheprovider -q 2>/dev/null | tail -40 > /tmp/cov-before.txt
```
Record the case count and the per-file covered lines (this file's coverage rows). Run the coverage command in-loop — against the live bed for integration/embedded clusters (confirm the bed is up first), locally for no-VM clusters.

- [ ] **Step 2: Merge the duplicate functions into one parametrized test**

Combine `test_foo_ssh` + `test_foo_telnet` (same body, different term) into:
```python
@pytest.mark.parametrize("term", ["ssh", "telnet"])
def test_foo(term, ...):
    ...   # identical assertions, term threaded through
```
Retain **every** assertion from each original. The parametrization must produce the **same number of cases** as the originals combined.

- [ ] **Step 3: Prove case count + coverage delta 0**

Run the same two commands as Step 1; assert the post-merge case count equals the pre-merge total and the per-file covered-line set is unchanged (delta 0). If anything differs, revert the merge — it wasn't a true duplicate.

- [ ] **Step 4: Stage with the evidence** (`git add` the file; record the count/coverage parity in the commit message Chris will use). No commit.

- [ ] **Step 5: Repeat Steps 1–4 per remaining cluster** (one cluster at a time; never batch unverified merges).

### Task 13: Tier-3 — remove provably-redundant tests (evidence per deletion)

**Files:**
- Modify/Delete: only tests that meet all three Spec §8.3 conditions.

**Interfaces:** none.

- [ ] **Step 1: Nominate a candidate and capture pre-removal coverage**

For each nominee, capture the full coverage report for the gate set the test belongs to — run `make coverage` against the live bed for integration/embedded nominees (bed up; fail-don't-skip), or the no-VM coverage for no-VM nominees. In-loop either way.

- [ ] **Step 2: Remove the test and re-run coverage**

Delete the test; re-run the same coverage report.

- [ ] **Step 3: Apply the deletion gate (all three must hold)**

- **(a)** total + per-line + per-branch coverage **byte-identical** with the test removed;
- **(b)** **no unique scenario tuple lost** — confirm another retained test still exercises the same host × term × transfer × backend × failure-mode combination (coverage parity alone is insufficient);
- **(c)** **document** the evidence (the coverage diff showing 0 change + the retained-test citation that covers the scenario) in the deletion's commit message.

If any condition fails, restore the test — it is not redundant.

- [ ] **Step 4: Stage with documented evidence** (`git add`; the evidence goes in the message Chris commits with). No commit. Repeat per nominee.

- [ ] **Step 5: Final full-suite verification (worker, live bed)**

The worker runs `make coverage` (+ `make coverage-unix` / `make coverage-embedded`
/ `make stability` as the deletions touched) against the live bed and confirms
total/per-line/per-branch coverage equals the Phase-0 baseline and the
marker-based gates select the expected sets. Only Tier-3 deletions that pass this
are kept; the rest are restored.

**Phase 3 boundary — Chris commits** the dedup tiers (one focused commit per tier,
with the parity/redundancy evidence in each message).

---

## Final verification checklist (worker runs; Chris commits)

- [ ] `make coverage` total/per-line/per-branch == Phase-0 full real-bed baseline.
- [ ] `make coverage-unit` == Phase-0 no-VM baseline.
- [ ] `make coverage-unix`, `make coverage-embedded` green; select the expected sets.
- [ ] `make nox` (all Pythons) green — the one heavy gate deferred to here.
- [ ] `make stability` tiers green (or unchanged-from-baseline).
- [ ] `ty` clean; `make docs` clean.
- [ ] Tier<->marker guards (`tests/unit/test_tier_marker_invariants.py`) pass.
- [ ] `repeat` runs the full local suite.
- [ ] Bed left healthy (`make vm-health`); no VM powered/restarted without asking.
