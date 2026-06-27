# Logger Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the `OttoLogger` `logging.Logger` subclass so `'otto'` is a plain standard logger (killing the duplicate-import hazard at its root and making otto library-friendly), move logging *mechanics* to `otto.logger.management`, and move the per-run output dir onto `OttoContext.output_dir`.

**Architecture:** Three-way split — `otto.logger` = library emit surface (`get_otto_logger()` returns a plain `logging.Logger`, + a `NullHandler`); `otto.logger.management` = CLI-called, context-free setup/output-dir/rotation; `OttoContext.output_dir` = where this run's artifacts live. CLI command callbacks do `get_context().output_dir = management.create_output_dir(...)`.

**Tech Stack:** Python 3.10+, stdlib `logging`, `rich`, `pytest` + `pytest-asyncio` (strict), `pytest-xdist`.

**Spec:** [docs/superpowers/specs/2026-06-27-logger-standardization-design.md](../specs/2026-06-27-logger-standardization-design.md)

## Global Constraints

- **Python 3.10+.** Real annotations only — **never** `from __future__ import annotations`. Quoted forward-refs are fine for `TYPE_CHECKING`-only names (matches existing `lab: "Lab"` style in `OttoContext`).
- **No self-commit.** Chris commits in otto-sh (the `prepare-commit-msg` hook needs a TTY). Every task's final step **stages only** (`git add`) and records the intended commit message. Do **not** run `git commit`.
- **`get_otto_logger()` returns a plain `logging.Logger`** (no subclass, no `setLoggerClass`). `otto.logger.management` is **context-free** (must not import `otto.context`). `OttoContext` owns `output_dir`.
- **No behavior change for CLI users** — same Rich console + file log, same per-run dir layout, same 5s rotation budget (`LOG_ROTATE_BUDGET_SECONDS = 5.0`).
- **Tests** are unit-tier, deterministic, `tmp_path`/`tmpdir`, no VMs, no heavy/looped xdist. Run the scoped `pytest` shown per task; **full gate once at the end** (Task 4): `make coverage`, `make typecheck`, `make docs`, plus smokes `otto --help` and `python -c "from otto import all_hosts, app"`. `make nox` / live `make coverage` = Chris.
- **Out of scope (separate workstreams):** lazy/import-light `otto/__init__.py` ([todo/import-light-otto-init.md](../../../todo/import-light-otto-init.md)). This plan leaves the eager `cli`/`configmodule`/`context` imports in `otto/__init__.py` untouched and only removes the obsolete eager `get_otto_logger()` *call*.

---

### Task 1: Add `output_dir` to `OttoContext`

**Files:**
- Modify: `src/otto/context.py` (the `OttoContext` dataclass, ~line 89-92)
- Test: `tests/unit/test_context.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `OttoContext.output_dir: Path | None` (default `None`) — the per-run artifact dir. Set by the CLI in Task 3; read by consumers in Task 3.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_context.py`:

```python
from pathlib import Path

from otto.context import OttoContext


def test_otto_context_output_dir_defaults_none_and_is_settable():
    # OttoContext requires a lab; use a minimal stand-in via the dataclass.
    ctx = OttoContext(lab=None)  # type: ignore[arg-type]
    assert ctx.output_dir is None
    ctx.output_dir = Path('/tmp/otto-run-xyz')
    assert ctx.output_dir == Path('/tmp/otto-run-xyz')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_context.py::test_otto_context_output_dir_defaults_none_and_is_settable -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword 'output_dir'` is not raised (it's a positional/attr error) — specifically `AttributeError`/dataclass has no `output_dir`, or the assert on `ctx.output_dir` fails with `AttributeError`.

- [ ] **Step 3: Add the field**

In `src/otto/context.py`, add the field to the `OttoContext` dataclass (after `log_command_output`, before `scope` so the `field(default_factory=...)` stays last):

```python
    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    output_dir: "Path | None" = None
    scope: HostScope = field(default_factory=HostScope)
```

(`Path` is already imported under `TYPE_CHECKING` in `context.py` — the quoted annotation needs no new import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_context.py::test_otto_context_output_dir_defaults_none_and_is_settable -v`
Expected: PASS

- [ ] **Step 5: Stage (do not commit)**

```bash
git add src/otto/context.py tests/unit/test_context.py
# Intended commit message (Chris commits):
# feat(context): add OttoContext.output_dir for the per-run artifact dir
```

---

### Task 2: Create `otto.logger.management` + library `NullHandler`

**Files:**
- Create: `src/otto/logger/management.py`
- Modify: `src/otto/logger/__init__.py` (add NullHandler + export `management`)
- Test: `tests/unit/logger/test_management.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `management.init_cli_logging(xdir: Path, log_level: str, keep_days: float, rich_log_file: bool = False, verbose: bool = False) -> None`
  - `management.create_output_dir(command: str, subcommand: str | None = None) -> Path`
  - `management.remove_old_logs(seconds: float, *, time_budget: float = LOG_ROTATE_BUDGET_SECONDS) -> None`
  - `management.reset() -> None`
  - `management.LOG_ROTATE_BUDGET_SECONDS: float` (= 5.0)
  - `otto.logger` attaches one `logging.NullHandler` to `'otto'` at import.

This module is **parallel** to the still-present `OttoLogger` for now — callers are migrated in Task 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/logger/test_management.py`:

```python
import logging

import pytest

from otto.logger import management


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def test_init_cli_logging_wires_console_handler_on_plain_logger(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    otto = logging.getLogger('otto')
    assert otto.level == logging.INFO
    # A real (non-Null) handler is attached, on a PLAIN logging.Logger.
    assert type(otto) is logging.Logger
    assert any(not isinstance(h, logging.NullHandler) for h in otto.handlers)


def test_create_output_dir_returns_and_creates_dir(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    out = management.create_output_dir('test', 'mysuite')
    assert out.exists() and out.is_dir()
    assert out.parent == tmp_path / 'test'
    assert (out / 'otto.log').exists()  # file handler created the log file


def test_remove_old_logs_respects_time_budget(tmp_path, monkeypatch):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    cmd_dir = tmp_path / 'test'
    cmd_dir.mkdir(parents=True, exist_ok=True)
    import os
    olds = []
    for i in range(6):
        d = cmd_dir / f'20200101_0000{i:02d}_000'
        d.mkdir()
        past = 10_000.0
        os.utime(d, (os.stat(d).st_atime - past, os.stat(d).st_mtime - past))
        olds.append(d)
    ticks = iter([float(n) for n in range(0, 1000)])
    monkeypatch.setattr(management.time, 'monotonic', lambda: next(ticks))
    management.remove_old_logs(seconds=60, time_budget=2.5)
    assert [d for d in olds if d.exists()], 'budget should stop before removing all'


def test_library_import_attaches_nullhandler():
    import otto.logger  # noqa: F401
    otto = logging.getLogger('otto')
    assert any(isinstance(h, logging.NullHandler) for h in otto.handlers)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/logger/test_management.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.logger.management'`.

- [ ] **Step 3: Create `src/otto/logger/management.py`**

```python
"""CLI-side logging management for otto.

otto-the-library only *emits* log records (via ``logging.getLogger('otto'…)``)
and never configures handlers — see ``otto.logger``. This module is the
application/CLI side: it configures the ``'otto'`` logger's handlers + formatters,
creates each invocation's output directory, and prunes old log directories.

**Context-free** by design (it does not import ``otto.context``):
``create_output_dir`` *creates and returns* the per-run directory; the CLI
records that path on ``OttoContext.output_dir``. These functions are called only
by ``otto/cli/*.py`` (and tests).

Scope guardrail: keep this strictly logging config + the per-run dir's
creation/rotation. It is not a catch-all.
"""

import atexit
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import FileHandler, LogRecord, getLogger
from logging.handlers import QueueHandler, QueueListener
from os import listdir
from pathlib import Path
from queue import Queue
from shutil import rmtree

from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from ..console import CONSOLE
from .formatters import RichFormatter, format_log_time

# Matches the timestamp directory names ``create_output_dir`` writes:
# ``YYYYMMDD_HHMMSS_mmm`` optionally followed by ``_<subcommand>``. Fail-safe so
# a misconfigured ``xdir`` can't lead to rmtree'ing unrelated subtrees.
_LOG_DIR_NAME_RE = re.compile(r'^\d{8}_\d{6}_\d{3}(_.+)?$')

# Max wall-clock seconds ``remove_old_logs`` may spend scanning per call — a
# safety valve against stat storms on large/slow (e.g. NFS) trees; a backlog
# drains across subsequent runs.
LOG_ROTATE_BUDGET_SECONDS = 5.0


@dataclass
class _LogConfig:
    xdir: Path | None = None
    keep_seconds: float | None = None
    rich_log_file: bool = False
    last_output_dir: Path | None = None
    listener: QueueListener | None = None
    atexit_registered: bool = False


_state = _LogConfig()


def reset() -> None:
    """Reset module state and detach otto's handlers (test helper)."""
    otto = getLogger('otto')
    for h in list(otto.handlers):
        otto.removeHandler(h)
    if _state.listener is not None:
        _state.listener.stop()
    _state.xdir = None
    _state.keep_seconds = None
    _state.rich_log_file = False
    _state.last_output_dir = None
    _state.listener = None
    _state.atexit_registered = False


def init_cli_logging(
    xdir: Path,
    log_level: str,
    keep_days: float,
    rich_log_file: bool = False,
    verbose: bool = False,
) -> None:
    """Configure the ``'otto'`` logger for a CLI invocation (was init_otto_logger)."""
    logger = getLogger('otto')
    logger.setLevel(log_level)
    is_debug = log_level == 'DEBUG'

    stdout_handler = RichHandler(
        level=log_level,
        console=CONSOLE,
        show_time=verbose,
        tracebacks_max_frames=20,
        tracebacks_show_locals=True,
        markup=True,
        highlighter=NullHighlighter(),
        show_path=is_debug,
        enable_link_path=False,
        log_time_format=format_log_time,
        omit_repeated_times=False,
    )
    logger.addHandler(stdout_handler)

    _state.xdir = Path(xdir)
    _state.rich_log_file = rich_log_file
    _state.keep_seconds = keep_days * 24 * 60 * 60


def set_keep_seconds(value: float | None) -> None:
    """Set/clear the retention period (used by the CLI and by tests)."""
    _state.keep_seconds = value


def _command_to_dir_name(command: str) -> str:
    return command.replace('-', '_')


def create_output_dir(command: str, subcommand: str | None = None) -> Path:
    """Create this invocation's output dir, wire the file handler, prune old
    logs, and return the dir. The caller records it on ``OttoContext.output_dir``.
    """
    if _state.xdir is None:
        raise RuntimeError(
            "init_cli_logging() must run before create_output_dir() (xdir unset)"
        )

    # Name the dir down to the millisecond (%f is microseconds; drop last 3).
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    command = _command_to_dir_name(command)
    sub = f'_{_command_to_dir_name(subcommand)}' if subcommand is not None else ''
    output_dir = _state.xdir / command / f'{timestamp}{sub}'
    output_dir.mkdir(parents=True)
    _state.last_output_dir = output_dir

    # Print the final output dir once at exit (atexit is LIFO; registering this
    # before the listener.stop below keeps it printing last).
    if not _state.atexit_registered:
        atexit.register(
            lambda: CONSOLE.print(
                f"\nOutput directory: {_state.last_output_dir}", highlight=False
            )
        )
        _state.atexit_registered = True

    if _state.keep_seconds is not None:
        remove_old_logs(_state.keep_seconds)

    _add_log_handlers(output_dir)
    return output_dir


def _add_log_handlers(output_dir: Path) -> None:
    """Wrap existing + file handlers in a QueueListener for non-blocking I/O."""
    logger = getLogger('otto')
    existing_handlers = list(logger.handlers)
    for h in existing_handlers:
        logger.removeHandler(h)

    log_file = FileHandler(output_dir / 'otto.log', mode='x')
    rich_formatter = RichFormatter()
    rich_formatter.rich = _state.rich_log_file
    log_file.setFormatter(rich_formatter)

    log_queue: Queue[LogRecord] = Queue(-1)
    _state.listener = QueueListener(
        log_queue, *existing_handlers, log_file, respect_handler_level=True
    )
    logger.addHandler(QueueHandler(log_queue))
    _state.listener.start()
    atexit.register(_state.listener.stop)


def remove_old_logs(
    seconds: float,
    *,
    time_budget: float = LOG_ROTATE_BUDGET_SECONDS,
) -> None:
    """Remove log dirs older than ``seconds``, time-boxed to ``time_budget``.

    When the budget is exceeded the scan stops early and resumes on the next
    call, bounding the per-run cost on large/slow (e.g. NFS) trees.
    """
    logger = getLogger('otto')
    xdir = _state.xdir
    if xdir is None or not xdir.is_dir():
        return

    oldest = (datetime.now() - timedelta(seconds=seconds)).timestamp()
    logged_deletion = False
    start = time.monotonic()
    budget_hit = False

    for cmd_dir_name in listdir(xdir):
        if budget_hit:
            break
        cmd_dir = xdir / cmd_dir_name
        if not cmd_dir.is_dir():
            continue
        for log_dir_name in listdir(cmd_dir):
            if time.monotonic() - start > time_budget:
                budget_hit = True
                break
            output_dir = cmd_dir / log_dir_name
            if not _LOG_DIR_NAME_RE.match(log_dir_name):
                continue
            if not output_dir.is_dir():
                continue
            if output_dir.stat().st_mtime < oldest:
                if not logged_deletion:
                    days = seconds / 60 / 60 / 24
                    days_str = f'{days:0.0f} {"day" if days == 1 else "days"}'
                    logger.info(
                        f"[magenta]Deleting log directories that are more than "
                        f"{days_str} old"
                    )
                    logged_deletion = True
                rmtree(output_dir)
                logger.debug(f"Removed {output_dir}")

    if budget_hit:
        logger.debug(
            "Log rotation hit its %gs time budget; remaining old directories "
            "will be removed on the next run.",
            time_budget,
        )
```

- [ ] **Step 4: Add the library `NullHandler` + `management` export**

Replace `src/otto/logger/__init__.py` contents with:

```python
import logging

from . import management as management
from .logger import (
    get_otto_logger as get_otto_logger,
)

# Library-citizen default: attach a NullHandler so importing otto as a library
# is silent unless the application configures handlers. Idempotent.
_otto = logging.getLogger("otto")
if not any(isinstance(h, logging.NullHandler) for h in _otto.handlers):
    _otto.addHandler(logging.NullHandler())
```

(`OttoLogger` and `init_otto_logger` re-exports are removed here — `OttoLogger` is deleted in Task 4; `init_otto_logger` is replaced by `management.init_cli_logging`. This file still imports `.logger`, which still defines `get_otto_logger` and — until Task 4 — `OttoLogger`/`init_otto_logger`; that's fine, we just stop re-exporting them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/logger/test_management.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Stage (do not commit)**

```bash
git add src/otto/logger/management.py src/otto/logger/__init__.py tests/unit/logger/test_management.py
# Intended commit message (Chris commits):
# feat(logger): add context-free otto.logger.management + library NullHandler
```

---

### Task 3: Cut over all call sites to `management` + `ctx.output_dir`

**Files:**
- Modify: `src/otto/cli/main.py` (~278), `src/otto/cli/docker.py:55`, `src/otto/cli/host.py:135`, `src/otto/cli/cov.py:79`, `src/otto/cli/reservation.py:43`, `src/otto/cli/monitor.py:69`, `src/otto/cli/test.py:219,519`, `src/otto/cli/run.py:66`
- Modify: `src/otto/host/interact.py:178-186`, `src/otto/suite/suite.py:12,21,163,188,193`
- Modify (tests): `tests/conftest.py:418-431`, `tests/unit/cli/conftest.py:34-44,116-119`, `tests/unit/logger/test_logger.py`

**Interfaces:**
- Consumes: `management.*` (Task 2), `OttoContext.output_dir` (Task 1), existing `otto.context.get_context` / `try_get_context`.
- Produces: no new API. After this task `OttoLogger`'s management methods are unused (deleted in Task 4). `OttoLogger` still exists, so the suite import still resolves until Task 4 — **but** this task already drops the `OttoLogger` type usage in `suite.py` (it isn't needed once `logger` is plain).

- [ ] **Step 1: Migrate `cli/main.py` init + handler filter**

In `src/otto/cli/main.py`, the `init_otto_logger(...)` call (lines ~278-285) becomes:

```python
    from logging import getLogger

    from ..host import HostFilter
    from ..logger import management

    management.init_cli_logging(
        xdir=xdir,
        log_level=log_level,
        keep_days=log_days,
        verbose=verbose,
        rich_log_file=rich_log_file,
    )
    for handler in getLogger('otto').handlers:
        handler.addFilter(HostFilter())
```

(Keep the existing `from ..host import HostFilter` import wherever it currently sits; the snippet shows the intent. Remove the old `logger = init_otto_logger(...)` binding and its `init_otto_logger` import.)

- [ ] **Step 2: Migrate the 7 `create_output_dir` callbacks**

In each file, change `logger.create_output_dir(<args>)` to record the dir on the active context. Add `from ..context import get_context` and `from ..logger import management` imports to each (place with the file's other imports). Exact edits:

- `src/otto/cli/docker.py:55`
  `logger.create_output_dir('docker', ctx.invoked_subcommand)`
  → `get_context().output_dir = management.create_output_dir('docker', ctx.invoked_subcommand)`
- `src/otto/cli/host.py:135`
  `logger.create_output_dir("host", f"{ctx.invoked_subcommand}")`
  → `get_context().output_dir = management.create_output_dir("host", f"{ctx.invoked_subcommand}")`
- `src/otto/cli/cov.py:79`
  `logger.create_output_dir('cov', ctx.invoked_subcommand)`
  → `get_context().output_dir = management.create_output_dir('cov', ctx.invoked_subcommand)`
- `src/otto/cli/reservation.py:43`
  `logger.create_output_dir('reservation', ctx.invoked_subcommand)`
  → `get_context().output_dir = management.create_output_dir('reservation', ctx.invoked_subcommand)`
- `src/otto/cli/monitor.py:69`
  `logger.create_output_dir("monitor")`
  → `get_context().output_dir = management.create_output_dir("monitor")`
- `src/otto/cli/test.py:519`
  `logger.create_output_dir('test', ctx.invoked_subcommand)`
  → `get_context().output_dir = management.create_output_dir('test', ctx.invoked_subcommand)`
- `src/otto/cli/run.py:66`
  `logger.create_output_dir("run", f"{ctx.invoked_subcommand}")`
  → `get_context().output_dir = management.create_output_dir("run", f"{ctx.invoked_subcommand}")`

- [ ] **Step 3: Migrate the `output_dir` reads**

- `src/otto/cli/test.py:219` — `log_dir = logger.output_dir` → `log_dir = get_context().output_dir` (add `from ..context import get_context` if not present).
- `src/otto/suite/suite.py` — drop line 12 `from otto.logger.logger import OttoLogger`; change line 21 `logger: OttoLogger = getLogger('otto')  # type: ignore` → `logger = getLogger('otto')`; add `from otto.context import get_context`. Then:
  - line 163 `self.suiteDir = logger.output_dir` → `self.suiteDir = get_context().output_dir`
  - line 188 `cls.testDir = logger.output_dir / 'setupClass'` → `cls.testDir = get_context().output_dir / 'setupClass'`
  - line 193 `cls.testDir = logger.output_dir / 'teardownClass'` → `cls.testDir = get_context().output_dir / 'teardownClass'`
- `src/otto/host/interact.py:178-186` — `_session_log_path` becomes (add `from ..context import try_get_context`):

```python
def _session_log_path() -> Path | None:
    """Return the path to the current invocation's ``otto.log``, if any."""
    ctx = try_get_context()
    output_dir = ctx.output_dir if ctx is not None else None
    if output_dir is None:
        return None
    return Path(output_dir) / 'otto.log'
```

- [ ] **Step 4: Migrate the test fixtures/patches**

- `tests/conftest.py:418-431` — `_reset_otto_logger_retention` fixture: replace the body so it resets management state instead of poking the singleton:

```python
@pytest.fixture(autouse=True)
def _reset_otto_logger_retention():
    """Reset otto's logging-management state between tests so log retention /
    output-dir config can't leak across tests in the same xdist worker (the
    root cause of the old test_cov ENOTDIR flakes)."""
    yield
    from otto.logger import management
    management.reset()
```

- `tests/unit/cli/conftest.py:34-44` — `no_logger_output_dir` fixture: change the patch target:

```python
    with patch('otto.logger.management.create_output_dir'):
        yield
```

- `tests/unit/cli/conftest.py:116-119` (inside `real_main_mocks`) — change the two patch targets and the snapshot reads:

```python
    logger = get_otto_logger()
    original_level = logger.level
    original_handlers = list(logger.handlers)
    with (
        patch.dict(os.environ, clean_env, clear=True),
        patch('otto.logger.management.remove_old_logs') as p_remove,
        patch('otto.logger.management.RichHandler') as p_rich,
        patch('otto.cli.main.get_repos', return_value=[repo]),
        ...
```

  Remove the now-invalid `original_xdir = getattr(logger, '_xdir', None)` and `original_keep_seconds = logger._keep_seconds` snapshot lines and any later restore of them — `management.reset()` (autouse, above) handles isolation. (If the fixture later references `original_xdir`/`original_keep_seconds`, delete those references too.)

- `tests/unit/logger/test_logger.py` — this file's rotation tests call `init_otto_logger(...)`, `logger.create_output_dir(...)`, `logger.remove_old_logs(...)`, `logger.output_dir`, `logger.xdir`. Migrate them to `management.*`: `from otto.logger import management`; `management.init_cli_logging(xdir=tmpdir, log_level='INFO', keep_days=7)`; `management.create_output_dir('pytest', 'logger_test')`; `management.remove_old_logs(seconds=..., time_budget=...)`; reads of `logger.output_dir` → the value returned by `create_output_dir`. The `time_budget` fake-clock test monkeypatches `otto.logger.management.time.monotonic`. Keep the behavior assertions identical. (The duplicate-detection probe and a guard test are added in Task 4.)

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/unit/logger tests/unit/cli tests/unit/suite tests/unit/host -q`
Expected: PASS. (If a CLI test fails because a callback now calls `get_context()` with no active context, that test must set a context — see Task 3 notes; most CLI unit tests invoke subcommands with `real_main_mocks` which calls `set_context`, or use the patched-out `create_output_dir`.)

- [ ] **Step 6: Smoke the import surface**

Run: `uv run python -c "import otto; from otto import all_hosts, app; from otto.logger import get_otto_logger, management; print('ok')"`
Expected: prints `ok`, no ImportError / no circular-import error.

- [ ] **Step 7: Stage (do not commit)**

```bash
git add src/otto/cli/ src/otto/host/interact.py src/otto/suite/suite.py tests/conftest.py tests/unit/cli/conftest.py tests/unit/logger/test_logger.py
# Intended commit message (Chris commits):
# refactor(logger): route CLI logging through management + ctx.output_dir
```

---

### Task 4: Delete the `OttoLogger` subclass; plain `get_otto_logger`; guard test

**Files:**
- Modify: `src/otto/logger/logger.py` (delete the class + `init_otto_logger`; rewrite `get_otto_logger`)
- Modify: `src/otto/__init__.py` (remove the eager call + comment)
- Test: `tests/unit/logger/test_logger_standard.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `get_otto_logger(name: str | None = None) -> logging.Logger` (plain). `OttoLogger` and `init_otto_logger` no longer exist.

- [ ] **Step 1: Write the failing guard tests**

Create `tests/unit/logger/test_logger_standard.py`:

```python
import logging

from otto.logger import get_otto_logger


def test_otto_logger_is_a_plain_standard_logger():
    """Regression guard for the duplicate-import hazard: with a custom Logger
    subclass the singleton's class could diverge across a double-import. A plain
    logging.Logger has a stable identity, so this can never recur."""
    lg = get_otto_logger()
    assert type(lg) is logging.Logger
    assert lg is logging.getLogger('otto')
    assert lg.name == 'otto'


def test_get_otto_logger_named_is_child_under_otto():
    child = get_otto_logger('host')
    assert child is logging.getLogger('otto.host')
    assert child.name == 'otto.host'


def test_no_ottologger_symbol_exported():
    import otto.logger as pkg
    assert not hasattr(pkg, 'OttoLogger')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/logger/test_logger_standard.py -v`
Expected: FAIL — `type(lg) is logging.Logger` is False (it's still `OttoLogger`); `test_no_ottologger_symbol_exported` may also fail if anything re-exports it.

- [ ] **Step 3: Rewrite `src/otto/logger/logger.py`**

Replace the entire file contents with just the plain emit-surface helper:

```python
"""otto's logger accessor.

``'otto'`` is a plain ``logging.Logger`` (no subclass). otto modules emit via
``get_otto_logger()`` / ``logging.getLogger(__name__)``; child loggers propagate
to ``'otto'``, where the CLI attaches handlers (see ``otto.logger.management``).
Configuring/replacing handlers is up to the application — otto-the-library only
emits (and ``otto.logger`` attaches a ``NullHandler``).
"""

from logging import Logger, getLogger


def get_otto_logger(name: str | None = None) -> Logger:
    """Return the ``'otto'`` logger (or the ``'otto.<name>'`` child)."""
    return getLogger(f"otto.{name}" if name else "otto")
```

This deletes `class OttoLogger`, `init_otto_logger`, `_add_log_handlers`,
`create_output_dir`, `remove_old_logs`, `_command_to_dir_name`, the `xdir` /
`output_dir` / `keep_seconds` / `rich_logging` members, the
`_LOG_DIR_NAME_RE` / `LOG_ROTATE_BUDGET_SECONDS` constants (now in
`management`), and the `setLoggerClass`/`getLoggerClass`/`cast` machinery.

- [ ] **Step 4: Remove the eager call from `otto/__init__.py`**

In `src/otto/__init__.py`, delete the obsolete comment (line 1) and the eager
call (line 4). **Keep** the re-export on line 2. Result — the top of the file
becomes:

```python
from otto.logger import get_otto_logger as get_otto_logger

# Blessed, concise form for declaring suite/instruction Options with validation:
```

(The `from pydantic.dataclasses import dataclass as options`, `from otto.cli
import app`, and the `.configmodule` / `.context` imports below are unchanged —
deferring those is the separate import-light workstream.)

- [ ] **Step 5: Run the guard tests**

Run: `uv run pytest tests/unit/logger/test_logger_standard.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Verify the previously-flaky scenario under cov + xdist**

Run: `uv run pytest tests/unit/logger tests/unit/test_filesystem.py tests/unit/monitor/test_collector_nfs.py -q --cov=otto.logger --cov=otto.logger.management`
Expected: PASS — no class-identity divergence (the exact multi-file `--cov`/xdist combo that was flaky before).

- [ ] **Step 7: Full gate**

Run: `make typecheck` → Expected: PASS (no `OttoLogger` type references remain; `suite.py` annotation dropped).
Run: `make docs` → Expected: PASS (no broken `OttoLogger` xrefs; check `docs/` for any `OttoLogger`/`init_otto_logger` references and update them to `get_otto_logger`/`management` if Sphinx flags them).
Run: `make coverage` → Expected: PASS — suite green, coverage at/above threshold.
Run: `uv run python -c "from otto import all_hosts, app; from otto.logger import get_otto_logger, management; print('ok')"` and `uv run otto --help` → Expected: both succeed (registrations intact; CLI help renders).

- [ ] **Step 8: Stage (do not commit)**

```bash
git add src/otto/logger/logger.py src/otto/__init__.py tests/unit/logger/test_logger_standard.py
git status --short   # confirm the whole workstream is staged, nothing committed
# Intended commit message (Chris commits):
# refactor(logger)!: delete OttoLogger subclass; 'otto' is a plain logging.Logger
```

- [ ] **Step 9: Hand off to Chris**

Report the green gate output + the per-task commit messages. `make nox` / live `make coverage` are Chris's. Optionally suggest squashing the four task commits into one.

---

## Self-Review

**1. Spec coverage:**
- §3.1 delete `OttoLogger` subclass → Task 4 (Step 3). ✓
- §3.2 library emit surface (`get_otto_logger` plain + NullHandler) → Task 4 (Step 3) + Task 2 (Step 4). ✓
- §3.3 `otto.logger.management` (context-free; `create_output_dir` returns Path; `init_cli_logging`; `remove_old_logs`; `reset`) → Task 2. ✓
- §3.4 `OttoContext.output_dir` + CLI wiring → Task 1 + Task 3 (Steps 1-2). ✓
- §3.5 call-site migration map → Task 3 (every row covered: init, 7 callbacks, output_dir reads, suite type drop, emit grabs unchanged, test patches). ✓
- §3.6 remove eager `get_otto_logger()` call from `otto/__init__` (keep re-export) → Task 4 (Step 4). ✓
- §2 invariant (all `otto.*` logs flow through otto's handlers in CLI mode) → preserved (handlers on `'otto'`; propagation unchanged); §4 library mode (NullHandler) → Task 2/Task 4 guard tests. ✓
- §5 why-it-works (plain Logger, stable identity) → Task 4 guard + Step 6 cov/xdist check. ✓
- §6 testing (management tests, guard, NFS rotation migration, ctx.output_dir wiring) → Tasks 2-4. ✓
- §7 risks (call-site migration → Task 3 Step 5; import cycle → Task 3 Step 6 smoke; context availability → Task 3 Step 5 note + Step 6; behavior unchanged → full gate). ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step shows complete code or an exact before/after; every run step has a command + expected result. The Task 3 migrations enumerate each site explicitly.

**3. Type consistency:** `get_otto_logger(name=None) -> logging.Logger` is consistent between Task 4's definition and all emit call sites (unchanged). `management.create_output_dir(command, subcommand=None) -> Path` matches its callers in Task 3. `OttoContext.output_dir: Path | None` (Task 1) matches the CLI writes (`get_context().output_dir = ...`) and reads (`get_context().output_dir`, `try_get_context().output_dir`) in Task 3. `management.init_cli_logging(...)` signature matches the `cli/main.py` call. `LOG_ROTATE_BUDGET_SECONDS` defined once (management) and used as the `time_budget` default. The autouse `management.reset()` fixture (Task 3) matches the `reset()` defined in Task 2.
