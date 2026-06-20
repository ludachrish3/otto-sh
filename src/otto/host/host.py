from __future__ import annotations

import asyncio
import re
from abc import ABC
from collections.abc import Callable, Sequence
from dataclasses import (
    dataclass,
    replace,
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
    Optional,
    Protocol,
    cast,
)

from ..utils import (
    CommandStatus,
    Status,
)

if TYPE_CHECKING:
    from .power import PowerController
    from .product import Product
    from .repeat import RepeatRunner
    from .session import Expect, HostSession

logger = getLogger('otto')

def get_logging_command_output_enabled() -> bool:
    """Return True if command-output logging is enabled on the active context."""
    from ..context import try_get_context
    ctx = try_get_context()
    return ctx.log_command_output if ctx is not None else True


def is_dry_run() -> bool:
    """Return True if dry-run mode is enabled on the active context."""
    from ..context import try_get_context
    ctx = try_get_context()
    return ctx.dry_run if ctx is not None else False


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

    log: bool | None = None
    """Per-command logging switch. ``None`` inherits the run-level ``log`` value.
    Set ``False`` to suppress this command's echo and output from the console and
    log file (e.g. a multi-KB inline payload); the returned ``CommandStatus`` is
    unaffected."""


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
    default_log: bool = True,
) -> ShellCommand:
    """Coerce ``item`` to a ``ShellCommand`` whose ``None`` fields inherit from defaults."""
    if isinstance(item, str):
        return ShellCommand(cmd=item, expects=default_expects, timeout=default_timeout, log=default_log)
    return ShellCommand(
        cmd=item.cmd,
        expects=item.expects if item.expects is not None else default_expects,
        timeout=item.timeout if item.timeout is not None else default_timeout,
        log=item.log if item.log is not None else default_log,
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

    id: str
    """Unique identifier for this host."""

    name: str
    """Human-readable name for this host."""

    resources: set[str]
    """Resources required to reserve this host."""

    products: list['Product']
    """Software-under-test deployed to this host (default empty)."""

    power_control: 'PowerController | None'
    """Pluggable power backend, or None when this host can't be power-controlled."""

    async def _interact(self) -> None:
        ...

    async def interact(self) -> None:
        ...

    async def run(self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float | None = None,
        log: bool = True,
        sudo: bool = False,
    ) -> RunResult:
        ...

    async def oneshot(self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        ...

    async def open_session(self,
        name: str,
    ) -> HostSession:
        ...

    async def send(self,
        text: str,
        log: bool = True,
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

    async def power(self, state: str | None = None) -> tuple[Status, str]: ...

    async def reboot(self, hard: bool = False, wait: bool = False, timeout: float = 600.0) -> tuple[Status, str]: ...

    async def shutdown(self) -> tuple[Status, str]: ...

    async def is_reachable(self, timeout: float = 10.0) -> bool: ...

    async def wait_until_up(self, timeout: float, interval: float = 2.0) -> bool: ...

    async def wait_until_down(self, timeout: float, interval: float = 2.0) -> bool: ...

    async def close(self) -> None:
        ...

    async def stage(self) -> tuple[Status, str]: ...

    async def install(self, stage_only: bool = False) -> tuple[Status, str]: ...

    async def uninstall(self) -> tuple[Status, str]: ...

    async def is_installed(self) -> bool: ...

    async def is_uninstalled(self) -> bool: ...

    async def __aenter__(self) -> "Host": ...

    async def __aexit__(self, *exc: object) -> None: ...


class BaseHost(ABC):

    id: str
    name: str
    log: bool
    resources: set[str]
    products: list['Product']
    power_control: 'PowerController | None'
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
    #  Privilege
    ####################

    def _elevate(self, cmd: str) -> tuple[str, list['Expect']]:
        """Return *(wrapped_cmd, extra_expects)* to run *cmd* with elevation.

        Default raises — only posix-shell hosts (via the ``PosixPrivilege``
        mixin) can elevate. Embedded/RTOS hosts have no ``sudo``."""
        raise NotImplementedError(
            f"sudo/elevation is not supported on '{self.__class__.__name__}'"
        ) from None

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """Switch the persistent session to another user via ``su``.

        Default raises — only posix-shell hosts (via ``PosixPrivilege``) support
        ``su``."""
        raise NotImplementedError(
            f"su/switch_user is not supported on '{self.__class__.__name__}'"
        ) from None

    def as_user(self, user: str = "root", password: str | None = None):
        """Async context manager to run a block as *user*.

        Default raises — only posix-shell hosts (via ``PosixPrivilege``) support
        ``su``-based user switching."""
        raise NotImplementedError(
            f"as_user is not supported on '{self.__class__.__name__}'"
        ) from None

    def _apply_sudo(self, sc: 'ShellCommand') -> 'ShellCommand':
        """Rewrite a ``ShellCommand`` to run under sudo, merging in the
        password ``Expect`` ahead of any caller-supplied expects."""
        wrapped, extra = self._elevate(sc.cmd)
        base = _normalize_expects(sc.expects) or []
        return replace(sc, cmd=wrapped, expects=extra + base)

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
        log: bool = True,
        sudo: bool = False,
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
            sudo: If ``True``, each command is rewritten through :meth:`_elevate` before
                execution. Hosts that do not support elevation (e.g. embedded/RTOS) raise
                :exc:`NotImplementedError` — see :meth:`_elevate`.

        Returns:
            :class:`RunResult` with the aggregate :class:`Status` and a list of
            per-command :class:`CommandStatus` entries.

        See Also:
            :meth:`oneshot`: stateless, concurrent-safe alternative for one-off commands.
        """
        default_expects = _normalize_expects(expects)
        if isinstance(cmds, (str, ShellCommand)):
            resolved = [_resolve_command(cmds, default_expects, timeout, log)]
            if sudo:
                resolved = [self._apply_sudo(sc) for sc in resolved]
            single = resolved[0]
            result = await self._run_one(
                single.cmd,
                expects=_normalize_expects(single.expects),
                timeout=single.timeout,
                log=cast(bool, single.log),  # _resolve_command collapsed the None sentinel
            )
            status = result.status if not result.status.is_ok else Status.Success
            return RunResult(status=status, statuses=[result])

        resolved = [_resolve_command(c, default_expects, None, log) for c in cmds]
        if sudo:
            resolved = [self._apply_sudo(sc) for sc in resolved]

        async def _run_sc(sc: ShellCommand, t: float | None) -> CommandStatus:
            return await self._run_one(
                sc.cmd,
                expects=_normalize_expects(sc.expects),
                timeout=t,
                log=cast(bool, sc.log),  # _resolve_command collapsed the None sentinel
            )

        return await _run_cmds_with_budget(_run_sc, resolved, timeout)

    async def _run_one(self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        """Per-command runner for the persistent shell session. Subclasses override."""
        raise NotImplementedError from None

    async def oneshot(self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        raise NotImplementedError from None

    async def open_session(self,
        name: str,
    ) -> HostSession:
        raise NotImplementedError from None

    async def send(self,
        text: str,
        log: bool = True,
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

    async def __aenter__(self) -> "BaseHost":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    ####################
    #  Product lifecycle
    ####################

    async def stage(self) -> tuple[Status, str]:
        """Stage every product onto this host (transfer/place, no install).

        Iterates :attr:`products` in declaration order, returning the first
        non-ok ``(Status, str)``; an empty list is a successful no-op.
        """
        for product in self.products:
            status, msg = await product.stage(cast('Host', self))
            if not status.is_ok:
                return status, msg
        return Status.Success, ""

    async def install(self, stage_only: bool = False) -> tuple[Status, str]:
        """Stage, then install every product.

        Calls :meth:`stage` first; returns early if ``stage_only`` is set or the
        stage step failed. Otherwise installs each product in declaration order,
        short-circuiting on the first failure. Projects may override for
        cross-product ordering/dependencies.
        """
        status, msg = await self.stage()
        if stage_only or not status.is_ok:
            return status, msg
        for product in self.products:
            status, msg = await product.install(cast('Host', self))
            if not status.is_ok:
                return status, msg
        return Status.Success, ""

    async def uninstall(self) -> tuple[Status, str]:
        """Uninstall every product (best-effort).

        Attempts every product even if one fails, returning the first non-ok
        result seen (so cleanup is not abandoned halfway).
        """
        first_error: tuple[Status, str] | None = None
        for product in self.products:
            status, msg = await product.uninstall(cast('Host', self))
            if not status.is_ok and first_error is None:
                first_error = (status, msg)
        return first_error if first_error is not None else (Status.Success, "")

    async def is_installed(self) -> bool:
        """True iff there is at least one product and all report installed.

        An empty :attr:`products` list is **not installed** (avoids the
        vacuous-truth surprise of ``all([])``)."""
        if not self.products:
            return False
        for product in self.products:
            if not await product.is_installed(cast('Host', self)):
                return False
        return True

    async def is_uninstalled(self) -> bool:
        """Inverse of :meth:`is_installed`."""
        return not await self.is_installed()

    ####################
    #  Power / reboot
    ####################

    def _require_power_control(self) -> 'PowerController':
        if self.power_control is None:
            raise ValueError(
                f"Host {self.name!r} has no power_control configured. Set a "
                f"power backend (lab '[power]' table or power_control=) before "
                f"calling power()/reboot(hard=True)."
            )
        return self.power_control

    async def power(self, state: str | None = None) -> tuple[Status, str]:
        """Power this host ``'on'``/``'off'``, or toggle when *state* is None.

        Toggling reads the controller's :meth:`~otto.host.power.PowerController.status`;
        if the controller can't report state, pass an explicit ``state``.
        """
        from .power import PowerState
        controller = self._require_power_control()
        if state == "on":
            return await controller.on(cast('Host', self))
        if state == "off":
            return await controller.off(cast('Host', self))
        if state is None:
            current = await controller.status(cast('Host', self))
            if current is None:
                raise ValueError(
                    f"power(toggle) on {self.name!r} needs a controller that "
                    f"reports status; pass state='on' or 'off'."
                )
            if current is PowerState.ON:
                return await controller.off(cast('Host', self))
            return await controller.on(cast('Host', self))
        raise ValueError(f"invalid power state {state!r}; expected 'on', 'off', or None")

    async def _soft_reboot(self) -> tuple[Status, str]:
        """Issue the in-shell reboot command. Per-family override; default raises."""
        raise NotImplementedError(
            f"soft reboot is not supported on '{self.__class__.__name__}'"
        ) from None

    async def reboot(
        self, hard: bool = False, wait: bool = False, timeout: float = 600.0
    ) -> tuple[Status, str]:
        """Reboot this host.

        ``hard=False`` (default) issues the in-shell reboot command
        (:meth:`_soft_reboot`); ``hard=True`` power-cycles via the
        :class:`~otto.host.power.PowerController`. When *wait*, block on
        :meth:`wait_until_up` (up to *timeout*, default 10 minutes); if the
        host is still unreachable when *timeout* expires, the result is
        downgraded to :attr:`~otto.utils.Status.Failed`.
        """
        if hard:
            status, msg = await self._require_power_control().cycle(cast('Host', self))
        else:
            status, msg = await self._soft_reboot()
        if status.is_ok and wait and not await self.wait_until_up(timeout):
            return Status.Failed, (
                f"{self.name!r} did not become reachable within {timeout}s after reboot"
            )
        return status, msg

    async def shutdown(self) -> tuple[Status, str]:
        """Power this host off from its own shell (distinct from external
        ``power('off')``). Per-family override; default raises.
        """
        raise NotImplementedError(
            f"shutdown is not supported on '{self.__class__.__name__}'"
        ) from None

    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Whether this host answers a lightweight connection probe.

        Per-family override; default raises (no generic probe).
        """
        raise NotImplementedError(
            f"is_reachable is not supported on '{self.__class__.__name__}'"
        ) from None

    async def wait_until_up(self, timeout: float, interval: float = 2.0) -> bool:
        """Poll :meth:`is_reachable` until reachable or *timeout*. Returns success."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_reachable():
                return True
            await asyncio.sleep(interval)
        return False

    async def wait_until_down(self, timeout: float, interval: float = 2.0) -> bool:
        """Poll :meth:`is_reachable` until *not* reachable or *timeout*."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not await self.is_reachable():
                return True
            await asyncio.sleep(interval)
        return False

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
        if is_dry_run():
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

        # Also respect the context and host logging flags
        return get_logging_command_output_enabled() and host.log

# TODO: Consider a way to make commands and their output log no matter what if the log level were debug.
@dataclass
class SuppressCommandOutput():
    """Suppress command/output logging for one host or globally.

    On enter, the prior state is snapshotted; on exit it is restored.
    That makes nesting safe — an inner context cannot clobber an outer
    one — and makes concurrent per-host suppressions race-free, since
    each context only touches its own host's ``log`` attribute.

    The no-host (global) path mutates ``log_command_output`` on the active
    :class:`~otto.context.OttoContext` when one is present. When no context
    is active the call is a no-op (there is nothing to suppress). Prefer the
    per-host form when suppressing work that runs concurrently.
    """

    host: Optional[Host] = None
    """Host object to suppress. If not provided, all host output is affected."""

    def __enter__(self):
        if self.host is not None:
            self._prev_host_log = self.host.log
            self.host.log = False
        else:
            from ..context import try_get_context
            self._ctx = try_get_context()
            self._prev_global = self._ctx.log_command_output if self._ctx is not None else True
            if self._ctx is not None:
                self._ctx.log_command_output = False

    def __exit__(self, *_):
        if self.host is not None:
            self.host.log = self._prev_host_log
        else:
            if self._ctx is not None:
                self._ctx.log_command_output = self._prev_global
