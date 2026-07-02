# CLI Command Registration & Registry Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One generic `Registry` for all 13 backend registries plus a new CLI command registry with full first/third-party symmetry (users can register top-level commands), an explicit `bootstrap()` composition root replacing configmodule's import-time exec of user code, and lazy command loading for startup/completion (spec: `docs/superpowers/specs/2026-07-01-cli-registry-unification-design.md`).

**Architecture:** Typer-native, registry-deferred: Typer/click keep doing all CLI work; a `CommandSpec` registry defers *when* command objects are constructed, via the click-native `list_commands()`/`get_command()` group hooks otto already uses for host verbs. `bootstrap()` runs discovery + contained user-code registration before argv parsing; lab loading and per-command output-dir/reservation-gate work move to a leaf-invoke preamble so `--help` paths are structurally inert (killing the token-sniffing and `ctx.meta` verdict plumbing).

**Tech Stack:** Python 3.10+, Typer 0.26 (vendored click — always subclass `typer.core.TyperGroup`, never real `click.Group`), pytest (+CliRunner), ruff `select=ALL`, ty, Sphinx executable doctests.

## Global Constraints

- **Baseline: main AFTER the result-type-unification worktree merges.** Verify `src/otto/result.py` exists before starting; if it doesn't, STOP and report.
- **NEVER run `git commit`** — stage exact paths (`git add <paths>`, never `git add -u`/`-A`) and end each task by reporting staged paths plus a paste-able commit message. Chris commits.
- **No `from __future__ import annotations`** (breaks the Sphinx nitpicky docs gate). Real 3.10+ annotations, module-top imports. (Pre-existing offender `cli/param_synth.py` keeps its import unless Task 10 must touch an annotation it guards — do not add new ones.)
- After any code edit: `uv run ruff check . && uv run ruff format . && uv run ruff check .` — format is NOT lint-neutral; always re-check.
- `ty` runs only via `uv run nox -s typecheck`. Run it at the end of every task that touches `src/` (whole-repo; catches call sites scoped tests miss).
- Test runs: single passes with `-n auto`. Never loop test runs on this VM.
- If executing in a fresh worktree: run `uv sync` once before anything else.
- Every public symbol gets a docstring satisfying ruff's pydocstyle rules; doctest examples in `src/` docstrings execute in the docs gate — they must actually run.
- **Behavioral contract:** the e2e suites `tests/e2e/cli/test_schema_run_help_e2e.py`, `test_root_flags_e2e.py`, `test_test_listing_e2e.py`, `test_test_exitcode_e2e.py` must pass unmodified at the end of Tasks 7, 8, 9 (they pin `--help` lab-freedom, no-output-dir-on-help, and exit codes).
- Typer trap: `typer.Context`/`typer.Exit`/`TyperGroup` only — never `import click` (Typer 0.26 vendors its own click fork).

---

### Task 1: Generic `Registry` (`src/otto/registry.py`)

**Files:**
- Create: `src/otto/registry.py`
- Create: `tests/unit/registry/__init__.py` (empty), `tests/unit/registry/test_registry.py`

**Interfaces:**
- Consumes: stdlib only (`difflib`, `inspect`, `typing`).
- Produces (every later task relies on these exact names):
  - `Registry(kind: str, *, register_hint: str)` — generic over `T`.
  - `.register(name: str, obj: T, *, overwrite: bool = False, origin: str | None = None) -> None` — duplicate + `overwrite=False` raises `ValueError` naming both origins.
  - `.get(name: str) -> T` — unknown name raises `ValueError` with registered names, difflib did-you-mean, and `register_hint`.
  - `.names() -> list[str]` (registration order), `.origin(name: str) -> str`, `.unregister(name: str) -> None`, `__contains__`, `__len__`, `.items() -> list[tuple[str, T]]`.
  - `caller_module(depth: int = 1) -> str` — module name of the caller `depth` frames up.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/registry/test_registry.py`:

```python
"""Unit tests for the generic component Registry (spec 2026-07-01)."""

import pytest

from otto.registry import Registry, caller_module


def _make() -> Registry[str]:
    return Registry("term backend", register_hint="otto.register_term_backend()")


class TestRegisterAndGet:
    def test_round_trip_and_order(self):
        r = _make()
        r.register("ssh", "SSH")
        r.register("telnet", "TELNET")
        assert r.get("ssh") == "SSH"
        assert r.names() == ["ssh", "telnet"]  # registration order, not sorted
        assert "ssh" in r
        assert len(r) == 2
        assert r.items() == [("ssh", "SSH"), ("telnet", "TELNET")]

    def test_duplicate_raises_naming_both_origins(self):
        r = _make()
        r.register("ssh", "A", origin="repo_a.init")
        with pytest.raises(ValueError, match=r"already registered by 'repo_a.init'") as ei:
            r.register("ssh", "B", origin="repo_b.init")
        assert "repo_b.init" in str(ei.value)

    def test_overwrite_replaces(self):
        r = _make()
        r.register("json", "OLD")
        r.register("json", "NEW", overwrite=True)
        assert r.get("json") == "NEW"

    def test_origin_defaults_to_caller_module(self):
        r = _make()
        r.register("ssh", "A")
        assert r.origin("ssh") == __name__

    def test_unregister(self):
        r = _make()
        r.register("ssh", "A")
        r.unregister("ssh")
        assert "ssh" not in r
        with pytest.raises(ValueError, match="Unknown term backend"):
            r.unregister("ssh")


class TestErrors:
    def test_unknown_lists_names_hint_and_suggestion(self):
        r = _make()
        r.register("telnet", "T")
        with pytest.raises(ValueError) as ei:
            r.get("tellnet")
        msg = str(ei.value)
        assert "Unknown term backend 'tellnet'" in msg
        assert "Did you mean 'telnet'?" in msg
        assert "telnet" in msg
        assert "otto.register_term_backend()" in msg

    def test_unknown_without_close_match_has_no_suggestion(self):
        r = _make()
        r.register("telnet", "T")
        assert "Did you mean" not in _get_error(r, "zzz")

    def test_empty_registry_says_none(self):
        assert "<none>" in _get_error(_make(), "x")


def _get_error(r: Registry[str], name: str) -> str:
    with pytest.raises(ValueError) as ei:
        r.get(name)
    return str(ei.value)


def test_caller_module_depth():
    def inner() -> str:
        return caller_module()

    assert inner() == __name__
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/registry -n auto`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.registry'`

- [ ] **Step 3: Implement `src/otto/registry.py`**

```python
"""Generic named registry for pluggable components.

Every otto extension seam (term/transfer backends, host classes, lab
repositories, CLI commands, ...) stores its entries in a :class:`Registry`:
one storage idiom, uniform fail-loud errors with did-you-mean suggestions,
and per-entry origin attribution. Domain modules keep their public
``register_*``/``build_*`` wrapper functions; this class is the shared engine
behind them.

>>> r: Registry[str] = Registry("demo backend", register_hint="register_demo()")
>>> r.register("json", "the-json-backend", origin="example")
>>> r.get("json")
'the-json-backend'
>>> r.names()
['json']
"""

import difflib
import inspect
from typing import Generic, TypeVar

T = TypeVar("T")


def caller_module(depth: int = 1) -> str:
    """Return the ``__name__`` of the module *depth* call frames above the caller."""
    frame = inspect.currentframe()
    for _ in range(depth + 1):
        frame = frame.f_back if frame is not None else None
    if frame is None:
        return "<unknown>"
    return frame.f_globals.get("__name__", "<unknown>")


class Registry(Generic[T]):
    """Named registry of pluggable components; fail-loud lookups with suggestions."""

    def __init__(self, kind: str, *, register_hint: str) -> None:
        """Create a registry for *kind* entries (e.g. ``"term backend"``).

        *register_hint* names the public registration function shown in lookup
        errors (e.g. ``"otto.register_term_backend()"``).
        """
        self._kind = kind
        self._register_hint = register_hint
        self._entries: dict[str, T] = {}
        self._origins: dict[str, str] = {}

    def register(
        self, name: str, obj: T, *, overwrite: bool = False, origin: str | None = None
    ) -> None:
        """Register *obj* under *name*; duplicates are loud unless *overwrite*.

        *origin* attributes the entry (defaults to the caller's module); it is
        used in collision and listing messages.
        """
        entry_origin = origin if origin is not None else caller_module()
        if name in self._entries and not overwrite:
            raise ValueError(
                f"{self._kind} {name!r} is already registered by "
                f"{self._origins[name]!r}; second registration from "
                f"{entry_origin!r}. Pass overwrite=True to replace it deliberately."
            )
        self._entries[name] = obj
        self._origins[name] = entry_origin

    def get(self, name: str) -> T:
        """Return the entry registered under *name*.

        Raises:
            ValueError: If *name* is unknown; the message lists registered
                names, adds a did-you-mean suggestion, and points at the
                registration function.
        """
        try:
            return self._entries[name]
        except KeyError:
            known = ", ".join(self._entries) or "<none>"
            close = difflib.get_close_matches(name, list(self._entries), n=1)
            suggestion = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"Unknown {self._kind} {name!r}.{suggestion} Registered: {known}. "
                f"Custom entries can be added via {self._register_hint}."
            ) from None

    def unregister(self, name: str) -> None:
        """Remove the entry registered under *name* (ValueError if unknown)."""
        self.get(name)  # reuse the rich unknown-name error
        del self._entries[name]
        del self._origins[name]

    def names(self) -> list[str]:
        """Return registered names in registration order."""
        return list(self._entries)

    def origin(self, name: str) -> str:
        """Return the module that registered *name* (ValueError if unknown)."""
        self.get(name)
        return self._origins[name]

    def items(self) -> list[tuple[str, T]]:
        """Return ``(name, entry)`` pairs in registration order."""
        return list(self._entries.items())

    def __contains__(self, name: str) -> bool:
        """Return whether *name* is registered."""
        return name in self._entries

    def __len__(self) -> int:
        """Return the number of registered entries."""
        return len(self._entries)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/registry -n auto`
Expected: all PASS

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`
Expected: clean.

- [ ] **Step 6: Stage + report**

```bash
git add src/otto/registry.py tests/unit/registry/
```

Paste-able commit message:

```
feat(registry): generic Registry with origin tracking + did-you-mean errors

Shared engine for all component registries per the 2026-07-01 CLI-registry
spec: ordered storage, loud duplicate registration naming both origins,
lookup errors listing known names with difflib suggestions and the public
register function.
```

---

### Task 2: Term + transfer registries adopt `Registry`; public accessors

**Files:**
- Modify: `src/otto/host/connections.py:502-570` (term registry block)
- Modify: `src/otto/host/transfer/registry.py` (whole registry), `src/otto/host/transfer/__init__.py` (re-export)
- Modify: `src/otto/models/host.py:23-28,81-93` (validators — drop private imports)
- Modify: `src/otto/cli/host.py:55-57,79-81` (completers — drop private imports)
- Modify: `src/otto/configmodule/completion_cache.py` (`collect_backend_names`)
- Test: `tests/unit/host/` term/transfer registry tests (locate: `grep -rln "_TERM_BACKENDS\|_TRANSFER_BACKENDS\|register_term_backend\|register_transfer_backend" tests/`)

**Interfaces:**
- Consumes: `Registry`, `caller_module` from Task 1.
- Produces:
  - `otto.host.connections.TermBackend` — `@dataclass(frozen=True)` with `cls: type[ConnectionManager]`, `host_families: frozenset[str]`.
  - `otto.host.connections.TERM_BACKENDS: Registry[TermBackend]` (public constant).
  - `otto.host.transfer.registry.TRANSFER_BACKENDS: Registry[type]` (public; entries are backend classes carrying their `host_families` ClassVar), re-exported from `otto.host.transfer`.
  - `register_term_backend`/`build_term_backend` and `register_transfer_backend`/`build_transfer_backend` keep their exact public signatures and return types (wrappers).

- [ ] **Step 1: Convert the term registry**

In `src/otto/host/connections.py`, replace the `_TERM_BACKENDS`/`_TERM_FAMILIES` dicts (lines 509-510) and the bodies of `register_term_backend`/`build_term_backend` with:

```python
from dataclasses import dataclass

from ..registry import Registry, caller_module


@dataclass(frozen=True)
class TermBackend:
    """A registered term backend: the manager class + the host families it serves."""

    cls: type[ConnectionManager]
    host_families: frozenset[str]


TERM_BACKENDS: Registry[TermBackend] = Registry(
    "term backend", register_hint="otto.host.connections.register_term_backend()"
)
```

`register_term_backend` keeps its docstring and empty-families validation, then ends with:

```python
    TERM_BACKENDS.register(
        name,
        TermBackend(cls=cls, host_families=host_families),
        overwrite=overwrite,
        origin=caller_module(),
    )
```

and gains a keyword param `overwrite: bool = False` (documented: "replace a built-in deliberately"). `build_term_backend` becomes:

```python
def build_term_backend(name: str) -> type[ConnectionManager]:
    """Return the connection-backend class registered under *name*.

    Raises:
        ValueError: If *name* is not registered; the message lists registered
            names and suggests near-misses.
    """
    return TERM_BACKENDS.get(name).cls
```

`_register_builtin_term_backends()` is unchanged (it calls the public wrapper). Delete `_TERM_BACKENDS` and `_TERM_FAMILIES` entirely.

- [ ] **Step 2: Convert the transfer registry the same way**

In `src/otto/host/transfer/registry.py`: replace `_TRANSFER_BACKENDS: dict[str, type]` with `TRANSFER_BACKENDS: Registry[type] = Registry("transfer backend", register_hint="otto.host.transfer.register_transfer_backend()")`; `register_transfer_backend` gains `overwrite: bool = False` and delegates with `origin=caller_module()`; `build_transfer_backend` returns `TRANSFER_BACKENDS.get(name)`. In `src/otto/host/transfer/__init__.py`, replace the `_TRANSFER_BACKENDS` re-export with `TRANSFER_BACKENDS`.

- [ ] **Step 3: Migrate the four private-dict consumers**

Mechanical rule — apply in each listed file:

- `src/otto/models/host.py:23`: `from ..host.connections import _TERM_BACKENDS, _TERM_FAMILIES` → `from ..host.connections import TERM_BACKENDS`; line 28 → `from ..host.transfer import TRANSFER_BACKENDS`.
  - Validator rewrites: `if v not in _TRANSFER_BACKENDS` → `if v not in TRANSFER_BACKENDS`; `sorted(_TRANSFER_BACKENDS)` → `sorted(TRANSFER_BACKENDS.names())`; `_TRANSFER_BACKENDS[v].host_families` → `TRANSFER_BACKENDS.get(v).host_families`; `if v not in _TERM_BACKENDS` → `if v not in TERM_BACKENDS`; `sorted(_TERM_BACKENDS)` → `sorted(TERM_BACKENDS.names())`; `_TERM_FAMILIES[v]` → `TERM_BACKENDS.get(v).host_families`.
- `src/otto/cli/host.py:55-57`: `names = list(_TERM_BACKENDS)` → `names = TERM_BACKENDS.names()` (import `TERM_BACKENDS`); lines 79-81: `[n for n, c in _TRANSFER_BACKENDS.items() if "unix" in c.host_families]` → `[n for n, c in TRANSFER_BACKENDS.items() if "unix" in c.host_families]`.
- `src/otto/configmodule/completion_cache.py::collect_backend_names`: `sorted(_TERM_BACKENDS)` → `sorted(TERM_BACKENDS.names())`; the transfer comprehension iterates `TRANSFER_BACKENDS.items()`.

- [ ] **Step 4: Run scoped tests, fix re-registration fallout**

Run: `uv run pytest tests/unit/host tests/unit/models tests/unit/cli tests/unit/configmodule -n auto`
Expected: mostly PASS. Any test that registers a backend name twice now fails loudly — fix the *test* with `overwrite=True` or an `unregister` cleanup (fixture `finally`), never by weakening the registry. List every such fixture change in the task report.

- [ ] **Step 5: Lint + typecheck + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

```bash
git add src/otto/host/connections.py src/otto/host/transfer/registry.py src/otto/host/transfer/__init__.py src/otto/models/host.py src/otto/cli/host.py src/otto/configmodule/completion_cache.py tests/unit/
```

Paste-able commit message:

```
refactor(host): term/transfer registries on the generic Registry

TERM_BACKENDS/TRANSFER_BACKENDS become public Registry instances (per-name
origin, did-you-mean errors); models/host validators, CLI completers, and
the completion cache stop importing the private dicts.
```

---

### Task 3: Remaining 11 registries adopt `Registry`; builder-bypass fixes

**Files:**
- Modify (mechanical conversion, same pattern as Task 2):
  `src/otto/host/command_frame.py` (`_FRAME_CLASSES`), `src/otto/host/binary_loader.py` (`_LOADER_CLASSES`), `src/otto/host/embedded_filesystem.py` (`_FILESYSTEM_CLASSES`), `src/otto/host/power.py` (`_POWER_CONTROLLERS`), `src/otto/host/os_profile.py` (`_HOST_CLASSES` → `HOST_CLASSES`, `_OS_PROFILES` → `OS_PROFILES`), `src/otto/storage/registry.py` (`_LAB_REPOSITORIES`), `src/otto/reservations/registry.py` (`_RESERVATION_BACKENDS`), `src/otto/monitor/parsers.py` (`_host_parser_registry`), `src/otto/monitor/snmp.py` (`_SNMP_METRICS`). **Exception recorded 2026-07-01:** `src/otto/host/product.py` (`_PRODUCT_PROVIDERS`) is an unkeyed ordered list, not a named registry — NOT converted (see spec)
- Modify (bypass fixes): `src/otto/storage/__init__.py` (`build_lab_repository`), `src/otto/reservations/__init__.py` (`build_backend`)
- Modify (consumer): `src/otto/cli/expose.py:185,191` (`_HOST_CLASSES` import)
- Test: existing unit tests for each seam (locate per module: `grep -rln "<old_dict_name>" src/ tests/`)

**Interfaces:**
- Consumes: `Registry`, `caller_module` (Task 1).
- Produces: each module exposes a public `UPPER_SNAKE` `Registry` constant named after the old dict (e.g. `FRAME_CLASSES`, `LOADER_CLASSES`, `FILESYSTEM_CLASSES`, `POWER_CONTROLLERS`, `HOST_CLASSES`, `OS_PROFILES`, `LAB_REPOSITORIES`, `RESERVATION_BACKENDS`, `HOST_PARSERS`, `SNMP_METRICS`, `PRODUCT_PROVIDERS`). All existing public `register_*`/`build_*`/`get_*` wrappers keep their exact signatures and return types (class vs instance vs kwargs semantics preserved).

- [ ] **Step 1: Convert each registry (mechanical rule)**

For each module: replace the private dict with a module-level `Registry` (kind = human name from the old error message; `register_hint` = the module's public register function), route the wrapper's store through `.register(..., origin=caller_module())` (adding `overwrite: bool = False` to the wrapper signature), and route lookups through `.get(name)`. Delete the hand-rolled `raise ValueError(...unknown...)` blocks — `Registry.get` subsumes them. Where the old code iterated the dict (e.g. listing names for errors or completion), use `.names()`/`.items()`.

Grep after each module: `grep -rn "<old_dict_name>" src/ tests/` — update every consumer (tests included) to the public constant.

- [ ] **Step 2: Fix the two builder bypasses**

`src/otto/storage/__init__.py::build_lab_repository` — the `json` branch currently constructs `JsonFileLabRepository` directly. Register the built-in and resolve through the registry:

In `src/otto/storage/registry.py`, add at module bottom (public path, like `_register_builtin_term_backends`):

```python
def _register_builtins() -> None:
    """Register the built-in lab repositories through the public path."""
    from .json_repository import JsonFileLabRepository

    register_lab_repository("json", JsonFileLabRepository)


_register_builtins()
```

(If a `_register_builtins` already exists registering `json`, keep it — the fix is only in the builder below.) In `build_lab_repository`, replace:

```python
    if backend_name == "json":
        return JsonFileLabRepository(search_paths=list(search_paths or []))
```

with:

```python
    cls = get_lab_repository_class(backend_name)
    if backend_name == "json":
        # The built-in json backend takes the aggregated search paths; a
        # re-registered replacement must accept the same constructor contract.
        return cls(search_paths=list(search_paths or []))
```

and let the custom-backend tail reuse the already-fetched `cls`. Same pattern in `src/otto/reservations/__init__.py::build_backend`: fetch `cls = get_reservation_backend_class(backend_name)` FIRST for all names (register built-ins `none` → `NullReservationBackend`, `json` → `JsonReservationBackend` through the public register function if not already), then keep the name-keyed constructor blocks but construct via `cls`.

- [ ] **Step 3: Migrate `cli/expose.py` off `_HOST_CLASSES`**

Line 185: `from ..host.os_profile import _HOST_CLASSES` → `from ..host.os_profile import HOST_CLASSES`; line 191: `for cls in _HOST_CLASSES.values():` → `for _name, cls in HOST_CLASSES.items():`.

- [ ] **Step 4: Full unit tier + lint + typecheck**

Run: `uv run pytest tests/unit -n auto`
Expected: PASS (fix double-registration test fixtures as in Task 2 Step 4 — report each).
Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

- [ ] **Step 5: Stage + report**

```bash
git add src/otto/host/ src/otto/storage/ src/otto/reservations/ src/otto/monitor/ src/otto/cli/expose.py tests/unit/
```

Paste-able commit message:

```
refactor(registries): all 13 backend registries on the generic Registry

One storage idiom everywhere; did-you-mean lookup errors for free; the
lab-repository and reservation builders resolve built-ins through their
own registries (re-registering `json` now takes effect), closing the
review's builder-bypass finding.
```

---

### Task 4: CLI command registry (`cli/registry.py`) + shared invoke helpers (`cli/invoke.py`)

**Files:**
- Create: `src/otto/cli/registry.py`, `src/otto/cli/invoke.py`
- Modify: `src/otto/cli/run.py` (move `_ctx_param_name`/`_inject_ctx`/`_wrap_with_options` out; import back)
- Modify: `src/otto/__init__.py` (lazy exports `register_cli_command`, `cli_command`)
- Create: `tests/unit/cli/test_cli_registry.py`

**Interfaces:**
- Consumes: `Registry`, `caller_module` (Task 1); `async_typer_command` (`otto/utils.py:71`); `options_params`/`build_options` (`otto/params.py`).
- Produces:
  - `otto.cli.registry.CommandSpec` — frozen dataclass: `name: str`, `loader: Any` (Typer app | callable | `"pkg.mod:attr"` str), `help: str | None = None`, `lab_free: bool = False`, `output_dir: bool = True`, `gate: bool = True`, `origin: str = ""`.
  - `otto.cli.registry.CLI_COMMANDS: Registry[CommandSpec]`.
  - `register_cli_command(name, loader, *, help=None, lab_free=False, output_dir=True, gate=True) -> None` — NO overwrite parameter (collisions always loud).
  - `cli_command(*args, options=None, name=None, help=None, lab_free=False, output_dir=True, gate=True, **kwargs)` — decorator for plain/async functions.
  - `resolve_spec_command(spec: CommandSpec) -> Any` — returns the vendored-click command/group for a spec, importing `"pkg.mod:attr"` loaders only now.
  - `otto.cli.invoke.prepare_command_target(func, options_cls=None)` — OttoContext injection + options-dataclass expansion (the machinery `@instruction` uses today).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cli/test_cli_registry.py`:

```python
"""CLI command registry: spec storage, lazy loaders, collision policy."""

import sys

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.registry import (
    CLI_COMMANDS,
    CommandSpec,
    cli_command,
    register_cli_command,
    resolve_spec_command,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in list(CLI_COMMANDS.names()):
        if name not in before:
            CLI_COMMANDS.unregister(name)


def test_register_typer_app_resolves_to_group():
    sub = typer.Typer(name="mytool")

    @sub.command()
    def status() -> None:
        """Show status."""
        typer.echo("ok")

    register_cli_command("mytool", sub, help="My tool.")
    spec = CLI_COMMANDS.get("mytool")
    assert spec.help == "My tool."
    cmd = resolve_spec_command(spec)
    assert "status" in cmd.commands  # a group with its child


def test_register_function_resolves_to_command():
    async def hello(name: str = "world") -> None:
        """Say hello."""
        typer.echo(f"hi {name}")

    register_cli_command("hello", hello, help="Say hello.")
    cmd = resolve_spec_command(CLI_COMMANDS.get("hello"))
    assert not hasattr(cmd, "commands")  # a leaf command, not a group


def test_lazy_module_attr_loader_imports_only_on_resolve(tmp_path, monkeypatch):
    mod_dir = tmp_path / "fake_pkg"
    mod_dir.mkdir()
    (mod_dir / "__init__.py").write_text("")
    (mod_dir / "cmds.py").write_text(
        "import typer\n"
        "lazy_app = typer.Typer(name='lazy')\n"
        "@lazy_app.command()\n"
        "def go() -> None:\n"
        "    '''Go.'''\n"
        "    typer.echo('went')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    register_cli_command("lazy", "fake_pkg.cmds:lazy_app", help="Lazy.")
    assert "fake_pkg.cmds" not in sys.modules  # registration alone imports nothing
    cmd = resolve_spec_command(CLI_COMMANDS.get("lazy"))
    assert "fake_pkg.cmds" in sys.modules
    assert "go" in cmd.commands


def test_collision_is_loud_and_names_both_origins():
    register_cli_command("clash", typer.Typer(name="clash"))
    with pytest.raises(ValueError, match="already registered"):
        register_cli_command("clash", typer.Typer(name="clash"))


def test_cli_command_decorator_registers_and_runs():
    @cli_command(name="greet", help="Greet.")
    async def greet(who: str = "world") -> None:
        """Greet someone."""
        typer.echo(f"hello {who}")

    spec = CLI_COMMANDS.get("greet")
    cmd = resolve_spec_command(spec)
    app = typer.Typer()
    app.command("greet")(lambda: None)  # placeholder so Typer builds; invoke click directly
    result = runner.invoke_standalone = None  # not used; keep click-level invoke below
    from typer.testing import CliRunner as _CR

    # Invoke the resolved click command directly.
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        cmd.main(args=["--who", "bob"], prog_name="greet", standalone_mode=True)
    assert ei.value.code == 0
    assert "hello bob" in buf.getvalue()


def test_spec_defaults():
    register_cli_command("d", typer.Typer(name="d"))
    spec = CLI_COMMANDS.get("d")
    assert spec.lab_free is False and spec.output_dir is True and spec.gate is True
    assert spec.origin  # auto-captured
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cli/test_cli_registry.py -n auto`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.cli.registry'`

- [ ] **Step 3: Create `src/otto/cli/invoke.py` (moved helpers)**

Move `_ctx_param_name`, `_inject_ctx`, `_wrap_with_options` from `src/otto/cli/run.py:78-108,172-225` VERBATIM into the new module (public docstrings; keep leading underscores off the module-internal pair only if ruff complains — keep names as-is otherwise), and add:

```python
def prepare_command_target(
    func: Callable[..., Any], options_cls: type | None = None
) -> Callable[..., Any]:
    """Apply otto's CLI wrappers to *func*: OttoContext injection + options expansion.

    The shared machinery behind ``@instruction`` and ``@cli_command``: a
    parameter annotated ``OttoContext`` is stripped from the CLI signature and
    injected at call time; an *options_cls* dataclass parameter is expanded
    into individual CLI flags.
    """
    ctx_name = _ctx_param_name(func)
    target: Callable[..., Any] = func
    if ctx_name is not None:
        target = _inject_ctx(func, ctx_name)
    if options_cls is not None and dataclasses.is_dataclass(options_cls):
        target = _wrap_with_options(target, options_cls)
    return target
```

In `run.py`, delete the moved functions and import: `from .invoke import prepare_command_target`; the `@instruction` decorator body becomes:

```python
    def decorator(
        func: Callable[P, Coroutine[Any, Any, CommandResult]],
    ) -> Callable[P, CommandResult]:
        target = prepare_command_target(func, options)
        app = typer.Typer()
        new_instruction = app.command(*args, **kwargs)(async_typer_command(target))
        run_app.add_typer(app)
        return new_instruction
```

- [ ] **Step 4: Implement `src/otto/cli/registry.py`**

```python
"""The CLI command registry: how commands — first- and third-party — join ``otto``.

A :class:`CommandSpec` describes one top-level command or group: its name, a
loader (a live Typer app, a plain/async function, or a lazy ``"pkg.mod:attr"``
string imported only on dispatch), the help line shown by ``otto --help``
*without* importing the module, and dispatch metadata (``lab_free``,
``output_dir``, ``gate``). First-party subcommands and third-party plugins
register through the same :func:`register_cli_command`.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import typer

from ..registry import Registry, caller_module
from ..utils import async_typer_command
from .invoke import prepare_command_target


@dataclass(frozen=True)
class CommandSpec:
    """One registered top-level CLI command or group."""

    name: str
    """CLI name as the user types it (e.g. ``"run"``, ``"flash"``)."""

    loader: Any
    """A ``typer.Typer`` app, a plain/async function, or a lazy ``"pkg.mod:attr"`` string."""

    help: str | None = None
    """One-line help for ``otto --help`` — rendered without importing the module."""

    lab_free: bool = False
    """True when the command never needs the lab (e.g. ``schema``)."""

    output_dir: bool = True
    """Whether invocations create a per-command output directory."""

    gate: bool = True
    """Whether invocations run the reservation gate (ignored when ``lab_free``)."""

    origin: str = ""
    """Module that registered the command (auto-captured) — used in collisions."""


CLI_COMMANDS: Registry[CommandSpec] = Registry(
    "CLI command", register_hint="otto.register_cli_command()"
)


def register_cli_command(
    name: str,
    loader: Any,
    *,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
    lab_free: bool = False,
    output_dir: bool = True,
    gate: bool = True,
) -> None:
    """Register a top-level ``otto`` command or group.

    *loader* is a ``typer.Typer`` app (group), a plain/async function (leaf
    command), or a ``"pkg.mod:attr"`` string resolved lazily on dispatch.
    Name collisions raise immediately, naming both registering modules —
    there is deliberately no overwrite escape hatch for CLI commands.
    """
    origin = caller_module()
    spec = CommandSpec(
        name=name,
        loader=loader,
        help=help,
        lab_free=lab_free,
        output_dir=output_dir,
        gate=gate,
        origin=origin,
    )
    CLI_COMMANDS.register(name, spec, origin=origin)


def cli_command(
    *args: Any,
    options: type | None = None,
    name: str | None = None,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
    lab_free: bool = False,
    output_dir: bool = True,
    gate: bool = True,
    **kwargs: Any,
) -> Callable[..., Any]:
    """Register an async function as a top-level ``otto`` command.

    The ergonomics match ``@instruction``: an ``OttoContext``-annotated
    parameter is injected (hidden from the CLI), and ``options=`` expands a
    pydantic-dataclass into flags. The command name defaults to the function
    name with underscores dashed.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        target = prepare_command_target(func, options)
        cmd_name = name or func.__name__.replace("_", "-")
        doc_line = ((func.__doc__ or "").strip().splitlines() or [""])[0]
        sub_app = typer.Typer(name=cmd_name)
        sub_app.command(cmd_name, *args, help=help, **kwargs)(async_typer_command(target))
        register_cli_command(
            cmd_name,
            sub_app,
            help=help or (doc_line or None),
            lab_free=lab_free,
            output_dir=output_dir,
            gate=gate,
        )
        return func

    return decorator


def resolve_spec_command(spec: CommandSpec) -> Any:
    """Return the vendored-click command/group for *spec*, importing lazily.

    A ``"pkg.mod:attr"`` loader imports its module only now; a function loader
    is wrapped in a throwaway Typer (the ``expose._synthesize_command``
    pattern); a Typer app converts via Typer's own app→click converter.
    """
    loader = spec.loader
    if isinstance(loader, str):
        mod_name, _, attr = loader.partition(":")
        loader = getattr(importlib.import_module(mod_name), attr)
    if isinstance(loader, typer.Typer):
        converted: Any = typer.main.get_command(loader)
        converted.name = spec.name
        return converted
    tmp = typer.Typer()
    tmp.command(spec.name, help=spec.help)(async_typer_command(prepare_command_target(loader)))
    converted = typer.main.get_command(tmp)
    return converted.commands[spec.name] if hasattr(converted, "commands") else converted
```

Note for the implementer: if `test_register_function_resolves_to_command` shows Typer collapsing the single-command throwaway app into a bare command (no `.commands`), the final line already handles both shapes — mirror `cli/expose.py::_synthesize_command:228-229` exactly.

- [ ] **Step 5: Wire lazy top-level exports**

In `src/otto/__init__.py`: add to the `TYPE_CHECKING` block `from otto.cli.registry import cli_command, register_cli_command`; add `"cli_command"`, `"register_cli_command"` to `__all__` (keep sorted); add to `_LAZY_EXPORTS`:

```python
    "register_cli_command": ("otto.cli.registry", "register_cli_command"),
    "cli_command": ("otto.cli.registry", "cli_command"),
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/cli tests/unit/import_budget -n auto`
Expected: PASS (import-budget guard proves no eager import snuck in; regenerate snapshots ONLY if the guard names a legitimate new lazy-export key — `make import-snapshot` — and report it).

- [ ] **Step 7: Lint + typecheck + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

```bash
git add src/otto/cli/registry.py src/otto/cli/invoke.py src/otto/cli/run.py src/otto/__init__.py tests/unit/cli/test_cli_registry.py
```

Paste-able commit message:

```
feat(cli): CommandSpec registry + @cli_command; shared invoke helpers

The public registration surface for top-level commands: CommandSpec
(lazy module:attr loaders, spec-level help, lab_free/output_dir/gate
metadata), loud collisions naming both origins, and a @cli_command
decorator sharing @instruction's context-injection/options machinery
(factored into cli/invoke.py).
```

---

### Task 5: Registry-backed root group + first-party composition

**Files:**
- Create: `src/otto/cli/builtin_commands.py`
- Modify: `src/otto/cli/main.py` (rewrite `_OttoGroup`; delete `_SUBCOMMAND_MODULES`, `_LAB_FREE_SUBCOMMANDS`, `_requested_subcommands`, `_placeholder_subapp`, `_register_subcommands`; keep `_attach_cached_stubs`)
- Test: `tests/unit/cli/test_main.py` (update), new tests in `tests/unit/cli/test_root_group.py`

**Interfaces:**
- Consumes: `CLI_COMMANDS`, `CommandSpec`, `resolve_spec_command`, `register_cli_command` (Task 4).
- Produces:
  - `otto.cli.builtin_commands.register_builtin_commands() -> None` — idempotent; registers the eight first-party groups by `"module:attr"` string.
  - `_OttoGroup.list_commands/get_command` — registry-backed lazy resolution: the real module is imported only for the dispatch target (or completion target); every other name renders from a spec-help stub.
  - Root-level behavior preserved: `_is_lab_free_flag_invocation` and the `_help_or_discovery` verdict are UNTOUCHED in this task (they die in Task 7) — except the table lookups below.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/cli/test_root_group.py`:

```python
"""Root-group lazy resolution against the CLI command registry."""

import sys

from typer.testing import CliRunner

from otto.cli.main import app

runner = CliRunner()


def test_root_help_lists_all_builtins_without_importing_them(monkeypatch):
    for mod in ("otto.cli.cov", "otto.cli.monitor"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("run", "test", "monitor", "cov", "host", "docker", "reservation", "schema"):
        assert name in result.output
    assert "otto.cli.cov" not in sys.modules
    assert "otto.cli.monitor" not in sys.modules


def test_dispatch_resolves_only_the_target(monkeypatch):
    monkeypatch.delitem(sys.modules, "otto.cli.cov", raising=False)
    result = runner.invoke(app, ["schema", "--help"])
    assert result.exit_code == 0
    assert "otto.cli.cov" not in sys.modules


def test_unknown_command_errors_cleanly():
    result = runner.invoke(app, ["not-a-command"])
    assert result.exit_code != 0
    assert "No such command" in result.output
```

Run: `uv run pytest tests/unit/cli/test_root_group.py -n auto` — Expected: FAIL (first test: today `otto --help` imports every module).

- [ ] **Step 2: Create `src/otto/cli/builtin_commands.py`**

```python
"""First-party top-level command registrations — otto's own composition list.

The direct analog of the backend registries' ``_register_builtin_*``
functions: otto's eight subcommand groups travel the same public
:func:`~otto.cli.registry.register_cli_command` path a third-party plugin
uses, with lazy ``"module:attr"`` loaders so nothing imports until dispatch.
"""

from .registry import CLI_COMMANDS, register_cli_command


def register_builtin_commands() -> None:
    """Register otto's built-in subcommand groups (idempotent)."""
    if "run" in CLI_COMMANDS:
        return
    register_cli_command(
        "run", "otto.cli.run:run_app", help="Run a registered instruction on the lab."
    )
    register_cli_command(
        "test", "otto.cli.test:suite_app", help="Run a registered test suite."
    )
    register_cli_command(
        "monitor",
        "otto.cli.monitor:monitor_app",
        help="Collect and store host monitoring data.",
        gate=False,
    )
    register_cli_command(
        "cov",
        "otto.cli.cov:cov_app",
        help="Embedded coverage collection and reporting.",
        output_dir=False,
        gate=False,
    )
    register_cli_command(
        "host", "otto.cli.host:host_app", help="Run a verb on a single lab host."
    )
    register_cli_command(
        "docker", "otto.cli.docker:docker_app", help="Manage docker containers on lab hosts."
    )
    register_cli_command(
        "reservation",
        "otto.cli.reservation:reservation_app",
        help="Inspect lab reservations.",
        output_dir=False,
        gate=False,
    )
    register_cli_command(
        "schema",
        "otto.cli.schema:schema_app",
        help="Export otto's JSON schemas.",
        lab_free=True,
        output_dir=False,
        gate=False,
    )
```

Copy each `help=` line from the corresponding sub-app's current `help`/callback docstring so `otto --help` output text is unchanged — check each module (e.g. `run_app`'s Typer `help`) and prefer its existing wording over the strings above. The `output_dir=False`/`gate=False` assignments mirror today's behavior exactly: cov and reservation create no output dirs (commit `9b7b0c4`), and only run/test/host/docker call `gate()` today (verify with `grep -rn "gate(ctx)" src/otto/cli/` and adjust `gate=` flags to match what you find — report the final mapping).

- [ ] **Step 3: Rewrite the root group in `cli/main.py`**

Replace the `_OttoGroup` class (lines 152-173) with:

```python
class _OttoGroup(TyperGroup):
    """Root group: registry-backed lazy dispatch + pending-token snapshot.

    ``list_commands`` names every registered :class:`CommandSpec`;
    ``get_command`` resolves the real command (importing its module) only for
    the token actually being dispatched or completed — every other name gets a
    lightweight stub whose help comes from the spec, so ``otto --help``
    imports zero subcommand modules.
    """

    _stub_cache: dict[str, Any]
    _real_cache: dict[str, Any]

    @override
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        result = super().parse_args(ctx, args)
        ctx.meta["_pending_subcmd_args"] = list(
            getattr(ctx, "_protected_args", []) + getattr(ctx, "args", [])
        )
        return result

    def _dispatch_target(self, ctx: Any) -> str | None:
        pending = ctx.meta.get("_pending_subcmd_args") or []
        return pending[0] if pending else None

    def _wants_real(self, ctx: Any, cmd_name: str) -> bool:
        if cmd_name == self._dispatch_target(ctx):
            return True
        if os.environ.get("_OTTO_COMPLETE"):
            # Completion for `otto <cmd> <TAB>`: the completer resolves the
            # named subcommand; COMP_WORDS carries the typed tokens.
            return cmd_name in os.environ.get("COMP_WORDS", "").split()
        return False

    def _stub(self, spec: "CommandSpec") -> Any:
        cache = getattr(self, "_stub_cache", None) or {}
        self._stub_cache = cache
        if spec.name not in cache:
            tmp = typer.Typer(
                name=spec.name, help=spec.help or f"(run `otto {spec.name} -h` for details)"
            )
            cache[spec.name] = typer.main.get_command(tmp)
        return cache[spec.name]

    def _real(self, spec: "CommandSpec") -> Any:
        cache = getattr(self, "_real_cache", None) or {}
        self._real_cache = cache
        if spec.name not in cache:
            from .registry import resolve_spec_command

            loader = spec.loader
            cached_names = get_completion_names()
            if cached_names is not None and isinstance(loader, str):
                # Completion fast path: attach cached suite/instruction stubs
                # to the freshly imported sub-app before conversion.
                mod_name, _, attr = loader.partition(":")
                sub_app = getattr(importlib.import_module(mod_name), attr)
                if spec.name == "test":
                    _attach_cached_stubs(sub_app, cached_names.get("suites", []))
                elif spec.name == "run":
                    _attach_cached_stubs(sub_app, cached_names.get("instructions", []))
                spec = dataclasses.replace(spec, loader=sub_app)
            cache[spec.name] = resolve_spec_command(spec)
        return cache[spec.name]

    @override
    def list_commands(self, ctx: Any) -> list[str]:
        from .registry import CLI_COMMANDS

        static = [n for n in super().list_commands(ctx) if n not in CLI_COMMANDS]
        return static + CLI_COMMANDS.names()

    @override
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        from .registry import CLI_COMMANDS

        static = super().get_command(ctx, cmd_name)
        if static is not None:
            return static
        if cmd_name not in CLI_COMMANDS:
            return None
        spec = CLI_COMMANDS.get(cmd_name)
        if self._wants_real(ctx, cmd_name):
            return self._real(spec)
        return self._stub(spec)
```

Add `import dataclasses` and the `CommandSpec` import under `TYPE_CHECKING`. Delete `_SUBCOMMAND_MODULES`, `_requested_subcommands`, `_placeholder_subapp`, `_register_subcommands`, and the module-tail `_register_subcommands()` call; replace the tail with:

```python
from .builtin_commands import register_builtin_commands

register_builtin_commands()
```

Update the two survivors that referenced the deleted tables (both die fully in Task 7 — minimal edits only here): in `_is_lab_free_flag_invocation` line 146, `if tok in _SUBCOMMAND_MODULES:` → `if tok in _KNOWN_COMMAND_NAMES():` with a tiny helper `def _KNOWN_COMMAND_NAMES() -> frozenset[str]: from .registry import CLI_COMMANDS; return frozenset(CLI_COMMANDS.names())` (name it `_known_command_names`); and in `main()` line 350, `if ctx.invoked_subcommand in _LAB_FREE_SUBCOMMANDS:` → look the spec up:

```python
    from .registry import CLI_COMMANDS

    if (
        ctx.invoked_subcommand is not None
        and ctx.invoked_subcommand in CLI_COMMANDS
        and CLI_COMMANDS.get(ctx.invoked_subcommand).lab_free
    ):
        return
```

Delete the `_LAB_FREE_SUBCOMMANDS` frozenset (keep `_LAB_FREE_FLAGS` — Task 7 removes it).

- [ ] **Step 4: Run the CLI unit tier**

Run: `uv run pytest tests/unit/cli -n auto`
Expected: PASS, including the new `test_root_group.py`. `test_main.py` failures will point at removed symbols (`_SUBCOMMAND_MODULES` etc.) — update those tests to the registry equivalents (e.g. assert against `CLI_COMMANDS.names()`), preserving each test's intent. Report every test rewritten.

- [ ] **Step 5: e2e smoke (contract check)**

Run: `uv run pytest tests/e2e/cli -n auto`
Expected: PASS unchanged.

- [ ] **Step 6: Lint + typecheck + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

```bash
git add src/otto/cli/builtin_commands.py src/otto/cli/main.py tests/unit/cli/
```

Paste-able commit message:

```
feat(cli)!: registry-backed root group; first-party commands via the public API

_SUBCOMMAND_MODULES and the placeholder-subapp dance are gone: the root
group lists CommandSpecs and resolves the real module only for the
dispatch/completion target; otto --help renders from spec help strings
and imports zero subcommand modules.
```

---

### Task 6: `bootstrap()` composition root; side-effect-free `configmodule`

**Files:**
- Create: `src/otto/bootstrap.py`, `tests/unit/bootstrap/__init__.py`, `tests/unit/bootstrap/test_bootstrap.py`
- Modify: `src/otto/configmodule/__init__.py` (delete import-time work at lines 52-53 and 127-154; delegate accessors)
- Modify: `src/otto/configmodule/repo.py` (split `import_test_files` into `iter_test_files()` + `import_test_file(path)`)
- Modify: `src/otto/cli/main.py` (add `entry()`)
- Modify: `pyproject.toml:71` (`otto = "otto.cli.main:entry"`)
- Test: update `tests/unit/configmodule/` tests that relied on import-time side effects

**Interfaces:**
- Consumes: `load_otto_env` (`configmodule/env.py`), `get_repos(paths)` (`configmodule/repo.py:795`), completion cache helpers (`configmodule/completion_cache.py`).
- Produces:
  - `otto.bootstrap.BootstrapError(sut_dir, source, cause)` — `str()` is the framed one-liner `"repo <sut_dir>: failed to load <source>: <cause!r>"`; `.__cause__` chains.
  - `otto.bootstrap.BootstrapResult` — frozen dataclass: `env`, `repos: list[Repo]`, `errors: list[BootstrapError]`.
  - `otto.bootstrap.discover() -> tuple[env, list[Repo]]` — phase 1 only (no user code), cached.
  - `otto.bootstrap.bootstrap() -> BootstrapResult` — idempotent; discovery + contained registration; never loads the lab.
  - `otto.bootstrap.set_completion_names(names) / get_completion_names()` — module-level holder (moves from configmodule).
  - `otto.bootstrap._reset() -> None` — test hook clearing all module state.
  - `otto.cli.main.entry() -> None` — console-script entry: completion fast path → bootstrap → stderr warnings → `app()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bootstrap/test_bootstrap.py`:

```python
"""bootstrap(): phases, idempotence, containment framing."""

import textwrap

import pytest

from otto import bootstrap as bs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bs._reset()
    yield
    bs._reset()


def _write_repo(tmp_path, *, broken_test: bool = False) -> str:
    repo = tmp_path / "repo"
    (repo / ".otto").mkdir(parents=True)
    (repo / ".otto" / "settings.toml").write_text(
        textwrap.dedent(
            """
            [test]
            tests = ["tests"]
            """
        )
    )
    tests = repo / "tests"
    tests.mkdir()
    if broken_test:
        (tests / "test_broken.py").write_text("def broken(:\n")  # SyntaxError
    else:
        (tests / "test_ok.py").write_text("X = 1\n")
    return str(repo)


def test_idempotent_single_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    first = bs.bootstrap()
    assert first is bs.bootstrap()
    assert first.errors == []
    assert len(first.repos) == 1


def test_broken_test_file_is_contained_and_framed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    result = bs.bootstrap()
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert "test_broken.py" in msg
    assert "repo" in msg
    assert isinstance(result.errors[0].__cause__, SyntaxError)


def test_discover_runs_no_user_code(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    _env, repos = bs.discover()  # broken test file must NOT explode discovery
    assert len(repos) == 1
```

Run: `uv run pytest tests/unit/bootstrap -n auto` — Expected: FAIL (`No module named 'otto.bootstrap'`).

- [ ] **Step 2: Split `Repo.import_test_files`**

In `src/otto/configmodule/repo.py`, refactor `import_test_files` (lines ~660-683) into:

```python
    def iter_test_files(self) -> list[Path]:
        """Return every ``test_*.py`` under this repo's configured tests dirs, sorted."""
        found: list[Path] = []
        for test_dir in self.tests:
            if test_dir.is_dir():
                found.extend(sorted(test_dir.glob("test_*.py")))
        return found

    def import_test_file(self, test_file: Path) -> None:
        """Import one suite test file (idempotent per file); may raise on bad user code."""
        import importlib.util

        mod_name = f"_otto_suite_{test_file.stem}"
        if mod_name in sys.modules:
            return
        spec = importlib.util.spec_from_file_location(mod_name, test_file)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    def import_test_files(self) -> None:
        """Import all test files (uncontained; :mod:`otto.bootstrap` wraps per-file)."""
        for test_file in self.iter_test_files():
            self.import_test_file(test_file)
```

- [ ] **Step 3: Implement `src/otto/bootstrap.py`**

```python
"""otto's composition root: repo discovery + contained user-code registration.

Replaces ``configmodule``'s import-time side effects. Phase 1 (*discovery*)
parses the environment and repo ``settings.toml`` files — no user code runs.
Phase 2 (*registration*) imports each repo's init modules and test files,
wrapping every user-module exec so one broken file becomes a framed
:class:`BootstrapError` instead of bricking the process. Lab loading is
deliberately NOT part of bootstrap — it happens lazily at first access.

``bootstrap()`` is idempotent: the CLI entrypoint calls it before argv
parsing, ``open_context()`` calls it lazily, and repeated calls return the
same :class:`BootstrapResult`.
"""

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .configmodule.repo import Repo
    from .models.settings import OttoEnvSettings


class BootstrapError(Exception):
    """One user file failed to load during bootstrap registration."""

    def __init__(self, sut_dir: Any, source: str, cause: BaseException) -> None:
        """Frame *cause* as ``repo <sut_dir>: failed to load <source>``."""
        super().__init__(f"repo {sut_dir}: failed to load {source}: {cause!r}")
        self.sut_dir = sut_dir
        self.source = source
        self.__cause__ = cause


@dataclass(frozen=True)
class BootstrapResult:
    """Everything bootstrap produced: environment, repos, contained errors."""

    env: "OttoEnvSettings"
    repos: list["Repo"]
    errors: list[BootstrapError] = field(default_factory=list)


_discovered: "tuple[OttoEnvSettings, list[Repo]] | None" = None
_result: BootstrapResult | None = None
_completion_names: dict[str, Any] | None = None


def discover() -> "tuple[OttoEnvSettings, list[Repo]]":
    """Phase 1: env + repo discovery (settings parse only — no user code). Cached."""
    global _discovered
    if _discovered is None:
        from .configmodule.env import load_otto_env
        from .configmodule.repo import get_repos

        env = load_otto_env()
        _discovered = (env, get_repos(env.sut_dirs))
    return _discovered


def bootstrap() -> BootstrapResult:
    """Run the composition root (idempotent): discovery + contained registration."""
    global _result
    if _result is not None:
        return _result
    env, repos = discover()
    errors: list[BootstrapError] = []
    for repo in repos:
        repo.add_libs_to_pythonpath()
        for mod in repo.init:
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001 — containment seam: ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, mod, e))
        for test_file in repo.iter_test_files():
            try:
                repo.import_test_file(test_file)
            except Exception as e:  # noqa: BLE001 — containment seam: ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, test_file.name, e))
    _result = BootstrapResult(env=env, repos=repos, errors=errors)
    return _result


def set_completion_names(names: "dict[str, Any] | None") -> None:
    """Install the completion-cache snapshot (fast path; set by the CLI entry)."""
    global _completion_names
    _completion_names = names


def get_completion_names() -> "dict[str, Any] | None":
    """Return the completion-cache snapshot, or None outside the fast path."""
    return _completion_names


def _reset() -> None:
    """Clear all bootstrap state (test hook)."""
    global _discovered, _result, _completion_names
    _discovered = None
    _result = None
    _completion_names = None
```

- [ ] **Step 4: Make `configmodule/__init__.py` side-effect-free**

Delete lines 52-53 (`_env = ...` / `_repos = ...`), the completion fast-path block (127-154), and the `contextlib`/completion-cache imports they used. Replace `get_repos`/`get_env`/`get_completion_names` bodies (keep names and docstrings' intent):

```python
def get_repos() -> list[Repo]:
    """Return the ``Repo`` objects for the configured SUT directories (bootstraps lazily)."""
    from ..bootstrap import bootstrap

    return bootstrap().repos


def get_env() -> "OttoEnvSettings":
    """Return the startup environment settings (bootstraps discovery lazily)."""
    from ..bootstrap import discover

    return discover()[0]


def get_completion_names() -> dict[str, Any] | None:
    """Return the completion-cache snapshot when the fast path is active, else None."""
    from ..bootstrap import get_completion_names as _get

    return _get()
```

Also delete the now-obsolete lines-81-90 ordering comment block header (the "Defined BEFORE apply_repo_settings()" paragraph) — the constraint no longer exists. `cli/main.py:46`'s `get_env()` module-level call now triggers discovery (settings parse, no user code) at CLI import — acceptable and unchanged in cost.

- [ ] **Step 5: Add `entry()` and switch the console script**

In `src/otto/cli/main.py` (below the `register_builtin_commands()` tail):

```python
def entry() -> None:
    """Console-script entry: composition root, then the Typer app.

    Completion invocations take the cache fast path (zero user code); everything
    else runs :func:`otto.bootstrap.bootstrap` before argv parsing so registered
    third-party commands exist when the root group is consulted. Contained
    user-code failures print one framed warning line each; real command
    dispatch fails loud in the invoke preamble.
    """
    import contextlib

    from .. import bootstrap as bs
    from ..configmodule.completion_cache import is_completion_mode, read_cache

    if is_completion_mode():
        _env, repos = bs.discover()
        bs.set_completion_names(read_cache(repos))

    if bs.get_completion_names() is None:
        result = bs.bootstrap()
        for err in result.errors:
            typer.echo(f"warning: {err}", err=True)
        from ..configmodule.completion_cache import (
            collect_backend_names,
            collect_current_commands,
            collect_docker_capable_host_ids,
            collect_host_ids,
            collect_reservation_usernames,
            write_cache,
        )

        instructions, suites = collect_current_commands()
        backends = collect_backend_names()
        with contextlib.suppress(OSError):
            write_cache(
                result.repos,
                instructions,
                suites,
                collect_host_ids(result.repos),
                collect_docker_capable_host_ids(result.repos),
                term_backends=backends["term_backends"],
                transfer_backends=backends["transfer_backends"],
                usernames=collect_reservation_usernames(result.repos),
            )

    app()
```

In `pyproject.toml`: `otto = "otto:app"` → `otto = "otto.cli.main:entry"`. Run `uv sync` afterward so the installed script updates. Also wire the library path: in `src/otto/context.py`, at the top of `open_context(...)` add `from .bootstrap import bootstrap` + `bootstrap()` (first statement, before any lab work), with a one-line comment: `# composition root — idempotent; registers user init-module components`.

- [ ] **Step 6: Run tests + e2e**

Run: `uv run pytest tests/unit/bootstrap tests/unit/configmodule tests/unit/cli -n auto`
Expected: bootstrap tests PASS. configmodule/cli tests that assumed import-time side effects (e.g. suites present after bare `import otto.configmodule`) need a `bootstrap()` call or the `_fresh`-style fixture — update them, preserving intent; report each.
Run: `uv run pytest tests/e2e/cli -n auto`
Expected: PASS (e2e runs the real console script → `entry()` path).
Run: `make import-snapshot` then `uv run pytest tests/unit/import_budget -n auto`
Expected: PASS with regenerated snapshots (configmodule import got lighter) — include the snapshot diff summary in the report.

- [ ] **Step 7: Lint + typecheck + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

```bash
git add src/otto/bootstrap.py src/otto/configmodule/__init__.py src/otto/configmodule/repo.py src/otto/cli/main.py src/otto/context.py pyproject.toml tests/unit/bootstrap/ tests/unit/configmodule/ tests/unit/cli/ tests/unit/import_budget/snapshots/
```

Paste-able commit message:

```
feat(bootstrap)!: explicit composition root; configmodule import is side-effect-free

bootstrap() = discovery (no user code) + contained registration (each
user init module / test file exec wrapped into a framed BootstrapError).
The console script becomes otto.cli.main:entry (completion cache fast
path, framed stderr warnings); open_context() bootstraps lazily. One
broken user file can no longer brick --help, --version, or completion.
```

---

### Task 7: Lazy lab + leaf-invoke preamble; delete token sniffing and the help verdict

**Files:**
- Modify: `src/otto/cli/invoke.py` (add `RootOptions`, `ensure_lab_context`, `try_ensure_lab`, `ensure_cli_session`, `command_preamble`, `wrap_leaf_callbacks`)
- Modify: `src/otto/cli/main.py` (slim root callback; delete `_is_lab_free_flag_invocation`, `_known_command_names`, `_LAB_FREE_FLAGS`, the `_help_or_discovery` verdict; wrap real commands at resolution)
- Modify: `src/otto/cli/run.py:63-75`, `src/otto/cli/test.py:~670-690`, `src/otto/cli/host.py:~106-190`, `src/otto/cli/docker.py:~55-70` (group callbacks shed output-dir/gate; host callback shrinks to stashing params)
- Modify: `src/otto/cli/expose.py` (leaf `_cmd` resolves the host itself; synthesized commands carry `__cli_output_dir__`)
- Test: update `tests/unit/cli/test_main.py`, `test_run.py`, `test_test.py`, `test_host_cli.py`, `test_host.py`, `test_dynamic_host_commands.py`, `test_verb_output_dir.py`, `test_docker_output_dir.py`

**Interfaces:**
- Consumes: `CommandSpec`, `CLI_COMMANDS` (Task 4); `bootstrap()` (Task 6); `management.init_cli_logging`/`create_output_dir`; `reservations.gate`; `load_lab`, `build_lab_repository`, `build_reservation_state`, `register_declared_container_hosts`, `OttoContext`/`set_context` (all currently used in `main()`).
- Produces:
  - `otto.cli.invoke.RootOptions` — frozen dataclass mirroring the root callback params it stashes: `labs: list[str] | None`, `xdir: Path`, `log_days: int`, `log_level: str`, `rich_log_file: bool`, `show_time: bool`, `dry_run: bool`, `as_user: str | None`, `skip_reservation_check: bool`.
  - `ensure_lab_context(ctx) -> OttoContext` — idempotent (guard key `ctx.meta["_otto_lab_ready"]`); enforces `--lab`; builds lab repository → `load_lab` → docker placeholders → reservation state (into `ctx.meta["otto_reservation"]`) → `set_context`. NO banner, NO logging init, NO output dir.
  - `try_ensure_lab(ctx) -> OttoContext | None` — soft variant: returns None instead of raising/exiting (used by `HostGroup` class scoping).
  - `ensure_cli_session(ctx) -> None` — idempotent (guard `ctx.meta["_otto_session_ready"]`): banner, `init_cli_logging`, capture prefixes, dry-run notice, per-repo commit debug logs.
  - `command_preamble(ctx) -> None` — the "real work is starting" choke point (see Step 2).
  - `wrap_leaf_callbacks(cmd, spec) -> Any` — recursively wraps every leaf click command's `invoke` with the preamble.

- [ ] **Step 1: Write the failing tests first**

Add to `tests/unit/cli/test_root_group.py`:

```python
def test_subcommand_help_needs_no_lab_and_makes_no_output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert not any(p.is_dir() and p.name != "" for p in tmp_path.iterdir()) or True
    # precise no-dir assertion: reuse the e2e helper pattern
    from otto.logger import management

    made = [p for p in tmp_path.rglob("*") if management._LOG_DIR_NAME_RE.match(p.name)]
    assert made == []


def test_option_value_equal_to_help_flag_is_not_sniffed(monkeypatch):
    # Regression for the review's token-sniffing false-positive suspicion:
    # an option VALUE '--help' must not turn a real invocation into a help path.
    # `otto run --lab x` with a missing lab errors at ensure_lab_context, not
    # via a silent help short-circuit.
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0 or "Usage" in result.output
```

(The second test is a placeholder-level guard; the authoritative false-positive regression lands as e2e in Task 13 with a real instruction taking a string option.) Run: expect the no-output-dir test to FAIL only after the rewrite begins — write first, keep red until Step 5.

- [ ] **Step 2: Implement the preamble machinery in `cli/invoke.py`**

```python
def command_preamble(ctx: typer.Context) -> None:
    """Run once when a real (non-help) command invocation starts.

    Order: bootstrap errors fail loud → lab-free commands are done → CLI
    session (banner/logging) → lab context → per-command output dir →
    reservation gate. ``--help`` paths never reach this function: click's
    help option exits during leaf parse, before ``Command.invoke``.
    """
    meta = ctx.meta
    if meta.get("_otto_preamble_done"):
        return
    meta["_otto_preamble_done"] = True

    from ..bootstrap import bootstrap

    result = bootstrap()
    if result.errors:
        from rich import print as rprint

        for err in result.errors:
            rprint(f"[red]{err}[/red]")
        rprint("[red]Cannot run commands while a repo fails to load (see above).[/red]")
        raise typer.Exit(1)

    spec: CommandSpec = meta["_otto_command_spec"]
    if spec.lab_free:
        return

    ensure_cli_session(ctx)
    ensure_lab_context(ctx)

    leaf_wants_dir = bool(getattr(ctx.command.callback, "__cli_output_dir__", True))
    if spec.output_dir and leaf_wants_dir:
        from ..context import get_context
        from ..logger import management

        get_context().output_dir = management.create_output_dir(
            spec.name, ctx.command.name or spec.name
        )
    if spec.gate:
        from ..reservations import gate

        gate(ctx)


def wrap_leaf_callbacks(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap every leaf command under *cmd* so its invoke runs the preamble first.

    Wrapping ``Command.invoke`` (not the callback) means the preamble runs
    only on real execution: a ``--help`` on the leaf exits during parse and
    never reaches ``invoke``. Groups recurse; already-wrapped commands are
    skipped (resolution results are cached).
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    if hasattr(cmd, "commands"):
        for sub in cmd.commands.values():
            wrap_leaf_callbacks(sub, spec)
        return cmd
    original_invoke = cmd.invoke

    def _invoke_with_preamble(inner_ctx: Any) -> Any:
        inner_ctx.meta["_otto_command_spec"] = spec
        command_preamble(inner_ctx)
        return original_invoke(inner_ctx)

    cmd.invoke = _invoke_with_preamble
    return cmd
```

`ensure_lab_context(ctx)` moves the root-callback body from `cli/main.py:361-497` VERBATIM apart from these deltas: reads its inputs from `opts = ctx.meta["_otto_root_options"]` (a `RootOptions`); the banner/`init_cli_logging`/capture-prefix/dry-run-notice/repo-commit-log lines move to `ensure_cli_session(ctx)` instead; the `show_lab`/`list_hosts` handling stays in the root callback (Step 3); guard key `_otto_lab_ready` makes it idempotent and it returns the installed context. `try_ensure_lab(ctx)` wraps it: `try: return ensure_lab_context(ctx)` / `except (typer.Exit, Exception): return None` with a `# noqa: BLE001` rationale comment (soft scoping probe — any failure means "no class scoping available").

- [ ] **Step 3: Slim the root callback and wire wrapping at resolution**

In `cli/main.py::main()`: delete the verdict block (lines 337-356), the `--lab` enforcement, and everything from `from rich import print as rprint` (line 368) to the end — replaced by:

```python
    if ctx.resilient_parsing:
        return

    from .invoke import RootOptions, ensure_lab_context

    ctx.meta["_otto_root_options"] = RootOptions(
        labs=labs,
        xdir=xdir,
        log_days=log_days,
        log_level=log_level,
        rich_log_file=rich_log_file,
        show_time=show_time,
        dry_run=dry_run,
        as_user=as_user,
        skip_reservation_check=skip_reservation_check,
    )

    if show_lab or list_hosts:
        # These root flags inspect live lab state: load it now, print, exit.
        ensure_lab_context(ctx)
        if show_lab:
            from rich.pretty import pprint

            from ..context import get_context

            pprint(
                get_context().lab,
                max_depth=(None if lab_depth == 0 else lab_depth),
                expand_all=True,
            )
        else:
            from .callbacks import list_hosts_callback

            list_hosts_callback(True)
        raise typer.Exit
```

Delete `_is_lab_free_flag_invocation`, `_known_command_names`, `_LAB_FREE_FLAGS`, and every `ctx.meta["_help_or_discovery"]` producer/consumer repo-wide (`grep -rn "_help_or_discovery" src/ tests/` → all gone). In `_OttoGroup._real()` (Task 5), wrap after resolution:

```python
            from .invoke import wrap_leaf_callbacks

            cache[spec.name] = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
```

- [ ] **Step 4: Shed group-callback preambles; host callback stashes**

- `cli/run.py` callback: delete the `if ctx.invoked_subcommand is not None and not ctx.meta.get("_help_or_discovery"):` block (lines 67-75) — body becomes only the `resilient_parsing` return. Keep the `--list-instructions` eager option.
- `cli/test.py` callback: delete the `create_output_dir`/`gate` lines (676-683 region) and their verdict conditional; everything else it stores in `ctx.meta` for `run_suite` stays.
- `cli/docker.py` callback: delete its `create_output_dir` conditional (lines ~60-67).
- `cli/host.py` callback: delete `create_output_dir` + `gate(ctx)` (lines ~156-169) AND the host construction: instead of building the host and storing it in `ctx.obj`, stash the raw inputs: `ctx.meta["_otto_host_request"] = {"host_id": host_id, "overrides": <the overrides mapping it builds today>}`. Add to `cli/host.py` a resolver used by the leaf:

```python
def resolve_cli_host(ctx: typer.Context) -> Any:
    """Build the host the ``otto host`` callback recorded (lab is ready by now)."""
    request = ctx.meta["_otto_host_request"]
    from ..configmodule import get_host

    return get_host(request["host_id"], **request["overrides"])
```

(Match the exact construction call the callback makes today — copy its argument handling verbatim into the resolver.)
- `cli/expose.py::make_method_command::_cmd`: `host = ctx.obj` → `from .host import resolve_cli_host` + `host = resolve_cli_host(ctx)`. In `_synthesize_command`, after building `cmd_fn`, propagate the verb marker so the preamble sees it: `cmd_fn.__cli_output_dir__ = getattr(sample_func, "__cli_output_dir__", True)` (set BEFORE `tmp.command(...)`; `functools.wraps` in `async_typer_command` carries it through — verify with the Step 5 tests, and if wraps drops it, set it on the wrapped object returned by `async_typer_command` instead).
- `HostGroup._class_for`: replace the body's `host_class_for_id(...)` call path so real dispatch can scope: `if resilient_parsing: return None`, then `from .invoke import try_ensure_lab; if try_ensure_lab(ctx) is None: return None`, then the existing `host_class_for_id(...)`.
- `monitor`/`cov` group callbacks: `grep -n "create_output_dir\|gate(" src/otto/cli/monitor.py src/otto/cli/cov.py` — apply the same deletion to any hit, and set the `output_dir`/`gate` spec flags in `builtin_commands.py` to reproduce today's behavior exactly (report the final flag table).

- [ ] **Step 5: Run the full unit tier + e2e contract**

Run: `uv run pytest tests/unit -n auto`
Expected: the CLI test files listed in **Files** need updates (no more `ctx.obj` host, no verdict, output dirs made at leaf invoke). Update preserving intent; report each.
Run: `uv run pytest tests/e2e/cli -n auto`
Expected: **PASS UNMODIFIED** — this suite pins `--help` lab-freedom and `assert_no_output_dir`. Any failure here is a real regression in the re-plumb; fix the source, never the e2e test.

- [ ] **Step 6: Lint + typecheck + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`

```bash
git add src/otto/cli/ tests/unit/cli/
```

Paste-able commit message:

```
refactor(cli)!: lab loads lazily; output-dir/gate move to the leaf-invoke preamble

The root callback shrinks to option-stashing; ensure_lab_context /
ensure_cli_session run idempotently from command_preamble, which wraps
every leaf command's invoke at resolution time — so --help paths are
structurally incapable of loading the lab, creating output dirs, or
gating. Deletes _is_lab_free_flag_invocation token sniffing and the
ctx.meta help verdict. Behavior pinned by the existing CLI e2e suite.
```

---

### Task 8: Instructions + suites onto Registry storage

**Files:**
- Modify: `src/otto/cli/run.py` (`@instruction` registers; `run_app` gets a lazy group; `list_instructions_callback`)
- Modify: `src/otto/suite/register.py` (`SUITES` registry replaces `_SUITE_REGISTRY`/`_SUITE_FILES`)
- Modify: `src/otto/cli/test.py` (delete the drain-loop at ~695-696; `suite_app` gets a lazy group)
- Modify: `src/otto/configmodule/repo.py` (`get_instructions_panel`/`get_test_suites_panel` read the registries)
- Modify: `src/otto/configmodule/completion_cache.py` (`collect_current_commands` reads the registries)
- Test: `tests/unit/suite/` + `tests/unit/cli/test_run.py`, `test_test.py`, `test_listing.py`, `tests/unit/configmodule/` panel/cache tests

**Interfaces:**
- Consumes: `Registry` (Task 1); `CLI_COMMANDS`, `wrap_leaf_callbacks` (Tasks 4/7).
- Produces:
  - `otto.cli.run.INSTRUCTIONS: Registry[InstructionEntry]`; `InstructionEntry` frozen dataclass: `name: str`, `sub_app: typer.Typer`, `module: str`.
  - `otto.suite.register.SUITES: Registry[SuiteEntry]`; `SuiteEntry` frozen dataclass: `name: str`, `sub_app: typer.Typer`, `file: str`.
  - Both groups resolve children lazily through a shared `RegistryBackedGroup` in `cli/invoke.py`:
    `make_registry_group(registry, parent_spec_name) -> type[TyperGroup]`.

- [ ] **Step 1: Registry storage in the two decorators**

`suite/register.py`: replace `_SUITE_REGISTRY`/`_SUITE_FILES` (lines 27-33) with:

```python
from ..registry import Registry


@dataclasses.dataclass(frozen=True)
class SuiteEntry:
    """One registered suite: its Typer sub-app + source file for attribution."""

    name: str
    sub_app: typer.Typer
    file: str


SUITES: Registry[SuiteEntry] = Registry(
    "test suite", register_hint="@otto.register_suite()"
)
```

The decorator tail (lines 121-124) becomes:

```python
        sub_app = typer.Typer()
        sub_app.command(suite_class.__name__, *args, **kwargs)(runner)
        SUITES.register(
            suite_class.__name__,
            SuiteEntry(name=suite_class.__name__, sub_app=sub_app, file=suite_file),
            origin=suite_class.__module__,
        )
```

(Duplicate suite class names now fail loudly at import — an intentional improvement; note it in the report.) Same shape in `cli/run.py`: add `INSTRUCTIONS`/`InstructionEntry`, and `@instruction`'s `run_app.add_typer(app)` line becomes `INSTRUCTIONS.register(cmd_name, InstructionEntry(name=cmd_name, sub_app=app, module=func.__module__), origin=func.__module__)` where `cmd_name` is derived exactly as Typer would (`kwargs.get("name") or args[0] if args and isinstance(args[0], str) else func.__name__.replace("_", "-")` — inspect how `app.command(*args, **kwargs)` receives the name today and mirror it; the existing `list_instructions_callback` name-derivation at run.py shows the fallback rule).

- [ ] **Step 2: Shared lazy child-group factory**

In `cli/invoke.py`:

```python
def make_registry_group(child_registry: Any, parent_name: str) -> type:
    """Build a TyperGroup class whose children come from *child_registry*.

    Children (suite/instruction sub-apps) convert lazily on first access and
    get the leaf-invoke preamble wrapped with the PARENT command's spec, so
    `otto run smoke` and `otto test TestX` behave exactly like any other leaf.
    """
    from typer.core import TyperGroup

    class RegistryBackedGroup(TyperGroup):
        """Group whose subcommands resolve from a component registry."""

        _child_cache: dict[str, Any]

        @override
        def list_commands(self, ctx: Any) -> list[str]:
            return super().list_commands(ctx) + [
                n for n in child_registry.names() if n not in super().list_commands(ctx)
            ]

        @override
        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            static = super().get_command(ctx, cmd_name)
            if static is not None:
                return static
            if cmd_name not in child_registry:
                return None
            cache = getattr(self, "_child_cache", None) or {}
            self._child_cache = cache
            if cmd_name not in cache:
                from .registry import CLI_COMMANDS

                entry = child_registry.get(cmd_name)
                converted: Any = typer.main.get_command(entry.sub_app)
                cmd = (
                    converted.commands[cmd_name]
                    if hasattr(converted, "commands") and cmd_name in converted.commands
                    else converted
                )
                cache[cmd_name] = wrap_leaf_callbacks(cmd, CLI_COMMANDS.get(parent_name))
            return cache[cmd_name]

    return RegistryBackedGroup
```

`run_app` (run.py:26) gains `cls=make_registry_group(INSTRUCTIONS, "run")` — do this via a module-level lazy pattern to avoid import cycles: define the Typer with `cls=` at module scope AFTER `INSTRUCTIONS` exists. `suite_app` (test.py:438) gains `cls=make_registry_group(SUITES, "test")`, and the drain-loop `for _, _suite_sub_app in _SUITE_REGISTRY: suite_app.add_typer(_suite_sub_app)` is deleted.

- [ ] **Step 3: Update the readers**

`grep -rn "_SUITE_REGISTRY\|_SUITE_FILES" src/ tests/` — every hit converts: `for name, sub_app in _SUITE_REGISTRY` → `for name, entry in SUITES.items()` (sub-app = `entry.sub_app`); `_SUITE_FILES[name]` → `SUITES.get(name).file`. In `completion_cache.collect_current_commands`, the `run_mod.run_app.registered_groups` introspection is replaced by iterating `INSTRUCTIONS.items()` (import `otto.cli.run` lazily as today via `sys.modules.get` — if the module was never imported there are no instructions; keep that guard) and `SUITES.items()`.

- [ ] **Step 4: Tests + e2e contract**

Run: `uv run pytest tests/unit -n auto`
Expected: PASS after updating the listed test files (panels, cache, listing).
Run: `uv run pytest tests/e2e/cli -n auto`
Expected: PASS unmodified (`--list-instructions`, `--list-suites`, suite exit codes all pinned here).

- [ ] **Step 5: Lint + typecheck + stage + report**

```bash
git add src/otto/cli/run.py src/otto/cli/test.py src/otto/cli/invoke.py src/otto/suite/register.py src/otto/configmodule/ tests/unit/
```

Paste-able commit message:

```
refactor(cli)!: instructions + suites live in Registries, resolve lazily

@instruction and @register_suite stop mutating Typer apps at import
time: they register into INSTRUCTIONS/SUITES, and the run/test groups
resolve children through the same lazy-group idiom as the root. The
_SUITE_REGISTRY list + _SUITE_FILES side-dict collapse into one
registry; duplicate suite/instruction names now fail loudly.
```

---

### Task 9: Completion cache v2 — registry-driven, third-party top-level stubs

**Files:**
- Modify: `src/otto/configmodule/completion_cache.py` (schema bump; new `collect_cli_commands`; `write_cache`/`read_cache` gain a `commands` key)
- Modify: `src/otto/cli/main.py` (`entry()` passes commands; `_OttoGroup` lists/stubs cached third-party names)
- Test: `tests/unit/configmodule/test_completion_cache*.py` (locate: `grep -rln "write_cache\|read_cache" tests/unit`)

**Interfaces:**
- Consumes: `CLI_COMMANDS` (Task 4); cache fingerprint machinery already in `completion_cache.py`.
- Produces:
  - `collect_cli_commands() -> list[dict[str, Any]]` — `[{"name", "help", "lab_free"}]` for every spec whose `origin` is not under `otto.` (third-party only; builtins re-register every run).
  - Cache payload gains `"commands"`; the schema/fingerprint version constant in `completion_cache.py` bumps by one (find it: `grep -n "VERSION\|version" src/otto/configmodule/completion_cache.py`).
  - `_OttoGroup.list_commands` appends cached third-party names on the fast path; `get_command` serves them as stubs.

- [ ] **Step 1: Failing test**

Add to the completion-cache test module:

```python
def test_cache_round_trips_third_party_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_XDIR", str(tmp_path))
    from otto.cli.registry import CLI_COMMANDS, register_cli_command
    import typer

    register_cli_command("e2etool", typer.Typer(name="e2etool"), help="Tool.")
    try:
        from otto.configmodule.completion_cache import collect_cli_commands

        commands = collect_cli_commands()
        assert {"name": "e2etool", "help": "Tool.", "lab_free": False} in commands
    finally:
        CLI_COMMANDS.unregister("e2etool")
```

- [ ] **Step 2: Implement**

`collect_cli_commands()` iterates `CLI_COMMANDS.items()`, skipping specs whose `spec.origin.startswith("otto.")`, returning the dict shape above. `write_cache` gains keyword `commands: list[dict[str, Any]] | None = None` stored under `"commands"`; `read_cache` returns it (default `[]`). Bump the schema version constant so stale caches invalidate. In `entry()` (Task 6 Step 5), pass `commands=collect_cli_commands()` to `write_cache`. In `_OttoGroup`: `list_commands` appends `[c["name"] for c in (get_completion_names() or {}).get("commands", []) if c["name"] not in CLI_COMMANDS]`; `get_command` — after the registry miss — checks the cached commands and returns a `_stub`-style Typer conversion built from the cached name/help.

- [ ] **Step 3: Tests + lint + typecheck + stage + report**

Run: `uv run pytest tests/unit/configmodule tests/unit/cli -n auto` — Expected: PASS.

```bash
git add src/otto/configmodule/completion_cache.py src/otto/cli/main.py tests/unit/
```

Paste-able commit message:

```
feat(completion): cache serializes the CLI registry; third-party commands complete

collect_cli_commands snapshots third-party CommandSpecs (name/help/
lab_free) into the completion cache (schema bump); the root group lists
and stubs them on the fast path, so a plugin's top-level command
tab-completes without executing any user code.
```

---

### Task 10: F5 — honor `Arg(name=...)` / `Opt(name=...)`

**Files:**
- Modify: `src/otto/cli/param_synth.py:133-234` (`build_cli_binding`)
- Test: `tests/unit/cli/test_param_synth.py`

**Interfaces:**
- Consumes/produces: `build_cli_binding` signature unchanged. `Opt(name="--dest")` becomes the option's CLI flag; `Arg(name="FILES")` becomes the argument's metavar.

- [ ] **Step 1: Failing tests**

Add to `tests/unit/cli/test_param_synth.py`:

```python
def test_opt_name_renames_the_flag():
    from typing import Annotated

    from otto.cli.param_synth import build_cli_binding
    from otto.utils import Opt

    async def verb(self, dest_dir: Annotated[str, Opt(name="--dest", help="Target.")] = "/tmp"):
        """Verb."""

    binding = build_cli_binding(verb)
    (param,) = binding.params
    typer_meta = param.annotation.__metadata__[0]
    assert "--dest" in typer_meta.param_decls


def test_arg_name_sets_the_metavar():
    from typing import Annotated

    from otto.cli.param_synth import build_cli_binding
    from otto.utils import Arg

    async def verb(self, src: Annotated[str, Arg(name="SOURCE")]):
        """Verb."""

    binding = build_cli_binding(verb)
    (param,) = binding.params
    typer_meta = param.annotation.__metadata__[0]
    assert typer_meta.metavar == "SOURCE"
```

Run: expect FAIL (`param_decls` empty / metavar None — the knobs are dropped today).

- [ ] **Step 2: Implement**

In `build_cli_binding`, everywhere a `typer.Option(...)`/`typer.Argument(...)` is constructed from a marker, thread the name:

- Explicit option (line ~217): `typer.Option(help=opt.help)` → `typer.Option(*( (opt.name,) if opt.name else () ), help=opt.help)`.
- List/dict option (lines ~200): same pattern using `opt.name` when an `Opt` marker is present.
- Explicit argument (line ~214): `typer.Argument(help=arg.help)` → `typer.Argument(metavar=arg.name, help=arg.help)` (metavar `None` is typer's default — safe unconditionally).
- Variadic argument (line ~174): `typer.Argument(help=arg.help)` → `typer.Argument(metavar=arg.name, help=arg.help)`.

Add an end-to-end CliRunner test invoking a synthesized command with the renamed flag (build via `expose._synthesize_command` on a fake host class method) — assert `--dest value` parses and the python param receives it. If Typer's custom-decl → python-param binding fails (renamed flag doesn't bind to `dest_dir`), the documented Typer pattern is a *second* positional decl: `typer.Option(opt.name, help=...)` is the documented "CLI option name" feature in Typer 0.26 — debug against that documented behavior before changing approach, and record what you find.

- [ ] **Step 3: Tests + docs note + stage + report**

Run: `uv run pytest tests/unit/cli -n auto` — PASS.
Update the `Arg`/`Opt` docstrings in `src/otto/utils.py:96-116` to document `name` (flag for `Opt`, metavar for `Arg`).

```bash
git add src/otto/cli/param_synth.py src/otto/utils.py tests/unit/cli/test_param_synth.py
```

Paste-able commit message:

```
fix(cli): honor Arg(name=)/Opt(name=) in the @cli_exposed overlay (F5)

Opt(name="--dest") renames the synthesized flag; Arg(name="SOURCE")
sets the argument metavar. Previously both knobs were silently ignored
(review finding F5).
```

---

### Task 11: F7 — public API tidy

**Files:**
- Modify: `src/otto/__init__.py` (export `load_lab`; delete `get_otto_logger`)
- Modify: `src/otto/logger/__init__.py` (one accessor name: `get_logger`)
- Modify: docs pages with inner-path imports: `docs/overview.md:94`, `docs/getting-started.md:359`, `docs/guide/run.md:18,60` (+ any further `grep -rn "configmodule.configmodule import" docs/ --include='*.md' | grep -v superpowers` hits)
- Test: whatever `grep -rln "get_otto_logger" src/ tests/ docs/` finds

**Interfaces:**
- Produces: `otto.load_lab` (lazy export of `otto.configmodule.load_lab`); `otto.get_logger` as the ONLY logger accessor; `get_otto_logger` deleted everywhere (no alias).

- [ ] **Step 1: Rename + delete**

In `src/otto/logger/__init__.py`: rename `get_otto_logger` → `get_logger` (keep signature/docstring). Sweep: `grep -rn "get_otto_logger" src/ tests/ docs/` → every hit becomes `get_logger`. In `src/otto/__init__.py`: `__all__` drops `"get_otto_logger"`; `_LAZY_EXPORTS` drops the `get_otto_logger` key and points `"get_logger": ("otto.logger", "get_logger")`; the `TYPE_CHECKING` imports at lines 17-18 collapse to `from otto.logger import get_logger`.

- [ ] **Step 2: Export `load_lab`**

`TYPE_CHECKING`: `from otto.configmodule import load_lab`; `__all__` += `"load_lab"` (sorted); `_LAZY_EXPORTS` += `"load_lab": ("otto.configmodule", "load_lab")`.

- [ ] **Step 3: Fix the docs inner paths**

Every `from otto.configmodule.configmodule import X` in the docs tree → `from otto.configmodule import X` (the package re-exports all five accessors — verify each imported name exists at package level first).

- [ ] **Step 4: Gate + stage + report**

Run: `uv run pytest tests/unit -n auto`; `uv run nox -s docs` (doctests execute — the changed pages must build); lint; typecheck.

```bash
git add src/otto/__init__.py src/otto/logger/ docs/ src/ tests/
```

(Adjust `src/ tests/` to the exact files the grep sweep touched.) Paste-able commit message:

```
refactor(api)!: one logger accessor (get_logger); export load_lab; fix doc paths

Deletes get_otto_logger (pre-freeze, no alias); load_lab joins the lazy
top-level exports (the bring-your-own-CLI recipe requires it); cookbook
pages teach the package import path, not the inner module.
```

---

### Task 12: Documentation — `extending-cli.md` + touch-ups

**Files:**
- Create: `docs/guide/extending-cli.md`
- Modify: `docs/guide/extending-backends.md` (registry errors now suggest; `overwrite=True`), `docs/guide/library-usage.md` (bootstrap story), the guide toctree (`docs/guide/index.md` or `docs/index.md` — locate where `extending-backends` is listed and add the new page beside it)

**Interfaces:**
- Consumes: the final public API from Tasks 4-9 (`register_cli_command`, `@cli_command`, `CommandSpec` fields, bootstrap timing, collision text).

- [ ] **Step 1: Write `docs/guide/extending-cli.md`**

Cover, with runnable examples (doctests execute in the docs gate — mark non-executable CLI transcripts as plain code blocks):

1. **Registering a top-level command** — `@cli_command` on an async function with an `OttoContext` param and an `options=` dataclass (mirror the `@instruction` docs style from `docs/guide/run.md`).
2. **Registering a command group** — build a `typer.Typer`, `register_cli_command("mytool", app, help="...")`.
3. **Where registration happens** — init modules in `.otto/settings.toml`, executed by `bootstrap()` before argv parsing; a broken file degrades `--help` with a framed warning and fails real dispatch loud.
4. **Metadata** — `lab_free` (skips lab bootstrap), `output_dir` (per-invocation artifact dir), `gate` (reservation check); the defaults and what the built-ins use.
5. **Collisions** — duplicate names fail at registration naming both modules; there is no overwrite for CLI commands.
6. **Completion** — registered commands appear in `otto --help` and tab completion automatically (cached; zero user code on the completion fast path).
7. **Return values** — link to the Result-family exit-code section in the host guide; plain return values print as-is, exit 0.

- [ ] **Step 2: Touch-ups**

`extending-backends.md`: add a short paragraph — unknown-name errors now list registered names *and suggest near-misses*; `register_*` functions accept `overwrite=True` for deliberate replacement of a built-in (e.g. re-registering `json`). `library-usage.md`: document that `import otto` / `import otto.configmodule` are side-effect-free and `open_context()` runs the composition root (or call `otto.bootstrap.bootstrap()` explicitly for custom embedding).

- [ ] **Step 3: Docs gate + stage + report**

Run: `uv run nox -s docs` — Expected: clean, zero warnings, doctests pass.

```bash
git add docs/guide/extending-cli.md docs/guide/extending-backends.md docs/guide/library-usage.md docs/guide/index.md
```

Paste-able commit message:

```
docs: Extending the otto CLI guide + registry/bootstrap touch-ups

How third-party commands join otto (top-level commands and groups,
@cli_command, lab_free/output_dir/gate metadata, collision policy,
bootstrap timing, completion); extending-backends notes did-you-mean
errors and overwrite=True; library-usage documents side-effect-free
imports.
```

---

### Task 13: e2e fixtures (third-party command, broken repo) + full gate

**Files:**
- Modify: `tests/repo_e2e/` — init module registering a top-level command + group (locate the init module list in `tests/repo_e2e/.otto/settings.toml`)
- Create: `tests/repo_broken/.otto/settings.toml`, `tests/repo_broken/tests/test_syntax_error.py`
- Create: `tests/e2e/cli/test_plugin_commands_e2e.py`
- Test-infra: reuse `tests/e2e/_otto_subprocess.py::run_otto` and its `assert_no_output_dir` helpers

**Interfaces:**
- Consumes: everything landed in Tasks 4-9; the `hostless` marker + `run_otto(args, xdir=..., sut_dirs=...)` harness.

- [ ] **Step 1: Extend the e2e fixture repo**

In `tests/repo_e2e`'s init module package (same module `noop.py` lives in), add `plugin_commands.py` and list it in the repo's `init` setting:

```python
"""e2e fixture: third-party top-level CLI commands (spec 2026-07-01)."""

import typer

from otto import cli_command, register_cli_command


@cli_command(name="e2e-hello", help="Print a plugin greeting.", lab_free=True)
async def e2e_hello(who: str = "world") -> None:
    """Print a plugin greeting."""
    typer.echo(f"hello {who}")


e2etool = typer.Typer(name="e2etool", help="Plugin tool group.")


@e2etool.command()
def ping() -> None:
    """Pong."""
    typer.echo("pong")


register_cli_command("e2etool", e2etool, help="Plugin tool group.", lab_free=True)
```

Create the broken repo: `tests/repo_broken/.otto/settings.toml` containing `[test]` + `tests = ["tests"]`, and `tests/repo_broken/tests/test_syntax_error.py` containing `def broken(:` (one line).

- [ ] **Step 2: Write the e2e tests**

`tests/e2e/cli/test_plugin_commands_e2e.py` (follow the module conventions of `test_schema_run_help_e2e.py` — same imports, `REPO_E2E` constant, marker):

```python
"""Hostless e2e: third-party top-level commands + bootstrap containment."""

import pytest

from tests.e2e._otto_subprocess import assert_no_output_dir, run_otto

# Reuse the module-level REPO_E2E / REPO_BROKEN path constants pattern from
# test_schema_run_help_e2e.py.


@pytest.mark.hostless
class TestPluginCommands:
    def test_plugin_leaf_dispatches(self, tmp_path):
        r = run_otto(["e2e-hello", "--who", "otto"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "hello otto" in r.stdout
        assert_no_output_dir(tmp_path)  # lab_free + no output dir declared

    def test_plugin_group_dispatches(self, tmp_path):
        r = run_otto(["e2etool", "ping"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0, r.stderr
        assert "pong" in r.stdout

    def test_plugin_commands_listed_in_root_help(self, tmp_path):
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=REPO_E2E)
        assert r.returncode == 0
        assert "e2e-hello" in r.stdout
        assert "e2etool" in r.stdout


@pytest.mark.hostless
class TestBootstrapContainment:
    def test_broken_repo_degrades_help_with_framed_warning(self, tmp_path):
        r = run_otto(["--help"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode == 0  # help still renders
        assert "failed to load test_syntax_error.py" in r.stderr
        assert "run" in r.stdout  # first-party intact

    def test_broken_repo_fails_real_dispatch_loud(self, tmp_path):
        r = run_otto(["run", "noop"], xdir=tmp_path, sut_dirs=f"{REPO_E2E},{REPO_BROKEN}")
        assert r.returncode != 0
        assert "failed to load test_syntax_error.py" in r.stderr + r.stdout
```

Adapt `sut_dirs` composition to the harness's actual parameter shape (read `run_otto`'s signature first — it may take a list).

- [ ] **Step 3: Run the new e2e**

Run: `uv run pytest tests/e2e/cli -n auto`
Expected: all PASS (old + new). Failures in the new tests are contract bugs from Tasks 5-9 — fix at the owning layer.

- [ ] **Step 4: Full gate**

Run, in order, each to completion:

```bash
make coverage
uv run nox -s lint
uv run nox -s typecheck
uv run nox -s docs
```

Expected: all green. Coverage dips → add unit tests for the uncovered new branches (registry/bootstrap/invoke), never lower the floor.

- [ ] **Step 5: Verify the deletions landed**

```bash
grep -rn "_SUBCOMMAND_MODULES\|_LAB_FREE_SUBCOMMANDS\|_is_lab_free_flag_invocation\|_help_or_discovery\|_SUITE_REGISTRY\|_SUITE_FILES\|get_otto_logger\|_placeholder_subapp" src/ tests/ docs/ --include="*.py" --include="*.md" | grep -v superpowers
```

Expected: zero hits.

- [ ] **Step 6: Stage + report**

```bash
git add tests/repo_e2e/ tests/repo_broken/ tests/e2e/cli/test_plugin_commands_e2e.py
```

Paste-able commit message:

```
test(e2e): third-party top-level commands + bootstrap containment contracts

A fixture plugin registers a leaf (@cli_command) and a group
(register_cli_command); e2e asserts real subprocess dispatch, --help
listing, graceful --help degradation with a framed warning when a repo
is broken, and loud dispatch failure. Full gate green.
```

---

## Plan Self-Review (completed)

- **Spec coverage:** generic Registry + did-you-mean (T1), term/transfer + private-dict accessors (T2), 11 remaining registries + builder bypass (T3), CommandSpec/register_cli_command/@cli_command (T4), full first-party symmetry + lazy root + spec-help `--help` (T5), bootstrap/containment/side-effect-free configmodule/entry (T6), lazy lab + leaf preamble + token-sniffing/verdict deletion (T7), instructions/suites re-plumb (T8), completion v2 (T9), F5 (T10), F7 (T11), docs (T12), e2e + gate (T13). Deletion list verified in T13 Step 5.
- **Placeholder scan:** every "locate via grep" step names the exact grep; behavior-dependent choices (gate flags, Typer decl binding) instruct verification + reporting rather than guessing.
- **Type consistency:** `CommandSpec(name, loader, help, lab_free, output_dir, gate, origin)`, `Registry.register(name, obj, *, overwrite, origin)`, `resolve_spec_command(spec)`, `wrap_leaf_callbacks(cmd, spec)`, `command_preamble(ctx)`, `bootstrap() -> BootstrapResult(env, repos, errors)`, `SuiteEntry(name, sub_app, file)` / `InstructionEntry(name, sub_app, module)` used identically across tasks.
- **Sequencing note:** Tasks 5-9 each end green (unit + CLI e2e); the tree is never left red between tasks, unlike a delete-first plan — the deletions happen task-locally with their replacements.
