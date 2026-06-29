# Import-Budget Guard (Phases A–C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip the heavy third-party stacks (fastapi/uvicorn, pytest, jinja2) off otto's startup path, then lock the trimmed footprint in with a deterministic, host-independent import-budget guard.

**Architecture:** Phase A defers/cuts the heavy imports out of the modules on the `import otto` / `otto --help` path (the denylist tests drive this, TDD-style). Phase B captures the *post-reduction* footprint as golden snapshots + count caps. Phase C wires a parametrized pytest gate plus `make` regeneration and a `make hyperfine` dev-tool bootstrap. A single measurement harness (`scripts/import_budget.py`) measures each surface in a clean, env-sanitized subprocess so the metric is module-count/identity, never wall-clock.

**Tech Stack:** Python 3.10+, Typer, pytest, `subprocess` + `sys.modules` for measurement, hyperfine (external Rust binary, dev-bootstrapped) for optional wall-clock validation only.

**Spec:** `docs/superpowers/specs/2026-06-29-import-budget-guard-design.md` (Parts A, B, C). Parts D (import-light `__init__`) and E (static-help `--help`) are **out of scope for this plan** — they are sequenced follow-ups with their own plans, to be started only after this guard merges.

## Global Constraints

- **Python floor:** 3.10. No `from __future__ import annotations` in new/edited code (trips otto's Sphinx nitpicky `-W` docs gate). Use real 3.10+ annotations; for deferred imports used only in annotations, quote the annotation and put the import under `if TYPE_CHECKING:`.
- **Determinism:** every measurement runs in a fresh subprocess with a sanitized env (all `OTTO_*` vars stripped) so the footprint reflects otto-core only, independent of the dev's labs/SUT dirs/XDIR.
- **The metric is module count / module identity — never wall-clock.** hyperfine is for manual before/after validation only and is never a gate.
- **Snapshots are otto-owned modules only** (`otto` + `otto.*`), so third-party version bumps don't churn them.
- **Behaviour must be unchanged:** `otto --help` still lists every subcommand with its real help text; `otto monitor`/`otto test`/`otto cov` still function.
- **Commit convention (this repo):** do NOT self-commit. Each task's final step **stages** the changes (`git add`) and surfaces a paste-able commit message for Chris (the prepare-commit-msg hook needs `/dev/tty`; agent commits mis-tag AI assistance). End each commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Gate:** the new test is a pure unit test (no VM/bed). Per-task verification = `uv run pytest <the new test>`; the full gate (`make coverage` + `make typecheck` + `make docs`) runs once at the end (Task 8). Coverage floor is 92.
- **Worktree:** work happens in `.claude/worktrees/import-budget-guard` (base `3687575`). Line numbers below were read from this base; re-confirm with a quick `grep` before editing if they have drifted.

---

## File Structure

**Created:**
- `scripts/import_budget.py` — measurement harness + surface config table + `--update`/`--hyperfine` CLI. Imported by the gate test.
- `tests/unit/import_budget/__init__.py` — empty package marker.
- `tests/unit/import_budget/test_import_budget.py` — the parametrized gate (denylist + cap + snapshot).
- `tests/unit/import_budget/snapshots/<surface>.txt` — one golden file per surface (sorted otto module names), generated post-reduction.
- `src/otto/suite/pytest_plugin.py` — `OttoOptionsPlugin` moved here (the `@pytest.fixture` class), so `register.py` becomes pytest-free.

**Modified (reduction):**
- `src/otto/coverage/renderer/html_renderer.py:28` — defer the `jinja2` import into the method that uses it.
- `src/otto/monitor/__init__.py:25` — drop eager `from .server import MonitorServer`; expose lazily via PEP 562 `__getattr__`.
- `src/otto/cli/monitor.py:27-29,118,137,143` — defer `MonitorServer` (fastapi) into function bodies; quote the one annotation.
- `src/otto/suite/register.py:19,35-59` — remove `import pytest`; move `OttoOptionsPlugin` out to `pytest_plugin.py`.
- `src/otto/suite/__init__.py` — PEP 562 lazy re-exports so importing a submodule doesn't pull `OttoSuite` (pytest) / the plugin.
- `src/otto/cli/test.py:122,131,132` — defer `import pytest`, `OttoPlugin`, `OttoOptionsPlugin` into `run_suite`; keep the pytest-free `_SUITE_REGISTRY` import at module top.

**Modified (tooling):**
- `Makefile` — add `import-snapshot` and `hyperfine` targets; hook `hyperfine` into `dev`; add both to `.PHONY`.

---

## Task 1: Measurement harness + surface config

**Files:**
- Create: `scripts/import_budget.py`
- Create: `tests/unit/import_budget/__init__.py`
- Create: `tests/unit/import_budget/test_import_budget.py`
- Test: `tests/unit/import_budget/test_import_budget.py`

**Interfaces:**
- Produces:
  - `Surface` dataclass: `key: str`, `argv: list[str]`, `deny: tuple[str, ...]`, `cap: int | None`.
  - `SURFACES: list[Surface]` — the ten gated surfaces.
  - `measure(argv: list[str]) -> dict` — returns `{"count": int, "modules": list[str], "otto_modules": list[str]}`, measured in a clean env-sanitized subprocess.
  - `SNAPSHOT_DIR: Path`, `snapshot_path(key: str) -> Path`.
  - `load_harness()` (in the test) — imports the script module by path.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/import_budget/__init__.py` (empty), then `tests/unit/import_budget/test_import_budget.py`:

```python
"""Deterministic import-budget guard: see docs/superpowers/specs/2026-06-29-import-budget-guard-design.md."""
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = _REPO_ROOT / "scripts" / "import_budget.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("import_budget", _HARNESS_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


harness = _load_harness()


def test_measure_returns_module_inventory():
    result = harness.measure(["python"])
    assert result["count"] > 0
    assert "otto" in result["otto_modules"]
    # otto_modules is a strict subset of modules, sorted.
    assert set(result["otto_modules"]) <= set(result["modules"])
    assert result["modules"] == sorted(result["modules"])


def test_surfaces_table_well_formed():
    keys = [s.key for s in harness.SURFACES]
    assert len(keys) == len(set(keys)), "surface keys must be unique"
    assert "import_otto" in keys and "help" in keys
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd .claude/worktrees/import-budget-guard && uv run pytest tests/unit/import_budget/test_import_budget.py -q`
Expected: FAIL — `FileNotFoundError`/`AttributeError` (harness not created yet).

- [ ] **Step 3: Create the harness**

Create `scripts/import_budget.py`:

```python
"""Measure otto's import footprint per CLI surface — deterministic, host-independent.

The metric is *module count / module identity*, never wall-clock. Each surface is
measured in a fresh subprocess with a sanitized env (all OTTO_* vars stripped) so
the footprint reflects otto-core only, regardless of the dev's labs / SUT dirs.

Usage:
    python scripts/import_budget.py            # print a per-surface count table
    python scripts/import_budget.py --update    # regenerate golden snapshots
    python scripts/import_budget.py --hyperfine  # also show wall-clock stats (manual)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "tests" / "unit" / "import_budget" / "snapshots"


@dataclass(frozen=True)
class Surface:
    key: str
    argv: list[str]
    deny: tuple[str, ...]
    cap: int | None = None


# Heavy third-party stacks that must stay off the surfaces that don't own them.
_ALL_HEAVY = ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")

SURFACES: list[Surface] = [
    Surface("import_otto", ["python"], _ALL_HEAVY),
    Surface("help", ["otto", "--help"], _ALL_HEAVY),
    Surface("run", ["otto", "run", "--help"], ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")),
    Surface("host", ["otto", "host", "--help"], ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")),
    Surface("reservation", ["otto", "reservation", "--help"], ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")),
    Surface("docker", ["otto", "docker", "--help"], ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")),
    Surface("schema", ["otto", "schema", "--help"], ("fastapi", "uvicorn", "starlette", "pytest", "jinja2")),
    Surface("monitor", ["otto", "monitor", "--help"], ("pytest", "jinja2")),       # fastapi allowed
    Surface("test", ["otto", "test", "--help"], ("fastapi", "uvicorn", "starlette", "jinja2")),  # pytest allowed
    Surface("cov", ["otto", "cov", "--help"], ("fastapi", "uvicorn", "starlette", "pytest")),    # jinja2 allowed
]

_CHILD = """
import sys, json
sys.argv = {argv!r}
import otto
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
print(json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods}}))
"""


def _sanitized_env() -> dict[str, str]:
    """Env with all OTTO_* vars stripped, so measurement is lab/host independent."""
    return {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}


def measure(argv: list[str]) -> dict:
    code = _CHILD.format(argv=argv)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=_sanitized_env(),
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def snapshot_path(key: str) -> Path:
    return SNAPSHOT_DIR / f"{key}.txt"


def write_snapshot(key: str, otto_modules: list[str]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path(key).write_text("\n".join(otto_modules) + "\n")


def read_snapshot(key: str) -> list[str]:
    return [ln for ln in snapshot_path(key).read_text().splitlines() if ln]


def _run_hyperfine(surface: Surface) -> None:
    if shutil.which("hyperfine") is None:
        print("  (hyperfine not found — run `make hyperfine` to install it)")
        return
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if surface.argv[:1] == ["python"]:
        cmd = f'{venv_py} -c "import otto"'
    else:
        cmd = f'{REPO_ROOT / ".venv" / "bin" / "otto"} {" ".join(surface.argv[1:])}'
    subprocess.run(["hyperfine", "--warmup", "5", "--min-runs", "20", "--shell=none", cmd], check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true", help="regenerate golden snapshots")
    ap.add_argument("--hyperfine", action="store_true", help="also show wall-clock stats (manual)")
    args = ap.parse_args()

    print(f"{'surface':14} {'total':>6} {'otto':>5}  heavy_present")
    for s in SURFACES:
        r = measure(s.argv)
        present = [d for d in s.deny if d in r["modules"]]
        print(f"{s.key:14} {r['count']:6d} {len(r['otto_modules']):5d}  {present}")
        if args.update:
            write_snapshot(s.key, r["otto_modules"])
            print(f"  -> wrote {snapshot_path(s.key).relative_to(REPO_ROOT)} ({len(r['otto_modules'])} modules)")
        if args.hyperfine:
            _run_hyperfine(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Stage + surface commit message**

```bash
git add scripts/import_budget.py tests/unit/import_budget/__init__.py tests/unit/import_budget/test_import_budget.py
```
Commit message for Chris:
```
feat(perf): add import-budget measurement harness

scripts/import_budget.py measures otto's per-surface import footprint in a
clean, env-sanitized subprocess (module count + sorted otto-module list).
Foundation for the deterministic import-budget guard.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 2: Reduce jinja2 (coverage renderer)

**Files:**
- Modify: `src/otto/coverage/renderer/html_renderer.py:28,73-75`
- Test: `tests/unit/import_budget/test_import_budget.py`

**Interfaces:**
- Consumes: `harness.measure`, `harness.SURFACES` (Task 1).
- Produces: jinja2 absent from the `import_otto`, `help`, and all non-`cov` surfaces.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/import_budget/test_import_budget.py`:

```python
def test_jinja2_off_startup_path():
    for key in ("import_otto", "help", "run", "monitor", "test"):
        surface = next(s for s in harness.SURFACES if s.key == key)
        result = harness.measure(surface.argv)
        assert "jinja2" not in result["modules"], f"jinja2 leaked into `{key}`"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_jinja2_off_startup_path -q`
Expected: FAIL — jinja2 present (pulled via `cli.cov` → coverage → renderer → html_renderer module-top import).

- [ ] **Step 3: Defer the jinja2 import**

In `src/otto/coverage/renderer/html_renderer.py`, delete the module-top import at line 28:

```python
from jinja2 import Environment, FileSystemLoader, select_autoescape
```

Find the method that builds the environment (around line 73, `self.env = Environment(...)`) and add the import as its first statement:

```python
        # Deferred so importing the renderer module (and thus `otto.coverage`,
        # pulled onto the CLI startup path via cli.cov) does not load jinja2.
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html"]),
        )
```

(If `Environment`/`FileSystemLoader`/`select_autoescape` are referenced in any other method or in a type annotation, add the same local import there, or quote the annotation with a `if TYPE_CHECKING:` import. Grep: `grep -n "Environment\|FileSystemLoader\|select_autoescape" src/otto/coverage/renderer/html_renderer.py`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_jinja2_off_startup_path -q`
Expected: PASS.

- [ ] **Step 5: Verify cov still renders (behaviour smoke)**

Run: `uv run pytest tests/unit/cov -q`
Expected: PASS (HTML rendering still works — jinja2 loads inside the renderer).

- [ ] **Step 6: Stage + surface commit message**

```bash
git add src/otto/coverage/renderer/html_renderer.py tests/unit/import_budget/test_import_budget.py
```
```
perf(coverage): defer jinja2 import into the HTML renderer

jinja2 was imported at module top of html_renderer, riding the CLI startup
path via cli.cov. Move it into the env-building method so `otto --help` and
non-cov subcommands no longer load it. Guarded by an import-budget test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 3: Reduce fastapi/uvicorn (monitor)

**Files:**
- Modify: `src/otto/monitor/__init__.py:21-34`
- Modify: `src/otto/cli/monitor.py:19,27-29,118,137,143`
- Test: `tests/unit/import_budget/test_import_budget.py`

**Interfaces:**
- Consumes: `harness.measure`, `harness.SURFACES`.
- Produces: fastapi/uvicorn/starlette absent from `import_otto`, `help`, and all subcommand surfaces except `monitor`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/import_budget/test_import_budget.py`:

```python
def test_fastapi_off_startup_path():
    for key in ("import_otto", "help", "run", "cov", "test"):
        surface = next(s for s in harness.SURFACES if s.key == key)
        result = harness.measure(surface.argv)
        for mod in ("fastapi", "uvicorn", "starlette"):
            assert mod not in result["modules"], f"{mod} leaked into `{key}`"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_fastapi_off_startup_path -q`
Expected: FAIL — fastapi present (via `otto.monitor/__init__` eager `.server` import, pulled by `otto.models` → `monitor.collector`, and by `cli.monitor`).

- [ ] **Step 3: Make `monitor/__init__` lazy for the server**

Edit `src/otto/monitor/__init__.py`. Replace the eager server import (line 25) with a PEP 562 lazy export. After the remaining eager imports (collector/events/factory/parsers), the file becomes:

```python
from typing import TYPE_CHECKING

from .collector import MetricCollector
from .events import MonitorEvent
from .factory import build_monitor_collector
from .parsers import DEFAULT_PARSERS, MetricParser

if TYPE_CHECKING:
    from .server import MonitorServer

__all__ = [
    "DEFAULT_PARSERS",
    "MetricCollector",
    "MetricParser",
    "MonitorEvent",
    "MonitorServer",
    "build_monitor_collector",
]


def __getattr__(name: str) -> object:
    """Lazily resolve MonitorServer so importing otto.monitor (e.g. via
    otto.models -> monitor.collector) does not pull in fastapi/uvicorn."""
    if name == "MonitorServer":
        from .server import MonitorServer

        return MonitorServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

(Keep the module docstring at the top unchanged.)

- [ ] **Step 4: Defer `MonitorServer` in `cli/monitor.py`**

In `src/otto/cli/monitor.py`:

1. Change the typing import (line 19) and add a `TYPE_CHECKING` block; delete the module-top server import (line 29):

```python
from typing import TYPE_CHECKING, Annotated

import typer

# TODO: Create a SqlPath class ...
from ..configmodule import all_hosts
from ..context import get_context
from ..logger import get_otto_logger, management
from ..monitor.collector import MetricCollector
from ..monitor.factory import build_monitor_collector

if TYPE_CHECKING:
    from ..monitor.server import MonitorServer
```

2. In `monitor()`, add a local import just before it instantiates the server (before line 114/118):

```python
    from ..monitor.server import MonitorServer

    collector = build_monitor_collector(hosts=selected, db_path=db)
    asyncio.run(
        _run_monitor(
            collector=collector,
            server=MonitorServer(collector),
            interval=timedelta(seconds=interval),
        )
    )
```

3. In `_serve_historical()`, add the local import:

```python
async def _serve_historical(path: Path) -> None:
    """Load historical data and serve the dashboard (no live collection)."""
    from ..monitor.server import MonitorServer

    collector = await _load_historical(path)
    server = MonitorServer(collector)
    await server.serve()
```

4. Quote the annotation in `_run_monitor` (line 143) so it is not evaluated at module load:

```python
async def _run_monitor(
    collector: MetricCollector,
    server: "MonitorServer",
    interval: timedelta,
    duration: timedelta | None = None,
) -> None:
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_fastapi_off_startup_path -q`
Expected: PASS.

- [ ] **Step 6: Verify monitor still works (behaviour smoke)**

Run: `uv run pytest tests/unit/monitor -q`
Then confirm the lazy export resolves:
Run: `uv run python -c "from otto.monitor import MonitorServer; print(MonitorServer.__name__)"`
Expected: tests PASS; prints `MonitorServer`.

- [ ] **Step 7: Stage + surface commit message**

```bash
git add src/otto/monitor/__init__.py src/otto/cli/monitor.py tests/unit/import_budget/test_import_budget.py
```
```
perf(monitor): keep fastapi off the startup path

monitor/__init__ eagerly imported .server (fastapi/uvicorn), so importing any
otto.monitor.* submodule (e.g. via otto.models -> monitor.collector) pulled the
web stack. Expose MonitorServer via PEP 562 __getattr__ and defer the server
import in cli/monitor into the command bodies. Guarded by an import-budget test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 4: Reduce pytest (suite + cli/test)

**Files:**
- Create: `src/otto/suite/pytest_plugin.py`
- Modify: `src/otto/suite/register.py:19,35-59`
- Modify: `src/otto/suite/__init__.py`
- Modify: `src/otto/cli/test.py:117,122,131,132` + `run_suite` body
- Test: `tests/unit/import_budget/test_import_budget.py`

**Interfaces:**
- Consumes: `harness.measure`, `harness.SURFACES`.
- Produces: pytest/pytest_asyncio absent from `import_otto`, `help`, and all subcommand surfaces except `test`. `OttoOptionsPlugin` importable from both `otto.suite.pytest_plugin` and (lazily) `otto.suite`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/import_budget/test_import_budget.py`:

```python
def test_pytest_off_startup_path():
    for key in ("import_otto", "help", "run", "host", "monitor", "cov"):
        surface = next(s for s in harness.SURFACES if s.key == key)
        result = harness.measure(surface.argv)
        assert "pytest" not in result["modules"], f"pytest leaked into `{key}`"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_pytest_off_startup_path -q`
Expected: FAIL — pytest present on every surface (via `cli.test` module-top imports + `suite/__init__` eager re-export of `OttoSuite` + `register.py` `import pytest`).

- [ ] **Step 3: Move `OttoOptionsPlugin` out of `register.py`**

Create `src/otto/suite/pytest_plugin.py`:

```python
"""Pytest plugin objects for otto suites. Imported only when running a suite —
kept out of register.py so importing the registry never pulls in pytest."""
from typing import Any

import pytest


class OttoOptionsPlugin:
    """Pytest plugin that provides the suite Options instance as a fixture.

    Tests request the ``suite_options`` fixture as a parameter::

        async def test_something(self, suite_options) -> None:
            assert suite_options.device_type == "router"
    """

    __name__ = "otto-options"

    def __init__(self, options: Any | None) -> None:
        self.options = options

    @pytest.fixture(scope="session")
    def suite_options(self) -> Any:
        """Return the Options dataclass instance populated from CLI arguments."""
        return self.options

    @pytest.fixture
    def ctx(self) -> Any:
        """Return the active OttoContext for this invocation."""
        from ..context import get_context

        return get_context()
```

In `src/otto/suite/register.py`: delete `import pytest` (line 19) and delete the entire `OttoOptionsPlugin` class (lines ~35-59). `register.py` now imports no pytest. (Leave `_SUITE_REGISTRY`, `register_suite`, `_options_params` intact.)

- [ ] **Step 4: Make `suite/__init__` lazy**

Replace `src/otto/suite/__init__.py` with PEP 562 lazy exports so importing any `otto.suite.*` submodule does not eagerly pull `OttoSuite` (pytest) or the plugin:

```python
"""Public API for otto test suites: ``OttoSuite``, ``register_suite``, ``OttoOptionsPlugin``."""
from typing import TYPE_CHECKING

from .register import register_suite as register_suite

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin
    from .suite import OttoSuite

__all__ = ["OttoOptionsPlugin", "OttoSuite", "register_suite"]


def __getattr__(name: str) -> object:
    if name == "OttoSuite":
        from .suite import OttoSuite

        return OttoSuite
    if name == "OttoOptionsPlugin":
        from .pytest_plugin import OttoOptionsPlugin

        return OttoOptionsPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

(`register_suite` lives in the now-pytest-free `register.py`, so re-exporting it eagerly is safe and keeps the decorator import path — `from otto.suite import register_suite`, used by user test files — working without pytest.)

- [ ] **Step 5: Defer pytest imports in `cli/test.py`**

In `src/otto/cli/test.py`:

1. Delete `import pytest` (line 122), `from ..suite.plugin import OttoPlugin` (line 131), and the `OttoOptionsPlugin` part of line 132. Keep `_SUITE_REGISTRY` (now pytest-free):

```python
from ..suite.register import _SUITE_REGISTRY
```

2. In `run_suite`, add the deferred imports as the first statements of the function body:

```python
def run_suite(...):
    """Execute a registered suite via pytest.main()."""
    import pytest

    from ..suite.plugin import OttoPlugin
    from ..suite.pytest_plugin import OttoOptionsPlugin
    ...
```

(Confirm `pytest`, `OttoPlugin`, `OttoOptionsPlugin` are referenced only inside function bodies after this change: `grep -n "pytest\.\|OttoPlugin\|OttoOptionsPlugin" src/otto/cli/test.py`. The `_SUITE_REGISTRY` use at module scope, ~line 628, stays.)

- [ ] **Step 6: Run to verify it passes**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py::test_pytest_off_startup_path -q`
Expected: PASS.

- [ ] **Step 7: Verify suites still register and run (behaviour smoke)**

Run: `uv run pytest tests/unit/suite tests/unit/cli -q`
Then confirm lazy access + a real suite invocation path:
Run: `uv run python -c "from otto.suite import OttoSuite, OttoOptionsPlugin, register_suite; print('ok')"`
Run: `uv run otto test --help`  (must still list registered suites and exit 0)
Expected: tests PASS; prints `ok`; help renders.

- [ ] **Step 8: Stage + surface commit message**

```bash
git add src/otto/suite/pytest_plugin.py src/otto/suite/register.py src/otto/suite/__init__.py src/otto/cli/test.py tests/unit/import_budget/test_import_budget.py
```
```
perf(suite): keep pytest off the startup path

pytest rode every CLI surface because cli/test imported it (and the pytest-based
OttoPlugin/OttoOptionsPlugin) at module top, and suite/__init__ eagerly
re-exported OttoSuite (a pytest base class) and register.py imported pytest for
its fixture plugin. Split OttoOptionsPlugin into suite/pytest_plugin, make
suite/__init__ lazy (PEP 562), and defer the pytest imports in cli/test into
run_suite. register.py is now pytest-free. Guarded by an import-budget test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 5: Capture baseline + full guard (denylist + cap + snapshot)

**Files:**
- Modify: `scripts/import_budget.py` (set `cap` values on `SURFACES`)
- Create: `tests/unit/import_budget/snapshots/*.txt` (generated)
- Modify: `tests/unit/import_budget/test_import_budget.py` (parametrized full guard)
- Test: `tests/unit/import_budget/test_import_budget.py`

**Interfaces:**
- Consumes: `harness.measure`, `harness.SURFACES`, `harness.read_snapshot`, `harness.write_snapshot` (Task 1).
- Produces: the committed gate — per-surface denylist + count cap + golden otto-module snapshot.

- [ ] **Step 1: Generate snapshots from the reduced state**

Run: `uv run python scripts/import_budget.py --update`
This prints the per-surface table and writes `tests/unit/import_budget/snapshots/<key>.txt`. Note the printed `total` counts — you will set caps from them in Step 2. Confirm every `heavy_present` column is `[]`.

- [ ] **Step 2: Set count caps with headroom**

In `scripts/import_budget.py`, set `cap=<measured total> + 15` on each `Surface` (HEADROOM = 15). Example (use the actual measured numbers from Step 1, do not copy these):

```python
    Surface("import_otto", ["python"], _ALL_HEAVY, cap=<measured>+15),
    ...
```

- [ ] **Step 3: Write the full parametrized guard test**

Replace the per-heavy `test_*_off_startup_path` functions in `tests/unit/import_budget/test_import_budget.py` with the consolidated gate (keep `_load_harness`/`harness` and the Task-1 tests):

```python
@pytest.mark.parametrize("surface", harness.SURFACES, ids=lambda s: s.key)
def test_import_budget(surface):
    result = harness.measure(surface.argv)

    # 1. Denylist: heavy third-party stacks must be absent.
    leaked = [d for d in surface.deny if d in result["modules"]]
    assert not leaked, f"`{surface.key}`: heavy modules leaked onto the path: {leaked}"

    # 2. Count cap: total modules must not exceed the post-reduction baseline + headroom.
    assert surface.cap is not None, f"`{surface.key}` has no cap set"
    assert result["count"] <= surface.cap, (
        f"`{surface.key}`: {result['count']} modules > cap {surface.cap}. "
        f"If intentional, re-run `make import-snapshot` and raise the cap."
    )

    # 3. Golden snapshot: the set of otto-owned modules must match exactly.
    expected = harness.read_snapshot(surface.key)
    assert result["otto_modules"] == expected, (
        f"`{surface.key}`: otto module set changed. "
        f"If intentional, re-run `make import-snapshot` and review the diff.\n"
        f"  added:   {sorted(set(result['otto_modules']) - set(expected))}\n"
        f"  removed: {sorted(set(expected) - set(result['otto_modules']))}"
    )
```

- [ ] **Step 4: Run the full guard**

Run: `uv run pytest tests/unit/import_budget/test_import_budget.py -q`
Expected: PASS (all surfaces — denylist clean, under cap, snapshot matches).

- [ ] **Step 5: Stage + surface commit message**

```bash
git add scripts/import_budget.py tests/unit/import_budget/
```
```
feat(perf): lock in the import budget with a deterministic guard

Capture the post-reduction otto-module snapshot per CLI surface and enforce a
three-layer gate: third-party denylist, per-surface module-count cap
(baseline + 15), and an otto-only golden snapshot. Host-independent; never
wall-clock.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 6: `make import-snapshot` regeneration target

**Files:**
- Modify: `Makefile:3` (`.PHONY`), add `import-snapshot` target near `schema`.

**Interfaces:**
- Consumes: `scripts/import_budget.py --update`.
- Produces: `make import-snapshot` — one-command regeneration of snapshots + caps reference.

- [ ] **Step 1: Add the target**

In `Makefile`, add `import-snapshot` to the `.PHONY` list (line 3), and add the recipe (near the `schema` target):

```makefile
import-snapshot: ## Regenerate import-budget golden snapshots + print per-surface counts (run after an intentional import change, then review the diff and update caps in scripts/import_budget.py)
	uv run python scripts/import_budget.py --update
```

- [ ] **Step 2: Verify it runs and is idempotent**

Run: `make import-snapshot`
Then: `git status --porcelain tests/unit/import_budget/snapshots/`
Expected: target prints the table; **no diff** (snapshots already match the committed state).

- [ ] **Step 3: Stage + surface commit message**

```bash
git add Makefile
```
```
build(perf): add `make import-snapshot` to regenerate import-budget snapshots

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 7: `make hyperfine` bootstrap + `--hyperfine` flag

**Files:**
- Modify: `Makefile:3` (`.PHONY`), add `hyperfine` target; add it to `dev`.
- (No code change to `scripts/import_budget.py` — `--hyperfine` and `_run_hyperfine` were added in Task 1.)

**Interfaces:**
- Consumes: GitHub hyperfine releases.
- Produces: `make hyperfine` installs a pinned hyperfine into `.venv/bin`; `make dev` includes it.

- [ ] **Step 1: Add the pinned bootstrap target**

In `Makefile`, add `hyperfine` to `.PHONY` (line 3) and add near `dev`:

```makefile
HYPERFINE_VERSION := 1.20.0

hyperfine: ## Install the pinned hyperfine benchmark binary into .venv/bin (dev tool for manual wall-clock validation; not a project dependency)
	@if [ -x "$(VENV_BIN)/hyperfine" ] && "$(VENV_BIN)/hyperfine" --version | grep -q "$(HYPERFINE_VERSION)"; then \
		echo "hyperfine $(HYPERFINE_VERSION) already installed"; \
	else \
		bash scripts/install_hyperfine.sh "$(HYPERFINE_VERSION)" "$(VENV_BIN)"; \
	fi
```

Update the `dev` recipe to call it:

```makefile
dev:
	uv sync
	git config core.hooksPath .githooks
	$(MAKE) hyperfine
	@echo "Dev environment ready"
```

- [ ] **Step 2: Create the installer script**

Create `scripts/install_hyperfine.sh` (detects OS/arch, downloads the right asset, sha256-verifies, installs):

```bash
#!/usr/bin/env bash
# Install a pinned hyperfine binary into a target bin dir. Usage:
#   scripts/install_hyperfine.sh <version> <bin_dir>
set -euo pipefail
VERSION="${1:?version required}"
BIN_DIR="${2:?bin dir required}"

os="$(uname -s)"; arch="$(uname -m)"
case "$os-$arch" in
  Linux-x86_64)  asset="x86_64-unknown-linux-musl" ;;
  Linux-aarch64) asset="aarch64-unknown-linux-gnu" ;;
  Darwin-x86_64) asset="x86_64-apple-darwin" ;;
  Darwin-arm64)  asset="aarch64-apple-darwin" ;;
  *) echo "unsupported platform: $os-$arch" >&2; exit 1 ;;
esac

# Pinned sha256 per asset for hyperfine v$VERSION. Fill in for the pinned version
# (download once, `sha256sum` the tarball, paste here). Keyed by asset triple.
declare -A SHA256=(
  ["x86_64-unknown-linux-musl"]="<sha256>"
  ["aarch64-unknown-linux-gnu"]="<sha256>"
  ["x86_64-apple-darwin"]="<sha256>"
  ["aarch64-apple-darwin"]="<sha256>"
)

tarball="hyperfine-v${VERSION}-${asset}.tar.gz"
url="https://github.com/sharkdp/hyperfine/releases/download/v${VERSION}/${tarball}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -fsSL --max-time 120 -o "$tmp/$tarball" "$url"
echo "${SHA256[$asset]}  $tmp/$tarball" | sha256sum -c -
tar xzf "$tmp/$tarball" -C "$tmp"
mkdir -p "$BIN_DIR"
install -m 0755 "$tmp"/hyperfine-*/hyperfine "$BIN_DIR/hyperfine"
"$BIN_DIR/hyperfine" --version
```

> **Pin the sha256s:** for the pinned version, download each of the four assets once, run `sha256sum`, and paste the digests into the `SHA256` map. Do not ship `<sha256>` placeholders — the `sha256sum -c` step must be real. (At minimum pin the `aarch64-unknown-linux-gnu` digest used on the dev VM; the others can be pinned as platforms are encountered, but leaving a placeholder makes that asset fail closed, which is acceptable.)

- [ ] **Step 3: Verify the bootstrap**

Run: `make hyperfine`
Then: `make hyperfine` (second run)
Expected: first run downloads + verifies + prints `hyperfine 1.20.0`; second run prints "already installed".

- [ ] **Step 4: Verify the `--hyperfine` flag**

Run: `uv run python scripts/import_budget.py --hyperfine` (let it run a couple of surfaces, Ctrl-C is fine)
Expected: per-surface count table interleaved with hyperfine mean±σ stats; no error if hyperfine present.

- [ ] **Step 5: Stage + surface commit message**

```bash
git add Makefile scripts/install_hyperfine.sh
```
```
build(perf): bootstrap hyperfine as a dev tool (`make hyperfine`)

hyperfine has no real PyPI distribution (the PyPI `hyperfine` is an unrelated
physics lib), so it can't live in pyproject. Pin v1.20.0 and install the
OS/arch-correct, sha256-verified release binary into .venv/bin; hook into
`make dev`. Used only for manual before/after wall-clock validation via
`import_budget.py --hyperfine`; never a gate.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 8: Registration smoke, full gate, and time validation

**Files:**
- Modify: `tests/unit/import_budget/test_import_budget.py` (registration smokes)
- Test: full gate.

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add registration-ordering smokes**

Append to `tests/unit/import_budget/test_import_budget.py` — the reductions (PEP 562 on monitor/suite, deferred imports) must not break registry population or the public API on either entry path:

```python
def test_monitor_server_still_resolves():
    # PEP 562 lazy export must still work for library users.
    result = harness.measure(["python"])
    assert "fastapi" not in result["modules"]
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-c", "from otto.monitor import MonitorServer; print(MonitorServer.__name__)"],
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "MonitorServer"


def test_suite_public_api_still_resolves():
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-c",
         "from otto.suite import OttoSuite, OttoOptionsPlugin, register_suite; print('ok')"],
        capture_output=True, text=True, check=True, env=harness._sanitized_env(),
    )
    assert out.stdout.strip() == "ok"
```

Run: `uv run pytest tests/unit/import_budget/ -q`
Expected: PASS.

- [ ] **Step 2: Run the full gate**

Run, in order:
```bash
make coverage      # full pinned suite + 92 floor (the new test runs here)
make typecheck     # ty clean (quoted annotations + TYPE_CHECKING blocks must type-check)
make docs          # 0 warnings (no new from-future-annotations; lazy __getattr__ modules build)
```
Expected: all green. If `make coverage` shows the floor dropped, add focused tests for any newly-uncovered deferred-import branches (e.g., the `__getattr__` `AttributeError` paths).

- [ ] **Step 3: Time-based validation (record before/after)**

Ensure hyperfine is installed (`make hyperfine`), then run the spec's exact command:
```bash
hyperfine --warmup 5 --min-runs 30 --shell=none \
  -n python-baseline ".venv/bin/python -c pass" \
  -n import-otto      ".venv/bin/python -c 'import otto'" \
  -n otto--help       ".venv/bin/otto --help"
```
Record the AFTER numbers in the spec's "Time-based validation" section beside the BEFORE table (`import otto` 291.9 ms, `otto --help` 334.2 ms). Expected: a wall-clock drop consistent with the module-count reduction (fastapi+pytest+jinja2 ≈ 122 ms removed). The deterministic guard is the gate; this is the sanity check.

- [ ] **Step 4: Stage + surface commit message**

```bash
git add tests/unit/import_budget/test_import_budget.py docs/superpowers/specs/2026-06-29-import-budget-guard-design.md
```
```
test(perf): registration smokes + record after-reduction wall-clock

Add library-API smokes (lazy MonitorServer + suite public API resolve) and
record the post-reduction hyperfine baseline in the spec.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Self-Review

**1. Spec coverage:**
- Part A (reduce fastapi/pytest/jinja2) → Tasks 2, 3, 4. ✓
- Part B (clean-subprocess measurement; otto-only golden snapshot; per-surface count cap; third-party denylist; surfaces = library + top-level + per-subcommand) → Tasks 1 (harness, env-sanitized, 10 surfaces) + 5 (snapshot/cap/denylist gate). ✓
- Part C (measurement harness; `--hyperfine` opt-in; config table; snapshots committed; gate test in `make coverage`; `make import-snapshot`; `make hyperfine` bootstrap, pinned, sha256, into `.venv/bin`, hooked into `make dev`) → Tasks 1, 6, 7. ✓
- Verification (denylist ad-hoc; `make coverage`/`typecheck`/`docs`; behaviour smokes; registration-ordering smoke; time validation) → Tasks 2–4 (behaviour smokes), 8 (full gate, registration smokes, hyperfine after-record). ✓
- Reduction success criteria table (monitor allows fastapi; test allows pytest; cov allows jinja2) → encoded in `SURFACES.deny` (Task 1) and asserted (Task 5). ✓
- Parts D and E → explicitly out of scope (header). ✓

**2. Placeholder scan:** The only intentional fill-ins are the measured `cap` integers (Task 5 Step 2 — cannot be known until reduction is measured) and the `sha256` digests (Task 7 — pinned at implementation time from the real assets). Both are flagged with explicit instructions, not silent TODOs. Reduction edits show complete before/after code. No "add error handling"/"similar to Task N" placeholders.

**3. Type consistency:** `measure()` returns `{"count","modules","otto_modules"}` — used consistently in Tasks 1–8. `Surface` fields (`key`, `argv`, `deny`, `cap`) consistent. `harness._sanitized_env()` reused in Task 8 smokes. `read_snapshot`/`write_snapshot`/`snapshot_path` defined in Task 1, used in Tasks 5–6. `OttoOptionsPlugin` moved to `suite/pytest_plugin.py` and imported from there in `cli/test.py` (Task 4) and re-exported lazily from `suite/__init__` — consistent.
