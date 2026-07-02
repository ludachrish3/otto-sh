# Result-Type Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `CommandStatus`, `RunResult`, and every `tuple[Status, str]` host-verb return with one `Result`/`CommandResult`/`Results` family (spec: `docs/superpowers/specs/2026-07-01-result-type-unification-design.md`).

**Architecture:** New leaf module `src/otto/result.py` holds the frozen-dataclass family with a polymorphic `exit_code` property. The old types are deleted up front so `ty` and the suite enumerate every call site; conversion proceeds in dependency order (command core → transfers → scalar verbs → CLI/callers), each task keeping its own scoped tests green, with the full gate at the end.

**Tech Stack:** Python 3.10+ dataclasses, Typer 0.26, pytest, ruff (`select=ALL`), ty, Sphinx executable doctests.

## Global Constraints

- **NEVER run `git commit`** — stage exact paths (`git add <paths>`, never `git add -u` or `git add -A`) and end each task by reporting the staged paths plus a paste-able commit message. Chris commits.
- **No `from __future__ import annotations`** anywhere (breaks the Sphinx nitpicky docs gate). Use real 3.10+ annotations (`X | None`) and module-top imports.
- After any code edit: `uv run ruff check . && uv run ruff format . && uv run ruff check .` — format is NOT lint-neutral, always re-check.
- `ty` only runs via `uv run nox -s typecheck`. Run it at the end of every task that touches `src/` (whole-repo, catches call sites your scoped tests miss).
- Test runs: single passes with `-n auto` (e.g. `uv run pytest tests/unit/host -n auto`). Never loop test runs on this VM.
- **The full suite is expected RED between Task 2 and Task 5** (delete-first). Each task's *scoped* tests must be green. The full gate runs only in Task 7.
- If executing in a fresh worktree: run `uv sync` once before anything else.
- Docstrings must satisfy ruff's pydocstyle rules (D-rules are enforced); every public symbol gets a docstring.
- Doctest examples in `src/` docstrings execute in the docs gate — they must actually run.

---

### Task 1: The `Result` family (`otto/result.py`)

**Files:**
- Create: `src/otto/result.py`
- Create: `tests/unit/result/test_result.py`
- Modify: `src/otto/__init__.py` (TYPE_CHECKING imports, `__all__`, `_LAZY_EXPORTS`)

**Interfaces:**
- Consumes: `otto.utils.Status` (enum: Success=0, Failed=1, Error=2, Unstable=3, Skipped=4; `.is_ok` True for Success/Skipped).
- Produces (every later task relies on these exact names):
  - `Result(status: Status, value: Any = None, msg: str = "")` — frozen; `.is_ok: bool`, `__bool__`, `.exit_code: int`.
  - `CommandResult(status, value=None, msg="", command: str = "", retcode: int = -1)` — frozen subclass; `value` holds the command's output string; ssh-like `.exit_code`.
  - `Results(status, value=None, msg="")` — frozen subclass of `Result` AND `Sequence[CommandResult]`; `value` is `list[CommandResult]`; classmethod `Results.collect(items, msg="")` computes the aggregate; `.only`, `.first_failure`, `.exit_code`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/result/__init__.py` (empty) and `tests/unit/result/test_result.py`:

```python
"""Unit tests for the otto.result family (spec 2026-07-01)."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from otto.result import CommandResult, Result, Results
from otto.utils import Status


class TestResult:
    def test_defaults(self):
        r = Result(Status.Success)
        assert r.value is None
        assert r.msg == ""
        assert r.is_ok
        assert bool(r)

    def test_bool_is_status_not_value(self):
        assert not Result(Status.Failed, value=[1, 2, 3])
        assert Result(Status.Success, value=[])

    def test_frozen(self):
        with pytest.raises(FrozenInstanceError):
            Result(Status.Success).status = Status.Failed  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (Status.Success, 0),
            (Status.Skipped, 0),  # is_ok -> 0, never 4
            (Status.Failed, 1),
            (Status.Error, 2),
            (Status.Unstable, 3),
        ],
    )
    def test_exit_code_status_mapping(self, status, code):
        assert Result(status).exit_code == code

    def test_value_can_hold_transfer_mapping(self):
        per_file = {Path("a.bin"): Result(Status.Success, value=Path("/dst/a.bin"))}
        r = Result(Status.Success, value=per_file)
        assert r.value[Path("a.bin")].value == Path("/dst/a.bin")


class TestCommandResult:
    def test_fields(self):
        cr = CommandResult(Status.Success, value="hi", command="echo hi", retcode=0)
        assert cr.value == "hi"
        assert cr.command == "echo hi"
        assert cr.retcode == 0
        assert cr.exit_code == 0

    def test_exit_code_is_retcode_when_failed(self):
        assert CommandResult(Status.Failed, command="x", retcode=42).exit_code == 42

    def test_exit_code_never_ran_is_255(self):
        assert CommandResult(Status.Error, command="x", retcode=-1).exit_code == 255

    def test_exit_code_failed_with_retcode_zero_falls_back_to_status(self):
        # e.g. expect mismatch: command exited 0 but otto marked it Failed
        assert CommandResult(Status.Failed, command="x", retcode=0).exit_code == 1

    def test_is_a_result(self):
        assert isinstance(CommandResult(Status.Success), Result)


def _cr(status: Status, retcode: int = 0, command: str = "c") -> CommandResult:
    return CommandResult(status, value="", command=command, retcode=retcode)


class TestResults:
    def test_collect_all_ok(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Skipped)])
        assert res.status is Status.Success
        assert res.is_ok

    def test_collect_aggregate_is_first_non_ok(self):
        res = Results.collect(
            [_cr(Status.Success), _cr(Status.Error, 5), _cr(Status.Failed, 1)]
        )
        assert res.status is Status.Error

    def test_collect_empty_is_success(self):
        res = Results.collect([])
        assert res.status is Status.Success
        assert len(res) == 0

    def test_sequence_behavior(self):
        items = [_cr(Status.Success, command="a"), _cr(Status.Success, command="b")]
        res = Results.collect(items)
        assert len(res) == 2
        assert res[0].command == "a"
        assert [c.command for c in res] == ["a", "b"]
        assert res[0:2] == items  # slice returns a plain list

    def test_bool_is_status_not_emptiness(self):
        assert Results.collect([])  # empty but ok -> truthy
        assert not Results.collect([_cr(Status.Failed, 1)])

    def test_only(self):
        assert Results.collect([_cr(Status.Success, command="a")]).only.command == "a"

    @pytest.mark.parametrize("n", [0, 2])
    def test_only_raises_unless_exactly_one(self, n):
        with pytest.raises(ValueError, match="exactly 1"):
            _ = Results.collect([_cr(Status.Success)] * n).only

    def test_first_failure(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Failed, 7)])
        assert res.first_failure is not None
        assert res.first_failure.retcode == 7
        assert Results.collect([_cr(Status.Success)]).first_failure is None

    def test_exit_code_delegates_to_first_failure(self):
        res = Results.collect([_cr(Status.Success), _cr(Status.Failed, 42)])
        assert res.exit_code == 42
        assert Results.collect([_cr(Status.Success)]).exit_code == 0

    def test_is_a_result(self):
        assert isinstance(Results.collect([]), Result)


def test_top_level_lazy_exports():
    import otto

    assert otto.Result is Result
    assert otto.CommandResult is CommandResult
    assert otto.Results is Results
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/result -n auto`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'otto.result'`

- [ ] **Step 3: Implement `src/otto/result.py`**

```python
"""Unified result family for host verbs.

Every ``@cli_exposed`` host verb returns a member of this family (except
``interact()``, which returns ``None``): scalar verbs return :class:`Result`
or :class:`CommandResult`; ``run()`` returns :class:`Results`. The CLI derives
its exit code from :attr:`Result.exit_code`.

>>> from otto.utils import Status
>>> r = Result(Status.Success, value=["mod_a"], msg="")
>>> r.is_ok, r.exit_code
(True, 0)
>>> cr = CommandResult(Status.Failed, value="", command="false", retcode=1)
>>> cr.exit_code
1
>>> res = Results.collect([cr])
>>> res.only.command, res.exit_code, bool(res)
('false', 1, False)
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, overload

from otto.utils import Status


@dataclass(frozen=True)
class Result:
    """Outcome of a host verb: status + optional payload + human diagnostic."""

    status: Status
    """Aggregate outcome; see :class:`~otto.utils.Status`."""

    value: Any = None
    """Verb-specific payload (see the per-verb table in the host guide)."""

    msg: str = ""
    """Human diagnostic; empty on success."""

    @property
    def is_ok(self) -> bool:
        """True when :attr:`status` counts as passing (Success or Skipped)."""
        return self.status.is_ok

    def __bool__(self) -> bool:
        """Truthiness follows :attr:`is_ok`, never the payload.

        An empty-but-successful result is truthy; a failed result carrying a
        payload is falsy.
        """
        return self.is_ok

    @property
    def exit_code(self) -> int:
        """CLI exit code: 0 when ok, otherwise ``status.value``."""
        return 0 if self.is_ok else self.status.value


@dataclass(frozen=True)
class CommandResult(Result):
    """Result of one shell command; :attr:`value` holds the command's output."""

    command: str = ""
    """The command that was issued."""

    retcode: int = -1
    """Shell return code; -1 means the command never ran."""

    @property
    def exit_code(self) -> int:
        """ssh-like CLI exit code: the command's own retcode.

        0 when ok; 255 when the command never ran (retcode -1, matching ssh's
        connection-error convention); ``status.value`` when the command exited
        0 but otto marked it failed (e.g. an expect mismatch).
        """
        if self.is_ok:
            return 0
        if self.retcode == -1:
            return 255
        if self.retcode != 0:
            return self.retcode
        return self.status.value


@dataclass(frozen=True)
class Results(Result, Sequence[CommandResult]):
    """Aggregate over per-command results; itself a :class:`Result`.

    Returned by ``run()`` only. :attr:`value` is ``list[CommandResult]`` in
    execution order. Build with :meth:`collect`, which computes the aggregate
    status: ``Success`` when every entry is ok, otherwise the first non-ok
    entry's status. Truthiness follows :attr:`is_ok`, not emptiness.
    """

    @classmethod
    def collect(cls, items: Sequence[CommandResult], msg: str = "") -> "Results":
        """Build a Results from per-command entries, computing the aggregate."""
        entries = list(items)
        status = next((e.status for e in entries if not e.is_ok), Status.Success)
        return cls(status=status, value=entries, msg=msg)

    def __len__(self) -> int:
        return len(self.value)

    @overload
    def __getitem__(self, index: int) -> CommandResult: ...
    @overload
    def __getitem__(self, index: slice) -> list[CommandResult]: ...
    def __getitem__(self, index: int | slice) -> "CommandResult | list[CommandResult]":
        return self.value[index]

    def __iter__(self) -> Iterator[CommandResult]:
        return iter(self.value)

    @property
    def only(self) -> CommandResult:
        """The sole entry when exactly one command ran; ValueError otherwise."""
        if len(self.value) != 1:
            raise ValueError(
                f"Results.only requires exactly 1 command result, got {len(self.value)}"
            )
        return self.value[0]

    @property
    def first_failure(self) -> CommandResult | None:
        """The first non-ok entry, or None when everything passed."""
        return next((e for e in self.value if not e.is_ok), None)

    @property
    def exit_code(self) -> int:
        """0 when ok, else the first failing command's :attr:`exit_code`."""
        failure = self.first_failure
        return 0 if failure is None else failure.exit_code
```

- [ ] **Step 4: Wire lazy exports in `src/otto/__init__.py`**

Add to the `TYPE_CHECKING` block:

```python
    from otto.result import CommandResult, Result, Results
```

Add to `__all__` (keep it sorted — ruff enforces): `"CommandResult"`, `"Result"`, `"Results"`.

Add to `_LAZY_EXPORTS`:

```python
    "Result": ("otto.result", "Result"),
    "CommandResult": ("otto.result", "CommandResult"),
    "Results": ("otto.result", "Results"),
```

Do NOT add any eager import — the module's import-light invariant (and the import-budget guard) depends on it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/result tests/unit/import_budget -n auto`
Expected: all PASS (import-budget guard proves no eager import snuck in).

- [ ] **Step 6: Lint + typecheck**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Run: `uv run nox -s typecheck`
Expected: clean (nothing else references `otto.result` yet).

- [ ] **Step 7: Stage + report**

```bash
git add src/otto/result.py src/otto/__init__.py tests/unit/result/
```

Paste-able commit message:

```
feat(result): add the unified Result/CommandResult/Results family

New otto.result module per the 2026-07-01 result-type-unification spec:
frozen-dataclass family with polymorphic CLI exit_code (ssh-like retcode
passthrough for commands, status mapping elsewhere), Results.collect
aggregation preserving RunResult semantics, lazy top-level exports.
```

---

### Task 2: Command core — delete `CommandStatus`/`RunResult`, convert run/oneshot

**Files:**
- Modify: `src/otto/utils.py` (delete `CommandStatus`)
- Modify: `src/otto/host/host.py` (delete `RunResult`; convert `_run_cmds_with_budget`, protocol + `BaseHost` `run`/`oneshot` annotations and docstrings)
- Modify: `src/otto/host/session.py`, `src/otto/host/unix_host.py`, `src/otto/host/local_host.py`, `src/otto/host/embedded_host.py`, `src/otto/host/docker_host.py`, `src/otto/host/remote_host.py`, `src/otto/host/__init__.py` (construction/annotation sites)
- Test: update `tests/unit/host/test_run_timeout.py`, `test_session.py`, `test_session_concurrency.py`, `test_session_logging.py`, `test_unix_host.py`, `test_embedded_host.py`, `test_docker_host.py`, `test_shell_command.py`, `test_privilege.py`, `test_hop.py`

**Interfaces:**
- Consumes: `Result`, `CommandResult`, `Results` (+ `Results.collect`) from Task 1.
- Produces: `Host.run(...) -> Results`; `Host.oneshot(...) -> CommandResult`; `_run_cmds_with_budget(run_one: Callable[[ShellCommand, float | None], Awaitable[CommandResult]], cmds, timeout) -> Results`. Attribute mapping used by every later task: `.output` → `.value`, `.statuses` → iterate the `Results` itself (or `.value`), `RunResult.only` → `Results.only`.

- [ ] **Step 1: Delete the old types**

In `src/otto/utils.py`: delete the entire `class CommandStatus(NamedTuple)` block (utils.py:196–~217). In `src/otto/host/host.py`: delete the entire `class RunResult` block (host.py:94–119).

- [ ] **Step 2: Enumerate every break**

Run: `uv run nox -s typecheck 2>&1 | tail -40` and `grep -rn "CommandStatus\|RunResult" src/ tests/ | grep -v "docs/"`
Expected: errors in exactly the src files listed above plus `cli/run.py`, `cli/expose.py`, `monitor/collector.py`, `monitor/parsers.py`, `configmodule/*`, `context.py`, transfer files — the src files NOT in this task's list are handled in Tasks 3–5; leave them broken.

- [ ] **Step 3: Convert construction sites (mechanical rule)**

In this task's files, apply everywhere:

```python
# BEFORE
CommandStatus(command, output, status, retcode)
# AFTER (keyword form — value replaces output)
CommandResult(status=status, value=output, command=command, retcode=retcode)
```

`CommandStatus` is a NamedTuple constructed positionally `(command, output, status, retcode)`; `CommandResult`'s field order differs, so ALWAYS convert to keyword arguments. Representative example from `_run_cmds_with_budget` (host.py:180):

```python
# BEFORE
statuses.append(
    CommandStatus(sc.cmd, "", Status.Error, -1)
)
# AFTER
entries.append(
    CommandResult(status=Status.Error, value="", command=sc.cmd, retcode=-1)
)
```

- [ ] **Step 4: Convert `_run_cmds_with_budget` aggregation**

```python
# BEFORE (host.py:156-160 + tail)
async def _run_cmds_with_budget(
    run_one: Callable[[ShellCommand, float | None], Awaitable[CommandStatus]],
    cmds: list[ShellCommand],
    timeout: float | None,
) -> RunResult:
    ...
    overall_status = Status.Success
    statuses: list[CommandStatus] = []
    ...
    return RunResult(status=overall_status, statuses=statuses)

# AFTER
async def _run_cmds_with_budget(
    run_one: Callable[[ShellCommand, float | None], Awaitable[CommandResult]],
    cmds: list[ShellCommand],
    timeout: float | None,
) -> Results:
    ...
    entries: list[CommandResult] = []
    ...
    return Results.collect(entries)
```

Delete the manual `overall_status` bookkeeping — `Results.collect` computes the aggregate with identical first-non-ok semantics. Import at module top: `from otto.result import CommandResult, Result, Results`.

- [ ] **Step 5: Update annotations + docstrings in this task's files**

Every `-> RunResult` becomes `-> Results`; every `-> CommandStatus` becomes `-> CommandResult`. Rewrite the `run`/`oneshot` docstring Returns sections (protocol at host.py:242/270 and implementations): run returns "A :class:`~otto.result.Results` aggregating one :class:`~otto.result.CommandResult` per command"; oneshot returns "A :class:`~otto.result.CommandResult`; ``value`` holds the output". Check `src/otto/host/__init__.py` re-exports: replace any `RunResult`/`CommandStatus` re-export with the new names (keep re-exporting from `otto.result` so `from otto.host import ...` callers keep working).

- [ ] **Step 6: Update this task's test files**

Mechanical rule for all 10 listed test files:
- `from otto.utils import CommandStatus` → `from otto.result import CommandResult` (and `Results` where used)
- `CommandStatus(cmd, out, status, rc)` → `CommandResult(status=status, value=out, command=cmd, retcode=rc)`
- `result.only.output` → `result.only.value`; `result.statuses` → `list(result)` or index the `Results` directly
- assertions on `RunResult` type → `Results`

- [ ] **Step 7: Run scoped tests**

Run: `uv run pytest tests/unit/host tests/unit/result -n auto`
Expected: PASS. (Transfer tests in that directory still pass — transfers aren't converted yet and still return tuples internally; only `test_transfer_*` failures would indicate you touched Task 3 scope too early.) If `test_transfer_*` files import the deleted names, apply Step 6's mechanical rule to just those imports.

- [ ] **Step 8: Lint**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`
Expected: clean for this task's files (other files' type errors are ty's domain, not ruff's).

- [ ] **Step 9: Stage + report**

```bash
git add src/otto/utils.py src/otto/host/ tests/unit/host/
```

Paste-able commit message:

```
refactor(host)!: run()->Results, oneshot()->CommandResult; delete old types

Deletes CommandStatus and RunResult (delete-first per spec so ty
enumerates every call site). Command output moves to .value; aggregation
moves to Results.collect with identical first-non-ok semantics.
```

---

### Task 3: Transfers — per-file `dict[Path, Result]` mapping

**Files:**
- Modify: `src/otto/host/transfer/base.py`, `unix_base.py`, `embedded_base.py`, `scp.py`, `sftp.py`, `ftp.py`, `nc.py`, `console.py`, `tftp.py`
- Modify: `src/otto/host/unix_host.py` (`get`/`put` + `_dry_run_transfer`), `src/otto/host/embedded_host.py` (`get`/`put`)
- Test: update `tests/unit/host/test_transfer_port.py`, `test_transfer_nc_get.py`, `test_transfer_nc_put.py`, `test_embedded_transfer.py`; add `tests/unit/host/test_transfer_per_file.py`

**Interfaces:**
- Consumes: `Result` from Task 1; `Status.Skipped` semantics.
- Produces: `get_files/put_files(...) -> Result` where `value: dict[Path, Result]` keyed by the source paths exactly as passed; each per-file `Result` has `value=dest_path` on success, per-file `msg` on failure, `Status.Skipped` for not-attempted. `Host.get/put -> Result` with the same shape. Helper `aggregate_transfer(per_file: dict[Path, Result]) -> Result` in `transfer/base.py`.

- [ ] **Step 1: Write the failing per-file semantics test**

Create `tests/unit/host/test_transfer_per_file.py`:

```python
"""Per-file transfer mapping semantics (spec 2026-07-01)."""

from pathlib import Path

from otto.host.transfer.base import aggregate_transfer
from otto.result import Result
from otto.utils import Status


def test_all_ok_aggregate():
    per_file = {
        Path("a"): Result(Status.Success, value=Path("/dst/a")),
        Path("b"): Result(Status.Success, value=Path("/dst/b")),
    }
    agg = aggregate_transfer(per_file)
    assert agg.is_ok
    assert agg.value is per_file
    assert agg.msg == ""


def test_failure_aggregate_is_first_non_ok_with_msg():
    per_file = {
        Path("a"): Result(Status.Success, value=Path("/dst/a")),
        Path("b"): Result(Status.Error, msg="b: connection reset"),
        Path("c"): Result(Status.Skipped, msg="not attempted"),
    }
    agg = aggregate_transfer(per_file)
    assert agg.status is Status.Error
    assert "b: connection reset" in agg.msg
    assert agg.value[Path("c")].status is Status.Skipped


def test_trailing_skipped_alone_never_fails_aggregate():
    per_file = {Path("a"): Result(Status.Skipped, msg="not attempted")}
    assert aggregate_transfer(per_file).is_ok
```

Run: `uv run pytest tests/unit/host/test_transfer_per_file.py -n auto`
Expected: FAIL — `aggregate_transfer` doesn't exist.

- [ ] **Step 2: Implement the base contract in `transfer/base.py`**

Add the helper and change the ABC:

```python
def aggregate_transfer(per_file: dict[Path, Result]) -> Result:
    """Fold a per-file mapping into the aggregate transfer Result.

    Aggregate status is the first non-ok entry's status (Skipped counts as
    ok); aggregate msg joins each non-ok entry's diagnostic.
    """
    status = next((r.status for r in per_file.values() if not r.is_ok), Status.Success)
    msg = "; ".join(r.msg for r in per_file.values() if not r.is_ok and r.msg)
    return Result(status=status, value=per_file, msg=msg)
```

Change `get_files`/`put_files` (base.py:146/176) return annotations from `tuple[Status, str]` to `Result`, and the per-backend template hooks (`_run_get`/`_run_put` in `unix_base.py`/`embedded_base.py`) from `-> tuple[Status, str]` to `-> dict[Path, Result]`. The template methods wrap: `return aggregate_transfer(await self._run_get(...))`.

- [ ] **Step 3: Convert each backend to produce the per-file dict**

Pattern (scp shown; same shape for sftp/ftp/console; nc keeps its streaming internals and builds the dict around them):

```python
# BEFORE (scp.py:105-121)
async def _get_one(src: Path) -> tuple[Status, str]:
    ...
    return Status.Success, ""

results: list[tuple[Status, str] | BaseException] = await asyncio.gather(
    *(_get_one(src) for src in src_files), return_exceptions=True
)
return _first_error(results)

# AFTER
async def _get_one(src: Path) -> Result:
    ...
    return Result(Status.Success, value=dest_dir / src.name)

gathered = await asyncio.gather(
    *(_get_one(src) for src in src_files), return_exceptions=True
)
per_file: dict[Path, Result] = {}
for src, outcome in zip(src_files, gathered, strict=True):
    if isinstance(outcome, BaseException):
        per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
    else:
        per_file[src] = outcome
return per_file
```

Rules: keys are the source paths exactly as passed (no resolution). Sequential backends (ftp/console/nc loops) mark files after the first failure `Result(Status.Skipped, msg="not attempted (earlier failure)")` instead of attempting them, matching current stop-on-error behavior. Delete `_first_error` once no backend uses it. `tftp.py` is a reserved placeholder raising `NotImplementedError` — only its annotations change.

- [ ] **Step 4: Convert `Host.get/put` + dry-run**

`unix_host.py:660-698` and the embedded equivalents: return annotation `-> Result`; bodies already delegate to `get_files`/`put_files`, which now return `Result`. `_dry_run_transfer` builds the same shape:

```python
def _dry_run_transfer(self, verb: str, src_files: list[Path], dest_dir: Path) -> Result:
    per_file = {
        src: Result(Status.Success, value=dest_dir / src.name) for src in src_files
    }
    return aggregate_transfer(per_file)
```

Update the protocol annotations + docstrings for `get`/`put` in `host/host.py:336-360` (per-file mapping contract, keys-as-passed, Skipped semantics).

- [ ] **Step 5: Update the four existing transfer test files**

Mechanical rule: `status, msg = await ...get_files(...)` → `res = await ...get_files(...)`; assert `res.status`/`res.msg`; per-file assertions become `res.value[src].status`. Where a test asserted `(Status.Success, "")` exactly, assert `res.is_ok and res.msg == ""` instead.

- [ ] **Step 6: Run scoped tests**

Run: `uv run pytest tests/unit/host -n auto`
Expected: PASS.

- [ ] **Step 7: Lint**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`

- [ ] **Step 8: Stage + report**

```bash
git add src/otto/host/transfer/ src/otto/host/unix_host.py src/otto/host/embedded_host.py src/otto/host/host.py tests/unit/host/
```

Paste-able commit message:

```
refactor(transfer)!: per-file dict[Path, Result] transfer results

get/put return one Result whose value maps each source path (as passed)
to its per-file Result: dest path on success, per-file diagnostic on
failure, Skipped for not-attempted. Backends build the mapping natively;
_first_error is gone.
```

---

### Task 4: Scalar verbs — power/reboot/load/unload/lsmod/login + file_ops

**Files:**
- Modify: `src/otto/host/power.py` (controller ABC + `CommandPowerController`), `src/otto/host/host.py` (`power`/`reboot` protocol + BaseHost, host.py:362-380/791-830), `src/otto/host/unix_host.py` (`lsmod`/`load`/`unload`, unix_host.py:705-760), `src/otto/host/embedded_host.py` (`load`/`unload`, embedded_host.py:618-680), `src/otto/host/file_ops.py`, login implementations (grep `def login` under `src/otto/host/`)
- Test: update `tests/unit/host/test_power.py`, `tests/unit/host/test_file_ops.py` (+ any kernel-module/login tests found by `grep -rln "lsmod\|def test_load\|login" tests/unit/host/`)

**Interfaces:**
- Consumes: `Result` from Task 1.
- Produces: `power() -> Result` (value=`PowerState | None`), `reboot()/load()/unload()/login() -> Result` (value=None), `lsmod() -> Result` (value=`list[str]`), `PowerController.on/off/cycle/status -> Result`, file_ops helpers `-> Result`.

- [ ] **Step 1: Convert signatures (mechanical rule)**

Every `-> tuple[Status, str]` in this task's files becomes `-> Result`; every `return status, msg` becomes `return Result(status, msg=msg)`. For `power()`, thread the controller's known state: `return Result(status, value=state, msg=msg)` where the implementation has a `PowerState`, else `value=None`. `lsmod()` changes from `-> list[str]` to `-> Result`: success is `Result(Status.Success, value=modules)`; the failure path (command failed) becomes `Result(Status.Error, msg=...)` instead of whatever it does today — check `_loaded_modules` (unix_host.py:709) and surface its failure as a non-ok Result rather than an exception. `PowerController` ABC methods (power.py, 6 tuple sites) convert identically — "conversion at the source" applies to the controller seam exactly as it does to transfers.

- [ ] **Step 2: Rewrite the six protocol docstrings**

In `host/host.py`, every "Returns a ``(Status, message)`` tuple" sentence (get/put were done in Task 3; power/reboot here) becomes a description of the `Result` contract including the `value` payload from the spec table.

- [ ] **Step 3: Update this task's tests**

Same mechanical rule as Task 3 Step 5: unpacking → attribute access. `lsmod` tests: `mods = await h.lsmod()` → `mods = (await h.lsmod()).value`.

- [ ] **Step 4: Run scoped tests**

Run: `uv run pytest tests/unit/host -n auto`
Expected: PASS.

- [ ] **Step 5: Lint + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`

```bash
git add src/otto/host/ tests/unit/host/
```

Paste-able commit message:

```
refactor(host)!: scalar verbs return Result (power/reboot/modules/login/file_ops)

tuple[Status, str] is gone from the host verb surface; PowerController
and file_ops convert at the source. lsmod gains a failure channel
(Result with value=list[str]) instead of raising.
```

---

### Task 5: CLI renderer + every remaining caller

**Files:**
- Modify: `src/otto/cli/expose.py` (`_render_result`), `src/otto/cli/run.py`, `src/otto/context.py` (run_on_all_hosts annotation, context.py:188-230), `src/otto/configmodule/configmodule.py`, `src/otto/configmodule/repo.py`, `src/otto/monitor/collector.py`, `src/otto/monitor/parsers.py`, plus whatever `grep -rn "CommandStatus\|RunResult\|tuple\[Status" src/` still shows EXCEPT `src/otto/docker/build.py`/`compose.py` and `src/otto/host/product.py` (internal, not host verbs — leave them; see note in Step 3)
- Test: update `tests/unit/cli/conftest.py`, `test_run.py`, `test_monitor.py`, `tests/unit/monitor/test_collector_run.py`, `tests/unit/configmodule/test_configmodule.py`, `tests/unit/cov/test_fetcher.py`, `test_merger.py`, `test_toolchain_discovery.py`, `test_embedded_collector.py`, `tests/unit/docker/test_build.py`, `test_compose.py`, fixture instructions `tests/repo1/pylib/repo1_instructions/nc_smoke.py`, `run_on_container.py`, `tests/repo_e2e/pylib/repo_e2e_instructions/noop.py`; extend `tests/unit/cli/test_expose*.py` (locate via `grep -rln "_render_result" tests/`)

**Interfaces:**
- Consumes: everything Tasks 1–4 produced.
- Produces: `_render_result(result: Any, success: str | None = None) -> None` handling exactly three shapes: `Result` family, `None`, plain-value fallback.

- [ ] **Step 1: Write the failing renderer tests**

Add to the expose test file:

```python
import pytest
import typer

from otto.cli.expose import _render_result
from otto.result import CommandResult, Result, Results
from otto.utils import Status


def _exit_code(result, success=None):
    try:
        _render_result(result, success)
    except typer.Exit as e:
        return e.exit_code
    return 0


def test_command_retcode_passthrough():
    res = Results.collect(
        [CommandResult(Status.Failed, value="", command="exit 42", retcode=42)]
    )
    assert _exit_code(res) == 42


def test_command_never_ran_exits_255():
    assert _exit_code(CommandResult(Status.Error, command="x", retcode=-1)) == 255


def test_status_mapping_for_plain_results():
    assert _exit_code(Result(Status.Error, msg="boom")) == 2
    assert _exit_code(Result(Status.Failed, msg="no")) == 1
    assert _exit_code(Result(Status.Skipped)) == 0


def test_ok_result_prints_success_message(capsys):
    _render_result(Result(Status.Success), success="Transfer complete.")
    assert "Transfer complete." in capsys.readouterr().out


def test_ok_transfer_mapping_prints_per_file_lines(capsys):
    from pathlib import Path

    per_file = {Path("a.bin"): Result(Status.Success, value=Path("/dst/a.bin"))}
    _render_result(Result(Status.Success, value=per_file))
    out = capsys.readouterr().out
    assert "a.bin" in out and "/dst/a.bin" in out


def test_failed_mapping_prints_per_entry_diagnostics(capsys):
    from pathlib import Path

    per_file = {Path("b.bin"): Result(Status.Error, msg="b.bin: reset")}
    with pytest.raises(typer.Exit):
        _render_result(Result(Status.Error, value=per_file, msg="1 file failed"))
    assert "b.bin: reset" in capsys.readouterr().out


def test_command_results_print_nothing_on_ok(capsys):
    _render_result(Results.collect([CommandResult(Status.Success, retcode=0)]))
    assert capsys.readouterr().out == ""


def test_plain_value_fallback(capsys):
    assert _exit_code(["third", "party"]) == 0
    assert "third" in capsys.readouterr().out


def test_none_prints_done(capsys):
    _render_result(None)
    assert "done" in capsys.readouterr().out
```

Run: `uv run pytest tests/unit/cli -k render -n auto`
Expected: FAIL (renderer still expects tuples).

- [ ] **Step 2: Rewrite `_render_result` (expose.py:35-68)**

```python
def _render_result(result: Any, success: str | None = None) -> None:
    """Render a host-verb result and signal failure via exit code.

    First-party verbs return the ``otto.result`` family (exit code comes from
    ``result.exit_code``); ``None`` means side-effect-only success. Any other
    value is the documented third-party fallback: printed as-is, exit 0.
    """
    from rich import print as rprint

    from otto.result import CommandResult, Result, Results

    if isinstance(result, Result):
        is_command = isinstance(result, (CommandResult, Results))
        if result.is_ok:
            if is_command:
                pass  # command output already streamed during execution
            elif success:
                rprint(f"[green]{success}[/green]")
            elif isinstance(result.value, dict):
                for src, entry in result.value.items():
                    rprint(f"{src} -> {entry.value}")
            elif isinstance(result.value, list):
                for item in result.value:
                    rprint(item)
            elif result.value is not None:
                rprint(result.value)
            return
        if result.msg:
            rprint(f"[red]{result.msg}[/red]")
        if isinstance(result.value, dict):
            for entry in result.value.values():
                if isinstance(entry, Result) and not entry.is_ok and entry.msg:
                    rprint(f"[red]{entry.msg}[/red]")
        elif isinstance(result, Results):
            for entry in result:
                if not entry.is_ok and entry.msg:
                    rprint(f"[red]{entry.msg}[/red]")
        raise typer.Exit(result.exit_code)

    if result is None:
        rprint(f"[green]{success}[/green]" if success else "[green]done[/green]")
        return

    rprint(result)  # documented third-party plain-value fallback, exit 0
```

- [ ] **Step 3: Sweep remaining callers**

Run `grep -rn "CommandStatus\|RunResult" src/` — convert every remaining hit with the Task 2 mechanical rules (imports, keyword construction, `.output` → `.value`). `monitor/collector.py`/`parsers.py` and `cov/` consume run results: they iterate/inspect — switch to iterating `Results` and reading `.value`. Fan-out annotations: `context.py:204` `dict[str, RunResult | BaseException]` → `dict[str, Results | BaseException]` (same in `configmodule/configmodule.py`). NOTE: `src/otto/docker/build.py`, `docker/compose.py`, `src/otto/host/product.py` keep their internal `tuple[Status, str]` helpers — they are not host verbs and their tuples never cross the verb surface; converting them is out of the spec's scope (record as a possible follow-up in the final report). If ty flags them because a converted verb feeds them, convert the specific seam ty names.

- [ ] **Step 4: Sweep remaining tests + fixture instructions**

Apply the mechanical rules to every file in this task's Test list. The three fixture instructions under `tests/repo1/`/`tests/repo_e2e/` are user-facing sample code — make them exemplary (attribute access, no unpacking, `if not res:` truthiness where natural).

- [ ] **Step 5: Whole-suite checkpoint**

Run: `uv run pytest tests/unit tests/e2e -m "not integration and not embedded and not stability" -n auto`
Expected: PASS — this is the first task where the whole hostless tier must be green again.
Run: `uv run nox -s typecheck`
Expected: clean — zero remaining references to the deleted types.

- [ ] **Step 6: Lint + stage + report**

Run: `uv run ruff check . && uv run ruff format . && uv run ruff check .`

```bash
git add src/otto/cli/ src/otto/context.py src/otto/configmodule/ src/otto/monitor/ src/otto/cov/ tests/unit/ tests/repo1/ tests/repo_e2e/
```

(Adjust the `src/otto/cov/` path to whatever Step 3's grep actually touched.)

Paste-able commit message:

```
refactor(cli)!: render the Result family; convert all remaining callers

_render_result collapses to one Result branch driven by the polymorphic
exit_code (ssh-like for commands, status-mapped elsewhere), a None
branch, and the documented plain-value fallback for third-party verbs.
Zero references to the deleted types remain.
```

---

### Task 6: Documentation sweep + "Exit codes" section

**Files:**
- Modify: `docs/guide/run.md`, `docs/guide/host/index.md`, `docs/guide/host/capabilities.md`, `docs/cookbook/async-patterns.md`, `docs/cookbook/suite-recipes.md`, `docs/cookbook/sessions-and-repeats.md`, `docs/getting-started.md`, `docs/contributing.md`, `docs/guide/library-usage.md`, `docs/guide/extending-backends.md` (+ any file `grep -rln "RunResult\|CommandStatus\|\.output\b\|status, msg\|\.only" docs/ --include='*.md' | grep -v superpowers` finds)

**Interfaces:**
- Consumes: the final contracts from Tasks 1–5 (verb table from the spec).

- [ ] **Step 1: Inventory**

Run: `grep -rn "RunResult\|CommandStatus\|(Status, \|status, msg\|\.only\|\.output" docs/ --include="*.md" | grep -v superpowers`
Every hit gets updated in this task; list them in the task report.

- [ ] **Step 2: Update every example (mechanical rules)**

- `res.only.output` → `res.only.value`; `RunResult` prose → `Results`; `CommandStatus` prose → `CommandResult`.
- `status, msg = await host.put(...)` → `res = await host.put(...)` with `res.status` / `res.msg` / `res.value[src]` as the example needs.
- Where an example checks success, prefer the truthiness idiom the family enables: `if not res: ...`.
- Doctests execute in the docs gate — every changed example must run.

- [ ] **Step 3: Add the "Exit codes" section**

In `docs/guide/host/index.md` (the CLI-facing host guide), add:

```markdown
## Exit codes

Every `otto host <name> <verb>` invocation derives its exit code from the
verb's `Result`:

| Situation | Exit code |
| --- | --- |
| Verb succeeded (incl. `Status.Skipped`) | 0 |
| `run`/`oneshot`: a command failed | that command's shell retcode (ssh-like: `run 'exit 42'` exits 42) |
| `run`/`oneshot`: the command never ran (connection failure) | 255 (matches ssh's convention) |
| Any other verb: `Status.Failed` | 1 |
| Any other verb: `Status.Error` | 2 (note: Click also uses 2 for CLI usage errors) |
| Any other verb: `Status.Unstable` | 3 |

Custom verbs on third-party host classes may return plain values instead of a
`Result`; the CLI prints them as-is and exits 0.
```

- [ ] **Step 4: Update `extending-backends.md`**

Two contract updates: (a) transfer backends return the per-file mapping (`dict[Path, Result]` from `_run_get`/`_run_put`, keys = source paths as passed, `Skipped` for not-attempted — show the scp-style example from Task 3 Step 3); (b) the plain-value fallback paragraph from Step 3's table footer.

- [ ] **Step 5: Docs gate**

Run: `uv run nox -s docs`
Expected: clean build, zero warnings (nitpicky `-W`), all doctests pass.

- [ ] **Step 6: Stage + report**

```bash
git add docs/guide/ docs/cookbook/ docs/getting-started.md docs/contributing.md
```

Paste-able commit message:

```
docs: match the Result-family contracts + document CLI exit codes

Every host-verb example moves to the Result family (attribute access,
truthiness idiom); new Exit codes section documents the ssh-like
retcode passthrough and status mapping; extending-backends gets the
per-file transfer mapping contract and the plain-value fallback.
```

---

### Task 7: Exit-code e2e + full gate

**Files:**
- Create: `tests/e2e/cli/test_exit_codes_e2e.py` (or extend the existing CLI-subprocess e2e module — locate via `ls tests/e2e/cli/`; follow its subprocess-invocation fixture pattern and its `hostless` marker usage)

**Interfaces:**
- Consumes: the built-in local host (`otto host local ...`, landed in `9b7b0c4`) so the tests stay hostless.

- [ ] **Step 1: Write the failing e2e tests**

Follow the existing CLI-subprocess pattern in `tests/e2e/cli/` (same env/fixture; shown here with plain `subprocess.run` — adapt to the module's helper):

```python
"""Real $? contracts for the Result family (spec 2026-07-01)."""

import pytest


@pytest.mark.hostless
class TestExitCodes:
    def test_run_passes_through_command_retcode(self, otto_cli):
        proc = otto_cli("host", "local", "run", "exit 42")
        assert proc.returncode == 42

    def test_run_success_exits_zero(self, otto_cli):
        proc = otto_cli("host", "local", "run", "true")
        assert proc.returncode == 0

    def test_failing_get_maps_status(self, otto_cli, tmp_path):
        proc = otto_cli("host", "local", "get", "/no/such/file", str(tmp_path))
        assert proc.returncode == 2  # Status.Error
        assert "no/such/file" in proc.stderr + proc.stdout
```

(`otto_cli` stands for the module's existing subprocess helper fixture — reuse it, do not invent a new one.)

- [ ] **Step 2: Run to verify current state, implement any gaps**

Run: `uv run pytest tests/e2e/cli/test_exit_codes_e2e.py -n auto`
Expected: PASS if Tasks 1–5 are complete. Any failure here is a real contract bug — fix it in the layer that owns it (renderer or verb), not in the test.

- [ ] **Step 3: Full gate**

Run, in order, each to completion:

```bash
make coverage
uv run nox -s lint
uv run nox -s typecheck
uv run nox -s docs
```

Expected: all green. If coverage dips below the floor, the new `result.py`/renderer branches need the missing unit tests — add them in `tests/unit/result/` or the expose tests, don't lower the floor.

- [ ] **Step 4: Verify nothing was left behind**

Run: `grep -rn "CommandStatus\|RunResult\|tuple\[Status, str\]" src/ tests/ docs/ --include="*.py" --include="*.md" | grep -v superpowers | grep -v "docker/build\|docker/compose\|host/product"`
Expected: zero hits. (The three excluded internal modules are the documented out-of-scope tuples.)

- [ ] **Step 5: Stage + report**

```bash
git add tests/e2e/cli/
```

Paste-able commit message:

```
test(e2e): assert real $? for the Result-family exit-code contract

run 'exit 42' exits 42 via the local host; failing get exits 2
(Status.Error). Full gate green: coverage, lint, typecheck, docs.
```

---

## Plan Self-Review (completed)

- **Spec coverage:** core types (T1), delete-first + command core (T2), per-file transfers (T3), scalar verbs incl. PowerController-at-the-source (T4), renderer/exit codes/fallback + fan-out helpers (T5), docs incl. Exit-codes section + extending-backends (T6), e2e `$?` + full gate (T7). Lazy exports T1; import-budget guard exercised T1 Step 5.
- **Scope note:** `docker/build.py`, `docker/compose.py`, `host/product.py` internal tuples deliberately excluded (not host verbs) — surfaced in T5 Step 3 and re-checked in T7 Step 4.
- **Type consistency:** `Results.collect`, `.only`, `.first_failure`, `.exit_code`, `aggregate_transfer` used with identical signatures across tasks.
