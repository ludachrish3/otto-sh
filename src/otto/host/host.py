from __future__ import annotations

import asyncio
import re
from abc import ABC
from collections.abc import Callable, Sequence
from dataclasses import (
    dataclass,
)
from datetime import datetime, timedelta
from logging import (
    Filter,
    LogRecord,
    getLogger,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Literal,
    Optional,
    Protocol,
)

from ..utils import (
    CommandStatus,
    Status,
)

if TYPE_CHECKING:
    from .repeat import RepeatRunner
    from .session import Expect, HostSession

TermType = Literal['ssh', 'telnet']
FileTransferType = Literal['scp', 'sftp', 'ftp', 'nc']

logger = getLogger('otto')

_logCommandOutput = True

def getLoggingCommandOutputEnabled(
) -> bool:

    global _logCommandOutput
    return _logCommandOutput

def _setLoggingCommandOutputEnabled(
    enabled: bool,
) -> None:

    global _logCommandOutput
    _logCommandOutput = enabled


_globalDryRun = False

def isDryRun() -> bool:
    """Return True if dry-run mode is enabled globally."""
    return _globalDryRun

def setDryRun(enabled: bool = True) -> None:
    """Enable or disable global dry-run mode."""
    global _globalDryRun
    _globalDryRun = enabled


@dataclass(slots=True)
class ShellCommand:
    """A command plus the per-command options that should be used to run it.

    Fields left as ``None`` inherit from the run-level kwargs on :meth:`Host.run`.
    A scalar :data:`~otto.host.session.Expect` value is accepted for ``expects``
    for ergonomics; it is normalized to a one-element list before execution.
    """

    cmd: str
    """Command string to execute."""

    expects: 'Expect | list[Expect] | None' = None
    """Per-command expects. ``None`` inherits the run-level ``expects`` value."""

    timeout: float | None = None
    """Per-command timeout cap. ``None`` inherits the run-level ``timeout`` value.

    In list form, the effective timeout is always bounded by the remaining
    cumulative budget.
    """


@dataclass(slots=True)
class RunResult:
    """Unified result of :meth:`Host.run` regardless of how many commands ran.

    ``statuses`` always has one entry per issued command. ``status`` is the
    aggregate: ``Status.Success`` when every entry is ok, otherwise the first
    non-ok status encountered (matching the old tuple-form semantics).
    """

    status: Status
    """Aggregate status across all commands."""

    statuses: list[CommandStatus]
    """Per-command statuses in execution order."""

    @property
    def only(self) -> CommandStatus:
        """Return the sole :class:`CommandStatus` when exactly one command ran.

        Raises ``ValueError`` otherwise — useful for single-command call sites
        that want to read fields directly without unpacking.
        """
        if len(self.statuses) != 1:
            raise ValueError(
                f"RunResult.only requires exactly 1 command status, got {len(self.statuses)}"
            )
        return self.statuses[0]


def _normalize_expects(
    expects: 'Expect | list[Expect] | None',
) -> list['Expect'] | None:
    """Wrap a scalar ``Expect`` (a 2-tuple) into a one-element list.

    ``None`` and existing lists pass through unchanged. Disambiguation is by
    ``isinstance(expects, tuple)`` — tuples and lists don't overlap.
    """
    if expects is None:
        return None
    if isinstance(expects, tuple):
        return [expects]
    return expects


def _resolve_command(
    item: 'str | ShellCommand',
    default_expects: 'Expect | list[Expect] | None',
    default_timeout: float | None,
) -> ShellCommand:
    """Coerce ``item`` to a ``ShellCommand`` whose ``None`` fields inherit from defaults."""
    if isinstance(item, str):
        return ShellCommand(cmd=item, expects=default_expects, timeout=default_timeout)
    return ShellCommand(
        cmd=item.cmd,
        expects=item.expects if item.expects is not None else default_expects,
        timeout=item.timeout if item.timeout is not None else default_timeout,
    )


async def _run_cmds_with_budget(
    run_one: Callable[[ShellCommand, float | None], Awaitable[CommandStatus]],
    cmds: list[ShellCommand],
    timeout: float | None,
) -> RunResult:
    """Run a list of commands sequentially under a shared timeout budget.

    Each command receives the minimum of its own ``ShellCommand.timeout`` and
    the remaining budget; when the budget is exhausted, remaining commands are
    skipped with ``Status.Error``. Used by both ``BaseHost.run`` and
    ``HostSession.run`` so the budgeting logic lives in one place.
    """
    deadline: float | None = None
    if timeout is not None:
        deadline = asyncio.get_running_loop().time() + timeout

    overall_status = Status.Success
    statuses: list[CommandStatus] = []

    for sc in cmds:
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                statuses.append(CommandStatus(
                    command=sc.cmd,
                    output='Skipped: cumulative timeout budget exhausted',
                    status=Status.Error,
                    retcode=-1,
                ))
                if overall_status.is_ok:
                    overall_status = Status.Error
                continue

        if sc.timeout is None:
            effective = remaining
        elif remaining is None:
            effective = sc.timeout
        else:
            effective = min(sc.timeout, remaining)

        result = await run_one(sc, effective)
        statuses.append(result)
        if not result.status.is_ok and overall_status.is_ok:
            overall_status = result.status

    return RunResult(status=overall_status, statuses=statuses)


class Host(Protocol):

    log: bool
    """Determines whether this host should log its output to stdout and log files."""

    async def _interact(self) -> None:
        ...

    def interact(self) -> None:
        ...

    async def run(self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        ...

    async def oneshot(self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        ...

    async def open_session(self,
        name: str,
    ) -> HostSession:
        ...

    async def send(self,
        text: str,
    ) -> None:
        ...

    async def expect(self,
        pattern: str | re.Pattern[str],
        timeout: float = 30.0,
    ) -> str:
        ...

    ####################
    #  File transfer
    ####################

    async def get(self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        ...

    async def put(self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        ...

    async def close(self) -> None:
        ...

class BaseHost(ABC):

    name: str
    _repeater: 'RepeatRunner'

    ####################
    #  Dry-run helpers
    ####################

    def _dry_run_result(self, cmd: str) -> CommandStatus:
        """Return a synthetic CommandStatus for dry-run mode."""
        self._log_command(f"[DRY RUN] {cmd}")
        return CommandStatus(command=cmd, output="[DRY RUN] Command not executed", status=Status.Skipped, retcode=0)

    def _dry_run_transfer(self, action: str, files: list[Path], dest: Path) -> tuple[Status, str]:
        """Return a synthetic transfer result for dry-run mode."""
        file_names = ", ".join(str(f) for f in files)
        self._log_command(f"[DRY RUN] {action}: {file_names} -> {dest}")
        return (Status.Skipped, f"[DRY RUN] {action}: {file_names} -> {dest}")

    ####################
    #  Command execution
    ####################

    async def _interact(self) -> None:
        raise NotImplementedError(
            f"The '{self.__class__.__name__}' class does not support interactive sessions"
        ) from None

    async def interact(self) -> None:
        """Open an interactive shell bridged to the local terminal.

        Subclasses implement :meth:`_interact` to do the actual protocol
        work. This wrapper exists so CLI and SDK callers have a single
        public entry point.
        """
        await self._interact()

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        """Execute one or more commands on the host via the persistent shell session.

        The session is stateful: working directory changes (``cd``), exported environment
        variables, and other shell state persist between calls, just as they would in
        an interactive terminal.

        Args:
            cmds: A single command (``str`` or :class:`ShellCommand`) or a sequence of
                commands. Strings and :class:`ShellCommand` objects may be mixed. For
                single-command calls, read the result via ``result.only`` (or
                ``result.statuses[0]``).
            expects: Default ``(pattern, response)`` pair(s) for interactive prompts.
                Accepts a single ``Expect`` tuple or a list of them. Each command
                inherits this value unless its own :attr:`ShellCommand.expects` is set.
            timeout: For a single command, the per-command timeout. For a sequence, a
                cumulative timeout shared across all commands — each command receives
                the remaining budget; when exhausted, remaining commands are skipped
                with ``Status.Error``. :attr:`ShellCommand.timeout` caps the per-command
                value but is still bounded by the remaining budget.

        Returns:
            :class:`RunResult` with the aggregate :class:`Status` and a list of
            per-command :class:`CommandStatus` entries.

        See Also:
            :meth:`oneshot`: stateless, concurrent-safe alternative for one-off commands.
        """
        default_expects = _normalize_expects(expects)
        if isinstance(cmds, (str, ShellCommand)):
            resolved = [_resolve_command(cmds, default_expects, timeout)]
            single_timeout = resolved[0].timeout
            result = await self._run_one(
                resolved[0].cmd,
                expects=_normalize_expects(resolved[0].expects),
                timeout=single_timeout,
            )
            status = result.status if not result.status.is_ok else Status.Success
            return RunResult(status=status, statuses=[result])

        resolved = [_resolve_command(c, default_expects, None) for c in cmds]

        async def _run_sc(sc: ShellCommand, t: float | None) -> CommandStatus:
            return await self._run_one(
                sc.cmd,
                expects=_normalize_expects(sc.expects),
                timeout=t,
            )

        return await _run_cmds_with_budget(_run_sc, resolved, timeout)

    async def _run_one(self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Per-command runner for the persistent shell session. Subclasses override."""
        raise NotImplementedError from None

    async def oneshot(self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        raise NotImplementedError from None

    async def open_session(self,
        name: str,
    ) -> HostSession:
        raise NotImplementedError from None

    async def send(self,
        text: str,
    ) -> None:
        raise NotImplementedError from None

    async def expect(self,
        pattern: str | re.Pattern[str],
        timeout: float = 30.0,
    ) -> str:
        raise NotImplementedError from None

    ####################
    #  File transfer
    ####################

    async def get(self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        raise NotImplementedError from None

    async def put(self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> tuple[Status, str]:
        raise NotImplementedError from None

    async def close(self) -> None:
        raise NotImplementedError from None

    ####################
    #  Repeat commands
    ####################

    def start_repeat(self,
        name: str,
        cmds: list[str] | str,
        interval: timedelta,
        times: int = -1,
        duration: timedelta = timedelta.max,
        until: datetime = datetime.max,
        on_result: Callable[[str, datetime, list[CommandStatus]], None] | None = None,
        max_history: int = 1000,
    ) -> None:
        if isDryRun():
            self._log_command(f"[DRY RUN] start_repeat({name!r}, {cmds}, interval={interval})")
            return
        self._repeater.start(
            name=name, cmds=cmds, interval=interval, times=times,
            duration=duration, until=until, on_result=on_result, max_history=max_history,
        )

    async def stop_repeat(self, name: str) -> None:
        await self._repeater.stop(name)

    async def stop_all_repeats(self) -> None:
        await self._repeater.stop_all()

    def repeat_results(
        self, name: str
    ) -> list[tuple[datetime, list[CommandStatus]]]:
        return self._repeater.get_results(name)

    ####################
    #  Logging
    ####################

    # TODO: Dynamically size the preamble to be max(configModule.lab.hosts.names) + 2 (1 space on each side)
    def _log_command(self,
        command: str,
    ) -> None:
        logger.info(f"[bold]@{self.name}   | {command}", extra={'host': self})

    def _log_output(self,
        output: str,
    ) -> None:
        preamble =  f"[yellow]@{self.name} > | "
        output_lines = [ f'{preamble}{line}' for line in output.splitlines() ]

        # A python 3.10 limitation does not allow escape characters within f-string closures.
        # Assign a variable to be a newline so it can be used within an f-string closure.
        newline = '\n'
        logger.info(f"{newline.join(output_lines)}",  extra={'host': self})


class HostFilter(Filter):
    """Filter log records based on whether command output logging is globally enabled."""

    host: Host | None

    def filter(self, record: LogRecord) -> bool:

        host: Host | None = getattr(record, 'host', None)

        # From this filter's perspective, all logs not related to logging can log
        if host is None:
            return True

        # Also respect the global and host logging flags
        return _logCommandOutput and host.log

# TODO: Consider a way to make commands and their output log no matter what if the log level were debug.
@dataclass
class SuppressCommandOutput():
    """Suppress command/output logging for one host or globally.

    On enter, the prior state is snapshotted; on exit it is restored.
    That makes nesting safe — an inner context cannot clobber an outer
    one — and makes concurrent per-host suppressions race-free, since
    each context only touches its own host's ``log`` attribute.

    The no-host (global) path still mutates shared module state, so
    overlapping global contexts across async tasks can still step on
    each other. Prefer the per-host form when suppressing work that
    runs concurrently.
    """

    host: Optional[Host] = None
    """Host object to suppress. If not provided, all host output is affected."""

    def __enter__(self):
        if self.host is not None:
            self._prev_host_log = self.host.log
            self.host.log = False
        else:
            self._prev_global = getLoggingCommandOutputEnabled()
            _setLoggingCommandOutputEnabled(False)

    def __exit__(self, *_):
        if self.host is not None:
            self.host.log = self._prev_host_log
        else:
            _setLoggingCommandOutputEnabled(self._prev_global)
