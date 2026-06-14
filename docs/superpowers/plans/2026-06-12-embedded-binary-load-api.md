# Embedded Binary Load API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give embedded hosts a first-class `load(file, name)` / `unload(name)` API — distinct from `put`/`get` — that loads a binary into the device runtime via a profile-declared `BinaryLoader` strategy, hides the payload, and offers an opt-in transfer-style progress bar.

**Architecture:** A new `BinaryLoader` value-object strategy (built-in `LlextHexLoader`) formats the device load/unload commands and reads their output, mirroring `CommandFrame`. The telnet write loop is taught to report bytes-per-chunk so a Rich bar can track the paced hex push (the only measurable progress). `EmbeddedHost.load`/`unload` delegate command shape to the loader, run with `log=False`, and return `(Status, str)`.

**Tech Stack:** Python 3.10+, asyncio, pytest / pytest-asyncio, Rich progress. Repo: `otto-sh`.

> **Depends on the (uncommitted) log-flag work.** `load()` uses the per-command `log=False` and the buffered-frame output already in the working tree (the prior feature's Commit 2). Commit that first (the two messages already provided) so this builds on a clean base, or just leave it staged — the code is present either way.

> **Commits:** In `otto-sh`, do **not** self-commit — the `prepare-commit-msg` hook needs `/dev/tty`. At each "Commit" step, **stage** the files; the controller hands Chris grouped paste-able `git commit` commands at the end.

---

## File Structure

**Created:**
- `src/otto/host/binary_loader.py` — `BinaryLoader` ABC, `LlextHexLoader`, registry (`register_binary_loader`/`build_binary_loader`). Mirrors `command_frame.py`.
- `tests/unit/host/test_binary_loader.py` — loader + registry unit tests.

**Modified:**
- `src/otto/host/session.py` — `ShellSession._write_progress` attr; `TelnetSession._write` per-chunk reporting; `write_progress` threaded through `run_cmd`/`_run_cmd_inner`/`SessionManager.run_cmd`.
- `src/otto/host/embeddedHost.py` — `loader` field + `__post_init__` coercion + `_require_loader`; `load()` / `unload()` methods.
- `tests/unit/host/test_session_output_buffering.py` — write-progress tests.
- `tests/unit/host/test_embeddedHost.py` — loader-field + `load`/`unload` tests.
- `tests/repo3/.otto/settings.toml` — `loader = "llext-hex"` on the zephyr profiles.
- `tests/repo3/tests/test_embedded_coverage.py` — use `host.load`/`host.unload`; drop `_drain_unload` + the hexlify.

---

## Task 1: `BinaryLoader` strategy + `LlextHexLoader` + registry

**Files:**
- Create: `src/otto/host/binary_loader.py`
- Test: `tests/unit/host/test_binary_loader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_binary_loader.py`:

```python
"""Unit tests for the BinaryLoader strategy and its registry."""

import pytest

from otto.host.binary_loader import (
    BinaryLoader,
    LlextHexLoader,
    build_binary_loader,
    register_binary_loader,
)


class TestLlextHexLoader:
    loader = LlextHexLoader()

    def test_type_name(self):
        assert LlextHexLoader.type_name == "llext-hex"

    def test_load_command_hex_encodes_payload(self):
        assert self.loader.load_command("cov_ext", b"\x01\xab\xff") == "llext load_hex cov_ext 01abff"

    def test_check_loaded_true_on_success_marker(self):
        ok, reason = self.loader.check_loaded("uart:~$ Successfully loaded extension cov_ext")
        assert ok is True
        assert reason == ""

    def test_check_loaded_false_returns_output_as_reason(self):
        ok, reason = self.loader.check_loaded("Failed to load: return code -8")
        assert ok is False
        assert "Failed to load" in reason

    def test_unload_command(self):
        assert self.loader.unload_command("cov_ext") == "llext unload cov_ext"

    def test_is_fully_unloaded_only_on_no_such_extension(self):
        assert self.loader.is_fully_unloaded("No such extension cov_ext") is True
        # A successful single unload that may have only decremented a refcount
        # is NOT "fully unloaded".
        assert self.loader.is_fully_unloaded("Unloaded extension cov_ext") is False

    def test_max_unload_rounds_default(self):
        assert LlextHexLoader.max_unload_rounds == 16


class TestRegistry:
    def test_builtin_resolves_by_name(self):
        assert isinstance(build_binary_loader("llext-hex"), LlextHexLoader)

    def test_is_a_binary_loader(self):
        assert isinstance(build_binary_loader("llext-hex"), BinaryLoader)

    def test_unknown_raises_with_known_list(self):
        with pytest.raises(ValueError, match="Unknown binary loader"):
            build_binary_loader("does-not-exist")

    def test_register_then_build(self):
        class CustomLoader(LlextHexLoader):
            type_name = "custom-loader-test"

        register_binary_loader("custom-loader-test", CustomLoader)
        assert isinstance(build_binary_loader("custom-loader-test"), CustomLoader)

    def test_register_rejects_name_mismatch(self):
        class Mismatch(LlextHexLoader):
            type_name = "right-name"

        with pytest.raises(ValueError, match="doesn't match"):
            register_binary_loader("wrong-name", Mismatch)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/host/test_binary_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.host.binary_loader'`.

- [ ] **Step 3: Create the module**

Create `src/otto/host/binary_loader.py`:

```python
"""
Pluggable *binary load* strategy for embedded hosts.

Loading a binary into a device's executable runtime — Zephyr's LLEXT
``llext load_hex`` is the first example — is **not** a file transfer: there is
no destination file or filesystem, the binary goes straight into the kernel's
loader. A :class:`BinaryLoader` is a small **stateless value object** (mirroring
:class:`~otto.host.command_frame.CommandFrame`) that formats the device's
load/unload commands and reads their output. The host executes; the loader never
touches the session.

A project can register additional loaders via :func:`register_binary_loader`
from a ``.otto`` init module — the same extension hook
:func:`otto.host.command_frame.register_command_frame` follows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class BinaryLoader(ABC):
    """How to load/unload a binary into an embedded target's runtime."""

    type_name: ClassVar[str]
    """Lab-data string for this loader (e.g. ``'llext-hex'``); unique across loaders."""

    max_unload_rounds: ClassVar[int] = 16
    """Cap on the unload-to-eviction loop the host drives (see
    :meth:`otto.host.embeddedHost.EmbeddedHost.unload`). Some loaders (LLEXT)
    refcount a resident binary, so one unload may decrement without evicting."""

    @abstractmethod
    def load_command(self, name: str, payload: bytes) -> str:
        """The device command that loads *payload* under *name*."""
        ...

    @abstractmethod
    def check_loaded(self, output: str) -> tuple[bool, str]:
        """Return ``(ok, reason)`` from a load command's output — ``reason`` is
        the failure text when ``ok`` is False, ``""`` otherwise."""
        ...

    @abstractmethod
    def unload_command(self, name: str) -> str:
        """The device command that unloads (one round of) *name*."""
        ...

    @abstractmethod
    def is_fully_unloaded(self, output: str) -> bool:
        """True when an unload round's output shows *name* no longer resident."""
        ...


class LlextHexLoader(BinaryLoader):
    """Zephyr LLEXT shell loader: ``llext load_hex`` / ``llext unload``.

    ``load_hex`` takes the hex-encoded ELF inline as one shell-command argument.
    LLEXT refcounts a resident extension, so a full eviction may need several
    ``unload`` rounds — :meth:`is_fully_unloaded` is True only once the shell
    reports ``No such extension``.
    """

    type_name = "llext-hex"

    def load_command(self, name: str, payload: bytes) -> str:
        return f"llext load_hex {name} {payload.hex()}"

    def check_loaded(self, output: str) -> tuple[bool, str]:
        ok = "Successfully loaded extension" in output
        return (True, "") if ok else (False, output.strip())

    def unload_command(self, name: str) -> str:
        return f"llext unload {name}"

    def is_fully_unloaded(self, output: str) -> bool:
        return "No such extension" in output


_LOADER_CLASSES: dict[str, type[BinaryLoader]] = {
    LlextHexLoader.type_name: LlextHexLoader,
}


def register_binary_loader(type_name: str, cls: type[BinaryLoader]) -> None:
    """Make a custom :class:`BinaryLoader` subclass available to lab data.

    Call from an init module listed in ``.otto/settings.toml`` — the same
    pattern :func:`otto.host.command_frame.register_command_frame` follows.
    """
    if cls.type_name != type_name:
        raise ValueError(
            f"register_binary_loader: type_name {type_name!r} doesn't match "
            f"{cls.__name__}.type_name = {cls.type_name!r}"
        )
    _LOADER_CLASSES[type_name] = cls


def build_binary_loader(type_name: str) -> BinaryLoader:
    """Construct the :class:`BinaryLoader` registered under *type_name*."""
    try:
        cls = _LOADER_CLASSES[type_name]
    except KeyError:
        known = ", ".join(sorted(_LOADER_CLASSES))
        raise ValueError(
            f"Unknown binary loader {type_name!r}. Registered loaders: {known}. "
            f"Custom loaders can be added via register_binary_loader()."
        ) from None
    return cls()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/host/test_binary_loader.py -v`
Expected: PASS (all).

- [ ] **Step 5: Stage**

```bash
git add src/otto/host/binary_loader.py tests/unit/host/test_binary_loader.py
```

---

## Task 2: Write-progress plumbing in `session.py`

**Files:**
- Modify: `src/otto/host/session.py` (`ShellSession.__init__` ~78, `run_cmd` ~316, `_run_cmd_inner` ~377, `TelnetSession._write` ~615, `SessionManager.run_cmd` ~1087)
- Test: `tests/unit/host/test_session_output_buffering.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_session_output_buffering.py`:

```python
from types import SimpleNamespace

from otto.host.session import TelnetSession


class TestWriteProgress:

    @pytest.mark.asyncio
    async def test_telnet_write_reports_progress_per_chunk(self):
        writes: list[bytes] = []
        writer = SimpleNamespace(write=writes.append)
        s = TelnetSession(reader=None, writer=writer, write_chunk_size=4)
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s._write("0123456789")  # 10 bytes, chunk 4 -> 3 writes

        assert b"".join(writes) == b"0123456789"
        assert progress == [(4, 10), (8, 10), (10, 10)]

    @pytest.mark.asyncio
    async def test_telnet_single_write_reports_once_at_completion(self):
        writer = SimpleNamespace(write=lambda b: None)
        s = TelnetSession(reader=None, writer=writer, write_chunk_size=0)  # unchunked
        progress: list[tuple[int, int]] = []
        s._write_progress = lambda done, total: progress.append((done, total))

        await s._write("abcd")

        assert progress == [(4, 4)]

    @pytest.mark.asyncio
    async def test_run_cmd_scopes_write_progress_to_framed_write(self, zephyr_session):
        # write_progress is set only for the framed command write, then cleared.
        s = zephyr_session
        seen: list[object] = []
        orig_write = s._write

        async def _record_write(data):
            if s._begin_marker in data:           # the framed command write
                seen.append(s._write_progress)
            await orig_write(data)

        s._write = _record_write
        cb = lambda done, total: None

        async def simulate():
            await asyncio.sleep(0.01)
            s.feed(
                f"\r\n{s._begin_marker}: command not found\r\n~$ "
                f"\r\nok\r\n~$ \r\n0\r\n~$ "
                f"\r\n{s._end_marker_prefix}: command not found\r\n~$ "
            )

        asyncio.create_task(simulate())
        await s.run_cmd("noop", write_progress=cb)

        assert seen == [cb]                # set during the framed write
        assert s._write_progress is None   # cleared afterward
```

(The `zephyr_session` fixture already exists in this file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/host/test_session_output_buffering.py::TestWriteProgress -v`
Expected: FAIL — `AttributeError: 'TelnetSession' object has no attribute '_write_progress'` / `run_cmd() got an unexpected keyword argument 'write_progress'`.

- [ ] **Step 3: Add the `_write_progress` attribute**

In `src/otto/host/session.py`, in `ShellSession.__init__` (after `self._on_output: Callable[[str], None] = lambda _: None`), add:

```python
        # Optional per-command write-progress sink: (bytes_written, total).
        # Set transiently around a single framed write (see _run_cmd_inner) and
        # honored by transports that pace their writes (TelnetSession). Used to
        # drive a transfer-style bar for bulk console pushes (EmbeddedHost.load).
        self._write_progress: Callable[[int, int], None] | None = None
```

- [ ] **Step 4: Report progress in `TelnetSession._write`**

Replace the `TelnetSession._write` chunk block:

```python
        data = re.sub(r'\r?\n', '\r', data)
        encoded = data.encode()
        chunk = self._write_chunk_size
        if chunk and len(encoded) > chunk:
            for i in range(0, len(encoded), chunk):
                self._writer.write(encoded[i:i + chunk])
                if self._write_chunk_delay:
                    await asyncio.sleep(self._write_chunk_delay)
        else:
            self._writer.write(encoded)
```

with:

```python
        data = re.sub(r'\r?\n', '\r', data)
        encoded = data.encode()
        total = len(encoded)
        chunk = self._write_chunk_size
        if chunk and total > chunk:
            for i in range(0, total, chunk):
                self._writer.write(encoded[i:i + chunk])
                if self._write_progress is not None:
                    self._write_progress(min(i + chunk, total), total)
                if self._write_chunk_delay:
                    await asyncio.sleep(self._write_chunk_delay)
        else:
            self._writer.write(encoded)
            if self._write_progress is not None:
                self._write_progress(total, total)
```

- [ ] **Step 5: Thread `write_progress` through `run_cmd` and `_run_cmd_inner`**

In `ShellSession.run_cmd`, add the parameter (after `on_output`):

```python
    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
        on_output: Callable[[str], None] | None = None,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandStatus:
```

and pass it to both `_run_cmd_inner` calls (alongside `sink`):

```python
            if timeout is not None:
                return await asyncio.wait_for(
                    self._run_cmd_inner(cmd, expects, sink, write_progress),
                    timeout=timeout,
                )
            return await self._run_cmd_inner(cmd, expects, sink, write_progress)
```

In `_run_cmd_inner`, add the parameter and scope it around the framed write. Change the signature:

```python
    async def _run_cmd_inner(
        self,
        cmd: str,
        expects: list[Expect] | None,
        on_output: Callable[[str], None],
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandStatus:
```

and replace the framed write (`await self._write(framed)`) with:

```python
        self._write_progress = write_progress
        try:
            await self._write(framed)
        finally:
            self._write_progress = None
```

- [ ] **Step 6: Thread `write_progress` through `SessionManager.run_cmd`**

In `SessionManager.run_cmd`, add the parameter and forward it:

```python
    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: bool = True,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandStatus:
        await self._ensure_session()
        if log:
            self._log_command(cmd)
        assert self._session is not None
        result = await self._session.run_cmd(
            cmd, expects=expects, timeout=timeout,
            on_output=None if log else _drop_output,
            write_progress=write_progress,
        )
        return result
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/unit/host/test_session_output_buffering.py tests/unit/host/test_session.py tests/unit/host/test_session_logging.py -q`
Expected: PASS (new write-progress tests + existing session tests unchanged).

- [ ] **Step 8: Stage**

```bash
git add src/otto/host/session.py tests/unit/host/test_session_output_buffering.py
```

---

## Task 3: `EmbeddedHost.loader` field + coercion + `_require_loader`

**Files:**
- Modify: `src/otto/host/embeddedHost.py` (imports ~46-66, `command_frame` field ~143-157, `__post_init__` ~235, add helper)
- Test: `tests/unit/host/test_embeddedHost.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_embeddedHost.py` (it already imports `pytest` and `ZephyrHost`; add `from otto.host.binary_loader import LlextHexLoader`):

```python
class TestLoaderField:

    def test_loader_string_coerced_to_instance(self):
        h = ZephyrHost(ip="192.0.2.1", ne="sprout", log=False, loader="llext-hex")
        assert isinstance(h.loader, LlextHexLoader)

    def test_loader_defaults_to_none(self):
        h = ZephyrHost(ip="192.0.2.1", ne="sprout", log=False)
        assert h.loader is None

    def test_require_loader_raises_when_none(self):
        h = ZephyrHost(ip="192.0.2.1", ne="sprout", log=False)
        with pytest.raises(ValueError, match="no binary loader"):
            h._require_loader()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/host/test_embeddedHost.py::TestLoaderField -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'loader'`.

- [ ] **Step 3: Add the import and field**

In `src/otto/host/embeddedHost.py`, add to the relative imports near the top (next to `from .command_frame import CommandFrame, ZephyrFrame`):

```python
from .binary_loader import BinaryLoader
```

In `class EmbeddedHost`, immediately after the `command_frame` field block (the field ends with its long docstring around line 157, before `default_dest_dir`), add:

```python
    loader: Optional[BinaryLoader] = None
    """Binary-load strategy for this target's runtime (e.g. Zephyr LLEXT).
    Unlike ``command_frame`` it is *optional* — many embedded hosts never load
    binaries. Lab data declares it by string in the ``loader`` field (e.g.
    ``"llext-hex"``); ``__post_init__`` resolves the string to an instance.
    ``load()`` / ``unload()`` fail loud (``ValueError``) when it is None. Projects
    register custom loaders via
    :func:`otto.host.binary_loader.register_binary_loader`."""
```

- [ ] **Step 4: Add the coercion and the `_require_loader` helper**

In `__post_init__`, right after the `command_frame` coercion block (after the `build_command_frame` lines, before the `if self.command_frame is None:` fail-loud), add:

```python
        # Same for ``loader`` — lab JSON declares the binary-load strategy by
        # name. Optional, so no fail-loud here (load()/unload() check at call).
        if isinstance(self.loader, str):
            from .binary_loader import build_binary_loader
            self.loader = build_binary_loader(self.loader)
```

Add a helper method to `EmbeddedHost` (place it just above the `# File transfer` section, after `expect`):

```python
    def _require_loader(self) -> BinaryLoader:
        """Return this host's binary loader, or fail loud if none is declared."""
        if self.loader is None:
            raise ValueError(
                f"EmbeddedHost {self.name!r} has no binary loader. Declare a "
                f"'loader' (e.g. \"llext-hex\") in the host's profile/lab data, "
                f"or pass an explicit loader, before calling load()/unload()."
            )
        return self.loader
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/host/test_embeddedHost.py::TestLoaderField -v`
Expected: PASS.

- [ ] **Step 6: Stage**

```bash
git add src/otto/host/embeddedHost.py tests/unit/host/test_embeddedHost.py
```

---

## Task 4: `EmbeddedHost.load()` / `unload()`

**Files:**
- Modify: `src/otto/host/embeddedHost.py` (imports; add methods after `put` ~438)
- Test: `tests/unit/host/test_embeddedHost.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_embeddedHost.py` (ensure imports: `from pathlib import Path`, `from unittest.mock import AsyncMock`, `from otto.utils import CommandStatus, Status`, `from otto.host.binary_loader import LlextHexLoader` — add any missing):

```python
def _ok(output: str) -> CommandStatus:
    return CommandStatus(command="c", output=output, status=Status.Success, retcode=0)


class TestLoad:

    @pytest.mark.asyncio
    async def test_load_runs_loader_command_with_log_false(self, host, tmp_path):
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = _ok("Successfully loaded extension cov_ext")
        f = tmp_path / "cov_ext.llext"
        f.write_bytes(b"\x01\x02\x03")

        status, err = await host.load(f, "cov_ext")

        assert status == Status.Success
        assert err == ""
        _, kwargs = host._session_mgr.run_cmd.await_args
        assert host._session_mgr.run_cmd.await_args.args[0] == "llext load_hex cov_ext 010203"
        assert kwargs["log"] is False
        assert "write_progress" not in kwargs       # off by default

    @pytest.mark.asyncio
    async def test_load_returns_error_when_marker_absent(self, host, tmp_path):
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = _ok("Failed to load: return code -8")
        f = tmp_path / "cov_ext.llext"
        f.write_bytes(b"\x01")

        status, err = await host.load(f, "cov_ext")

        assert status == Status.Error
        assert "Failed to load" in err

    @pytest.mark.asyncio
    async def test_load_raises_without_loader(self, host, tmp_path):
        host.loader = None
        f = tmp_path / "x.llext"
        f.write_bytes(b"\x00")
        with pytest.raises(ValueError, match="no binary loader"):
            await host.load(f, "x")

    @pytest.mark.asyncio
    async def test_load_show_progress_passes_write_progress(self, host, tmp_path, monkeypatch):
        from contextlib import asynccontextmanager
        import otto.host.embeddedHost as eh

        @asynccontextmanager
        async def _fake_progress():
            yield object()

        monkeypatch.setattr(eh, "_acquire_shared_progress", _fake_progress)
        monkeypatch.setattr(eh, "make_rich_progress_handler", lambda progress, name: (lambda *a: None))
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = _ok("Successfully loaded extension cov_ext")
        f = tmp_path / "cov_ext.llext"
        f.write_bytes(b"\x01\x02")

        await host.load(f, "cov_ext", show_progress=True)

        assert host._session_mgr.run_cmd.await_args.kwargs["write_progress"] is not None


class TestUnload:

    @pytest.mark.asyncio
    async def test_unload_drains_until_fully_unloaded(self, host):
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.side_effect = [
            _ok("Unloaded extension cov_ext"),   # round 1: decremented, still resident
            _ok("No such extension cov_ext"),    # round 2: fully evicted
        ]

        status, err = await host.unload("cov_ext")

        assert status == Status.Success
        assert host._session_mgr.run_cmd.await_count == 2

    @pytest.mark.asyncio
    async def test_unload_not_loaded_succeeds_first_round(self, host):
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = _ok("No such extension cov_ext")

        status, _ = await host.unload("cov_ext")

        assert status == Status.Success
        assert host._session_mgr.run_cmd.await_count == 1

    @pytest.mark.asyncio
    async def test_unload_errors_if_never_evicted(self, host):
        host.loader = LlextHexLoader()
        host._session_mgr = AsyncMock()
        host._session_mgr.run_cmd.return_value = _ok("Unloaded extension cov_ext")  # never "No such"

        status, err = await host.unload("cov_ext")

        assert status == Status.Error
        assert "still resident" in err
        assert host._session_mgr.run_cmd.await_count == LlextHexLoader.max_unload_rounds

    @pytest.mark.asyncio
    async def test_unload_raises_without_loader(self, host):
        host.loader = None
        with pytest.raises(ValueError, match="no binary loader"):
            await host.unload("x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/host/test_embeddedHost.py::TestLoad tests/unit/host/test_embeddedHost.py::TestUnload -v`
Expected: FAIL — `AttributeError: 'ZephyrHost' object has no attribute 'load'`.

- [ ] **Step 3: Add the transfer-progress imports**

In `src/otto/host/embeddedHost.py`, add to the relative imports near the top:

```python
from .transfer import _acquire_shared_progress, make_rich_progress_handler
```

- [ ] **Step 4: Add `load()` and `unload()`**

In `class EmbeddedHost`, after the `put` method (~line 438), add:

```python
    ####################
    #  Binary load
    ####################

    async def load(
        self,
        file: Path,
        name: str,
        show_progress: bool = False,
        timeout: float | None = 120.0,
    ) -> tuple[Status, str]:
        """Load a binary into the device runtime via the host's binary loader.

        Distinct from :meth:`put` (a *file* transfer to a mounted filesystem):
        ``load`` pushes a binary into the target's loader (e.g. Zephyr LLEXT's
        ``llext load_hex``), with no destination file. The payload is read from
        *file*, formatted into the device command by the loader, and sent with
        ``log=False`` so the (large) encoded payload never reaches the console
        or log. Returns ``(Status, str)`` like :meth:`put`/:meth:`get`; the
        ``str`` carries the device's failure text on error.

        ``show_progress`` is **off by default** (the bar only renders in
        interactive / ``otto run``; under ``otto test`` output is captured). When
        enabled it drives a transfer-style Rich bar from the paced telnet write
        of the payload — the only measurable progress (the device's relocation
        emits no incremental signal). Fails loud (``ValueError``) if the host
        declares no loader.
        """
        loader = self._require_loader()
        if isDryRun():
            return self._dry_run_transfer("LOAD", [file], Path(name))
        payload = file.read_bytes()
        cmd = loader.load_command(name, payload)
        if show_progress:
            async with _acquire_shared_progress() as progress:
                handler = make_rich_progress_handler(progress, self.name)

                def _wp(done: int, total: int) -> None:
                    handler(str(file), f"{self.name}:{name}", done, total)

                result = await self._session_mgr.run_cmd(
                    cmd, timeout=timeout, log=False, write_progress=_wp,
                )
        else:
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout, log=False)
        ok, reason = loader.check_loaded(result.output)
        if ok:
            return Status.Success, ""
        return Status.Error, f"load {name} from {file} failed: {reason}"

    async def unload(
        self,
        name: str,
        timeout: float | None = 20.0,
    ) -> tuple[Status, str]:
        """Unload *name* from the device runtime, draining to full eviction.

        Some loaders (LLEXT) refcount a resident binary, so one unload may only
        decrement it. ``unload`` loops the loader's unload command until
        :meth:`~otto.host.binary_loader.BinaryLoader.is_fully_unloaded` reports
        the binary gone (bounded by ``loader.max_unload_rounds``). Idempotent:
        unloading something not loaded succeeds on the first round. Returns
        ``(Status, str)``; fails loud (``ValueError``) if no loader is declared.
        """
        loader = self._require_loader()
        if isDryRun():
            return self._dry_run_transfer("UNLOAD", [], Path(name))
        cmd = loader.unload_command(name)
        last = ""
        for _ in range(loader.max_unload_rounds):
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout)
            last = result.output
            if loader.is_fully_unloaded(result.output):
                return Status.Success, ""
        return Status.Error, (
            f"{name} still resident after {loader.max_unload_rounds} unload "
            f"rounds: {last.strip()}"
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/host/test_embeddedHost.py -v`
Expected: PASS (TestLoad, TestUnload, TestLoaderField, and all existing embedded tests).

- [ ] **Step 6: Stage**

```bash
git add src/otto/host/embeddedHost.py tests/unit/host/test_embeddedHost.py
```

---

## Task 5: repo3 wiring

**Files:**
- Modify: `tests/repo3/.otto/settings.toml` (zephyr profiles ~47-61)
- Modify: `tests/repo3/tests/test_embedded_coverage.py`

- [ ] **Step 1: Declare the loader on the profiles**

In `tests/repo3/.otto/settings.toml`, add `loader = "llext-hex"` to **both** profile tables, right after their `transfer = "console"` line:

`[os_profiles."zephyr-3.7"]` block:
```toml
command_frame = "zephyr"
transfer = "console"
loader = "llext-hex"
max_filename_len = 32
```

`[os_profiles."zephyr-4.4"]` block:
```toml
command_frame = "zephyr"
transfer = "console"
loader = "llext-hex"
max_filename_len = 32
```

- [ ] **Step 2: Swap `_extension_hex_from` for a path helper**

In `tests/repo3/tests/test_embedded_coverage.py`, replace:

```python
def _extension_hex_from(build_dir: str) -> str:
    """Hex of the pre-built, stripped LLEXT extension for *build_dir* (sent via ``load_hex``)."""
    llext = Path(build_dir) / "zephyr" / f"{_extension()}.stripped.llext"
    if not llext.exists():
        raise RuntimeError(
            f"extension not built: {llext} — build product/ first (see its README)"
        )
    return binascii.hexlify(llext.read_bytes()).decode()
```

with:

```python
def _extension_path_from(build_dir: str) -> Path:
    """Path of the pre-built, stripped LLEXT extension for *build_dir* (passed to host.load)."""
    llext = Path(build_dir) / "zephyr" / f"{_extension()}.stripped.llext"
    if not llext.exists():
        raise RuntimeError(
            f"extension not built: {llext} — build product/ first (see its README)"
        )
    return llext
```

Then remove the now-unused `import binascii` (search the file for `binascii`; if this was its only use, delete the import line).

- [ ] **Step 3: Delete `_drain_unload`**

Delete the entire `async def _drain_unload(host, ext, max_rounds=16): ...` function (its drain loop now lives in `host.unload`).

- [ ] **Step 4: Rewrite `_load_extension` to use `host.load`/`host.unload`**

Replace the `host_hex` dict + the per-host load loop. Change:

```python
        built: set[tuple[str, 'str | None']] = set()
        host_hex: dict[str, str] = {}
        host_build_dir: dict[str, str] = {}
        for host in hosts:
            build_dir = _build_dir_for(host)
            zver = _zver_for(host)
            if (build_dir, zver) not in built:
                await _build_extension_for(build_dir, zver)
                built.add((build_dir, zver))
            host_build_dir[host.id] = build_dir
            host_hex[host.id] = _extension_hex_from(build_dir)

        for host in hosts:
            # Evict any resident copy first so load_hex installs the freshly-built
            # bytes (see _drain_unload): otherwise llext_load refcount-bumps the
            # stale build, the rebuilt .gcno's new stamp no longer matches the
            # dumped .gcda, and `otto cov report` fails with a stamp mismatch.
            await _drain_unload(host, ext)
            # The hex payload is multi-KB; log=False keeps it out of the console
            # and log file. result.output is unaffected (checked below).
            result = await host.oneshot(
                f"llext load_hex {ext} {host_hex[host.id]}", timeout=120, log=False,
            )
            # cmd_llext_load_hex always returns shell-success (0) even on a load
            # error, so the exit status can't be trusted — check the printed
            # outcome. A clean device prints "Successfully loaded extension".
            if "Successfully loaded extension" not in result.output:
                raise RuntimeError(f"load_hex did not load {ext} on {host.id}: {result.output}")
            # Run the gcov constructor so cov_dump has a registered gcov_info.
            await _call(host, "cov_init")
            logger.info("Loaded %s (%s) on %s", ext, host_build_dir[host.id], host.id)
```

to:

```python
        built: set[tuple[str, 'str | None']] = set()
        host_llext: dict[str, Path] = {}
        host_build_dir: dict[str, str] = {}
        for host in hosts:
            build_dir = _build_dir_for(host)
            zver = _zver_for(host)
            if (build_dir, zver) not in built:
                await _build_extension_for(build_dir, zver)
                built.add((build_dir, zver))
            host_build_dir[host.id] = build_dir
            host_llext[host.id] = _extension_path_from(build_dir)

        for host in hosts:
            # Evict any resident copy first so load installs the freshly-built
            # bytes: otherwise llext_load refcount-bumps the stale build, the
            # rebuilt .gcno's new stamp no longer matches the dumped .gcda, and
            # `otto cov report` fails with a stamp mismatch. host.unload drains
            # the LLEXT use-count to 0 (idempotent when nothing is loaded).
            await host.unload(ext)
            status, err = await host.load(host_llext[host.id], name=ext)
            if not status.is_ok:
                raise RuntimeError(f"load did not load {ext} on {host.id}: {err}")
            # Run the gcov constructor so cov_dump has a registered gcov_info.
            await _call(host, "cov_init")
            logger.info("Loaded %s (%s) on %s", ext, host_build_dir[host.id], host.id)
```

- [ ] **Step 5: Swap the teardown unload**

In the same fixture's teardown (after the `yield`), replace the exact line:

```python
                await host.oneshot(f"llext unload {ext}", timeout=20)
```

with:

```python
                await host.unload(ext)
```

If the teardown wraps the unload in a try/except for a host that's already gone, leave that wrapper intact and only swap the call. (Verify with `grep -n "llext unload" tests/repo3/tests/test_embedded_coverage.py` after the edit — there should be **no** `llext unload` strings left in the file; they're all `host.unload` now.)

- [ ] **Step 6: Verify it parses**

Run: `python -c "import ast; ast.parse(open('tests/repo3/tests/test_embedded_coverage.py').read())"`
Expected: no output.

- [ ] **Step 7: Stage**

```bash
git add tests/repo3/.otto/settings.toml tests/repo3/tests/test_embedded_coverage.py
```

---

## Task 6: Full verification

- [ ] **Step 1: Run the binary-loader + host + session suites**

Run: `python -m pytest tests/unit/host/ -q`
Expected: PASS. No regressions; new `test_binary_loader.py`, write-progress, and `load`/`unload` tests included.

- [ ] **Step 2: Lint the changed/new files**

Run: `python -m ruff check src/otto/host/binary_loader.py src/otto/host/session.py src/otto/host/embeddedHost.py`
Expected: clean, or no *new* findings beyond the repo's pre-existing `D209` docstring baseline.

- [ ] **Step 3: Live-bed check (must FAIL loudly if the bed is down — never skip)**

Run: `OTTO_SUT_DIRS=/home/vagrant/otto-sh/tests/repo3 otto -l embedded test TestEmbeddedCoverage`
Verify: install still works (the suite reaches `cov_init`/the op tests), and the log stays free of the `load_hex` hex wall and prompt/`retval` scaffolding. Pre-existing `LeakedProductLoopError` failures are unrelated. (The progress bar only animates in interactive/`otto run`; under `otto test` output is captured — the contract test proves the wiring.)

If the bed is unreachable, this must fail with a clear host-named error — do **not** mark it skipped.

---

## Notes for the executor

- **`load()` hides the payload via `log=False`** (the prior feature's per-command log flag) — argument-passed, not `host.log` mutation. Do not reintroduce `SuppressCommandOutput` here.
- **`unload()` owns the drain loop**; the "fully evicted" predicate (`is_fully_unloaded` → `"No such extension"`) lives in `LlextHexLoader`, keeping the LLEXT refcount quirk out of generic `EmbeddedHost`.
- **Write-progress is telnet-only and scoped** to the single framed write (`_run_cmd_inner` sets it immediately before and clears it immediately after) — SSH/local `_write` ignore it.
- **The `loader` profile key** flows to the host automatically (os_profile validates against host fields + `__post_init__` coercion). Task 6 Step 1's full-suite run exercises the repo3 settings parse; if a separate profile-key allowlist exists in `os_profile.py`/`factory.py` and rejects `loader`, add it there.
