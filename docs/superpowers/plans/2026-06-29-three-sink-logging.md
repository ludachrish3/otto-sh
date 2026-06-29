# Three-Sink Logging with `LogMode` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace otto's single log file with three sinks — the live console, `console.log` (a faithful console transcript), and `verbose.log` (an "everything" record) — governed by a per-command/per-host `LogMode` disposition, and capture generic `logging.getLogger(__name__)` records from product code.

**Architecture:** A small `LogMode` enum (`NORMAL`/`QUIET`/`NEVER`) decides where a command's I/O goes. Command echo/output records are tagged with their effective mode; a console-suppress filter on the console + `console.log` handlers drops `QUIET` records while `verbose.log` keeps them; `NEVER` is redacted at the source (including session diagnostics). The CLI attaches its shared `QueueHandler` to `otto.*` plus auto-derived product package prefixes so generic loggers are captured without third-party noise.

**Tech Stack:** Python 3.10+, stdlib `logging` (`QueueHandler`/`QueueListener`/`Filter`), `rich` (`RichHandler`), `pydantic` (settings/host specs), `typer` (CLI), `pytest`.

**Design spec:** `docs/superpowers/specs/2026-06-28-three-sink-logging-design.md`

## Global Constraints

- **Annotations:** Use real Python 3.10+ annotations. **Never** add `from __future__ import annotations` — it trips otto's Sphinx nitpicky (`-W`) docs gate.
- **`@override`:** Every method that overrides a base must carry `@override` (from `typing_extensions`); ty enforces `missing-override-decorator`.
- **Lint/format:** New/changed code must pass `ruff format` and the project's strict ruff lint (the tree was just swept). Keep line length ≤ 100.
- **Per-task gate:** `make coverage` (runs unit suite + coverage floor). Note: `make coverage` does **not** run `ty` — run `make typecheck` too when types change.
- **Final gate (end of plan):** `make coverage && make nox && make typecheck && make docs` — all green. The coverage floor is enforced; new code needs tests.
- **Host field changes:** A runtime per-host field is mirrored in `src/otto/models/host.py`. Changing the `log` field type requires updating that spec mirror AND running the **full** `tests/unit` (not just `tests/unit/host`) — this class of change has broken `main` before.
- **Commits:** STAGE ONLY — implementers run `git add <task files>` and **never** `git commit` (this repo's interactive `prepare-commit-msg` hook needs a TTY and mis-tags agent commits). The controller reviews each task off an isolated tree-snapshot diff and surfaces a paste-able commit message; the **user** commits. The "commit message" block at the end of each task is the message to hand off, not a command to run.
- **Worktree:** Work happens in the worktree `/home/vagrant/otto-sh/.claude/worktrees/three-sink-logging` (branch `worktree-three-sink-logging`). Run `uv run --no-sync <cmd>` for tools.

---

### Task 1: `LogMode` enum + `effective_mode` helper

**Files:**
- Create: `src/otto/logger/mode.py`
- Test: `tests/unit/logger/test_mode.py`

**Interfaces:**
- Produces:
  - `class LogMode(Enum)` with members `NORMAL`, `QUIET`, `NEVER` (string values `"normal"`, `"quiet"`, `"never"`).
  - `LogMode.rank -> int` property: `NORMAL=0 < QUIET=1 < NEVER=2` (restrictiveness order).
  - `effective_mode(*modes: LogMode) -> LogMode`: returns the most restrictive (highest rank); with no args returns `LogMode.NORMAL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/logger/test_mode.py
from otto.logger.mode import LogMode, effective_mode


def test_logmode_values():
    assert LogMode.NORMAL.value == "normal"
    assert LogMode.QUIET.value == "quiet"
    assert LogMode.NEVER.value == "never"


def test_logmode_rank_orders_normal_quiet_never():
    assert LogMode.NORMAL.rank < LogMode.QUIET.rank < LogMode.NEVER.rank


def test_effective_mode_is_most_restrictive():
    assert effective_mode(LogMode.NORMAL, LogMode.QUIET) is LogMode.QUIET
    assert effective_mode(LogMode.QUIET, LogMode.NEVER) is LogMode.NEVER
    assert effective_mode(LogMode.NORMAL, LogMode.NORMAL) is LogMode.NORMAL


def test_effective_mode_no_args_is_normal():
    assert effective_mode() is LogMode.NORMAL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/logger/test_mode.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.logger.mode'`

- [ ] **Step 3: Write the implementation**

```python
# src/otto/logger/mode.py
"""Per-command / per-host logging disposition.

``LogMode`` decides *where* a host's command I/O is recorded — independent of the
log *level* (INFO vs DEBUG), which stays native to the ``logger.info``/
``logger.debug`` call. See ``docs/superpowers/specs/2026-06-28-three-sink-logging-design.md``.
"""

from enum import Enum


class LogMode(Enum):
    """Disposition of a host's command echo/output across the log sinks.

    - ``NORMAL`` — logged at the call's level, shown everywhere.
    - ``QUIET`` — suppressed from the console + ``console.log``, kept in ``verbose.log``.
    - ``NEVER`` — redacted from every sink at every level, including session diagnostics.

    ``LogMode`` governs command I/O only; ``logger.warning``/``logger.error`` and
    other non-command records are never suppressed by it.
    """

    NORMAL = "normal"
    QUIET = "quiet"
    NEVER = "never"

    @property
    def rank(self) -> int:
        """Restrictiveness order: ``NORMAL`` (0) < ``QUIET`` (1) < ``NEVER`` (2)."""
        return _RANK[self]


_RANK = {LogMode.NORMAL: 0, LogMode.QUIET: 1, LogMode.NEVER: 2}


def effective_mode(*modes: LogMode) -> LogMode:
    """Return the most restrictive of *modes* (``NORMAL`` when called with none)."""
    return max(modes, key=lambda m: m.rank, default=LogMode.NORMAL)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/unit/logger/test_mode.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/logger/mode.py tests/unit/logger/test_mode.py
# Commit message (run yourself; hook needs a TTY):
#   feat(logger): add LogMode disposition enum + effective_mode helper
```

---

### Task 2: Mode-aware command logging + console-suppress filter

Make `_log_command`/`_log_output` carry the effective `LogMode` on each record and skip emission for `NEVER`. Rename `HostFilter`'s behavior to a *console-suppress* filter that drops `QUIET` command records and honors the global suppression flag. This task is **behavior-preserving** for existing tests: the filter is still attached to every handler (Task 6 restricts it), so `QUIET` is still suppressed everywhere for now, exactly like today's `log=False`.

**Files:**
- Modify: `src/otto/host/host.py` (`_log_command`, `_log_output`, `HostFilter`)
- Test: `tests/unit/host/test_session_logging.py` (add filter tests)

**Interfaces:**
- Consumes: `LogMode` from Task 1.
- Produces:
  - `Host._log_command(self, command: str, mode: LogMode = LogMode.NORMAL) -> None`
  - `Host._log_output(self, output: str, mode: LogMode = LogMode.NORMAL) -> None`
  - Records emitted carry `extra={"host": self, "log_mode": mode}`.
  - `HostFilter.filter` drops a host-tagged record when its `log_mode is LogMode.QUIET`/`LogMode.NEVER`, or when global command-output logging is disabled.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_session_logging.py  (add)
import logging

from otto.host.host import HostFilter
from otto.logger.mode import LogMode


def _record(mode):
    rec = logging.LogRecord("otto", logging.INFO, __file__, 0, "msg", None, None)
    rec.host = object()
    rec.log_mode = mode
    return rec


def test_console_suppress_filter_drops_quiet_and_never():
    f = HostFilter()
    assert f.filter(_record(LogMode.NORMAL)) is True
    assert f.filter(_record(LogMode.QUIET)) is False
    assert f.filter(_record(LogMode.NEVER)) is False


def test_console_suppress_filter_passes_non_command_records():
    f = HostFilter()
    rec = logging.LogRecord("otto", logging.WARNING, __file__, 0, "boom", None, None)
    assert f.filter(rec) is True  # no host tag → always passes (warnings/errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/host/test_session_logging.py -k console_suppress -q`
Expected: FAIL — `_log_mode` not set / filter still returns truthy `host.log` bool.

- [ ] **Step 3: Update `_log_command` / `_log_output` and `HostFilter`**

In `src/otto/host/host.py`, add the import near the other otto imports:

```python
from ..logger.mode import LogMode
```

Replace `_log_command` / `_log_output` (currently lines ~951-967):

```python
    def _log_command(
        self,
        command: str,
        mode: LogMode = LogMode.NORMAL,
    ) -> None:
        if mode is LogMode.NEVER:
            return
        logger.info(
            f"[bold]@{self.name}   | {command}",
            extra={"host": self, "log_mode": mode},
        )

    def _log_output(
        self,
        output: str,
        mode: LogMode = LogMode.NORMAL,
    ) -> None:
        if mode is LogMode.NEVER:
            return
        preamble = f"[yellow]@{self.name} > | "
        output_lines = [f"{preamble}{line}" for line in output.splitlines()]
        newline = "\n"
        logger.info(
            f"{newline.join(output_lines)}",
            extra={"host": self, "log_mode": mode},
        )
```

Replace `HostFilter` (currently lines ~970-986):

```python
class HostFilter(Filter):
    """Console-side suppress filter: drops QUIET/NEVER command records and honors
    the global command-output flag. Attached to the console + ``console.log``
    handlers only — ``verbose.log`` keeps the records (see ``management``)."""

    @override
    def filter(self, record: LogRecord) -> bool:
        host: Host | None = getattr(record, "host", None)
        # Non-command records (no host tag) — e.g. warnings/errors — always pass.
        if host is None:
            return True
        mode: LogMode = getattr(record, "log_mode", LogMode.NORMAL)
        if mode is not LogMode.NORMAL:  # QUIET or NEVER → not on the console side
            return False
        return get_logging_command_output_enabled()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/host/test_session_logging.py -q`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Run the broader host suite for regressions**

Run: `uv run --no-sync pytest tests/unit/host -q`
Expected: PASS (no behavior change yet — `QUIET` still filtered everywhere).

- [ ] **Step 6: Stage + commit**

```bash
git add src/otto/host/host.py tests/unit/host/test_session_logging.py
# Commit message:
#   feat(host): tag command logs with LogMode; console-suppress filter reads it
```

---

### Task 3: Thread per-command `LogMode` through `run`/`oneshot`/`send`

Change the per-command `log` parameter and `ShellCommand.log` from `bool` to `LogMode`, keeping `LogMode.NORMAL` as the default so untouched call sites are unaffected. Thread the mode through the session so `NEVER` drops the output sink and `QUIET`/`NORMAL` tag their records. Per-host composition comes in Task 4 (here, `_run_one` passes the per-command mode straight through).

**Files:**
- Modify: `src/otto/host/host.py` (`ShellCommand.log`, `_resolve_command`, `Host.run`, `Host._run_one`, `Host.oneshot`, `Host.send` signatures)
- Modify: `src/otto/host/session.py` (`SessionManager.run_cmd`, `SessionManager.oneshot`, `SessionManager.send`, `HostSession.run`, `HostSession.send`)
- Modify: `src/otto/host/unix_host.py`, `local_host.py`, `embedded_host.py`, `docker_host.py`, `remote_host.py` (`_run_one`/`oneshot`/`send` override signatures: `log: bool` → `log: LogMode`)
- Test: `tests/unit/host/test_shell_command.py`, `tests/unit/host/test_session_logging.py`

**Interfaces:**
- Consumes: `LogMode`, `effective_mode` (Task 1); mode-aware `_log_command`/`_log_output` (Task 2).
- Produces:
  - `ShellCommand.log: LogMode | None = None`
  - `Host.run(..., log: LogMode = LogMode.NORMAL, ...)`, `Host._run_one(..., log: LogMode = LogMode.NORMAL)`, `Host.oneshot(..., log: LogMode = LogMode.NORMAL)`, `Host.send(..., log: LogMode = LogMode.NORMAL)`
  - `_resolve_command(..., default_log: LogMode = LogMode.NORMAL)`
  - `SessionManager.run_cmd(..., log: LogMode = LogMode.NORMAL)` / `.oneshot(..., log: LogMode = ...)` / `.send(..., log: LogMode = ...)`; output sink is `_drop_output` for `NEVER`, otherwise a mode-tagging sink.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_shell_command.py  (add)
from otto.host.host import ShellCommand, _resolve_command
from otto.logger.mode import LogMode


def test_shellcommand_log_defaults_to_none_and_inherits_normal():
    sc = _resolve_command("echo hi", None, None)
    assert sc.log is LogMode.NORMAL


def test_resolve_command_inherits_explicit_mode():
    sc = _resolve_command(ShellCommand("x", log=LogMode.QUIET), None, None)
    assert sc.log is LogMode.QUIET


def test_resolve_command_uses_default_mode():
    sc = _resolve_command("x", None, None, default_log=LogMode.NEVER)
    assert sc.log is LogMode.NEVER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/host/test_shell_command.py -k log -q`
Expected: FAIL — `ShellCommand.log` is `bool | None`; `_resolve_command` default is `True`.

- [ ] **Step 3: Update `ShellCommand`, `_resolve_command`, and the `Host`/session signatures**

In `src/otto/host/host.py`:

`ShellCommand.log` field (replace lines ~87-90):

```python
    log: "LogMode | None" = None
    """Per-command logging disposition. ``None`` inherits the run-level ``log``.
    ``QUIET`` keeps output in ``verbose.log`` but off the console; ``NEVER``
    redacts it from every sink. The returned ``CommandStatus`` is unaffected."""
```

`_resolve_command` (replace the `default_log: bool = True` signature and the two `log=` constructions):

```python
def _resolve_command(
    item: "str | ShellCommand",
    default_expects: "Expect | list[Expect] | None",
    default_timeout: float | None,
    default_log: LogMode = LogMode.NORMAL,
) -> ShellCommand:
    """Coerce ``item`` to a ``ShellCommand`` whose ``None`` fields inherit from defaults."""
    if isinstance(item, str):
        return ShellCommand(
            cmd=item, expects=default_expects, timeout=default_timeout, log=default_log
        )
    return ShellCommand(
        cmd=item.cmd,
        expects=item.expects if item.expects is not None else default_expects,
        timeout=item.timeout if item.timeout is not None else default_timeout,
        log=item.log if item.log is not None else default_log,
    )
```

`Host.run` — change `log: Annotated[bool, Exclude] = True` to `log: Annotated[LogMode, Exclude] = LogMode.NORMAL`, and the two `cast("bool", ...)` to `cast("LogMode", ...)`.

`Host._run_one`, `Host.oneshot`, `Host.send` — change `log: bool = True` → `log: LogMode = LogMode.NORMAL`.

In `src/otto/host/session.py`:

`SessionManager.run_cmd` (replace `log: bool = True` and the body's `if log:` / sink selection):

```python
    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: LogMode = LogMode.NORMAL,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandStatus:
        await self._ensure_session()
        if log is not LogMode.NEVER:
            self._log_command(cmd, log)
        assert self._session is not None  # noqa: S101 — _ensure_session sets _session or raises
        return await self._session.run_cmd(
            cmd,
            expects=expects,
            timeout=timeout,
            on_output=_sink_for(self._log_output, log),
            redact=log is LogMode.NEVER,
            write_progress=write_progress,
        )
```

Add a module-level sink helper near `_drop_output` in `session.py`:

```python
def _sink_for(
    log_output: "Callable[[str, LogMode], None]",
    mode: LogMode,
) -> "Callable[[str], None]":
    """Return the per-command output sink for *mode*.

    ``NEVER`` discards; ``NORMAL``/``QUIET`` forward each line to ``log_output``
    tagged with the mode so the console-suppress filter can act on it.
    """
    if mode is LogMode.NEVER:
        return _drop_output
    return lambda line: log_output(line, mode)
```

> Note: `self._log_command` / `self._log_output` on `SessionManager`/`HostSession` are bound from the host's `_log_command`/`_log_output`, which now accept `(text, mode)`. Update the constructor parameter type hints and the lambda defaults (`lambda *_: None`) accordingly.

`SessionManager.oneshot` — `log: bool = True` → `log: LogMode = LogMode.NORMAL`; `if log:` → `if log is not LogMode.NEVER:` for `_log_command(cmd, log)`; the SSH `if log:` line-logging → `if log is not LogMode.NEVER: self._log_output(line, log)`; the telnet path forwards `log=log` unchanged.

`SessionManager.send` and `HostSession.send` and `HostSession.run` — `log: bool` → `log: LogMode`; `if log:` → `if log is not LogMode.NEVER:`; pass the mode to `_log_command(..., log)` / `_log_output(..., sc.log)`.

`_Session.run_cmd` — add a `redact: bool = False` parameter and thread it to `_run_cmd_inner` (consumed in Task 5):

```python
    async def run_cmd(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
        on_output: Callable[[str], None] | None = None,
        redact: bool = False,
        write_progress: Callable[[int, int], None] | None = None,
    ) -> CommandStatus:
        ...
        await self._ensure_ready()
        sink = on_output if on_output is not None else self._on_output
        # pass redact into both _run_cmd_inner calls (direct + asyncio.wait_for)
```

Update the per-host `_run_one`/`oneshot`/`send` overrides in `unix_host.py`, `local_host.py`, `embedded_host.py`, `docker_host.py`, `remote_host.py` to `log: LogMode = LogMode.NORMAL` and forward `log=log` to the session manager (mechanical signature change; bodies already forward `log`).

- [ ] **Step 4: Run the focused tests**

Run: `uv run --no-sync pytest tests/unit/host/test_shell_command.py tests/unit/host/test_session_logging.py -q`
Expected: PASS.

- [ ] **Step 5: Migrate existing per-command `log=` literals in tests**

Existing tests pass `log=False`/`log=True` to `run`/`oneshot`/`send`. Replace `log=True` → `log=LogMode.NORMAL` and (for per-command suppression) `log=False` → `log=LogMode.QUIET`, except the password/hex tests which become `LogMode.NEVER` (Task 10 reclassifies the production sites; update only test call sites here). Search:

Run: `uv run --no-sync pytest tests/unit/host -q`
Fix each `log=` type error the run surfaces, then re-run until green.

- [ ] **Step 6: Typecheck + commit**

```bash
uv run --no-sync ty check src/otto/host
git add src/otto/host tests/unit/host
# Commit message:
#   feat(host): thread per-command LogMode through run/oneshot/send
```

---

### Task 4: Per-host `log` field → `LogMode` (+ spec mirror, schema, drift guard)

Promote the standing per-host `log` flag from `bool` to `LogMode`, compose it with the per-command mode (`effective_mode`), update the `models/host.py` spec mirror with bool→LogMode backward-compat coercion, bump the host-spec schema, and update the drift-guard test. **Run the full `tests/unit`** after this task.

**Files:**
- Modify: `src/otto/host/unix_host.py:250`, `local_host.py:116`, `embedded_host.py:251`, `docker_host.py:98`, `remote_host.py:123` (`log: bool` → `log: LogMode`)
- Modify: `src/otto/host/host.py` (`_run_one` callers compose `effective_mode(self.log, log)`; `SuppressCommandOutput` sets `LogMode.QUIET`)
- Modify: `src/otto/models/host.py:147` (`log: bool = True` → `log: LogMode = LogMode.NORMAL` + coercion validator)
- Modify: schema version constant + `tests/unit/models/test_host_specs.py` drift guard
- Test: `tests/unit/host/test_session_logging.py`, `tests/unit/models/test_host_specs.py`, `tests/unit/host/test_unix_host.py`

**Interfaces:**
- Consumes: `LogMode`, `effective_mode`.
- Produces: per-host `log: LogMode = LogMode.NORMAL`; effective command mode `= effective_mode(host.log, command.log)`; lab-data `"log": true/false` still accepted (coerced `True→NORMAL`, `False→QUIET`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_session_logging.py  (add)
from otto.logger.mode import LogMode


def test_effective_mode_composes_host_and_command(make_unix_host):
    host = make_unix_host()            # existing fixture/helper in this module
    host.log = LogMode.QUIET
    # a NORMAL command on a QUIET host runs QUIET; a NEVER command stays NEVER
    from otto.logger.mode import effective_mode
    assert effective_mode(host.log, LogMode.NORMAL) is LogMode.QUIET
    assert effective_mode(host.log, LogMode.NEVER) is LogMode.NEVER
```

```python
# tests/unit/models/test_host_specs.py  (add)
from otto.logger.mode import LogMode


def test_hostspec_log_accepts_bool_and_coerces():
    from otto.models.host import UnixHostSpec  # adjust to the actual spec class
    spec = UnixHostSpec.model_validate(_minimal_unix_kwargs() | {"log": False})
    assert spec.log is LogMode.QUIET
    spec2 = UnixHostSpec.model_validate(_minimal_unix_kwargs() | {"log": True})
    assert spec2.log is LogMode.NORMAL
```

(Define `_minimal_unix_kwargs()` from the existing builders in that test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/host/test_session_logging.py -k effective_mode_composes tests/unit/models/test_host_specs.py -k log_accepts -q`
Expected: FAIL — per-host `log` is `bool`; spec field is `bool`.

- [ ] **Step 3: Change the runtime fields**

In each host class, replace the field:

```python
    log: LogMode = field(default=LogMode.NORMAL, repr=False)
    """Standing per-host logging disposition. ``QUIET`` keeps this host's command
    I/O in ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere
    (warnings/errors are unaffected)."""
```

(`remote_host.py`'s field has no default — keep it required: `log: LogMode`.)

Import `LogMode` in each module (`from ..logger.mode import LogMode`).

- [ ] **Step 4: Compose effective mode at the `_run_one` seam**

In each subclass `_run_one`/`oneshot`/`send` (or once in `Host` if they delegate), compute the effective mode before calling the session manager:

```python
        effective = effective_mode(self.log, log)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout, log=effective)
```

Update `SuppressCommandOutput.__enter__` to set `self.host.log = LogMode.QUIET` (and the global form is unchanged — it flips `ctx.log_command_output`). The snapshot/restore now stores/restores a `LogMode`.

- [ ] **Step 4b: Close the Task-2 transition in `HostFilter`**

Task 2 left a transitional line in `HostFilter.filter` that reads the `bool`
`host.log` directly:

```python
        if not host.log:  # transitional per-host bool suppression (removed in Task 4)
            return False
```

Now that `host.log` is a `LogMode` folded into each record's `log_mode` via
`effective_mode(self.log, command.log)` at the emit seam (Step 4), this line is
both wrong (a `LogMode` is always truthy) and redundant. **Remove it** — the
filter should drop a command record solely on `record.log_mode` + the global
flag:

```python
    @override
    def filter(self, record: LogRecord) -> bool:
        host: Host | None = getattr(record, "host", None)
        if host is None:
            return True
        mode: LogMode = getattr(record, "log_mode", LogMode.NORMAL)
        if mode is not LogMode.NORMAL:
            return False
        return get_logging_command_output_enabled()
```

Then update the two `test_local_host.py` tests that asserted the old
`host.log`-reading behavior — `test_run_command_with_local_suppression` and
`test_concurrent_per_host_suppression_does_not_conflict_globally` — to assert the
new mechanism: a `QUIET` host's emitted command records carry
`log_mode=LogMode.QUIET` (so the filter drops them), rather than the filter
reading `host.log`. Drive them through the emit path (`_log_command`/`_log_output`
with the effective mode) or construct records with `log_mode=LogMode.QUIET`.

- [ ] **Step 5: Update the spec mirror + coercion + schema**

In `src/otto/models/host.py`, replace `log: bool = True` with:

```python
    log: LogMode = LogMode.NORMAL

    @field_validator("log", mode="before")
    @classmethod
    def _coerce_log_bool(cls, v: object) -> object:
        # Backward-compat: lab data may still declare log = true/false.
        if isinstance(v, bool):
            return LogMode.QUIET if v is False else LogMode.NORMAL
        return v
```

Import `LogMode` and `field_validator`. Bump the host-spec schema version constant (wherever the autocomplete/hosts.json schema version lives — grep `schema` version in `models/jsonschema.py` / completion cache) by one, and regenerate any committed schema via `make schema` if the repo tracks it.

- [ ] **Step 6: Update the drift guard**

Adjust `tests/unit/models/test_host_specs.py` runtime-field assertions so `log` is compared as `LogMode.NORMAL` (not `True`).

- [ ] **Step 7: Run the FULL unit suite**

Run: `uv run --no-sync pytest tests/unit -q`
Expected: PASS. Fix any `log=`/`host.log=` literals the run surfaces (tests, fixtures, monitor).

- [ ] **Step 8: Typecheck + commit**

```bash
uv run --no-sync ty check src/otto
git add src/otto/host src/otto/models tests/unit
# Commit message:
#   feat(host): per-host log field becomes LogMode (spec mirror + bool coercion)
```

---

### Task 5: `NEVER` redaction of session diagnostics

Honor the `redact` flag threaded in Task 3: the three content-bearing DEBUG diagnostics in `_run_cmd_inner` emit a redacted placeholder instead of the raw command/chunk/buffer, so the su password and embedded hex never leak even at `--log-level DEBUG`.

**Files:**
- Modify: `src/otto/host/session.py` (`_run_cmd_inner`)
- Test: `tests/unit/host/test_session_logging.py`

**Interfaces:**
- Consumes: `redact: bool` parameter on `_Session.run_cmd` / `_run_cmd_inner` (Task 3).
- Produces: when `redact` is true, the framed-write, begin-marker, and buffer-preview DEBUG lines render `<redacted N bytes>` rather than content.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_session_logging.py  (add)
import logging


async def test_never_redacts_session_diagnostics(make_session, caplog):
    # make_session: existing helper that builds a _Session over a fake transport
    sess = make_session(echo="SECRETPW")
    with caplog.at_level(logging.DEBUG, logger="otto"):
        await sess.run_cmd("SECRETPW", redact=True)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETPW" not in blob
    assert "<redacted" in blob
```

(Adapt `make_session` to the existing session test harness in that file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/host/test_session_logging.py -k never_redacts -q`
Expected: FAIL — diagnostics print `cmd='SECRETPW'`.

- [ ] **Step 3: Redact the diagnostics**

In `_run_cmd_inner`, thread `redact` and guard the three `logger.debug` calls. Replace the framed-write block:

```python
        if redact:
            logger.debug(
                f"{self._log_tag}: framed write cmd=<redacted> "
                f"payload=<redacted {len(framed)} bytes>"
            )
        else:
            logger.debug(f"{self._log_tag}: framed write cmd={cmd!r} payload={shown!r}")
```

Guard the begin-marker line:

```python
                        if not redact:
                            logger.debug(
                                f"{self._log_tag}: begin marker matched on chunk={data!r}"
                            )
```

Guard the buffer-preview summary:

```python
        if redact:
            logger.debug(
                f"{self._log_tag}: run_cmd done cmd=<redacted> retcode={retcode} "
                f"output_len={len(output)} buffer=<redacted {len(buffer)} bytes>"
            )
        else:
            logger.debug(
                f"{self._log_tag}: run_cmd done cmd={cmd!r} retcode={retcode} "
                f"output_len={len(output)} buffer={buffer_preview!r}"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/unit/host/test_session_logging.py -k never_redacts -q`
Expected: PASS.

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/host/session.py tests/unit/host/test_session_logging.py
# Commit message:
#   feat(host): redact session DEBUG diagnostics for NEVER commands
```

---

### Task 6: Three-sink topology in `management.py`

Rename `otto.log` → `console.log`, add `verbose.log`, set the `'otto'` logger level to the most-verbose floor, give `verbose.log` its INFO-floor / DEBUG-at-`--log-level DEBUG` level, apply the single `--rich-log-file` to both files, and restrict the console-suppress filter to console + `console.log` (so `verbose.log` keeps `QUIET`). This is where `QUIET` behavior diverges from today.

**Files:**
- Modify: `src/otto/logger/management.py` (`_LogConfig`, `init_cli_logging`, `_add_log_handlers`, `reset`)
- Modify: `src/otto/cli/main.py` (the `HostFilter` attach loop — restrict to non-verbose handlers)
- Test: `tests/unit/logger/test_management.py`

**Interfaces:**
- Consumes: `RichFormatter` (existing), `HostFilter` (Task 2), `LogMode` records (Tasks 2-4).
- Produces:
  - `init_cli_logging(..., show_time: bool = False)` (renamed from `verbose`).
  - Per-run dir contains `console.log` **and** `verbose.log`.
  - Module-level helper `verbose_floor(log_level: str) -> int` → `DEBUG` if `log_level == "DEBUG"` else `INFO`.
  - `management.attach_console_suppress_filter(filt)` applies *filt* to the console + console.log handlers only (used by `main.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/logger/test_management.py  (replace the otto.log assertions; add)
import logging

from otto.host.host import HostFilter
from otto.host.host import LogMode  # re-exported; or from otto.logger.mode


def test_create_output_dir_writes_console_and_verbose(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test", "mysuite")
    assert (out / "console.log").exists()
    assert (out / "verbose.log").exists()
    assert not (out / "otto.log").exists()


def test_verbose_log_keeps_quiet_console_log_drops_it(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.attach_console_suppress_filter(HostFilter())
    out = management.create_output_dir("test")
    log = logging.getLogger("otto")
    host = type("H", (), {"name": "h1"})()
    log.info("@h1 > | quiet line", extra={"host": host, "log_mode": LogMode.QUIET})
    management._state.listener.stop()  # flush the queue
    assert "quiet line" in (out / "verbose.log").read_text()
    assert "quiet line" not in (out / "console.log").read_text()


def test_verbose_floor():
    assert management.verbose_floor("INFO") == logging.INFO
    assert management.verbose_floor("WARNING") == logging.INFO
    assert management.verbose_floor("DEBUG") == logging.DEBUG
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/logger/test_management.py -q`
Expected: FAIL — only `otto.log` exists; no `verbose_floor`/`attach_console_suppress_filter`.

- [ ] **Step 3: Implement the topology**

In `management.py`, extend `_LogConfig` with `show_time: bool = False` and `verbose_handler`/`console_log_handler` references as needed, and add:

```python
def verbose_floor(log_level: str) -> int:
    """The 'otto' logger / verbose.log floor: DEBUG when debugging, else INFO."""
    return logging.DEBUG if log_level == "DEBUG" else logging.INFO
```

`init_cli_logging` — rename `verbose` param to `show_time`, set the logger level to the floor, and store it:

```python
def init_cli_logging(
    xdir: Path,
    log_level: str,
    keep_days: float,
    rich_log_file: bool = False,
    show_time: bool = False,
) -> None:
    logger = getLogger("otto")
    logger.setLevel(verbose_floor(log_level))   # floor so INFO reaches the queue at WARNING
    logger.propagate = False
    is_debug = log_level == "DEBUG"

    stdout_handler = RichHandler(
        level=log_level,          # console filters up to --log-level
        console=CONSOLE,
        show_time=show_time,
        ...
    )
    logger.addHandler(stdout_handler)
    _state.xdir = Path(xdir)
    _state.rich_log_file = rich_log_file
    _state.keep_seconds = keep_days * 24 * 60 * 60
    _state.console_handler = stdout_handler
    _state.log_level = log_level
```

`_add_log_handlers` — build two file handlers and fan all three through the listener:

```python
def _make_file_handler(path: Path, level: int, rich: bool) -> FileHandler:
    fh = FileHandler(path, mode="x")
    fh.setLevel(level)
    fmt = RichFormatter()
    fmt.rich = rich
    fh.setFormatter(fmt)
    return fh


def _add_log_handlers(output_dir: Path) -> None:
    logger = getLogger("otto")
    for h in list(logger.handlers):
        if isinstance(h, (NullHandler, QueueHandler)) or h is _state.console_handler:
            logger.removeHandler(h)

    console_handlers = [_state.console_handler] if _state.console_handler is not None else []
    level = logging.getLevelName(_state.log_level)
    console_log = _make_file_handler(output_dir / "console.log", level, _state.rich_log_file)
    verbose_log = _make_file_handler(
        output_dir / "verbose.log", verbose_floor(_state.log_level), _state.rich_log_file
    )
    _state.console_log_handler = console_log
    _state.verbose_handler = verbose_log

    log_queue: Queue[LogRecord] = Queue(-1)
    _state.listener = QueueListener(
        log_queue, *console_handlers, console_log, verbose_log, respect_handler_level=True
    )
    logger.addHandler(QueueHandler(log_queue))
    _state.listener.start()
    atexit.register(_stop_listener)


def attach_console_suppress_filter(filt: Filter) -> None:
    """Apply *filt* to the console + console.log handlers only (NOT verbose.log)."""
    for h in (_state.console_handler, _state.console_log_handler):
        if h is not None:
            h.addFilter(filt)
```

Update `reset()` to clear the new `_state` fields. In `cli/main.py`, replace the attach loop:

```python
    from ..host import HostFilter
    management.attach_console_suppress_filter(HostFilter())
```

- [ ] **Step 4: Run tests**

Run: `uv run --no-sync pytest tests/unit/logger -q`
Expected: PASS (new sink tests + existing, minus the removed `otto.log` assertion).

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/logger/management.py src/otto/cli/main.py tests/unit/logger/test_management.py
# Commit message:
#   feat(logger): three sinks (console.log + verbose.log) with console-suppress filter
```

---

### Task 7: CLI flags — `--show-time`, `--lab-depth`, `acting as` → `logger.info`

Rename `--verbose`/`-v` → `--show-time`, add `--lab-depth` to control the `--show-lab` display depth, route the `acting as` notice through the logger, and update the `init_cli_logging` call.

**Files:**
- Modify: `src/otto/cli/main.py` (`verbose` param → `show_time`; new `lab_depth` param; `init_cli_logging(show_time=...)`; `acting as` block; `show_lab` block)
- Test: `tests/unit/cli/test_main.py`, `tests/unit/cli/test_listing.py`

**Interfaces:**
- Produces: `--show-time` (bool, default False), `--lab-depth` (int, default 3, `0` = unlimited). The callback passes `show_time=show_time` to `init_cli_logging`. `acting as` is emitted via `logger.info`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/cli/test_main.py  (add)
def test_show_time_flag_replaces_verbose(run_cli):
    # run_cli: existing CliRunner helper
    result = run_cli(["--help"])
    assert "--show-time" in result.output
    assert "--verbose" not in result.output


def test_lab_depth_flag_present(run_cli):
    result = run_cli(["--help"])
    assert "--lab-depth" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/cli/test_main.py -k "show_time or lab_depth" -q`
Expected: FAIL — `--verbose` still present; no `--lab-depth`.

- [ ] **Step 3: Update the CLI**

Replace the `verbose` parameter (lines ~200-206):

```python
    show_time: Annotated[
        bool,
        typer.Option(
            "--show-time",
            help="Show per-line timestamps on the live console (log files are always timestamped).",
        ),
    ] = False,
    lab_depth: Annotated[
        int,
        typer.Option(
            "--lab-depth",
            min=0,
            help="Depth for --show-lab output (0 = unlimited).",
        ),
    ] = 3,
```

`init_cli_logging` call → `show_time=show_time` (was `verbose=verbose`).

`acting as` block (lines ~398-402):

```python
    if identity is not None and identity.source == "--as-user":
        logger.info(
            f"[bold magenta][reservations] acting as {identity.username!r} (--as-user)[/bold magenta]"
        )
```

`show_lab` block (lines ~411-419) → depth from `--lab-depth`:

```python
    if show_lab:
        from rich.pretty import pprint

        pprint(lab, max_depth=(None if lab_depth == 0 else lab_depth), expand_all=True)
        raise typer.Exit
```

Grep for any other `verbose` reference in `main.py` (e.g. `--show-lab` depth) and replace.

- [ ] **Step 4: Run tests**

Run: `uv run --no-sync pytest tests/unit/cli/test_main.py tests/unit/cli/test_listing.py -q`
Expected: PASS.

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/cli/main.py tests/unit/cli
# Commit message:
#   feat(cli): rename --verbose to --show-time; add --lab-depth; log 'acting as'
```

---

### Task 8: Library / external logger capture

Add a `[logging] capture = [...]` settings table, derive product package prefixes from each repo's `init`/`libs`, and attach the shared `QueueHandler` to those prefixes (plus `otto.*`) with the verbose floor level — so `logging.getLogger(__name__)` from product code is captured without third-party noise.

**Files:**
- Modify: `src/otto/models/settings.py` (`SettingsModel` + new `LoggingConfigSpec`)
- Modify: `src/otto/configmodule/repo.py` (`Repo.logging_capture` property; `Repo.product_log_prefixes()` derivation)
- Modify: `src/otto/logger/management.py` (`capture_external_loggers(prefixes)`)
- Modify: `src/otto/cli/main.py` (collect prefixes across repos; call `capture_external_loggers`)
- Test: `tests/unit/logger/test_management.py`, `tests/unit/configmodule/test_repo.py`

**Interfaces:**
- Produces:
  - `LoggingConfigSpec(OttoModel)` with `capture: list[str] = []`; `SettingsModel.logging: LoggingConfigSpec = LoggingConfigSpec()`.
  - `Repo.product_log_prefixes() -> set[str]` = `{first segment of each init entry} ∪ {immediate child package dirs of each libs path} ∪ {logging.capture entries}`.
  - `management.capture_external_loggers(prefixes: Iterable[str]) -> None`: adds the shared `QueueHandler` to each `getLogger(prefix)` and sets its level to the verbose floor.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/logger/test_management.py  (add)
import logging


def test_capture_external_logger_lands_in_sinks(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    management.capture_external_loggers(["myproduct"])
    logging.getLogger("myproduct.install").info("product line")
    logging.getLogger("asyncssh").info("third-party noise")
    management._state.listener.stop()
    verbose = (out / "verbose.log").read_text()
    assert "product line" in verbose
    assert "third-party noise" not in verbose
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/logger/test_management.py -k capture_external -q`
Expected: FAIL — no `capture_external_loggers`.

- [ ] **Step 3: Add the settings spec**

In `src/otto/models/settings.py`:

```python
class LoggingConfigSpec(OttoModel):
    """Boundary spec for the ``[logging]`` section of ``settings.toml``."""

    capture: list[str] = Field(default_factory=list)
```

Add to `SettingsModel`: `logging: LoggingConfigSpec = LoggingConfigSpec()`.

- [ ] **Step 4: Derive prefixes on `Repo`**

In `src/otto/configmodule/repo.py`, store `self.logging_capture = list(model.logging.capture)` in `parse_settings`, and add:

```python
    def product_log_prefixes(self) -> set[str]:
        """Top-level package names whose ``logging.getLogger(__name__)`` records
        otto should capture: declared init module roots, immediate sub-packages
        of each ``libs`` dir, and explicit ``[logging] capture`` entries."""
        prefixes: set[str] = set(self.logging_capture)
        for mod in self.init:
            prefixes.add(mod.split(".", 1)[0])
        for lib in self.libs:
            if lib.is_dir():
                for child in lib.iterdir():
                    if (child / "__init__.py").exists():
                        prefixes.add(child.name)
        return prefixes
```

- [ ] **Step 5: Implement the capture attach + wire it in `main.py`**

In `management.py`:

```python
def capture_external_loggers(prefixes: "Iterable[str]") -> None:
    """Route the named top-level loggers into otto's sinks (CLI/app only)."""
    if _state.listener is None:
        return
    queue_handler = next(
        (h for h in getLogger("otto").handlers if isinstance(h, QueueHandler)), None
    )
    if queue_handler is None:
        return
    floor = verbose_floor(_state.log_level)
    for prefix in prefixes:
        lg = getLogger(prefix)
        lg.setLevel(floor)
        if queue_handler not in lg.handlers:
            lg.addHandler(queue_handler)
```

In `cli/main.py`, after `create_output_dir` has wired the listener for the chosen subcommand (the capture must run after `_add_log_handlers`), collect prefixes and attach:

```python
    prefixes: set[str] = set()
    for repo in repos:
        prefixes |= repo.product_log_prefixes()
    management.capture_external_loggers(prefixes)
```

> Placement note: `capture_external_loggers` needs the `QueueHandler` to exist, which `create_output_dir` creates per subcommand. Call it from the same place each subcommand calls `create_output_dir` (or immediately after, in the subcommand callbacks). Add a one-line helper in `main.py` invoked alongside `create_output_dir`.

- [ ] **Step 6: Run tests**

Run: `uv run --no-sync pytest tests/unit/logger/test_management.py tests/unit/configmodule/test_repo.py -q`
Expected: PASS.

- [ ] **Step 7: Typecheck + commit**

```bash
uv run --no-sync ty check src/otto
git add src/otto/models/settings.py src/otto/configmodule/repo.py src/otto/logger/management.py src/otto/cli/main.py tests/unit
# Commit message:
#   feat(logger): capture product-package loggers via [logging] capture + prefix derivation
```

---

### Task 9: Library-citizen guarantees (no CLI baggage on import)

Pin down that importing otto **as a library** stays clean: a single `NullHandler` on `'otto'`, `propagate=True`, no console/file handlers, no `QueueListener`, and no attachment to root or product prefixes. Also confirm a consumer's own handler receives otto records, and that `management.reset()` restores the library-citizen state. These tests guard against the CLI wiring leaking into library use.

**Files:**
- Test: `tests/unit/logger/test_library_usage.py` (new)

**Interfaces:**
- Consumes: `otto.logger` import side effects (Task 0 baseline + unchanged), `management.init_cli_logging` / `reset` (Tasks 6, 8), `capture_external_loggers` (Task 8), `get_otto_logger` / `otto.get_logger` (Task 11 — import-time only; the re-export test lives in Task 11).

- [ ] **Step 1: Write the failing/forcing test**

```python
# tests/unit/logger/test_library_usage.py
"""Guarantees for using otto as a library — no CLI-specific handler baggage."""

import logging

import pytest

from otto.logger import get_otto_logger, management


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _otto():
    return logging.getLogger("otto")


def test_plain_import_attaches_only_nullhandler():
    # Fresh library-citizen state (the autouse reset() restored it).
    handlers = _otto().handlers
    assert handlers, "otto should carry its NullHandler"
    assert all(isinstance(h, logging.NullHandler) for h in handlers)


def test_library_logger_propagates_to_consumer_root():
    # A library consumer configures ITS OWN handler on the root logger; otto's
    # records must reach it (propagate=True in library-citizen mode).
    assert _otto().propagate is True
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    root = logging.getLogger()
    handler = _Capture()
    root.addHandler(handler)
    try:
        get_otto_logger("demo").warning("library warning")
    finally:
        root.removeHandler(handler)
    assert "library warning" in records


def test_no_queue_listener_or_file_handlers_on_import():
    assert management._state.listener is None
    # No FileHandler / QueueHandler anywhere on 'otto' in library mode.
    from logging.handlers import QueueHandler

    for h in _otto().handlers:
        assert not isinstance(h, (logging.FileHandler, QueueHandler))


def test_reset_restores_library_citizen_state_after_cli_init(tmp_path):
    # Simulate a CLI run, then reset() and confirm we're back to library mode.
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("test")
    management.capture_external_loggers(["some_product_pkg"])
    management.reset()

    otto = _otto()
    assert otto.propagate is True
    assert all(isinstance(h, logging.NullHandler) for h in otto.handlers)
    assert management._state.listener is None
    # The product prefix logger must not retain otto's QueueHandler after reset.
    from logging.handlers import QueueHandler

    prod = logging.getLogger("some_product_pkg")
    assert not any(isinstance(h, QueueHandler) for h in prod.handlers)
```

- [ ] **Step 2: Run the tests**

Run: `uv run --no-sync pytest tests/unit/logger/test_library_usage.py -q`
Expected: the first three pass immediately (library-citizen behavior already holds); `test_reset_restores_library_citizen_state_after_cli_init` FAILS if `reset()` does not detach the `QueueHandler` from captured product prefixes.

- [ ] **Step 3: Make `reset()` detach captured-prefix handlers**

If Step 2's reset test fails, extend `management.reset()` to remove otto's `QueueHandler` from any logger it was attached to via `capture_external_loggers`. Track the captured prefixes in `_state` (e.g. `_state.captured_prefixes: list[str]`) when `capture_external_loggers` runs, and in `reset()`:

```python
    from logging.handlers import QueueHandler

    for prefix in _state.captured_prefixes:
        lg = getLogger(prefix)
        for h in list(lg.handlers):
            if isinstance(h, QueueHandler):
                lg.removeHandler(h)
        lg.setLevel(logging.NOTSET)
    _state.captured_prefixes = []
```

Add `captured_prefixes: list[str] = field(default_factory=list)` to `_LogConfig`, populate it in `capture_external_loggers`, and clear it in `reset()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync pytest tests/unit/logger/test_library_usage.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Stage (do NOT commit)**

```bash
git add tests/unit/logger/test_library_usage.py src/otto/logger/management.py
# Commit message to hand to the user:
#   test(logger): pin library-citizen guarantees (no CLI handler baggage)
```

---

### Task 10: Reclassify the `log=False` / `host.log` call sites

Apply the disposition table from the spec to the production call sites.

**Files:**
- Modify: `src/otto/host/file_ops.py:114`, `src/otto/host/unix_host.py:721` → `LogMode.QUIET`
- Modify: `src/otto/host/embedded_host.py:657-661`, `src/otto/host/privilege.py:56` → `LogMode.NEVER`
- Modify: `src/otto/monitor/factory.py:42` → `host.log = LogMode.NEVER`
- Modify: `src/otto/configmodule/repo.py:680` (`LocalHost(log=LogMode.QUIET)`)
- Test: `tests/unit/host/test_file_ops.py`, `tests/unit/host/test_privilege.py`, `tests/unit/monitor/test_monitor_factory.py`, `tests/unit/host/test_embedded_host.py`

**Interfaces:**
- Consumes: `LogMode` (Task 1); `NEVER` redaction (Task 5); three sinks (Task 6).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/host/test_privilege.py  (add/adapt)
from otto.logger.mode import LogMode


async def test_su_password_sent_with_never(mock_send):
    # mock_send records (text, log) calls
    await _perform_su(mock_send.send, mock_send.expect, "root", "hunter2", lambda u: None)
    pw_calls = [c for c in mock_send.calls if c.text.startswith("hunter2")]
    assert pw_calls and all(c.log is LogMode.NEVER for c in pw_calls)
```

```python
# tests/unit/monitor/test_monitor_factory.py  (add/adapt)
from otto.logger.mode import LogMode


def test_monitor_host_set_to_never(make_monitor_host):
    host = make_monitor_host()
    assert host.log is LogMode.NEVER
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest tests/unit/host/test_privilege.py tests/unit/monitor/test_monitor_factory.py -k "never" -q`
Expected: FAIL — sites still pass `log=False`.

- [ ] **Step 3: Apply the reclassifications**

- `file_ops.py:114`: `result = await self.oneshot(cmd, log=LogMode.QUIET)`
- `unix_host.py:721`: `result = await self.oneshot("cat /proc/modules", log=LogMode.QUIET)`
- `embedded_host.py:657-661`: both `run_cmd(..., log=LogMode.NEVER)`
- `privilege.py:56`: `await send(pw + "\n", log=LogMode.NEVER)`
- `monitor/factory.py:42`: `host.log = LogMode.NEVER`
- `repo.py:680`: `host = LocalHost(log=LogMode.QUIET)`

Add `from ..logger.mode import LogMode` (or the correct relative depth) to each file.

- [ ] **Step 4: Run tests**

Run: `uv run --no-sync pytest tests/unit/host/test_file_ops.py tests/unit/host/test_privilege.py tests/unit/monitor/test_monitor_factory.py tests/unit/host/test_embedded_host.py -q`
Expected: PASS.

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/host/file_ops.py src/otto/host/unix_host.py src/otto/host/embedded_host.py src/otto/host/privilege.py src/otto/monitor/factory.py src/otto/configmodule/repo.py tests/unit
# Commit message:
#   feat: reclassify log=False call sites to LogMode.QUIET / NEVER
```

---

### Task 11: Re-export `get_otto_logger` as `otto.get_logger`

Make `otto.get_logger` the blessed accessor (sugar over `get_otto_logger`), so product code has an ergonomic option alongside the generic `logging.getLogger(__name__)`.

**Files:**
- Modify: `src/otto/__init__.py` (re-export)
- Test: `tests/unit/logger/test_logger.py`

**Interfaces:**
- Produces: `otto.get_logger` is `otto.logger.get_otto_logger`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/logger/test_logger.py  (add)
def test_otto_get_logger_reexport():
    import otto
    from otto.logger import get_otto_logger

    assert otto.get_logger is get_otto_logger
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/unit/logger/test_logger.py -k reexport -q`
Expected: FAIL — `otto` has no `get_logger`.

- [ ] **Step 3: Add the re-export**

In `src/otto/__init__.py`:

```python
from .logger import get_otto_logger as get_logger
```

(Place with the other public re-exports; keep alphabetical/`as`-import style consistent with the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/unit/logger/test_logger.py -k reexport -q`
Expected: PASS.

- [ ] **Step 5: Stage + commit**

```bash
git add src/otto/__init__.py tests/unit/logger/test_logger.py
# Commit message:
#   feat(logger): re-export get_otto_logger as otto.get_logger
```

---

### Task 12: Sweep `otto.log` references + full gate

Update remaining references to the renamed file and run the full gate.

**Files:**
- Modify: `tests/conftest.py`, `tests/repo1/pylib/repo1_instructions/{install.py,nc_smoke.py}`, `tests/repo1/tests/{test_stability_fixture.py,test_coverage_product.py}`, `tests/repo3/tests/test_embedded_coverage.py`, `tests/e2e/host/test_interact_e2e.py`, `tests/unit/host/test_interact.py`, and any other `otto.log` reference surfaced by grep.
- Modify: docs that mention the single `otto.log` / `--verbose` (grep `docs/`).

**Interfaces:**
- Consumes: all prior tasks.

- [ ] **Step 1: Find every remaining reference**

Run:
```bash
grep -rn "otto\.log\b\|otto\.log'" tests/ src/ docs/ | grep -v "verbose.log\|console.log"
grep -rn "\-\-verbose\b\|verbose=True\|verbose=False" src/ docs/ tests/ | grep -vi "show_time"
```

- [ ] **Step 2: Update each reference**

Replace `otto.log` with `console.log` where the test wants the console transcript, or `verbose.log` where it wants the full record (judge per assertion — most coverage/interact fixtures read whatever the run wrote; `console.log` is the like-for-like replacement). Replace `--verbose` docs/usages with `--show-time`.

- [ ] **Step 3: Run the unit + nox + typecheck + docs gate**

Run:
```bash
make coverage
make typecheck
make nox
make docs
```
Expected: all green; coverage at or above the floor.

- [ ] **Step 4: Stage + commit**

```bash
git add -A
# Commit message:
#   refactor: rename otto.log -> console.log across tests/docs; --verbose -> --show-time
```

---

## Self-Review

**Spec coverage:**
- `LogMode` model (NORMAL/QUIET/NEVER, default NORMAL, per-command + per-host, effective=most-restrictive) → Tasks 1, 3, 4.
- Level native (no param) → preserved (no level param added).
- Scope: LogMode gates command I/O only; warnings/errors always captured → Task 2 filter (`host is None → True`), reinforced by Task 6 (filter not on verbose).
- Three sinks + logger level floor + per-handler levels → Task 6.
- `console.log` faithful transcript (same level + filter, always timestamped, rich strip) → Task 6 (`RichFormatter` already timestamps).
- `acting as` promoted; `--show-lab` not logged → Task 7.
- `verbose.log` "everything" minus NEVER, ignoring console-suppress → Tasks 5, 6.
- `NEVER` redaction at source incl. session diagnostics → Tasks 3 (sink), 5 (diagnostics).
- `--rich-log-file` both files → Task 6.
- `--verbose` → `--show-time`; new `--lab-depth` → Task 7.
- Library capture (otto.* + product prefixes + `[logging] capture`) → Task 8.
- Library-citizen guarantees (NullHandler-only on import, propagate, no QueueListener/file/root/prefix handlers, reset restores) → Task 9.
- `otto.get_logger` sugar → Task 11.
- Reclassify ~6 call sites → Task 10.
- File rename + test churn → Tasks 6, 12.
- Per-host field → spec mirror + schema + drift guard → Task 4.
- Testing strategy items (LogMode unit, sink topology, NEVER redaction, console.log faithfulness, --rich-log-file, CLI flags, library capture, library-citizen, scope guarantee) → distributed across Tasks 1-10.

**Placeholder scan:** No `TBD`/`TODO`/"handle edge cases". Each code step shows the change. Two steps ("adapt to existing helper") reference existing test harnesses (`make_session`, `make_unix_host`, `run_cli`) rather than inventing them — the implementer wires to the real fixtures in those files.

**Type consistency:** `LogMode` / `effective_mode` signatures are consistent across tasks; `log: LogMode` default `LogMode.NORMAL` everywhere; `ShellCommand.log: LogMode | None`; session `redact: bool`; `verbose_floor(str) -> int`; `capture_external_loggers(Iterable[str])`; `product_log_prefixes() -> set[str]`.

## Execution Handoff

After the plan is approved, choose an execution approach (see end of session).
