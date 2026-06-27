# Kernel modules + per-class CLI parsers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `load`/`unload`/`lsmod` kernel-module verbs to `UnixHost` (and CLI-expose the existing embedded `load`/`unload`), on top of a per-class CLI-parser enhancement that lets the same verb name carry a different signature per host class.

**Architecture:** Part 1 makes `HostGroup.get_command` build each verb's Typer parser from the *resolved host class's* method (cached per class+verb), generalizing the existing per-class *scoping* to per-class *parsing*. Part 2 adds `UnixHost.load`/`unload`/`lsmod` (insmod a controller-side `.ko`, rmmod, read `/proc/modules`), deciding sudo from Spec A's `current_user`, then retrofits `@cli_exposed` onto embedded `load`/`unload` — viable only because of Part 1.

**Tech Stack:** Python 3.10+, asyncio, Typer/Click (Typer vendors its own click fork), pytest / pytest-asyncio, `unittest.mock`.

**Spec:** [docs/superpowers/specs/2026-06-27-kernel-modules-and-per-class-cli-design.md](../specs/2026-06-27-kernel-modules-and-per-class-cli-design.md)
**Builds on:** Spec A (`current_user`) — this branch is stacked on `worktree-per-session-user-elevation`.

## Global Constraints

- **Additive only.** No existing public signatures change behaviour. Embedded `load`/`unload` keep their exact parameters (only an `@cli_exposed` decorator + transparent `Annotated[...]` CLI markers are added; positional callers are unaffected).
- **Stage only — do NOT `git commit`.** otto's `prepare-commit-msg` hook mis-attributes agent commits. Subagents do NOT run git at all; the controller stages. Chris commits.
- **Coverage floor: 92%.** `make coverage` must stay green. Add a test for every new branch.
- **Type checking (`ty`) must stay clean.** Methods added directly to `UnixHost` may call `self._q`/`self.oneshot`/`self.run`/`self.put`/`self.rm`/`self.current_user` without `# ty: ignore` (all resolve on `UnixHost`/its bases). Do not add stray ignores.
- **No new `from __future__ import annotations`.** All edited files already have it; leave as-is. Create no new source modules.
- **Quoting:** use the inherited `self._q(x)` helper (shlex-quote) for shell args — do NOT import `shlex` into `unix_host.py`.
- **CLI markers:** `Annotated[T, Arg(...)]` = positional, `Annotated[T, Opt(...)]` = `--option`, `Annotated[T, Exclude]` = hidden (filled with its default). These live in `otto.utils`.
- **Tests are unit-tier**, mirroring `tests/unit/host/` (real hosts + mocked `put`/`run`/`oneshot`) and `tests/unit/cli/test_dynamic_host_commands.py` (synthesizer).

## File Structure

- `src/otto/cli/expose.py` — `HostGroup.get_command` resolves the parser per host class; new `_class_command` helper + per-class cache.
- `src/otto/host/unix_host.py` — new `load`/`unload`/`lsmod` + private `_loaded_modules`; add `Opt` to the `otto.utils` import.
- `src/otto/host/embedded_host.py` — `@cli_exposed` + `Annotated` CLI markers on the existing `load`/`unload` (bodies unchanged).
- `tests/unit/cli/test_dynamic_host_commands.py` — per-class parser tests (Part 1) + embedded-retrofit CLI test.
- `tests/unit/host/test_unix_host.py` — `load`/`unload`/`lsmod`/`_loaded_modules` tests.

---

### Task 1: Per-class CLI parsers

**Files:**
- Modify: `src/otto/cli/expose.py` (`HostGroup.get_command` at lines 231-236; add `_class_command` method)
- Test: `tests/unit/cli/test_dynamic_host_commands.py`

**Interfaces:**
- Consumes: existing `collect_exposed_methods(cls)`, `_synthesize_command(...)`, `inspect.getattr_static`.
- Produces: `HostGroup.get_command` returns a per-class parser; `HostGroup._class_command(cls, cmd_name, attr_name) -> click.Command` (cached).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/cli/test_dynamic_host_commands.py`:

```python
def test_class_command_builds_parser_from_the_given_class():
    """The same verb name on two classes yields parsers shaped by each class."""
    from typing import Annotated
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Arg, Opt

    class HostX:
        @cli_exposed
        async def frob(self, target: Annotated[str, Arg()]) -> None:
            ...

    class HostY:
        @cli_exposed
        async def frob(self, target: Annotated[str | None, Opt()] = None) -> None:
            ...

    g = HostGroup(name="host")
    cmd_x = g._class_command(HostX, "frob", "frob")
    cmd_y = g._class_command(HostY, "frob", "frob")
    px = {p.name: p for p in cmd_x.params}
    py = {p.name: p for p in cmd_y.params}
    assert px["target"].param_type_name == "argument"   # required positional
    assert py["target"].param_type_name == "option"      # --target


def test_class_command_caches_per_class_and_verb():
    from typing import Annotated
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Arg

    class HostX:
        @cli_exposed
        async def frob(self, target: Annotated[str, Arg()]) -> None:
            ...

    g = HostGroup(name="host")
    first = g._class_command(HostX, "frob", "frob")
    second = g._class_command(HostX, "frob", "frob")
    assert first is second  # cached, not rebuilt


def test_get_command_uses_resolved_class_parser(monkeypatch):
    from typing import Annotated
    from unittest.mock import MagicMock
    from otto.cli.expose import HostGroup
    from otto.utils import cli_exposed, Opt

    class HostY:
        @cli_exposed
        async def frob(self, target: Annotated[str | None, Opt()] = None) -> None:
            ...

    g = HostGroup(name="host")
    monkeypatch.setattr(g, "_class_for", lambda ctx: HostY)
    cmd = g.get_command(MagicMock(), "frob")
    params = {p.name: p for p in cmd.params}
    assert params["target"].param_type_name == "option"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/cli/test_dynamic_host_commands.py -k "class_command or resolved_class_parser" -v`
Expected: FAIL — `AttributeError: 'HostGroup' object has no attribute '_class_command'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/cli/expose.py`, replace the body of `get_command` (lines 231-236) and add `_class_command` directly above it inside the `HostGroup` class:

```python
        def _class_command(self, cls: type, cmd_name: str, attr_name: str) -> Any:
            """Build (and cache) the verb's command from *cls*'s own method, so a
            verb name shared across classes can carry a different signature per
            class. Cached per ``(cls, cmd_name)``."""
            cache = getattr(self, "_class_cmd_cache", None)
            if cache is None:
                cache = self._class_cmd_cache = {}
            key = (cls, cmd_name)
            if key not in cache:
                fn = inspect.getattr_static(cls, attr_name, None) or getattr(cls, attr_name)
                help_text = getattr(fn, "__cli_help__", None) or (
                    (fn.__doc__ or "").strip().splitlines() or [""]
                )[0]
                cache[key] = _synthesize_command(cmd_name, attr_name, help_text, fn)
            return cache[key]

        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            self._ensure_dynamic()
            cls = self._class_for(ctx)
            if cls is None:
                # Completion / unresolved host → the unscoped global command.
                return super().get_command(ctx, cmd_name)
            verbs = collect_exposed_methods(cls)
            if cmd_name in self._dynamic_names and cmd_name not in verbs:
                return None  # dynamic verb not exposed on this host class
            if cmd_name in verbs:
                return self._class_command(cls, cmd_name, verbs[cmd_name])
            return super().get_command(ctx, cmd_name)  # static (non-dynamic) commands
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/cli/test_dynamic_host_commands.py -v`
Expected: PASS (new tests + existing tests in the file — no regressions)

- [ ] **Step 5: Stage**

```bash
git add src/otto/cli/expose.py tests/unit/cli/test_dynamic_host_commands.py
```
Paste-able message: `feat(cli): resolve host-verb parsers per host class`

---

### Task 2: `UnixHost._loaded_modules` + `lsmod`

**Files:**
- Modify: `src/otto/host/unix_host.py` (add methods after `rm`/file-ops, near the existing `@cli_exposed` verbs ~line 679)
- Test: `tests/unit/host/test_unix_host.py`

**Interfaces:**
- Consumes: inherited `self.oneshot(cmd) -> CommandStatus` (`.status.is_ok`, `.output`).
- Produces: `UnixHost._loaded_modules() -> list[str]`; `UnixHost.lsmod() -> list[str]` (`@cli_exposed`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_unix_host.py` (use `from unittest.mock import AsyncMock`; `from otto.utils import CommandStatus, Status`):

```python
def _unix_host():
    from otto.host.unix_host import UnixHost
    return UnixHost(ip="10.0.0.1", element="box", creds={"admin": "secret"},
                    user="admin", log=False)


@pytest.mark.asyncio
async def test_loaded_modules_parses_proc_modules_column_one():
    from unittest.mock import AsyncMock
    from otto.utils import CommandStatus, Status
    host = _unix_host()
    proc = "ext4 737280 2 - Live 0x0\nnvme 49152 3 nvme_core, Live 0x0\n"
    host.oneshot = AsyncMock(return_value=CommandStatus("cat /proc/modules", proc, Status.Success, 0))
    assert await host._loaded_modules() == ["ext4", "nvme"]


@pytest.mark.asyncio
async def test_loaded_modules_empty_when_read_fails():
    from unittest.mock import AsyncMock
    from otto.utils import CommandStatus, Status
    host = _unix_host()
    host.oneshot = AsyncMock(return_value=CommandStatus("cat /proc/modules", "", Status.Error, 1))
    assert await host._loaded_modules() == []


@pytest.mark.asyncio
async def test_lsmod_returns_loaded_module_names():
    from unittest.mock import AsyncMock
    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=["ext4", "nvme"])
    assert await host.lsmod() == ["ext4", "nvme"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "loaded_modules or lsmod" -v`
Expected: FAIL — `AttributeError: 'UnixHost' object has no attribute '_loaded_modules'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/unix_host.py`, add these methods to `UnixHost` (next to the other `@cli_exposed` file-ops, e.g. after `rm`/before the connectivity helpers):

```python
    @cli_exposed
    async def lsmod(self) -> list[str]:
        """List the kernel modules currently loaded on the host."""
        return await self._loaded_modules()

    async def _loaded_modules(self) -> list[str]:
        """Loaded module names, read from ``/proc/modules`` — the source ``lsmod``
        formats. World-readable (no sudo), no ``lsmod`` binary dependency; column
        one is the module name, already ``-``→``_`` normalized by the kernel."""
        result = await self.oneshot("cat /proc/modules")
        if not result.status.is_ok:
            return []
        return [line.split()[0] for line in result.output.splitlines() if line.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "loaded_modules or lsmod" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/unix_host.py tests/unit/host/test_unix_host.py
```
Paste-able message: `feat(host): UnixHost.lsmod via /proc/modules`

---

### Task 3: `UnixHost.load`

**Files:**
- Modify: `src/otto/host/unix_host.py` (add `Opt` to the `from ..utils import (...)` block at lines 58-63; add `load` method)
- Test: `tests/unit/host/test_unix_host.py`

**Interfaces:**
- Consumes: `self.put(file, dest_dir, show_progress=...) -> tuple[Status, str]`; `self.current_user -> str` (Spec A); `self.run(cmd, sudo=...) -> RunResult` (`.status.is_ok`, `.only.output`); `self.rm(path, force=True)`; `self._q(x)`.
- Produces: `UnixHost.load(file, name=None, dest_dir=Path("/tmp"), show_progress=False) -> tuple[Status, str]` (`@cli_exposed`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_unix_host.py`:

```python
def _run_result(cmd, output, status, retcode):
    from otto.host.host import RunResult
    from otto.utils import CommandStatus
    return RunResult(status=status, statuses=[CommandStatus(cmd, output, status, retcode)])


@pytest.mark.asyncio
async def test_load_stages_then_insmod_sudo_for_nonroot(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"          # non-root
    ko = tmp_path / "my-mod.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/my-mod.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    status, msg = await host.load(ko)
    assert status is Status.Success and msg == ""
    host.put.assert_awaited_once()
    assert host.run.await_args.args[0] == "insmod /tmp/my-mod.ko"
    assert host.run.await_args.kwargs["sudo"] is True
    host.rm.assert_awaited_once()                      # staged file cleaned up


@pytest.mark.asyncio
async def test_load_no_sudo_when_current_user_root(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "root"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(return_value=_run_result("insmod /tmp/m.ko", "", Status.Success, 0))
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    await host.load(ko)
    assert host.run.await_args.kwargs["sudo"] is False


@pytest.mark.asyncio
async def test_load_put_failure_short_circuits(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "m.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Error, "scp failed"))
    host.run = AsyncMock()
    status, msg = await host.load(ko)
    assert status is Status.Error and "staging" in msg
    host.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_error_message_uses_normalized_name(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    ko = tmp_path / "foo-bar.ko"
    ko.write_bytes(b"\x00")
    host.put = AsyncMock(return_value=(Status.Success, ""))
    host.run = AsyncMock(return_value=_run_result("insmod ...", "Invalid module format", Status.Error, 1))
    host.rm = AsyncMock(return_value=(Status.Success, ""))
    status, msg = await host.load(ko)
    assert status is Status.Error
    assert "foo_bar" in msg and "Invalid module format" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "test_load_" -v`
Expected: FAIL — `AttributeError: 'UnixHost' object has no attribute 'load'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/unix_host.py`, add `Opt` to the `from ..utils import (...)` block (it currently imports `Arg, CommandStatus, Exclude, Status, cli_exposed`) so it reads:
```python
from ..utils import (
    Arg,
    CommandStatus,
    Exclude,
    Opt,
    Status,
    cli_exposed,
    ...
)
```
Then add the `load` method to `UnixHost` (next to `lsmod`):

```python
    @cli_exposed(success="Module loaded.")
    async def load(
        self,
        file: Annotated[Path, Arg(help="Kernel module .ko to insert.")],
        name: Annotated[str | None, Opt(help="Module name; defaults to the file stem.")] = None,
        dest_dir: Annotated[Path, Exclude] = Path("/tmp"),
        show_progress: Annotated[bool, Exclude] = False,
    ) -> tuple[Status, str]:
        """Insert a kernel module: stage the .ko to the host, then ``insmod`` it.

        ``put`` lands the .ko on the target (as the login/transfer user); the
        ``insmod`` runs in the shell session — under ``sudo`` unless the session
        is already root (Spec A's ``current_user``). The staged file is removed
        afterward (the module lives in kernel memory once inserted). ``name``
        defaults to the file stem (``-``→``_``) and is used in error text.
        """
        resolved = (name or file.stem).replace("-", "_")
        dest = dest_dir / file.name
        status, put_msg = await self.put(file, dest_dir, show_progress=show_progress)
        if not status.is_ok:
            return status, f"staging {file} failed: {put_msg}"
        need_sudo = self.current_user != "root"
        result = await self.run(f"insmod {self._q(dest)}", sudo=need_sudo)
        await self.rm(dest, force=True)  # best-effort cleanup
        if result.status.is_ok:
            return Status.Success, ""
        return Status.Error, f"insmod {resolved} failed: {result.only.output.strip()}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "test_load_" -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/unix_host.py tests/unit/host/test_unix_host.py
```
Paste-able message: `feat(host): UnixHost.load (insmod a controller-side .ko)`

---

### Task 4: `UnixHost.unload`

**Files:**
- Modify: `src/otto/host/unix_host.py` (add `unload` method)
- Test: `tests/unit/host/test_unix_host.py`

**Interfaces:**
- Consumes: `self._loaded_modules() -> list[str]` (Task 2); `self.current_user`; `self.run(cmd, sudo=...) -> RunResult`; `self._q(x)`.
- Produces: `UnixHost.unload(name) -> tuple[Status, str]` (`@cli_exposed`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/host/test_unix_host.py`:

```python
@pytest.mark.asyncio
async def test_unload_idempotent_when_not_resident():
    from unittest.mock import AsyncMock
    from otto.utils import Status
    host = _unix_host()
    host._loaded_modules = AsyncMock(return_value=["ext4"])
    host.run = AsyncMock()
    status, msg = await host.unload("my_mod")
    assert status is Status.Success and msg == ""
    host.run.assert_not_awaited()           # not resident → no rmmod


@pytest.mark.asyncio
async def test_unload_rmmod_with_sudo_when_resident():
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=["my_mod"])
    host.run = AsyncMock(return_value=_run_result("rmmod my_mod", "", Status.Success, 0))
    status, msg = await host.unload("my-mod")        # dash normalized to my_mod
    assert status is Status.Success
    assert host.run.await_args.args[0] == "rmmod my_mod"
    assert host.run.await_args.kwargs["sudo"] is True


@pytest.mark.asyncio
async def test_unload_error_maps_rmmod_failure():
    from unittest.mock import AsyncMock, MagicMock
    from otto.utils import Status
    host = _unix_host()
    host._session_mgr = MagicMock()
    host._session_mgr.current_user = "admin"
    host._loaded_modules = AsyncMock(return_value=["my_mod"])
    host.run = AsyncMock(return_value=_run_result("rmmod my_mod", "Module my_mod is in use", Status.Error, 1))
    status, msg = await host.unload("my_mod")
    assert status is Status.Error and "in use" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "test_unload_" -v`
Expected: FAIL — `AttributeError: 'UnixHost' object has no attribute 'unload'`

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/unix_host.py`, add the `unload` method to `UnixHost` (next to `load`):

```python
    @cli_exposed(success="Module unloaded.")
    async def unload(
        self,
        name: Annotated[str, Arg(help="Module name to remove.")],
    ) -> tuple[Status, str]:
        """Remove a kernel module (``rmmod``). Idempotent: removing a module that
        is not resident succeeds without running ``rmmod`` (mirrors
        :meth:`~otto.host.embedded_host.EmbeddedHost.unload`)."""
        resolved = name.replace("-", "_")
        if resolved not in await self._loaded_modules():
            return Status.Success, ""
        need_sudo = self.current_user != "root"
        result = await self.run(f"rmmod {self._q(resolved)}", sudo=need_sudo)
        if result.status.is_ok:
            return Status.Success, ""
        return Status.Error, f"rmmod {resolved} failed: {result.only.output.strip()}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/host/test_unix_host.py -k "test_unload_" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/unix_host.py tests/unit/host/test_unix_host.py
```
Paste-able message: `feat(host): UnixHost.unload (rmmod, idempotent vs /proc/modules)`

---

### Task 5: Embedded `load`/`unload` CLI retrofit

**Files:**
- Modify: `src/otto/host/embedded_host.py` (`load` at lines 572-577, `unload` at lines 618-621 — add `@cli_exposed` + `Annotated` markers; bodies unchanged)
- Test: `tests/unit/cli/test_dynamic_host_commands.py`, `tests/unit/host/test_embedded_host.py`

**Interfaces:**
- Consumes: Task 1's per-class parsers; existing `Arg`/`Exclude`/`cli_exposed` (already imported in `embedded_host.py`).
- Produces: `EmbeddedHost.load`/`unload` become `@cli_exposed` with `name` a required positional.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/cli/test_dynamic_host_commands.py`:

```python
def test_embedded_and_unix_load_have_per_class_signatures():
    """Same verb name, divergent signatures: embedded `load` requires a
    positional `name`; unix `load` exposes it as the `--name` option."""
    from otto.cli.expose import HostGroup
    from otto.host.embedded_host import ZephyrHost
    from otto.host.unix_host import UnixHost

    g = HostGroup(name="host")
    emb = {p.name: p for p in g._class_command(ZephyrHost, "load", "load").params}
    unix = {p.name: p for p in g._class_command(UnixHost, "load", "load").params}
    assert emb["name"].param_type_name == "argument"   # embedded: required positional
    assert emb["name"].required is True
    assert unix["name"].param_type_name == "option"      # unix: --name


def test_embedded_load_unload_are_cli_exposed():
    from otto.cli.expose import collect_exposed_methods
    from otto.host.embedded_host import ZephyrHost
    verbs = collect_exposed_methods(ZephyrHost)
    assert "load" in verbs and "unload" in verbs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/cli/test_dynamic_host_commands.py -k "embedded" -v`
Expected: FAIL — `assert 'load' in {...}` (embedded load/unload not yet `@cli_exposed`)

- [ ] **Step 3: Write minimal implementation**

In `src/otto/host/embedded_host.py`, decorate and annotate `load` (replace its signature header lines 572-577, body unchanged):

```python
    @cli_exposed(success="Binary loaded.")
    async def load(
        self,
        file: Annotated[Path, Arg(help="Binary to load into the device runtime.")],
        name: Annotated[str, Arg(help="Name to register the loaded binary under.")],
        show_progress: Annotated[bool, Exclude] = False,
        timeout: Annotated[float | None, Exclude] = 120.0,
    ) -> tuple[Status, str]:
```

and `unload` (replace its signature header lines 618-621, body unchanged):

```python
    @cli_exposed(success="Binary unloaded.")
    async def unload(
        self,
        name: Annotated[str, Arg(help="Name of the binary to unload.")],
        timeout: Annotated[float | None, Exclude] = 20.0,
    ) -> tuple[Status, str]:
```

(`Path`, `Annotated`, `Arg`, `Exclude`, `Status`, `cli_exposed` are already imported in `embedded_host.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/cli/test_dynamic_host_commands.py -k "embedded" tests/unit/host/test_embedded_host.py -q`
Expected: PASS — new CLI tests pass AND the existing embedded `load`/`unload` tests still pass (decorator + `Annotated` are behaviour-transparent).

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/embedded_host.py tests/unit/cli/test_dynamic_host_commands.py
```
Paste-able message: `feat(host): CLI-expose embedded load/unload (per-class parser)`

---

### Task 6: Full-gate verification

**Files:** none (verification only).

- [ ] **Step 1: Run the touched unit suites**

Run: `uv run --no-sync pytest tests/unit/cli/test_dynamic_host_commands.py tests/unit/cli/test_host_cli.py tests/unit/host/test_unix_host.py tests/unit/host/test_embedded_host.py -v`
Expected: PASS — no regressions.

- [ ] **Step 2: Type check**

Run: `make typecheck`
Expected: clean.

- [ ] **Step 3: Coverage gate**

Run: `make coverage`
Expected: PASS, total coverage ≥ 92%. If any new branch is uncovered (e.g. `_loaded_modules` empty-output path, the put-failure short-circuit), add a targeted test and re-run.

- [ ] **Step 4: Docs gate**

Run: `make docs`
Expected: 0 warnings. New docstrings reference real symbols; `:meth:` cross-refs (e.g. to `EmbeddedHost.unload`) must resolve — if Sphinx flags an unresolved `:meth:`/`:attr:`, soften it to a plain ``literal`` (the same fix Spec A used for `PosixPrivilege` docstrings).

- [ ] **Step 5: Stage any coverage top-up tests**

```bash
git add tests/unit/host/ tests/unit/cli/
```
Paste-able message: `test: coverage top-up for kernel-module verbs`

> `make nox` (5 Pythons, live beds) is the heavy full gate — leave it to Chris per the dev-VM load policy; do not run it here.

---

## Self-Review

**1. Spec coverage** — every spec section maps to a task:
- §3 per-class CLI parsers → Task 1 (`get_command` + `_class_command` + cache; completion fallback preserved).
- §4.1 `load` → Task 3 (stage via `put`, `insmod`, sudo from `current_user`, cleanup, `name` default+normalize, error mapping, put-failure short-circuit).
- §4.2 `unload` → Task 4 (`rmmod`, idempotent vs `/proc/modules`, normalize, error mapping).
- §4.3 `lsmod`/`_loaded_modules` → Task 2 (`/proc/modules` column 1, empty on failure, no sudo).
- §4.4 embedded retrofit → Task 5 (`@cli_exposed` + `Annotated`, `name` required, per-class parser test).
- §5 sudo soundness → exercised by the root/non-root `load`/`unload` tests (Tasks 3-4); Local/Docker untouched (non-goal).
- §5 dry-run → composes over `put`/`run`/`oneshot` (already dry-run-aware); not separately tasked.
- §6 testing → each task ships its tests; Task 6 runs the gate.

**2. Placeholder scan** — no TBD/TODO; every code step shows full code; every test shows full assertions; exact commands with expected output.

**3. Type consistency** — `_loaded_modules() -> list[str]` defined in Task 2, consumed in Task 4. `load(file, name=None, dest_dir=Path("/tmp"), show_progress=False)` and `unload(name)` signatures stable across their task and the spec. `_class_command(cls, cmd_name, attr_name)` named identically in Task 1 (def) and used via `get_command`. `current_user` (Spec A) read identically in Tasks 3-4. `self._q`/`self.oneshot`/`self.run`/`self.put`/`self.rm` are the real inherited members. Embedded `load`/`unload` keep their parameter names (`file`, `name`, `show_progress`, `timeout`).
