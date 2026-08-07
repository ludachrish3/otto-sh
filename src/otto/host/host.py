"""Async host abstraction: the Host protocol, BaseHost ABC, and run helpers."""

import asyncio
import math
import re
import uuid
from abc import ABC
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import (
    dataclass,
    replace,
)
from logging import (
    Filter,
    LogRecord,
    getLogger,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    NoReturn,
    Protocol,
    TypeVar,
    cast,
)

from typing_extensions import Self, override

from ..logger.mode import LogMode, effective_mode
from ..result import CommandResult, Result, Results
from ..utils import (
    Arg,
    Exclude,
    Opt,
    Status,
    WaitTimeoutError,
    cli_exposed,
    wait_for_async,
)

if TYPE_CHECKING:
    from .app_shell import AppShell
    from .power import PowerController
    from .product import Product
    from .session import HostSession

    # Quoted-forward-ref only (see BaseHost.app_shell); keeping this TypeVar
    # under TYPE_CHECKING avoids a runtime import of otto.host.app_shell,
    # which would blow the import-budget guard on a bare ``import otto``.
    AppShellT = TypeVar("AppShellT", bound=AppShell)

# Runtime type alias — mirrored from session.Expect so get_type_hints can resolve
# it without a circular import (session.py imports from host.py at module level).
Expect = tuple[str | re.Pattern[str], str]

DEFAULT_COMMAND_TIMEOUT = 30.0
"""Seconds a single command may run before otto gives up on it.

Applies to :meth:`Host.run`, :meth:`Host.exec` and
:meth:`~otto.host.session.HostSession.run` when no explicit timeout is
given. Pass ``float("inf")`` for a deliberately unbounded command; there is
no other way to disable the bound.
"""

DEFAULT_REBOOT_DOWN_TIMEOUT = 60.0
"""Default bound (seconds) for ``reboot(wait=True)``'s down phase — a host
still reachable this long after the reboot command means the reboot didn't
take, and the wait fails loudly instead of watching the old OS.

Pass ``down_timeout=0`` (or negative) to skip the down phase — for hosts
whose reachability probe target survives the reboot."""

_EXEC_REAP_TIMEOUT = 5.0
"""Seconds to wait for a terminated command to report its exit status.

A process can ignore SIGTERM, so the post-terminate reap must itself be
bounded — an unbounded wait here would defeat the timeout it implements.
"""


def _validate_timeout(timeout: float) -> float:
    """Reject timeout values ``asyncio.wait_for`` would silently misinterpret.

    Annotations are not enforced at runtime, so this guards the public entry
    points against callers a type checker never sees. ``float("inf")`` is
    allowed — it is the supported spelling for an unbounded command.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(f"timeout must be a number, got {type(timeout).__name__}: {timeout!r}")
    if math.isnan(timeout):
        raise ValueError("timeout must not be NaN")
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0, got {timeout!r}")
    return float(timeout)


logger = getLogger(__name__)


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
    A scalar ``Expect`` value is accepted for ``expects``
    for ergonomics; it is normalized to a one-element list before execution.
    """

    cmd: str
    """Command string to execute."""

    expects: "Expect | list[Expect] | None" = None
    """Per-command expects. ``None`` inherits the run-level ``expects`` value."""

    timeout: float | None = None
    """Per-command timeout cap. ``None`` inherits the run-level ``timeout`` value.

    In list form, the effective timeout is always bounded by the remaining
    cumulative budget.
    """

    log: "LogMode | None" = None
    """Per-command logging disposition. ``None`` inherits the run-level ``log`` value."""


def _normalize_expects(
    expects: "Expect | list[Expect] | None",
) -> list["Expect"] | None:
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
    item: "str | ShellCommand",
    default_expects: "Expect | list[Expect] | None",
    default_timeout: float | None,
    default_log: LogMode = LogMode.NORMAL,
) -> ShellCommand:
    """Coerce ``item`` to a ``ShellCommand`` whose ``None`` fields inherit from defaults.

    A non-``None`` ``item.timeout`` is validated here — ``ShellCommand`` is a
    public, exported dataclass, so ``item.timeout`` is exactly the kind of
    caller-supplied value a type checker never sees (a downstream suite
    building ``ShellCommand(cmd, timeout=float("nan"))`` directly). Leaving it
    unvalidated would forward NaN/negative values straight to
    ``asyncio.wait_for``, bypassing the same guard every other entry point
    applies. ``None`` is passed through untouched — it means "inherit
    ``default_timeout``", which is already validated by the caller.
    """
    if isinstance(item, str):
        return ShellCommand(
            cmd=item, expects=default_expects, timeout=default_timeout, log=default_log
        )
    return ShellCommand(
        cmd=item.cmd,
        expects=item.expects if item.expects is not None else default_expects,
        timeout=(_validate_timeout(item.timeout) if item.timeout is not None else default_timeout),
        log=item.log if item.log is not None else default_log,
    )


async def _run_cmds_with_budget(
    run_one: Callable[[ShellCommand, float], Awaitable[CommandResult]],
    cmds: list[ShellCommand],
    timeout: float,
) -> Results:
    """Run a list of commands sequentially under a shared timeout budget.

    Each command receives the minimum of its own ``ShellCommand.timeout`` and
    the remaining budget; when the budget is exhausted, remaining commands are
    skipped with ``Status.Error``. Used by both ``BaseHost.run`` and
    ``HostSession.run`` so the budgeting logic lives in one place.

    *timeout* is always a real number — the public entry points validate it —
    so there is no unbounded branch here. ``float("inf")`` yields an infinite
    deadline, which every comparison below handles naturally.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    entries: list[CommandResult] = []

    for sc in cmds:
        remaining = deadline - loop.time()
        if remaining <= 0:
            entries.append(
                CommandResult(
                    status=Status.Error,
                    value="Skipped: cumulative timeout budget exhausted",
                    command=sc.cmd,
                    retcode=-1,
                    timed_out=True,
                )
            )
            continue

        effective = remaining if sc.timeout is None else min(sc.timeout, remaining)
        entries.append(await run_one(sc, effective))

    return Results.collect(entries)


class Host(Protocol):
    """Structural protocol defining the public interface every otto host must satisfy.

    Implementations of :class:`Host` connect otto to a specific target type
    (SSH, serial console, QEMU, etc.). :class:`BaseHost` provides concrete
    default implementations for the shared mechanics; individual host classes
    such as ``UnixHost`` or ``EmbeddedHost`` inherit from :class:`BaseHost` and
    implement the family-specific hooks.
    """

    log: LogMode
    """Standing per-host logging disposition. Composed with the per-command
    mode via ``effective_mode`` at the emit seam."""

    id: str
    """Unique identifier for this host."""

    name: str
    """Human-readable name for this host."""

    resources: set[str]
    """Resources required to reserve this host."""

    products: list["Product"]
    """Software-under-test deployed to this host (default empty)."""

    power_control: "PowerController | None"
    """Pluggable power backend, or None when this host can't be power-controlled."""

    async def _login(self, as_user: str | None = None) -> None: ...

    async def login(self, as_user: str | None = None) -> None:
        """Open an interactive shell bridged to the local terminal."""
        ...

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
        sudo: bool = False,
    ) -> Results:
        """Run one or more commands on the host and collect their results.

        Args:
            cmds: A single command or a sequence of commands to run in order.
                Strings and :class:`ShellCommand` objects may be mixed.
            expects: Optional ``(pattern, response)`` pair(s) for interactive
                prompts. Inherited by each command unless overridden per-command.
            timeout: Per-command timeout for a single command, or a cumulative
                budget shared across all commands in a sequence. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`. Execution is always bounded;
                pass ``float("inf")`` for a deliberately unbounded command.
            log: Whether to log command output for this call.
            sudo: If ``True``, each command is run with elevated privileges.
                Implementations that do not support elevation raise
                :exc:`NotImplementedError`.

        Returns:
            A :class:`~otto.result.Results` aggregating one :class:`~otto.result.CommandResult`
            per command.
        """
        ...

    async def exec(
        self,
        cmd: str,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run a single command outside the typical stateful ``run`` workflow.

        Concurrency safety is implementation-dependent. Host families with an
        independent exec primitive (e.g.
        :class:`~otto.host.unix_host.UnixHost`,
        :class:`~otto.host.local_host.LocalHost`) open a fresh connection or
        subprocess per call, so ``exec`` is safe to use concurrently from
        multiple coroutines. Families exposing only a single console (e.g.
        :class:`~otto.host.embedded_host.EmbeddedHost`) share the persistent
        session and are **not** concurrency-safe — see the concrete class.

        Returns:
            A :class:`~otto.result.CommandResult`; ``value`` holds the output.
        """
        ...

    async def open_session(
        self,
        name: str,
    ) -> "HostSession":
        """Open a named auxiliary session on this host.

        Named sessions are independent of the host's default persistent
        session and of each other, allowing concurrent shell interactions.
        The caller is responsible for closing the returned
        :class:`~otto.host.session.HostSession` when done.
        """
        ...

    async def send(
        self,
        text: str,
        log: LogMode = LogMode.NORMAL,
    ) -> None:
        """Send raw text to the host's persistent session without waiting for a response.

        Useful for driving interactive prompts or menu-driven interfaces where
        a full :meth:`run` round-trip is not appropriate.
        """
        ...

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for *pattern* to appear in the host's session output.

        Args:
            pattern: A literal string or compiled regex to match against output.
            timeout: Maximum seconds to wait before raising a timeout error.

        Returns:
            The matched text.
        """
        ...

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> Result:
        """Download one or more files from the host to a local directory.

        Returns a :class:`~otto.result.Result` whose ``value`` is a
        ``dict[Path, Result]`` mapping each source path — keyed exactly as
        passed, with no resolution — to its per-file outcome: ``value=dest_path``
        on success, a per-file ``msg`` on failure, or
        :attr:`~otto.utils.Status.Skipped` (``"not attempted (earlier failure)"``)
        for a file a sequential backend never reached. The aggregate status is
        the first non-ok entry's status (Skipped counts as ok, so a trailing run
        of Skipped never fails the aggregate on its own).
        """
        ...

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        mode: int | str | None = None,
    ) -> Result:
        """Upload one or more local files to a directory on the host.

        ``mode`` sets the permission bits on the uploaded files: an ``int``
        (``0o755``) from Python, or a string always read as octal (``"755"``,
        ``"0755"``, ``"0o755"``). ``None`` leaves the backend's default
        permissions. Hosts whose transfer backend has no permission model
        (embedded ``console``/``tftp``) reject a non-``None`` mode before
        transferring anything.

        Returns a :class:`~otto.result.Result` whose ``value`` is a
        ``dict[Path, Result]`` mapping each source path — keyed exactly as
        passed, with no resolution — to its per-file outcome: ``value=dest_path``
        on success, a per-file ``msg`` on failure, or
        :attr:`~otto.utils.Status.Skipped` (``"not attempted (earlier failure)"``)
        for a file a sequential backend never reached. The aggregate status is
        the first non-ok entry's status (Skipped counts as ok, so a trailing run
        of Skipped never fails the aggregate on its own). A file that
        transferred but whose ``mode`` could not be applied is an error entry
        that still carries its ``dest_path``.
        """
        ...

    async def power(self, state: str | None = None) -> Result:
        """Power this host on, off, or toggle (when *state* is ``None``).

        Returns a :class:`~otto.result.Result`; on success ``value`` is the
        commanded :class:`~otto.host.power.PowerState` (or ``None`` when
        unknown), and ``msg`` carries controller diagnostics.
        """
        ...

    async def reboot(
        self,
        hard: bool = False,
        wait: bool = False,
        timeout: float = 600.0,
        down_timeout: float = DEFAULT_REBOOT_DOWN_TIMEOUT,
        poll_interval: float = 2.0,
    ) -> Result:
        """Reboot this host.

        ``hard=False`` issues an in-shell reboot; ``hard=True`` power-cycles
        via the :class:`~otto.host.power.PowerController`. When *wait* is
        ``True``, blocks through a two-phase watch: first the host must go
        DOWN (bounded by the lesser of *down_timeout* and *timeout* — pass
        ``down_timeout<=0`` to skip this phase for hosts whose reachability
        probe target survives the reboot), then come back UP and answer a
        recovery probe (within the remainder of *timeout*), polling every
        *poll_interval* seconds. Returns a :class:`~otto.result.Result`.
        """
        ...

    async def shutdown(self) -> Result:
        """Power this host off from its own shell.

        Distinct from :meth:`power` ``('off')``, which uses an external power
        controller. Returns a :class:`~otto.result.Result`.
        """
        ...

    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Return ``True`` if the host responds to a connection probe within *timeout* seconds."""
        ...

    async def wait_until_up(self, timeout: float, interval: float = 2.0) -> bool:
        """Poll until the host is reachable or *timeout* seconds elapse.

        Returns ``True`` if reachable before the deadline, ``False`` otherwise.
        """
        ...

    async def wait_until_down(self, timeout: float, interval: float = 2.0) -> bool:
        """Poll until the host is unreachable or *timeout* seconds elapse.

        Returns ``True`` if unreachable before the deadline, ``False`` otherwise.
        """
        ...

    async def close(self) -> None:
        """Close the host's persistent session and release any held resources."""
        ...

    async def stage(self) -> Result:
        """Stage every product onto this host (transfer/place, no install).

        Returns a :class:`~otto.result.Result`.
        """
        ...

    async def install(self, stage_only: bool = False) -> Result:
        """Stage and then install every product on this host.

        When *stage_only* is ``True``, stops after staging without installing.
        Returns a :class:`~otto.result.Result`, short-circuiting on the first
        failure.
        """
        ...

    async def uninstall(self) -> Result:
        """Uninstall every product from this host (best-effort).

        Returns a :class:`~otto.result.Result` — the first non-ok outcome, after
        attempting every product.
        """
        ...

    async def is_installed(self) -> bool:
        """Return ``True`` iff at least one product is declared and all are installed."""
        ...

    async def is_uninstalled(self) -> bool:
        """Return ``True`` iff :meth:`is_installed` returns ``False``."""
        ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: object) -> None: ...


class BaseHost(ABC):
    """Abstract base class providing shared mechanics for all host implementations.

    :class:`BaseHost` implements the cross-cutting concerns that every host
    family needs — command budgeting, dry-run stubs, product lifecycle,
    and power/reboot orchestration. Concrete
    host classes (``UnixHost``,
    ``EmbeddedHost``, etc.) inherit from :class:`BaseHost`, implement the
    family-specific hooks (``_run_one``, ``exec``, ``_soft_reboot``, …),
    and satisfy the :class:`Host` protocol.
    """

    id: str
    name: str
    log: LogMode
    resources: set[str]
    products: list["Product"]
    power_control: "PowerController | None"

    @override
    def __str__(self) -> str:
        """Return the human-readable display name.

        So ``print(host)`` / ``f"{host}"`` / ``log.info("... %s", host)`` render
        the friendly name (e.g. ``Lab X Server``), not the correlation id or the
        dataclass repr. ``repr`` is left to the dataclass (id + fields) for
        debugging; identity/correlation still uses ``host.id``.
        """
        return self.name

    ####################
    #  Dry-run helpers
    ####################

    def _dry_run_result(self, cmd: str) -> CommandResult:
        """Return a synthetic CommandResult for dry-run mode."""
        self._log_command(f"[DRY RUN] {cmd}")
        return CommandResult(
            status=Status.Skipped, value="[DRY RUN] Command not executed", command=cmd, retcode=0
        )

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
        ``--mode 789`` is the caller's own input and costs nothing to catch, so
        a dry run should catch it. Backend capability is **not** checked — that
        belongs to the real transfer, which is where the backend is actually
        selected and used.
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

    ####################
    #  Privilege
    ####################

    def _elevate(self, cmd: str) -> tuple[str, list["Expect"]]:
        """Return *(wrapped_cmd, extra_expects)* to run *cmd* with elevation.

        Default raises — only posix-shell hosts (via the ``PosixPrivilege``
        mixin) can elevate. Embedded/RTOS hosts have no ``sudo``.
        """
        raise NotImplementedError(
            f"sudo/elevation is not supported on '{self.__class__.__name__}'"
        ) from None

    async def switch_user(self, user: str = "", password: str | None = None) -> None:
        """Switch the persistent session to another user via ``su``.

        Default raises — only posix-shell hosts (via ``PosixPrivilege``) support
        ``su``.
        """
        raise NotImplementedError(
            f"su/switch_user is not supported on '{self.__class__.__name__}'"
        ) from None

    def as_user(self, user: str = "root", password: str | None = None) -> NoReturn:
        """Async context manager to run a block as *user*.

        Default raises — only posix-shell hosts (via ``PosixPrivilege``) support
        ``su``-based user switching.
        """
        raise NotImplementedError(
            f"as_user is not supported on '{self.__class__.__name__}'"
        ) from None

    @property
    def current_user(self) -> str:
        """User this host's default shell session is currently running as.

        Seeded from the login user; changes only through :meth:`switch_user` /
        :meth:`as_user`. See :attr:`~otto.host.session.HostSession.current_user`
        for named sessions.
        """
        return self._session_mgr.current_user  # ty: ignore[unresolved-attribute]

    def _apply_sudo(self, sc: "ShellCommand") -> "ShellCommand":
        """Rewrite a ``ShellCommand`` to run under sudo.

        Merges in the password ``Expect`` ahead of any caller-supplied expects.
        """
        wrapped, extra = self._elevate(sc.cmd)
        base = _normalize_expects(sc.expects) or []
        return replace(sc, cmd=wrapped, expects=extra + base)

    ####################
    #  Command execution
    ####################

    async def _login(self, as_user: str | None = None) -> None:
        raise NotImplementedError(
            f"The '{self.__class__.__name__}' class does not support interactive sessions"
        ) from None

    @cli_exposed
    async def login(self, as_user: str | None = None) -> None:
        """Open an interactive shell bridged to the local terminal.

        Subclasses implement ``_login`` to do the actual protocol
        work. This wrapper exists so CLI and SDK callers have a single
        public entry point.

        stdin and stdout are bridged directly to the remote terminal and the
        session is recorded to the otto log. Press ``Ctrl+]`` to disconnect
        locally without ending the remote session; type ``exit`` or ``logout``
        to end the session normally.

        Args:
            as_user: Land the session on this login instead of the
                connection's configured default, replaying any login-proxy
                hops needed to reach it (see :mod:`otto.host.login_proxy`).
                Hosts that cannot proxy raise :exc:`NotImplementedError`.
        """
        await self._login(as_user)

    @cli_exposed
    async def run(
        self,
        cmds: Annotated[
            str | ShellCommand | Sequence[str | ShellCommand],
            Arg(variadic=True, elem_type=str, help="Command(s) to run."),
        ],
        expects: Annotated[Expect | list[Expect] | None, Exclude] = None,
        timeout: Annotated[
            float,
            Opt(help="Per-command/cumulative timeout (seconds); use inf for unbounded.", min=0.0),
        ] = DEFAULT_COMMAND_TIMEOUT,
        log: Annotated[LogMode, Exclude] = LogMode.NORMAL,
        sudo: bool = False,
    ) -> Results:
        """Execute one or more commands on the host via the persistent shell session.

        The session is stateful: working directory changes (``cd``), exported environment
        variables, and other shell state persist between calls, just as they would in
        an interactive terminal.

        Args:
            cmds: A single command (``str`` or :class:`ShellCommand`) or a sequence of
                commands. Strings and :class:`ShellCommand` objects may be mixed. For
                single-command calls, read the result via ``result.only``.
            expects: Default ``(pattern, response)`` pair(s) for interactive prompts.
                Accepts a single ``Expect`` tuple or a list of them. Each command
                inherits this value unless its own :attr:`ShellCommand.expects` is set.
            timeout: For a single command, the per-command timeout. For a sequence, a
                cumulative timeout shared across all commands — each command receives
                the remaining budget; when exhausted, remaining commands are skipped
                with ``Status.Error``. :attr:`ShellCommand.timeout` caps the per-command
                value but is still bounded by the remaining budget.
                Defaults to :data:`DEFAULT_COMMAND_TIMEOUT`; pass
                ``float("inf")`` to opt out of the bound.
            sudo: If ``True``, each command is rewritten through ``_elevate`` before
                execution. Hosts that do not support elevation (e.g. embedded/RTOS) raise
                :exc:`NotImplementedError` — see ``_elevate``.

        Returns:
            A :class:`~otto.result.Results` aggregating one :class:`~otto.result.CommandResult`
            per command.

        See Also:
            :meth:`exec`: stateless, concurrent-safe alternative for one-off commands.
        """
        timeout = _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)
        if isinstance(cmds, (str, ShellCommand)):
            resolved = [_resolve_command(cmds, default_expects, timeout, log)]
            if sudo:
                resolved = [self._apply_sudo(sc) for sc in resolved]
            single = resolved[0]
            result = await self._run_one(
                single.cmd,
                expects=_normalize_expects(single.expects),
                # _resolve_command collapsed the None sentinel into a concrete float.
                timeout=single.timeout if single.timeout is not None else timeout,
                # _resolve_command collapsed the None sentinel into a concrete LogMode.
                log=single.log if single.log is not None else LogMode.NORMAL,
            )
            return Results.collect([result])

        resolved = [_resolve_command(c, default_expects, None, log) for c in cmds]
        if sudo:
            resolved = [self._apply_sudo(sc) for sc in resolved]

        async def _run_sc(sc: ShellCommand, t: float) -> CommandResult:
            return await self._run_one(
                sc.cmd,
                expects=_normalize_expects(sc.expects),
                timeout=t,
                # _resolve_command collapsed the None sentinel into a concrete LogMode.
                log=sc.log if sc.log is not None else LogMode.NORMAL,
            )

        return await _run_cmds_with_budget(_run_sc, resolved, timeout)

    def _effective_log(self, log: LogMode) -> LogMode:
        """Most-restrictive of this host's standing mode and the per-command mode."""
        return effective_mode(self.log, log)

    async def _run_one(
        self,
        cmd: str,
        timeout: float,
        expects: list[Expect] | None = None,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Per-command runner for the persistent shell session. Subclasses override."""
        raise NotImplementedError from None

    async def exec(
        self,
        cmd: str,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run a single command outside the persistent shell session.

        Validates *timeout* and delegates to ``_exec_one``, which each host
        family implements. Do not override this method — override
        ``_exec_one``, so the validation cannot be bypassed.

        Args:
            cmd: Shell command to run.
            timeout: Seconds before the command is abandoned. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`; pass ``float("inf")`` for a
                deliberately unbounded command.
            log: Logging disposition for this call.
        """
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._exec_one(cmd, timeout=timeout, log=log)

    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Family-specific stateless command runner. Subclasses override."""
        raise NotImplementedError from None

    async def open_session(
        self,
        name: str,
    ) -> "HostSession":
        """Open a named auxiliary session on this host. Subclasses must override."""
        raise NotImplementedError from None

    @asynccontextmanager
    async def app_shell(
        self,
        shell_cls: "type[AppShellT]",
        *,
        user: str | None = None,
        timeout: float | None = None,
    ) -> "AsyncIterator[AppShellT]":
        """Run *shell_cls* on a dedicated session; see the sessions recipe.

        ``timeout``, if given, becomes this session's default prompt-wait
        (governs the launch wait and every :meth:`~otto.host.app_shell.AppShell.cmd`
        call that doesn't pass its own ``timeout=``); falls back to the shell
        class's :attr:`~otto.host.app_shell.AppShell.cmd_timeout` when omitted.
        """
        # Function-level, not module scope: connections → login_proxy → host
        # is a runtime import cycle back to this module. Hoisted above the
        # try (with the host label) so neither can raise inside the finally,
        # where they would mask the body's exception.
        from .connections import teardown_step

        host_name = self.name
        name = f"__appshell_{shell_cls.__name__.lower()}_{uuid.uuid4().hex[:6]}__"
        session = await self.open_session(name)
        try:
            target = user if user is not None else shell_cls.user
            if target is not None:
                await session.switch_user(target)
            async with shell_cls.attach(session, timeout=timeout) as shell:
                yield shell
        finally:
            with teardown_step(host_name, "app-shell session close"):
                await session.close()

    async def send(
        self,
        text: str,
        log: LogMode = LogMode.NORMAL,
    ) -> None:
        """Send raw text to the host's persistent session. Subclasses must override."""
        raise NotImplementedError from None

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        """Wait for *pattern* in the session output.

        Validates *timeout* and delegates to ``_expect_one``. Do not
        override this method — override ``_expect_one``, so the validation
        and the advertised default cannot drift per host family.

        Args:
            pattern: A literal string or compiled regex to match against output.
            timeout: Maximum seconds to wait. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`; pass ``float("inf")`` to wait
                indefinitely.
        """
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            self._log_command(
                "[DRY RUN] expect() skipped — pattern would never match without a live session"
            )
            return ""
        return await self._expect_one(pattern, timeout)

    async def _expect_one(
        self,
        pattern: str | re.Pattern[str],
        timeout: float,
    ) -> str:
        """Family-specific pattern wait. Subclasses override."""
        raise NotImplementedError from None

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
    ) -> Result:
        """Download files from the host to a local directory. Subclasses must override."""
        raise NotImplementedError from None

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        mode: int | str | None = None,
    ) -> Result:
        """Upload local files to a directory on the host. Subclasses must override."""
        raise NotImplementedError from None

    async def close(self) -> None:
        """Close the persistent session and release held resources. Subclasses must override."""
        raise NotImplementedError from None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    ####################
    #  Product lifecycle
    ####################

    @cli_exposed
    async def stage(self) -> Result:
        """Stage every product onto this host (transfer/place, no install).

        Iterates :attr:`products` in declaration order, returning the first
        non-ok :class:`~otto.result.Result`; an empty list is a successful no-op.
        """
        for product in self.products:
            result = await product.stage(cast("Host", self))
            if not result.is_ok:
                # Returned whole: a product's CommandResult carries the retcode
                # and output that the CLI turns into an exit code.
                return result
        return Result(Status.Success)

    @cli_exposed
    async def install(self, stage_only: bool = False) -> Result:
        """Stage, then install every product.

        Calls :meth:`stage` first; returns early if ``stage_only`` is set or the
        stage step failed. Otherwise installs each product in declaration order,
        short-circuiting on the first failure. Projects may override for
        cross-product ordering/dependencies.
        """
        stage_result = await self.stage()
        if stage_only or not stage_result.is_ok:
            return stage_result
        for product in self.products:
            result = await product.install(cast("Host", self))
            if not result.is_ok:
                return result
        return Result(Status.Success)

    @cli_exposed
    async def uninstall(self) -> Result:
        """Uninstall every product (best-effort).

        Attempts every product even if one fails, returning the first non-ok
        result seen (so cleanup is not abandoned halfway).
        """
        first_failure: Result | None = None
        for product in self.products:
            result = await product.uninstall(cast("Host", self))
            if not result.is_ok and first_failure is None:
                first_failure = result
        return first_failure if first_failure is not None else Result(Status.Success)

    @cli_exposed(output_dir=False)
    async def is_installed(self) -> bool:
        """Return True iff there is at least one product and all are installed.

        An empty :attr:`products` list is **not installed** (avoids the
        vacuous-truth surprise of ``all([])``).
        """
        if not self.products:
            return False
        for product in self.products:
            if not await product.is_installed(cast("Host", self)):
                return False
        return True

    @cli_exposed(output_dir=False)
    async def is_uninstalled(self) -> bool:
        """Inverse of :meth:`is_installed`."""
        return not await self.is_installed()

    ####################
    #  Power / reboot
    ####################

    def _require_power_control(self) -> "PowerController":
        if self.power_control is None:
            raise ValueError(
                f"Host {self.name!r} has no power_control configured. Set a "
                f"power backend (lab '[power]' table or power_control=) before "
                f"calling power()/reboot(hard=True)."
            )
        return self.power_control

    @cli_exposed
    async def power(self, state: "Annotated[str | None, Arg()]" = None) -> Result:
        """Power this host ``'on'``/``'off'``, or toggle when *state* is None.

        Toggling reads the controller's :meth:`~otto.host.power.PowerController.status`;
        if the controller can't report state, pass an explicit ``state``. On
        success ``value`` is the commanded :class:`~otto.host.power.PowerState`.
        """
        from .power import PowerState

        def _with_state(result: Result, commanded: PowerState) -> Result:
            value = commanded if result.is_ok else None
            return Result(result.status, value=value, msg=result.msg)

        controller = self._require_power_control()
        if state == "on":
            return _with_state(await controller.on(cast("Host", self)), PowerState.ON)
        if state == "off":
            return _with_state(await controller.off(cast("Host", self)), PowerState.OFF)
        if state is None:
            current = await controller.status(cast("Host", self))
            if current is None:
                raise ValueError(
                    f"power(toggle) on {self.name!r} needs a controller that "
                    f"reports status; pass state='on' or 'off'."
                )
            if current is PowerState.ON:
                return _with_state(await controller.off(cast("Host", self)), PowerState.OFF)
            return _with_state(await controller.on(cast("Host", self)), PowerState.ON)
        raise ValueError(f"invalid power state {state!r}; expected 'on', 'off', or None")

    async def _soft_reboot(self) -> Result:
        """Issue the in-shell reboot command. Per-family override; default raises."""
        raise NotImplementedError(
            f"soft reboot is not supported on '{self.__class__.__name__}'"
        ) from None

    @cli_exposed
    async def reboot(
        self,
        hard: bool = False,
        wait: bool = False,
        timeout: float = 600.0,
        down_timeout: float = DEFAULT_REBOOT_DOWN_TIMEOUT,
        poll_interval: float = 2.0,
    ) -> Result:
        """Reboot this host.

        ``hard=False`` (default) issues the in-shell reboot command
        (``_soft_reboot``); ``hard=True`` power-cycles via the
        :class:`~otto.host.power.PowerController`. When *wait*, block through
        a two-phase watch: first the host must go DOWN (bounded by the minimum
        of *down_timeout* and *timeout* — a host that never goes down means the
        reboot didn't take), then come back UP (within the remainder of
        *timeout*), probing every *poll_interval* seconds. Pass
        ``down_timeout<=0`` to skip the down phase entirely and proceed
        straight to the up wait — the documented opt-out for hosts whose
        reachability probe target survives the reboot (e.g. ``LocalHost``, or
        an embedded target reached through a console server that stays up
        across the reboot). Either phase expiring downgrades the result to
        :attr:`~otto.utils.Status.Failed` with a message naming the phase.
        """
        if hard:
            result = await self._require_power_control().cycle(cast("Host", self))
        else:
            result = await self._soft_reboot()
        # The just-issued reboot kills every cached transport, but the caches
        # don't know it: ConnectionManager.ssh() returns a cached connection
        # object without an aliveness check, so any reachability probe below
        # would read the dead cache and vacuously succeed. Drop the stale
        # per-connection state now — probes must dial fresh. Gated on
        # result.is_ok: rebuilding after a reboot that was never actually
        # issued (e.g. a hard reboot whose power-off leg failed) would orphan
        # live transports instead of dead ones. Residual: a soft reboot that
        # silently did not take (e.g. sudo denied inside the issued command)
        # still drops live transports here — unavoidable at issue time,
        # because the disconnect race makes the command's own result
        # untrustworthy.
        rebuild = getattr(self, "rebuild_connections", None)
        if result.is_ok and rebuild is not None:
            rebuild()
        if result.is_ok and wait:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            down_bound = min(down_timeout, timeout)
            if down_timeout > 0 and not await self.wait_until_down(
                down_bound, interval=poll_interval
            ):
                return Result(
                    Status.Failed,
                    msg=(
                        f"{self.name!r} never went down within {down_bound}s of the "
                        f"reboot — the reboot likely did not take"
                    ),
                )
            remaining = max(0.0, deadline - loop.time())
            if not await self.wait_until_up(remaining, interval=poll_interval):
                return Result(
                    Status.Failed,
                    msg=f"{self.name!r} did not become reachable within {timeout}s after reboot",
                )
            if not await self._confirm_recovered(deadline, poll_interval):
                return Result(
                    Status.Failed,
                    msg=(
                        f"{self.name!r} accepts connections but its shell never answered "
                        f"within {timeout}s of the reboot — likely still booting"
                    ),
                )
        return result

    @cli_exposed
    async def shutdown(self) -> Result:
        """Power this host off from its own shell (distinct from external ``power('off')``).

        Per-family override; default raises.
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
        # probe_first (the wait_for default, kept deliberately): an exhausted
        # budget still probes once, so reboot()'s up-wait cannot fail a host
        # that IS up just because the down phase consumed the whole budget —
        # at the cost of one probe bound past the deadline in the worst case.
        try:
            await wait_for_async(
                self.is_reachable,
                timeout,
                interval=interval,
                on_timeout=f"{self.name!r} not reachable within {timeout}s",
            )
        except WaitTimeoutError:
            return False
        return True

    async def wait_until_down(self, timeout: float, interval: float = 2.0) -> bool:
        """Poll :meth:`is_reachable` until *not* reachable or *timeout*."""

        async def unreachable() -> bool:
            return not await self.is_reachable()

        # probe_first kept for the same edge-of-budget reason as wait_until_up.
        try:
            await wait_for_async(
                unreachable,
                timeout,
                interval=interval,
                on_timeout=f"{self.name!r} still reachable after {timeout}s",
            )
        except WaitTimeoutError:
            return False
        return True

    async def _confirm_recovered(
        self,
        _deadline: float,
        _poll_interval: float,
        /,
    ) -> bool:
        """Post-reboot recovery gate: is the host USABLE, not merely reachable.

        The default accepts reachability as recovery — the right call for
        families with no stronger probe (an RTOS shell has no ``true``).
        :class:`~otto.host.unix_host.UnixHost` overrides this with a real
        command round-trip: early-boot sshd can accept a TCP connection and
        then stall, so "accepts a connection" must never be the recovery
        criterion where a shell probe exists. *deadline* is an asyncio
        loop-clock instant (``loop.time()`` scale), not a duration.
        """
        return True

    ####################
    #  Logging
    ####################

    # TODO: Dynamically size the preamble to be max(len(h.name) for h in all_hosts()) + 2 (1 space on each side)  # noqa: E501 — TODO comment
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

        # A python 3.10 limitation does not allow escape characters within f-string closures.
        # Assign a variable to be a newline so it can be used within an f-string closure.
        newline = "\n"
        logger.info(
            f"{newline.join(output_lines)}",
            extra={"host": self, "log_mode": mode},
        )


class HostFilter(Filter):
    """Console-side suppress filter: drops QUIET/NEVER records and honors the global flag.

    Attached to the console + ``console.log`` handlers only —
    ``verbose.log`` keeps the records (see ``management``).

    The per-host standing mode is now folded into each record's ``log_mode`` via
    ``BaseHost._effective_log`` at the emit seam, so the filter decides purely on
    ``record.log_mode`` plus the global command-output flag.
    """

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


# TODO: Consider a way to make commands and their output log no matter what if the log level were debug.  # noqa: E501 — TODO comment
@dataclass
class SuppressCommandOutput:
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

    host: "Host | None" = None
    """Host object to suppress. If not provided, all host output is affected."""

    def __enter__(self) -> None:
        if self.host is not None:
            self._prev_host_log = self.host.log
            self.host.log = LogMode.QUIET
        else:
            from ..context import try_get_context

            self._ctx = try_get_context()
            self._prev_global = self._ctx.log_command_output if self._ctx is not None else True
            if self._ctx is not None:
                self._ctx.log_command_output = False

    def __exit__(self, *_: object) -> None:
        if self.host is not None:
            self.host.log = self._prev_host_log
        elif self._ctx is not None:
            self._ctx.log_command_output = self._prev_global
