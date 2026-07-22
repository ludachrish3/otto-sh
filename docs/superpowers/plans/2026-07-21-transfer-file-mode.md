# File permission mode on transfers to hosts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `mode` parameter to `put` so files transferred to a host land with the permissions the caller asked for, with CLI octal that can never be misread as decimal.

**Architecture:** `BaseFileTransfer.put_files` grows `mode` and owns the whole policy: parse it, refuse it pre-flight on backends with no permission model, and after a successful `_run_put` apply it through a single `_apply_mode` hook. All four unix backends (`scp`, `sftp`, `ftp`, `nc`) are served by one implementation on their shared `UnixFileTransfer` base. Hosts only declare the parameter and pass it through.

**Tech Stack:** Python 3.10-3.14, Typer 0.27 (CLI), `ty` (type checker), pytest + pytest-asyncio, nox, Sphinx (MyST).

**Spec:** [`docs/superpowers/specs/2026-07-21-transfer-file-mode-design.md`](../specs/2026-07-21-transfer-file-mode-design.md)

## Global Constraints

- **Never** `from __future__ import annotations` — it trips Sphinx nitpicky `-W`.
- Prefer lists over tuples in public APIs; callables return dataclasses.
- `ty` runs only at the nox typecheck session — budget a `make typecheck` round after source edits.
- Docstrings are load-bearing: every new public helper needs one, and the docs build must be **clean** (not incremental) or broken `:meth:` refs slip through.
- Commit with a conventional prefix and an `Assisted-by: Claude Opus 4.8 (1M context)` trailer. This is a worktree, so self-commit is fine.
- No manual `CHANGELOG.md` edit — `cliff.toml` generates it from commits.
- Run the whole `tests/unit` suite before declaring done; do not put heavy parallel load on the dev VM.

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/otto/host/transfer/base.py` | mode parsing, chmod command building, the `put_files` seam, the `_apply_mode` hook | Modify |
| `src/otto/host/transfer/unix_base.py` | `_apply_mode` for scp/sftp/ftp/nc | Modify |
| `src/otto/host/local_host.py` | `_apply_mode` for local copies; `LocalHost.put` param | Modify |
| `src/otto/host/unix_host.py` | `UnixHost.put` param | Modify |
| `src/otto/host/embedded_host.py` | `EmbeddedHost.put` param (fails loudly downstream) | Modify |
| `src/otto/host/docker_host.py` | `put` param + explicit in-container chmod | Modify |
| `src/otto/host/host.py` | `Host` protocol + `BaseHost` signatures, dry-run banner | Modify |
| `src/otto/host/transfer/__init__.py` | re-export the new helpers | Modify |
| `tests/unit/host/test_transfer_mode.py` | every unit-level guard for this feature | Create |
| `tests/unit/cli/test_dynamic_host_commands.py` | the CLI decimal-trap guard | Modify |
| `docs/guide/hosts/index.md`, `docs/guide/cli-reference.md` | user-facing docs | Modify |

---

### Task 1: Octal parsing + chmod command helpers

Pure functions with no I/O — the foundation everything else calls.

**Files:**
- Modify: `src/otto/host/transfer/base.py`
- Modify: `src/otto/host/transfer/__init__.py`
- Test: `tests/unit/host/test_transfer_mode.py` (create)

**Interfaces:**
- Consumes: `Result`, `Status` (already imported in `base.py`).
- Produces:
  - `MAX_FILE_MODE: int` = `0o7777`
  - `parse_file_mode(value: int | str | None) -> Result` — ok with `value=int|None`, or error with `msg`.
  - `chmod_command(mode: int, paths: list[Path]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_transfer_mode.py`:

```python
"""Permission-mode support for transfers to hosts (spec 2026-07-21)."""

from pathlib import Path

import pytest

from otto.host.transfer.base import MAX_FILE_MODE, chmod_command, parse_file_mode
from otto.utils import Status


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("755", 0o755),
        ("0755", 0o755),
        ("0o755", 0o755),
        ("0O755", 0o755),
        ("640", 0o640),
        ("4755", 0o4755),  # setuid permitted, as chmod permits it
        ("0", 0),
        ("7777", MAX_FILE_MODE),
    ],
)
def test_string_modes_are_always_octal(raw, expected):
    result = parse_file_mode(raw)
    assert result.is_ok, result.msg
    assert result.value == expected


def test_bare_755_is_octal_not_decimal():
    # The whole point: 755 base-10 is 0o1363, which is NOT what a user means.
    assert parse_file_mode("755").value == 0o755 == 493


def test_int_mode_passes_through_untouched():
    # Python API: mode=0o755 is already a mode; never re-read as base-8.
    result = parse_file_mode(0o755)
    assert result.is_ok
    assert result.value == 0o755


def test_none_is_ok_and_means_no_mode():
    result = parse_file_mode(None)
    assert result.is_ok
    assert result.value is None


@pytest.mark.parametrize("raw", ["789", "rwx", "", "0x1ff", "u+x", "7 5 5"])
def test_non_octal_strings_are_rejected(raw):
    result = parse_file_mode(raw)
    assert result.status is Status.Error
    assert repr(raw) in result.msg


def test_negative_mode_rejected():
    result = parse_file_mode(-1)
    assert result.status is Status.Error
    assert "negative" in result.msg


@pytest.mark.parametrize("raw", ["77777", 0o10000])
def test_out_of_range_mode_rejected(raw):
    result = parse_file_mode(raw)
    assert result.status is Status.Error
    assert "out of range" in result.msg


def test_bool_is_not_a_mode():
    # bool subclasses int; True would silently become 0o1 without a guard.
    result = parse_file_mode(True)
    assert result.status is Status.Error


def test_chmod_command_is_octal_without_prefix_and_batched():
    cmd = chmod_command(0o755, [Path("/opt/a"), Path("/opt/b")])
    assert cmd == "chmod 755 /opt/a /opt/b"


def test_chmod_command_quotes_hostile_paths():
    cmd = chmod_command(0o644, [Path("/opt/my file"), Path("/opt/it's")])
    assert "'/opt/my file'" in cmd
    assert cmd.count("chmod") == 1
    # A bare shell would split these; quoting must survive.
    import shlex

    assert shlex.split(cmd)[2:] == ["/opt/my file", "/opt/it's"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'MAX_FILE_MODE' from 'otto.host.transfer.base'`

- [ ] **Step 3: Implement the helpers**

In `src/otto/host/transfer/base.py`, add `import shlex` to the imports, then add after `validate_filename_lengths`:

```python
MAX_FILE_MODE = 0o7777
"""Highest permission value ``mode`` accepts.

Twelve bits: setuid, setgid, sticky, then the three rwx triads — the same
range ``chmod(1)`` accepts as an octal argument.
"""


def parse_file_mode(value: int | str | None) -> Result:
    """Normalize a transfer permission *mode*, or explain why it is not one.

    Strings are **always** interpreted base-8, with or without a ``0o``/``0``
    prefix, because that is the only reading a permission mode can sensibly
    have — ``--mode 755`` read as decimal would silently mean ``0o1363``.
    Integers are taken as-is: a Python caller writing ``mode=0o755`` has
    already expressed the value, and re-reading it base-8 would corrupt it.

    Returns an ok :class:`~otto.result.Result` whose ``value`` is the ``int``
    mode (or ``None`` when no mode was requested), or a failing one whose
    ``msg`` names the offending input. Mirrors
    :func:`validate_filename_lengths` so the two fold identically in
    :meth:`BaseFileTransfer.put_files`.
    """
    if value is None:
        return Result(Status.Success, value=None)
    # bool subclasses int, so True would silently become 0o1 without this.
    if isinstance(value, bool):
        return Result(
            Status.Error,
            msg=f"invalid octal mode {value!r}: expected octal digits (e.g. 755, 0644, 0o4755)",
        )
    if isinstance(value, int):
        mode = value
    else:
        try:
            mode = int(value, 8)
        except ValueError:
            return Result(
                Status.Error,
                msg=(
                    f"invalid octal mode {value!r}: digits must be 0-7 "
                    f"(e.g. 755, 0644, 0o4755)"
                ),
            )
    if mode < 0:
        return Result(Status.Error, msg=f"invalid octal mode {value!r}: must not be negative")
    if mode > MAX_FILE_MODE:
        return Result(
            Status.Error,
            msg=f"mode 0o{mode:o} out of range (max 0o{MAX_FILE_MODE:o})",
        )
    return Result(Status.Success, value=mode)


def chmod_command(mode: int, paths: list[Path]) -> str:
    """Build one batched ``chmod`` command covering every path in *paths*.

    A single invocation for the whole batch, so a multi-file transfer costs
    one extra round trip rather than one per file. The mode is rendered as
    bare octal because that is what ``chmod(1)`` expects — a ``0o`` prefix
    would be parsed as a filename on most implementations.
    """
    quoted = " ".join(shlex.quote(str(p)) for p in paths)
    return f"chmod {mode:o} {quoted}"
```

- [ ] **Step 4: Re-export from the package**

In `src/otto/host/transfer/__init__.py`, add `MAX_FILE_MODE`, `chmod_command`, and `parse_file_mode` to the `from .base import (...)` block and to `__all__`, keeping both lists alphabetically sorted (`__all__` currently sorts uppercase names first — put `MAX_FILE_MODE` with `TRANSFER_BACKENDS`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov`
Expected: PASS (all parametrized cases)

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/transfer/base.py src/otto/host/transfer/__init__.py tests/unit/host/test_transfer_mode.py
git commit -m "feat(transfer): parse_file_mode + chmod_command helpers

Strings are ALWAYS base-8: --mode 755 means 0o755, never decimal 755.
Ints pass through untouched so a Python caller's 0o755 is not re-read.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 2: The `put_files` seam — capability, pre-flight, `_apply_mode`

**Files:**
- Modify: `src/otto/host/transfer/base.py` (`BaseFileTransfer`, ~L122-247)
- Test: `tests/unit/host/test_transfer_mode.py`

**Interfaces:**
- Consumes: `parse_file_mode`, `chmod_command`, `aggregate_transfer` from Task 1.
- Produces:
  - `BaseFileTransfer.supports_mode: bool` (class attr, default `False`)
  - `BaseFileTransfer._apply_mode(dest_paths: list[Path], mode: int) -> Result` (raises `NotImplementedError` by default)
  - `BaseFileTransfer.put_files(src_files, dest_dir, show_progress=True, mode: int | str | None = None) -> Result`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_transfer_mode.py`:

```python
from otto.host.transfer.base import BaseFileTransfer
from otto.result import Result


class _FakeBackend(BaseFileTransfer):
    """Minimal backend recording what put_files asked of it."""

    def __init__(self, name="fake", supports_mode=False, chmod_result=None, outcomes=None):
        super().__init__(name=name)
        self.supports_mode = supports_mode
        self.run_put_calls = 0
        self.apply_mode_calls: list[tuple[list[Path], int]] = []
        self._chmod_result = chmod_result or Result(Status.Success)
        self._outcomes = outcomes

    async def _run_put(self, src_files, dest_dir, progress_factory):
        self.run_put_calls += 1
        if self._outcomes is not None:
            return dict(self._outcomes)
        return {src: Result(Status.Success, value=dest_dir / src.name) for src in src_files}

    async def _run_get(self, src_files, dest_dir, progress_factory):
        return {}

    async def _apply_mode(self, dest_paths, mode):
        self.apply_mode_calls.append((list(dest_paths), mode))
        return self._chmod_result


@pytest.mark.asyncio
async def test_unsupported_backend_fails_before_any_bytes_move():
    # The point of the pre-flight check: assert _run_put was never reached.
    backend = _FakeBackend(name="zephyr1", supports_mode=False)
    result = await backend.put_files([Path("a.bin")], Path("/RAM:"), False, mode="755")
    assert not result.is_ok
    assert backend.run_put_calls == 0
    assert "zephyr1" in result.msg
    assert "_FakeBackend" in result.msg
    assert "0o755" in result.msg


@pytest.mark.asyncio
async def test_bad_octal_fails_before_any_bytes_move():
    backend = _FakeBackend(supports_mode=True)
    result = await backend.put_files([Path("a.bin")], Path("/opt"), False, mode="789")
    assert not result.is_ok
    assert backend.run_put_calls == 0
    assert "789" in result.msg


@pytest.mark.asyncio
async def test_no_mode_never_calls_apply_mode():
    backend = _FakeBackend(supports_mode=True)
    result = await backend.put_files([Path("a.bin")], Path("/opt"), False)
    assert result.is_ok
    assert backend.apply_mode_calls == []


@pytest.mark.asyncio
async def test_apply_mode_receives_only_landed_dest_paths():
    src_ok, src_bad, src_skip = Path("ok.bin"), Path("bad.bin"), Path("skip.bin")
    backend = _FakeBackend(
        supports_mode=True,
        outcomes={
            src_ok: Result(Status.Success, value=Path("/opt/ok.bin")),
            src_bad: Result(Status.Error, msg="connection reset"),
            src_skip: Result(Status.Skipped, msg="not attempted"),
        },
    )
    await backend.put_files([src_ok, src_bad, src_skip], Path("/opt"), False, mode="755")
    assert backend.apply_mode_calls == [([Path("/opt/ok.bin")], 0o755)]


@pytest.mark.asyncio
async def test_apply_mode_skipped_entirely_when_nothing_landed():
    src = Path("bad.bin")
    backend = _FakeBackend(
        supports_mode=True,
        outcomes={src: Result(Status.Error, msg="connection reset")},
    )
    result = await backend.put_files([src], Path("/opt"), False, mode="755")
    assert backend.apply_mode_calls == []
    assert not result.is_ok
    assert "connection reset" in result.msg


@pytest.mark.asyncio
async def test_chmod_failure_downgrades_but_keeps_dest_path():
    src = Path("a.bin")
    backend = _FakeBackend(
        supports_mode=True,
        chmod_result=Result(Status.Error, msg="chmod: Operation not permitted"),
    )
    result = await backend.put_files([src], Path("/opt"), False, mode="755")
    assert not result.is_ok
    entry = result.value[src]
    assert entry.status is Status.Error
    # The bytes DID land — a caller must be able to tell that apart from a
    # transfer that never happened.
    assert entry.value == Path("/opt/a.bin")
    assert "Operation not permitted" in entry.msg


@pytest.mark.asyncio
async def test_supports_mode_without_apply_mode_raises():
    class _Broken(_FakeBackend):
        _apply_mode = BaseFileTransfer._apply_mode

    backend = _Broken(supports_mode=True)
    with pytest.raises(NotImplementedError, match="_apply_mode"):
        await backend.put_files([Path("a.bin")], Path("/opt"), False, mode="755")


@pytest.mark.asyncio
async def test_default_backend_does_not_support_mode():
    assert BaseFileTransfer.supports_mode is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k "unsupported or bad_octal or apply_mode or chmod_failure or supports_mode or no_mode"`
Expected: FAIL — `put_files() got an unexpected keyword argument 'mode'`

- [ ] **Step 3: Add the capability attribute and hook**

In `src/otto/host/transfer/base.py`, inside `BaseFileTransfer`, add after the `host_families` class attribute and its docstring:

```python
    supports_mode: bool = False
    """Whether this backend can apply a permission ``mode`` after a put.

    Declarative, like :attr:`host_families`: ``put_files`` reads it
    **pre-flight** and refuses a ``mode`` it could never honour before any
    bytes move — a 200 MB upload that ends in "this backend has no
    permission model" helps nobody. A backend setting this ``True`` must
    implement :meth:`_apply_mode`.

    ``False`` for embedded backends (``console``, ``tftp``): a Zephyr
    filesystem has no permission bits to set.
    """
```

Then add the hook, next to the abstract `_run_put`/`_run_get`:

```python
    async def _apply_mode(self, dest_paths: list[Path], mode: int) -> Result:
        """Set *mode* on the already-transferred *dest_paths*.

        Called once per ``put_files`` with the destination paths that
        actually landed — never with files that failed or were skipped,
        and never at all when nothing landed. Implementations should apply
        the mode in a **single** batched operation (see
        :func:`chmod_command`) so a multi-file transfer costs one extra
        round trip rather than N.

        Deliberately not an ``abstractmethod``: backends that cannot
        support modes leave :attr:`supports_mode` ``False`` and never reach
        this. A backend that flips the flag without implementing this gets
        a loud failure rather than a silent no-op.
        """
        raise NotImplementedError(
            f"{type(self).__name__} sets supports_mode = True but does not "
            f"implement _apply_mode()."
        )

    async def _finish_put(self, per_file: dict[Path, Result], mode: int | None) -> dict[Path, Result]:
        """Apply *mode* to the files that landed, downgrading them if chmod fails.

        A chmod failure keeps ``value=dest_path`` on the downgraded entry:
        the bytes did land, only the permissions did not, and a caller must
        be able to tell that apart from a transfer that never happened.
        """
        if mode is None:
            return per_file
        landed = {
            src: r for src, r in per_file.items() if r.status is Status.Success and r.value
        }
        if not landed:
            return per_file
        mode_result = await self._apply_mode([r.value for r in landed.values()], mode)
        if mode_result.is_ok:
            return per_file
        for src, r in landed.items():
            per_file[src] = Result(
                Status.Error,
                value=r.value,
                msg=f"{src}: transferred, but setting mode 0o{mode:o} failed: {mode_result.msg}",
            )
        return per_file
```

- [ ] **Step 4: Wire `put_files`**

Replace the body of `BaseFileTransfer.put_files` (keep `get_files` untouched). Note the pre-flight order — mode parse, then capability, then the existing filename check:

```python
    async def put_files(
        self,
        src_files: list[Path],
        dest_dir: Path,
        show_progress: bool = True,
        mode: int | str | None = None,
    ) -> Result:
        """Upload *src_files* to *dest_dir*, validating filenames and driving progress display.

        Rejects a bad or unhonourable *mode* and over-limit basenames up
        front — in that order, cheapest and most specific first — then
        acquires the process-wide shared Rich progress bar (if
        *show_progress*) and delegates to the concrete backend's
        ``_run_put``. When *mode* is set, the files that landed are chmod-ed
        in one batch afterwards (see :meth:`_apply_mode`).

        *mode* is the permission bits for the uploaded files: an ``int``
        (``0o755``) from Python, or a string that is **always** read as
        octal (``"755"``, ``"0755"``, ``"0o755"``). ``None`` leaves whatever
        permissions the backend's own defaults produce.

        Returns the aggregate :class:`~otto.result.Result` whose ``value``
        maps each source path (exactly as passed) to its per-file
        :class:`~otto.result.Result`.
        """
        from .progress import _acquire_shared_progress, make_rich_progress_factory

        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            return aggregate_transfer(
                {f: Result(mode_check.status, msg=mode_check.msg) for f in src_files}
            )
        resolved_mode: int | None = mode_check.value
        if resolved_mode is not None and not self.supports_mode:
            msg = (
                f"host {self._name!r}: {type(self).__name__} has no permission "
                f"model; cannot apply mode 0o{resolved_mode:o}. Drop the mode "
                f"argument (--mode on the CLI) or transfer with a backend that "
                f"supports it."
            )
            return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in src_files})

        name_check = validate_filename_lengths(
            src_files,
            self._max_filename_len,
            self._name,
        )
        if not name_check.is_ok:
            return aggregate_transfer(
                {f: Result(name_check.status, msg=name_check.msg) for f in src_files}
            )
        if not show_progress:
            per_file = await self._run_put(src_files, dest_dir, None)
        else:
            async with _acquire_shared_progress() as progress:
                per_file = await self._run_put(
                    src_files,
                    dest_dir,
                    make_rich_progress_factory(progress, self._name),
                )
        return aggregate_transfer(await self._finish_put(per_file, resolved_mode))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov`
Expected: PASS

- [ ] **Step 6: Verify nothing regressed**

Run: `uv run pytest tests/unit/host -q --no-cov`
Expected: PASS, 1158+ tests

- [ ] **Step 7: Commit**

```bash
git add src/otto/host/transfer/base.py tests/unit/host/test_transfer_mode.py
git commit -m "feat(transfer): mode pre-flight + _apply_mode hook on put_files

supports_mode is checked BEFORE _run_put, so a mode a backend can never
honour fails without moving bytes. A chmod failure downgrades the entry
but keeps value=dest_path: the bytes landed, the permissions did not.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 3: `UnixFileTransfer._apply_mode` — covers scp, sftp, ftp, nc

**Files:**
- Modify: `src/otto/host/transfer/unix_base.py`
- Test: `tests/unit/host/test_transfer_mode.py`

**Interfaces:**
- Consumes: `chmod_command` (Task 1), `_apply_mode` contract (Task 2).
- Produces: `UnixFileTransfer.supports_mode = True` and its `_apply_mode` override — inherited unchanged by `ScpFileTransfer`, `SftpFileTransfer`, `FtpFileTransfer`, `NcFileTransfer`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_transfer_mode.py`:

```python
from unittest.mock import AsyncMock

from otto.host.transfer.unix_base import UnixFileTransfer
from otto.result import CommandResult


def _unix_backend(exec_cmd):
    return UnixFileTransfer(connections=object(), name="web1", exec_cmd=exec_cmd)


@pytest.mark.asyncio
async def test_unix_backends_support_mode():
    from otto.host.transfer import (
        FtpFileTransfer,
        NcFileTransfer,
        ScpFileTransfer,
        SftpFileTransfer,
    )

    for cls in (ScpFileTransfer, SftpFileTransfer, FtpFileTransfer, NcFileTransfer):
        assert cls.supports_mode is True, cls.__name__


@pytest.mark.asyncio
async def test_unix_apply_mode_issues_exactly_one_batched_chmod():
    exec_cmd = AsyncMock(return_value=CommandResult(Status.Success, value="", command="", retcode=0))
    backend = _unix_backend(exec_cmd)
    result = await backend._apply_mode(
        [Path("/opt/a"), Path("/opt/b"), Path("/opt/c")], 0o755
    )
    assert result.is_ok
    # Batching is the contract, not just the outcome: N files, ONE exec.
    assert exec_cmd.await_count == 1
    assert exec_cmd.await_args.args[0] == "chmod 755 /opt/a /opt/b /opt/c"


@pytest.mark.asyncio
async def test_unix_apply_mode_reports_chmod_failure():
    exec_cmd = AsyncMock(
        return_value=CommandResult(
            Status.Failed, value="chmod: Operation not permitted", command="chmod", retcode=1
        )
    )
    backend = _unix_backend(exec_cmd)
    result = await backend._apply_mode([Path("/opt/a")], 0o755)
    assert not result.is_ok
    assert "Operation not permitted" in result.msg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k unix`
Expected: FAIL — `assert False is True` (`supports_mode` still the inherited `False`)

- [ ] **Step 3: Implement**

In `src/otto/host/transfer/unix_base.py`, add the imports and the override. The imports become:

```python
from pathlib import Path

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status
from .base import BaseFileTransfer, chmod_command
```

Then inside `UnixFileTransfer`, after the `host_families` attribute:

```python
    supports_mode = True
    """Every unix backend ends with the file on a posix filesystem reachable
    through the host's shell, so one ``chmod`` serves scp, sftp, ftp and nc
    alike."""
```

And after `prepare`:

```python
    @override
    async def _apply_mode(self, dest_paths: list[Path], mode: int) -> Result:
        """Chmod the transferred files in one batched command over the host shell.

        Uses the same ``exec_cmd`` seam the backends already hold, so the
        cost is a single extra round trip regardless of file count.
        """
        result = await self._exec_cmd(chmod_command(mode, dest_paths))
        if result.status.is_ok:
            return Result(Status.Success)
        return Result(
            Status.Error,
            msg=result.value or f"chmod exited {result.retcode}",
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/host/transfer/unix_base.py tests/unit/host/test_transfer_mode.py
git commit -m "feat(transfer): mode support for scp/sftp/ftp/nc via UnixFileTransfer

One batched chmod on the shared unix base serves all four backends; the
test asserts ONE exec for N files, so the batching cannot silently rot.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 4: `LocalFileTransfer._apply_mode` + the embedded loud-failure guard

**Files:**
- Modify: `src/otto/host/local_host.py` (`LocalFileTransfer`, ~L46-103)
- Test: `tests/unit/host/test_transfer_mode.py`

**Interfaces:**
- Consumes: the `_apply_mode` contract (Task 2).
- Produces: `LocalFileTransfer.supports_mode = True` and its `_apply_mode` override.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_transfer_mode.py`:

```python
@pytest.mark.asyncio
async def test_local_transfer_really_chmods(tmp_path):
    from otto.host.local_host import LocalFileTransfer

    src_dir, dest_dir = tmp_path / "src", tmp_path / "dest"
    src_dir.mkdir()
    src = src_dir / "app.bin"
    src.write_text("#!/bin/sh\n")
    src.chmod(0o600)

    backend = LocalFileTransfer(name="local")
    result = await backend.put_files([src], dest_dir, False, mode="755")

    assert result.is_ok, result.msg
    assert (dest_dir / "app.bin").stat().st_mode & 0o7777 == 0o755


@pytest.mark.asyncio
async def test_local_transfer_without_mode_leaves_copy2_permissions(tmp_path):
    from otto.host.local_host import LocalFileTransfer

    src_dir, dest_dir = tmp_path / "src", tmp_path / "dest"
    src_dir.mkdir()
    src = src_dir / "app.bin"
    src.write_text("x")
    src.chmod(0o600)

    backend = LocalFileTransfer(name="local")
    assert (await backend.put_files([src], dest_dir, False)).is_ok
    # shutil.copy2 preserves the source mode; no mode means we do not touch it.
    assert (dest_dir / "app.bin").stat().st_mode & 0o7777 == 0o600


@pytest.mark.asyncio
async def test_embedded_transfer_refuses_mode_loudly():
    from otto.host.transfer.console import ConsoleFileTransfer

    backend = ConsoleFileTransfer(name="zephyr1", exec_cmd=AsyncMock())
    result = await backend.put_files([Path("app.bin")], Path("/RAM:"), False, mode="755")

    assert not result.is_ok
    assert "zephyr1" in result.msg
    assert "no permission model" in result.msg
    # Never silently ignored.
    assert ConsoleFileTransfer.supports_mode is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k "local or embedded"`
Expected: FAIL — the local case fails with the unsupported-backend message (`supports_mode` still `False`); the embedded case should already pass.

- [ ] **Step 3: Implement**

In `src/otto/host/local_host.py`, inside `LocalFileTransfer`, add after the class docstring:

```python
    supports_mode = True
    """A local copy lands on the machine's own filesystem, so ``Path.chmod``
    applies the mode directly — no shell, no transport."""
```

And add the override after `_run_get`:

```python
    @override
    async def _apply_mode(self, dest_paths: list[Path], mode: int) -> Result:
        """Chmod the copied files directly, off the event loop.

        ``Path.chmod`` is a blocking syscall; the whole batch runs in one
        worker thread rather than one hop per file.
        """

        def _chmod_all() -> None:
            for path in dest_paths:
                path.chmod(mode)

        try:
            await asyncio.to_thread(_chmod_all)
        except OSError as e:
            return Result(Status.Error, msg=str(e))
        return Result(Status.Success)
```

`asyncio`, `Path`, `Result`, `Status` and `override` are already imported in this module.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/host/local_host.py tests/unit/host/test_transfer_mode.py
git commit -m "feat(transfer): mode support for LocalFileTransfer

Also pins the embedded contract: console/tftp refuse a mode loudly and
name the host, rather than accepting it and doing nothing.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 5: `mode` on the host `put` methods + dry-run banner + the CLI decimal trap

This is where the parameter becomes reachable from the CLI, so the decimal-trap guard lands here.

**Files:**
- Modify: `src/otto/host/host.py` (`Host` protocol `put` ~L329-341, `BaseHost.put` ~L736-742, `_dry_run_transfer` ~L476-494)
- Modify: `src/otto/host/unix_host.py` (`put` ~L762-779)
- Modify: `src/otto/host/embedded_host.py` (`put` ~L544-575)
- Modify: `src/otto/host/local_host.py` (`LocalHost.put` ~L326-347)
- Test: `tests/unit/host/test_transfer_mode.py`, `tests/unit/cli/test_dynamic_host_commands.py`

**Interfaces:**
- Consumes: `put_files(..., mode=...)` (Task 2), `parse_file_mode` (Task 1).
- Produces: `put(src_files, dest_dir, mode=None, show_progress=True)` on all host classes; `_dry_run_transfer(action, files, dest, mode=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_transfer_mode.py`:

```python
from typing import get_args

from otto.cli.param_synth import build_cli_binding
from otto.host.unix_host import UnixHost


def test_put_exposes_mode_as_a_string_cli_option():
    binding = build_cli_binding(UnixHost.put)
    assert "mode" in [p.name for p in binding.params]
    mode_param = next(p for p in binding.params if p.name == "mode")
    # Typer must see a STRING, so it can never coerce "755" to decimal 755.
    assert get_args(mode_param.annotation)[0] is str
    assert mode_param.default is None
```

And in `tests/unit/cli/test_dynamic_host_commands.py` — which already imports
`inspect`, `typer`, `CliRunner` and `UnixHost` — append the end-to-end guard:

```python
def test_cli_mode_755_is_octal_not_decimal():
    """`--mode 755` must mean 0o755 (493), never decimal 755 (0o1363).

    Drives the REAL synthesized parameter through Typer's parser. If `mode`
    were annotated `int`, Typer would hand over the integer 755 and the
    string assertion below would fail — which is what makes this a guard
    rather than a restatement of parse_file_mode's own unit test.
    """
    from otto.cli.param_synth import build_cli_binding
    from otto.host.transfer.base import parse_file_mode

    mode_param = next(p for p in build_cli_binding(UnixHost.put).params if p.name == "mode")
    captured = {}

    def cmd(mode=None):
        captured["raw"] = mode

    cmd.__signature__ = inspect.Signature([mode_param])
    app = typer.Typer()
    app.command()(cmd)

    result = CliRunner().invoke(app, ["--mode", "755"])
    assert result.exit_code == 0, result.output
    assert captured["raw"] == "755"  # a STRING leaves the CLI...
    assert parse_file_mode(captured["raw"]).value == 0o755  # ...read base-8
    assert parse_file_mode(captured["raw"]).value != 755  # ...never decimal
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k cli_option`
Expected: FAIL — `assert 'mode' in [...]`

**Prove the decimal trap is a real guard:** temporarily change the `mode` annotation in `unix_host.py` to `Annotated[int | None, Opt(...)]`, run `uv run pytest tests/unit/host/test_transfer_mode.py -k cli_option -q --no-cov`, and confirm it FAILS on the `is str` assertion. Revert before continuing. A guard that passes against the wrong implementation is not a guard.

- [ ] **Step 3: Update the dry-run banner**

In `src/otto/host/host.py`, replace `_dry_run_transfer`:

```python
    def _dry_run_transfer(
        self,
        action: str,
        files: list[Path],
        dest: Path,
        mode: int | str | None = None,
    ) -> Result:
        """Return a synthetic per-file transfer result for dry-run mode.

        Builds the same ``value: dict[Path, Result]`` shape as a real transfer,
        keyed by the source paths exactly as passed. Every file is marked
        ``Status.Skipped`` (which counts as ok) with a ``[DRY RUN]`` diagnostic,
        so the folded aggregate is Skipped and its ``msg`` names the action.

        A *mode* is parsed here even though nothing is transferred: a typo'd
        ``--mode 789`` is the caller's own input and costs nothing to catch,
        so a dry run should catch it. Backend capability is **not** checked —
        that belongs to the real transfer, which is where the backend is
        actually selected and used.
        """
        from .transfer.base import parse_file_mode

        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            self._log_command(f"[DRY RUN] {action}: {mode_check.msg}")
            return Result(
                Status.Error,
                value={src: Result(Status.Error, msg=mode_check.msg) for src in files},
                msg=mode_check.msg,
            )
        suffix = f" (mode 0o{mode_check.value:o})" if mode_check.value is not None else ""
        file_names = ", ".join(str(f) for f in files)
        self._log_command(f"[DRY RUN] {action}: {file_names} -> {dest}{suffix}")
        per_file = {
            src: Result(Status.Skipped, value=dest / src.name, msg=f"[DRY RUN] {action}: {src}")
            for src in files
        }
        # Every file is Skipped (ok), so the fold would report Success; a dry-run
        # transfer is explicitly Skipped, and the aggregate msg carries the banner.
        return Result(
            Status.Skipped,
            value=per_file,
            msg=f"[DRY RUN] {action}: {file_names} -> {dest}{suffix}",
        )
```

- [ ] **Step 4: Add `mode` to the protocol and base**

In `src/otto/host/host.py`, add `mode: int | str | None = None,` after `dest_dir: Path,` in both the `Host` protocol `put` (~L329) and `BaseHost.put` (~L736). Extend the protocol docstring with:

```
        ``mode`` sets the permission bits on the uploaded files: an ``int``
        (``0o755``) from Python, or a string always read as octal
        (``"755"``, ``"0755"``, ``"0o755"``). ``None`` leaves the backend's
        default permissions. Hosts whose transfer backend has no permission
        model (embedded ``console``/``tftp``) reject a non-``None`` mode
        before transferring anything.
```

- [ ] **Step 5: Thread it through the three delegating hosts**

In each of `unix_host.py`, `embedded_host.py`, and `local_host.py`, add this parameter to `put` immediately after `dest_dir: Path,`:

```python
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
```

Add `Opt` to the `from ..utils import ...` line in each file (all three already import `Arg`/`Exclude`/`Status`/`cli_exposed` from there). Then in each body, pass `mode` to the dry-run call and to `put_files`:

```python
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir, mode)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.put_files(src_files, dest_dir, show_progress, mode)
```

(`LocalHost.put` has no `SuppressCommandOutput` wrapper — keep its existing shape and just add the `mode` argument to `put_files`.)

Extend each `put` docstring with a line naming the mode behaviour; for `embedded_host.py` say explicitly that a non-``None`` mode is rejected because the device filesystem has no permission bits.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py tests/unit/cli/test_dynamic_host_commands.py -q --no-cov`
Expected: PASS

- [ ] **Step 7: Run the full host + cli suites**

Run: `uv run pytest tests/unit/host tests/unit/cli -q --no-cov`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/otto/host/host.py src/otto/host/unix_host.py src/otto/host/embedded_host.py src/otto/host/local_host.py tests/unit/host/test_transfer_mode.py tests/unit/cli/test_dynamic_host_commands.py
git commit -m "feat(host): --mode on put for unix, embedded and local hosts

The CLI option is a STRING so Typer cannot coerce 755 to decimal; the
method parses it base-8. Guard proven red against an int annotation.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 6: Docker — explicit in-container chmod

**Files:**
- Modify: `src/otto/host/docker_host.py` (`put` ~L437-500)
- Test: `tests/unit/host/test_transfer_mode.py`

**Interfaces:**
- Consumes: `chmod_command`, `parse_file_mode` (Task 1), `_dry_run_transfer(..., mode)` (Task 5).
- Produces: `DockerContainerHost.put(src_files, dest_dir, mode=None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_transfer_mode.py`:

```python
@pytest.mark.asyncio
async def test_docker_put_chmods_inside_the_container(monkeypatch):
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost.__new__(DockerContainerHost)
    src = Path("app.bin")
    dest_dir = Path("/opt/bin")

    parent_execs: list[str] = []

    async def fake_parent_exec(cmd, *a, **kw):
        parent_execs.append(cmd)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    async def fake_parent_put(files, stage, *a, **kw):
        from otto.host.transfer.base import aggregate_transfer

        return aggregate_transfer({f: Result(Status.Success, value=stage / f.name) for f in files})

    container_execs: list[str] = []

    async def fake_exec(cmd, *a, **kw):
        container_execs.append(cmd)
        return CommandResult(Status.Success, value="", command=cmd, retcode=0)

    parent = type("P", (), {"exec": staticmethod(fake_parent_exec), "put": staticmethod(fake_parent_put)})()
    monkeypatch.setattr(type(host), "parent", property(lambda self: parent))
    monkeypatch.setattr(type(host), "container_id", property(lambda self: "abc123"))
    monkeypatch.setattr(host, "exec", fake_exec, raising=False)
    monkeypatch.setattr(host, "_ensure_running", AsyncMock(), raising=False)

    result = await host.put([src], dest_dir, mode="755")

    assert result.is_ok, result.msg
    # chmod runs INSIDE the container, not on the parent, and exactly once.
    assert container_execs == ["chmod 755 /opt/bin/app.bin"]
    assert not any("chmod" in c for c in parent_execs)
    # The staging put must NOT carry the mode — staging is deleted anyway,
    # and relying on `docker cp` to preserve it is what we are avoiding.
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k docker`
Expected: FAIL — `put() got an unexpected keyword argument 'mode'`

- [ ] **Step 3: Implement**

In `src/otto/host/docker_host.py`, add `mode` to the `put` signature after `dest_dir: Path,`:

```python
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
```

Add `Opt` to the `from ..utils import Arg, Status, cli_exposed` line. Change the dry-run line to pass `mode`, and add the parse right after it:

```python
        if is_dry_run():
            return self._dry_run_transfer("PUT", files, dest_dir, mode)
        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            return aggregate_transfer(
                {f: Result(Status.Error, msg=mode_check.msg) for f in files}
            )
        resolved_mode: int | None = mode_check.value
        await self._ensure_running()
```

Extend the local import at the top of the method body:

```python
        from .transfer import aggregate_transfer, chmod_command, parse_file_mode
```

Then, in the `try` block, replace `return aggregate_transfer(per_file)` (the one after the `docker cp` loop) with:

```python
            if resolved_mode is not None:
                landed = [
                    r.value for r in per_file.values() if r.status is Status.Success and r.value
                ]
                if landed:
                    # chmod INSIDE the container: `self.exec` is a docker exec,
                    # while `self.parent.exec` would chmod the staging copy on
                    # the parent. Deliberately not relying on `docker cp` to
                    # preserve modes — that is undocumented third-party
                    # behaviour in the trust path.
                    chmod = await self.exec(chmod_command(resolved_mode, landed))
                    if not chmod.status.is_ok:
                        for f, r in per_file.items():
                            if r.status is Status.Success:
                                per_file[f] = Result(
                                    Status.Error,
                                    value=r.value,
                                    msg=(
                                        f"{f}: transferred, but setting mode "
                                        f"0o{resolved_mode:o} failed: {chmod.value}"
                                    ),
                                )
            return aggregate_transfer(per_file)
```

Leave `self.parent.put(files, stage)` unchanged — the staging copy must not carry the mode. Extend the method docstring to say chmod runs inside the container after the copies land.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/host/test_transfer_mode.py -q --no-cov -k docker`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/otto/host/docker_host.py tests/unit/host/test_transfer_mode.py
git commit -m "feat(docker): apply put --mode inside the container

Explicit docker-exec chmod after the copies land, rather than staging
the mode and trusting `docker cp` to preserve it.

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

### Task 7: Documentation + full gates

**Files:**
- Modify: `docs/guide/hosts/index.md` (transfer section, ~L88-113)
- Modify: `docs/guide/cli-reference.md` (`put` / `get` arguments, ~L261-269)

- [ ] **Step 1: Document the CLI option**

In `docs/guide/cli-reference.md`, under `### put / get arguments`, add a table row (matching the existing option-table style in that file):

| `--mode TEXT` | backend default | **`put` only.** Octal permission bits for the uploaded file(s) — `755`, `0644`, `0o4755`. Always read as octal, never decimal. Rejected on hosts whose transfer backend has no permission model (embedded `console`/`tftp`). |

- [ ] **Step 2: Document the host-API behaviour**

In `docs/guide/hosts/index.md`, in the file-transfer section, add after the existing `put`/`get` example:

````markdown
`put` takes an optional `mode` — the permission bits the uploaded files
should end up with:

```python
res = await host.put([Path("app.bin")], Path("/opt/bin"), mode=0o755)
```

From the CLI the same value is written as an octal string, which is
**always** read base-8 — `--mode 755` means `0o755`, never decimal 755:

```console
$ otto host web1 put ./app.bin /opt/bin --mode 755
```

The mode is applied after the bytes land, in a single batched `chmod`
covering the whole transfer. If the transfer succeeds but the `chmod`
fails, those files are reported as errors that still carry their
destination path — so a caller can tell "never arrived" apart from
"arrived with the wrong permissions".

Embedded hosts (`console`, `tftp`) have no permission model; passing a
`mode` to one fails before any bytes move rather than being silently
ignored.
````

Add a `mode` note to the per-class semantics list further down the same section.

- [ ] **Step 3: Typecheck**

Run: `make typecheck`
Expected: clean. `ty` runs only in this session, so this is the first time the new annotations are checked.

- [ ] **Step 4: Lint**

Run: `nox -s lint`
Expected: clean (ruff check + ruff format --check).

- [ ] **Step 5: Clean docs build**

Run: `rm -rf docs/_build && make docs`
Expected: no warnings. Incremental Sphinx misses broken `:doc:`/`:meth:` refs in docstrings, so the `rm -rf` is required, not optional.

- [ ] **Step 6: Full unit suite**

Run: `uv run pytest tests/unit -q`
Expected: PASS, ≥1158 tests (the baseline) plus the new ones.

- [ ] **Step 7: Task gate**

Run: `make coverage`
Expected: PASS. Use `scripts/junit_failures.py` to triage any failure — `make coverage | tail` eats make's exit code.

- [ ] **Step 8: Commit**

```bash
git add docs/guide/hosts/index.md docs/guide/cli-reference.md
git commit -m "docs(transfer): document put --mode and its octal parsing

Assisted-by: Claude Opus 4.8 (1M context)"
```

---

## Self-Review

**Spec coverage:** goals → Tasks 2/5 (one parameter honoured by every capable backend), Task 1 (unmisreadable octal), Tasks 2/4 (loud embedded failure), Tasks 1/2/3 (one parse site, one error site, batched chmod), Task 3 (custom backends inherit via `UnixFileTransfer`). Non-goals are respected: no `get` mode, no symbolic modes, no `chown`, no `PosixFileOps.chmod`. Every spec section maps to a task; the `param_synth`-must-not-change constraint is honoured (no task touches it).

**Type consistency:** `parse_file_mode` returns `Result` (never a bare `int`) at every call site — Tasks 2, 5, 6. `_apply_mode(dest_paths: list[Path], mode: int) -> Result` is identical in Tasks 2, 3, 4. `supports_mode` is a plain `bool` class attribute throughout. `mode: int | str | None` is the parameter type in every `put` and in `put_files`; only `_apply_mode` takes a resolved `int`, and it is only ever reached after `parse_file_mode`.
