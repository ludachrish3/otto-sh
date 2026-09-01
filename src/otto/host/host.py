"""Async host abstraction: the Host protocol, BaseHost ABC, and run helpers."""

import asyncio
import math
import re
import shlex
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

from typing_extensions import Never, Self, override

from ..logger.mode import LogMode, effective_mode
from ..result import CommandNotRunError, CommandResult, NotRunResult, Result, Results
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
    from .dev_tool import DevTool
    from .inventory_ref import InventoryRef
    from .lab_info import LabInfo
    from .power import PowerController
    from .product import Product
    from .session import HostSession
    from .toolchain import Toolchain

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


def _validate_user(user: str) -> None:
    """Refuse a user value docker or chown could misparse; forms are otherwise verbatim."""
    if not isinstance(user, str):
        raise TypeError(
            f"user must be a string (e.g. 'root', '1000', '1000:1000'); got {type(user).__name__}"
        )
    if not user or any(c.isspace() for c in user):
        raise ValueError(f"user must be a non-empty string with no whitespace; got {user!r}")


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


def refuse_declined_fact(result: "Result", *, asked: str) -> None:
    """Raise instead of inventing a device fact a dry run declined to measure.

    For a verb whose RETURN TYPE cannot carry a status -- ``exists() -> bool``,
    ``ls() -> list[str]`` -- there is nowhere to put "I did not look". The
    contract allows exactly three behaviours when logic branches on a device
    fact: fabricate it, decline loudly, or never run the logic. The caller's
    ``if`` is the logic here and otto does not own it, so only the middle one
    is available -- and returning the falsy default is the FIRST one wearing a
    plausible face. ``ls`` answered ``[]`` (an empty directory, indistinguishable
    from a real one) and ``exists`` answered ``False`` (the file is not there):
    both silent, both immediately actionable, both fiction. The
    :class:`~otto.result.NotRunResult` primitive does not reach these callers
    at all, which is why the ``Status.Skipped``-keyed sweep walked past them.

    A no-op for every other status, a genuine device failure included: a real
    ``ls`` of a path that does not exist IS a measurement, and what those verbs
    return for it is not this function's business.

    Args:
        result: the command result the verb just obtained.
        asked: the question in the CALLER's vocabulary (``"exists('/etc/hosts')"``),
            so the error names the API call rather than only the shell line otto
            would have sent.

    Raises:
        ~otto.result.CommandNotRunError: *result* is a dry run's decline.
    """
    if result.status is not Status.NotRun:
        return
    raise CommandNotRunError(asked, str(getattr(result, "host_name", "")))


def refuse_declined_match(
    pattern: "str | re.Pattern[str]",
    host: str,
    log_command: "Callable[[str, LogMode], None]",
) -> Never:
    """Announce the wait a dry run declines, then refuse to invent its match.

    ``expect()`` returns a ``str`` -- the matched text -- and a ``str`` cannot
    carry "I did not look", so this is ``refuse_declined_fact``'s problem in a
    second shape and it gets the same answer. What it replaced was the empty
    string, which is the fabrication wearing the most plausible face available:
    the caller's ``if "READY" in await host.expect(...)`` reads it as *the
    pattern did not match*, and drives the failure path of a device that was
    never asked. A ``send`` + ``expect`` pair is the drive-an-interactive-prompt
    idiom, so that fiction steered menu walks and login dialogues.

    Unlike ``refuse_declined_fact`` this ALWAYS raises: there is no result to
    adjudicate, and the only callers are the two ``is_dry_run()`` arms
    (:meth:`BaseHost.expect` and
    :meth:`~otto.host.session.HostSession.expect`), which is also why it takes
    the announcement sink rather than a host object -- the two live on
    different classes and must not grow two wordings of the same line.

    SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: the banner is emitted before
    the raise, so the dry run still says what it declined to wait for, and
    every announcement made before this point has already reached the sinks.

    **This is usually where a session preview ENDS, and that is by design.** A
    session script is ``send`` -> ``expect`` -> ``send`` -> ``expect``, where
    step N+1 is chosen from step N's output: the dry run announces the part
    configuration alone can produce and then reaches a wait it cannot answer
    without the device. That is the same three-part shape the link and tunnel
    previews ship (the plan, the pure refusals, the honest gap), so the raised
    message says so -- a caller who hits it should understand the preview ran
    out of things it can know, not that something broke.

    Args:
        pattern: the pattern the caller asked to wait for, named in the banner
            and in the error exactly as passed.
        host: the host (or host id) the wait would have run against.
        log_command: the caller's own command-log sink, called at
            :attr:`~otto.logger.mode.LogMode.NORMAL` -- an announcement, never
            a payload.

    Raises:
        ~otto.result.CommandNotRunError: always.
    """
    log_command(
        f"[DRY RUN] expect({pattern!r}) — nothing was read; a dry run issues no "
        f"command for a pattern to match",
        LogMode.NORMAL,
    )
    raise CommandNotRunError(
        f"expect({pattern!r})",
        host,
        detail=(
            "A session preview is a PREFIX: everything announced above is what "
            "configuration alone can produce, and this wait is the point where the "
            "script needs the device's answer to choose what to send next. The "
            "preview ended here because it ran out of things it can know — nothing "
            "is wrong with the script."
        ),
    )


def refuse_declined_elevation(
    verb: str,
    user: str,
    host: str,
    log_command: "Callable[[str, LogMode], None]",
) -> Never:
    """Announce the elevation a dry run declines, and refuse to claim it happened.

    Elevation returns ``None``, so there is no status to harden -- but unlike
    ``send`` it is NOT safe to announce and return, because every caller
    finishes by STAMPING the tracked user (``HostSession.switch_user``,
    ``PosixPrivilege.switch_user`` via ``SessionManager._set_current_user``,
    and both ``as_user`` forms). A session reporting a user it never became
    steers ``as_user``'s undo chain and every "am I root here?" check.

    It also raises rather than letting the failure emerge on its own. Without
    an arm the declines DO stop the device work -- every ``send`` and
    ``expect`` underneath is guarded -- but the first ``expect`` raises inside
    :func:`~otto.host.login_proxy.run_proxy`, whose blanket ``except
    Exception`` re-reports it as
    :class:`~otto.host.login_proxy.LoginProxyError`: "login proxy failed
    becoming 'root'". That is a diagnosis of a device that was never
    contacted -- the same wrong-story defect a fabricated payload is, pointed
    at the operator instead of the parser.

    Shared by the HOST-level elevation
    (:class:`~otto.host.privilege.PosixPrivilege`, which drives the default
    session) and the SESSION-level one
    (:meth:`~otto.host.session.HostSession.switch_user`), because they are the
    same refusal on two objects and must not grow two wordings of it.

    Args:
        verb: the method the caller invoked, named in the caller's words.
        user: the elevation target, already defaulted for display.
        host: the host (or host id) the elevation would have run on.
        log_command: the caller's own command-log sink, called at
            :attr:`~otto.logger.mode.LogMode.NORMAL` -- an announcement, never
            a payload.

    Raises:
        ~otto.result.CommandNotRunError: always.
    """
    asked = f"{verb}({user!r})"
    log_command(
        f"[DRY RUN] {asked} — no elevation attempted; the session's user is unchanged",
        LogMode.NORMAL,
    )
    raise CommandNotRunError(asked, host)


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


def _log_tree_state(directory: "Path") -> "set[tuple[str, int, int, int]]":
    """Identity of every file under *directory*: relative path, size, mtime, ctime.

    Taken before and after a haul, the DIFFERENCE is what that haul retrieved
    — which is the only honest reading of ``require_product_logs``. A ``dest``
    is routinely reused (last run's output dir, ``--dest ./logs``), so "the
    product directory has files in it" answers a question nobody asked: it is
    satisfied by an earlier haul's files while this one retrieved nothing.

    Stamps are carried, not just the path, so a log re-fetched under the name
    it already had counts — that is the ordinary second run.

    **ctime is the load-bearing component**, because mtime can be forged and
    is: ``ScpOptions.preserve`` (:mod:`otto.host.options`) forwards straight to
    ``asyncssh.scp``'s ``preserve``, so a re-fetch of an unchanged log lands
    with byte-identical size AND mtime. On such a lab every ordinary run would
    be failed for retrieving exactly what it was asked for. No userspace API
    can set ctime, so any re-write advances it.

    Size and mtime stay in the tuple even though ctime dominates them on
    POSIX: another component can only ever make MORE things count as
    retrieved, never fewer, so carrying them cannot manufacture a false pass —
    at worst they are redundant on a filesystem that maintains ctime properly.
    """
    if not directory.exists():
        return set()
    state: set[tuple[str, int, int, int]] = set()
    for path in directory.rglob("*"):
        if path.is_file():
            stat = path.stat()
            state.add(
                (
                    str(path.relative_to(directory)),
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
    return state


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

    lab_info: "LabInfo"
    """The resolved lab this host came from (name, declared resources, metadata)."""

    resources: frozenset[str]
    """This host's own reservation identifiers — a slot (spec 2026-08-28
    three-level-reservations §3); empty for containers and ``local``. The lab's
    are on :attr:`lab_info`."""

    element_resources: frozenset[str]
    """The reservation identifiers of the ELEMENT this host belongs to (spec
    2026-08-28 three-level-reservations §3) — stamped by the loader, like
    ``element_metadata``; empty for containers and ``local``.

    Carried per member host because there is no element registry at runtime:
    an element is in play exactly when one of its hosts is, so the union over
    elements in play falls out of the union over hosts in play."""

    inventory_ref: "InventoryRef"
    """Inventory provenance (see :class:`~otto.host.inventory_ref.InventoryRef`); empty for an
    inline host."""

    products: list["Product"]
    """Software-under-test deployed to this host (default empty)."""

    dev_tools: list["DevTool"]
    """Repo-internal tooling deployed to this host (default empty)."""

    toolchain: "Toolchain"
    """Toolchain this host's products are built with, and the tools it installs."""

    debug_log_globs: list[str]
    """Remote paths/glob patterns ``get_debug_logs`` fetches (default empty)."""

    power_control: "PowerController | None"
    """Pluggable power backend, or None when this host can't be power-controlled."""

    source_lab: str
    """Lab this host came from, stamped by the loader (see :attr:`BaseHost.source_lab`)."""

    async def _login(self, user: str | None = None) -> None: ...

    async def login(self, user: str | None = None) -> None:
        """Open an interactive shell bridged to the local terminal."""
        ...

    async def run(
        self,
        cmds: str | ShellCommand | Sequence[str | ShellCommand],
        expects: Expect | list[Expect] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
        sudo: bool = False,
        user: str | None = None,
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
            sudo: If ``True``, each command is run with elevated privileges,
                through whichever mechanism the host's userland resolved.
                Implementations that do not support elevation raise
                :exc:`NotImplementedError`; a host whose userland offers no
                mechanism raises
                :exc:`~otto.host.errors.UnsupportedOnUserlandError`.
            user: Run the commands as this user. Containers implement this
                (``docker exec -u``); every other family refuses loudly — a
                persistent session's identity is
                :meth:`~otto.host.privilege.PosixPrivilege.as_user`'s job,
                and the stateless :meth:`exec`/:meth:`put`/:meth:`get` take
                ``user=`` directly on unix. ``None`` means the container's
                declared default, or the connection's own identity elsewhere.
                On containers the persistent channel binds its user at open;
                a later ``run()`` naming a different user refuses — ``close()``
                or ``rebuild_connections()`` to rebind.

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
        user: str | None = None,
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

        Args:
            cmd: Shell command to run.
            timeout: Seconds before the command is abandoned.
            log: Logging disposition for this call.
            user: Run the command as this user. Containers act via
                ``docker exec -u``; ssh-term unix hosts AUTHENTICATE as the
                user — the command runs over that user's own SSH connection,
                so direct-cred users only (a login reachable only through
                proxy hops refuses). Every other family (telnet-term unix,
                embedded, local) refuses loudly. ``None`` means the
                container's declared default, or the connection's own
                identity elsewhere.

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

        Under a dry run nothing is opened and the handle is a
        :class:`~otto.host.session.DeclinedSession`, whose methods announce
        what they would have done and decline at the point of use. Closing it
        is safe and does nothing.
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

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run, which issues no
                command for a pattern to match — see
                :func:`refuse_declined_match`.
        """
        ...

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        user: str | None = None,
    ) -> Result:
        """Download one or more files from the host to a local directory.

        ``user`` is accepted for interface uniformity with :meth:`put`.
        Containers ignore it — reads are ownership-indifferent, so there is
        nothing to chown. Unix hosts honour it by AUTHENTICATING as that
        user — the transfer rides that user's own connection, so the read
        happens with their permissions (direct-cred users only; never over the
        ``ftp`` backend). Families with neither path refuse it loudly instead
        of silently ignoring it.

        Returns a :class:`~otto.result.Result` whose ``value`` is a
        ``dict[Path, Result]`` mapping each source path — keyed exactly as
        passed, with no resolution — to its per-file outcome: ``value=dest_path``
        on success, a per-file ``msg`` on failure, or
        :attr:`~otto.utils.Status.Skipped` (``"not attempted (earlier failure)"``)
        for a file a sequential backend never reached. The aggregate status is
        the first non-ok entry's status (Skipped counts as ok, so a trailing run
        of Skipped never fails the aggregate on its own). Under ``--dry-run``
        every entry is :attr:`~otto.utils.Status.NotRun` instead, which is NOT
        ok, so the aggregate is non-ok and no caller reads the transfer as
        having happened; the per-file ``value`` still carries the destination
        path, because that is computed locally and IS the preview.
        """
        ...

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        mode: int | str | None = None,
        user: str | None = None,
    ) -> Result:
        """Upload one or more local files to a directory on the host.

        ``mode`` sets the permission bits on the uploaded files: an ``int``
        (``0o755``) from Python, or a string always read as octal (``"755"``,
        ``"0755"``, ``"0o755"``). ``None`` leaves the backend's default
        permissions. Hosts whose transfer backend has no permission model
        (embedded ``console``/``tftp``) reject a non-``None`` mode before
        transferring anything.

        ``user`` chowns the landed files inside a container to that identity
        — root performs the chown after the files land, since the image's
        default user may not own what ``docker cp`` just placed; a chown
        failure fails the transfer loudly, per file. Any form ``chown``
        itself accepts is passed through verbatim: a name, a UID, or the
        ``UID:GID``/``name:group`` owner:group spelling. Unix hosts reach
        the same end differently: the transfer AUTHENTICATES as that user and
        rides their own connection, so the bytes land owned by them with
        no chown step at all — direct-cred users only, never over the ``ftp``
        backend, and a destination still relative after ``default_dest_dir``
        resolution lands under *their* home. Host families with neither path
        refuse a non-``None`` ``user`` loudly rather than silently ignoring
        it.

        Returns a :class:`~otto.result.Result` whose ``value`` is a
        ``dict[Path, Result]`` mapping each source path — keyed exactly as
        passed, with no resolution — to its per-file outcome: ``value=dest_path``
        on success, a per-file ``msg`` on failure, or
        :attr:`~otto.utils.Status.Skipped` (``"not attempted (earlier failure)"``)
        for a file a sequential backend never reached. The aggregate status is
        the first non-ok entry's status (Skipped counts as ok, so a trailing run
        of Skipped never fails the aggregate on its own). Under ``--dry-run``
        every entry is :attr:`~otto.utils.Status.NotRun` instead, which is NOT
        ok, so the aggregate is non-ok and no caller reads the transfer as
        having happened; the per-file ``value`` still carries the destination
        path, because that is computed locally and IS the preview. A file that
        transferred but whose ``mode`` could not be applied, or whose ``user``
        chown failed, is an error entry that still carries its ``dest_path``.
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

    async def stage(self, owner: str | None = None) -> Result:
        """Stage every product onto this host (transfer/place, no install).

        *owner* narrows the walk to that repo's products; ``None`` is every
        product. Returns a :class:`~otto.result.Result`.
        """
        ...

    async def install(self, stage_only: bool = False, owner: str | None = None) -> Result:
        """Stage and then install every product on this host.

        When *stage_only* is ``True``, stops after staging without installing.
        *owner* narrows the walk to that repo's products. Returns a
        :class:`~otto.result.Result`, short-circuiting on the first failure.
        """
        ...

    async def uninstall(
        self,
        get_product_logs: bool = True,
        get_debug_logs: bool = True,
        owner: str | None = None,
    ) -> Result:
        """Gather logs, uninstall every product, gather debug logs (best-effort).

        Returns a :class:`~otto.result.Result` — the first non-ok outcome, after
        attempting every step.
        """
        ...

    async def cleanup(self, get_product_logs: bool = True, get_debug_logs: bool = True) -> Result:
        """Uninstall, then remove dev tools and toolchain tools (best-effort)."""
        ...

    async def is_installed(self, owner: str | None = None) -> bool:
        """Return ``True`` iff at least one product is declared and all are installed."""
        ...

    async def is_uninstalled(self, owner: str | None = None) -> bool:
        """Return ``True`` iff :meth:`is_installed` returns ``False``."""
        ...

    async def is_clean(self) -> bool:
        """Return ``True`` iff no product, dev tool, or toolchain tool is present."""
        ...

    def log_dest(self, dest: "Path | None" = None) -> Path:
        """Local root for this host's retrieved logs: ``<base>/logs/<host-id>``."""
        ...

    async def get_logs(
        self,
        product: bool = True,
        debug: bool = True,
        require_product_logs: bool = False,
        dest: "Path | None" = None,
        owner: str | None = None,
    ) -> Result:
        """Gather product and debug logs into this host's log destination."""
        ...

    async def get_product_logs(
        self, dest: "Path | None" = None, owner: str | None = None
    ) -> Result:
        """Retrieve each product's logs into ``…/logs/<host-id>/product/``."""
        ...

    async def get_debug_logs(self, dest: "Path | None" = None) -> Result:
        """Fetch ``debug_log_globs`` matches into ``…/logs/<host-id>/debug/``."""
        ...

    async def install_dev_tools(self, owner: str | None = None) -> Result:
        """Stage then install every dev tool (declaration order, first failure wins).

        *owner* narrows the walk to that repo's tools; ``None`` is every tool.
        """
        ...

    async def uninstall_dev_tools(self, owner: str | None = None) -> Result:
        """Remove every dev tool (best-effort), narrowed to *owner*'s when given."""
        ...

    async def install_toolchain_tools(self) -> Result:
        """Install the toolchain's declared tools (transfer, rename, chown, per tool)."""
        ...

    async def remove_toolchain_tools(self) -> Result:
        """Remove the toolchain's declared tools from this host (best-effort)."""
        ...

    async def toolchain_tools_absent(self) -> bool:
        """Return ``True`` iff none of the toolchain's declared tools is present."""
        ...

    async def install_tools(self, dev: bool = True, toolchain: bool = False) -> Result:
        """Install tool kinds conditionally: dev tools on, toolchain artifacts off."""
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
    lab_info: "LabInfo"
    resources: frozenset[str]
    element_resources: frozenset[str]
    inventory_ref: "InventoryRef"
    products: list["Product"]
    dev_tools: list["DevTool"]
    toolchain: "Toolchain"
    power_control: "PowerController | None"

    debug_log_globs: list[str]
    """Remote paths/glob patterns the default :meth:`get_debug_logs` fetches.

    Settable per host class, per OS profile, and per host in ``lab.json``;
    default empty. A pattern (``*``, ``?``, ``[``) is expanded on the device by
    :meth:`~otto.host.file_ops.PosixFileOps.glob`, so a host family without
    that capability must declare concrete paths or override the method.
    """

    source_lab: str = ""
    """Name of the lab this host came from — assigned by the LOADER, not lab data.

    Not a ``lab.json`` field: the host specs are ``extra='forbid'``, so a lab
    file can neither set it nor lie about it. It is stamped by
    :func:`otto.host.factory.create_host_from_dict` (``lab_name=``), swept in
    per component by :func:`otto.config.lab.load_lab`, and backstopped by
    :meth:`otto.config.lab.Lab.add_host` for hosts built outside the loader
    (container hosts, the built-in ``local``).

    It exists because merging erases attribution: ``a + b`` yields ONE lab
    named ``a+b``, and from then on the lab cannot say which of its components
    any given host was declared in. This stamp is that memory, per host.
    The default ``""`` means "never registered with a lab" (a bare factory
    call), which is a different statement from any lab name.
    """

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

    def _dry_run_result(self, cmd: str, log: LogMode = LogMode.NORMAL) -> CommandResult:
        """Announce *cmd* and return a decline that cannot be read as its answer.

        The returned :class:`~otto.result.NotRunResult` is ``Status.NotRun``
        (``is_ok`` False) and RAISES
        :exc:`~otto.result.CommandNotRunError` on ``value``. It replaced a
        synthetic SUCCESS — ``Status.Skipped`` / ``retcode 0`` / ``is_ok``
        True, carrying the literal string ``"[DRY RUN] Command not executed"``
        — which was a poison pill shaped exactly like data: every status
        branch passed it and every parser chewed it, so userland probes
        settled fabricated capabilities, ``link list`` reported every link
        clean and ``tunnel add`` accused hosts of missing socat. See the
        design spec, ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md``
        section 4.

        ``retcode=-1`` is this family's documented "the command never ran"
        sentinel (see :attr:`~otto.result.CommandResult.retcode`), not a
        second invention. ``value`` is left at its default and never reaches
        the instance: the property absorbs the write and poisons the read.

        SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: hardening the returned
        object changes nothing about the ``[DRY RUN]`` line below, which is
        the dry run's entire product.

        *log* is the caller's per-command mode, exactly as the real runner
        receives it, and is folded with the host's standing mode HERE rather
        than by each call site. This method IS the dry-run path's emit seam —
        it calls ``_log_command`` directly — and ``_effective_log`` belongs at
        the emit seam (see :class:`HostFilter`), so folding inside is the one
        place it cannot be forgotten. The fold is a most-restrictive ``max``
        (:func:`~otto.logger.mode.effective_mode`), hence idempotent, so a
        caller that hands over an already-folded mode is still correct.

        Honouring the mode matters beyond noise: ``write_file`` sends the
        file's whole body base64-encoded at ``LogMode.QUIET``, so a dry run
        that logged at ``NORMAL`` put the contents of a credentials file on
        the console that a real run keeps off it. ``QUIET`` removes the line
        from the console and ``console.log``; it still reaches ``verbose.log``,
        which carries no :class:`HostFilter`.
        """
        self._log_command(f"[DRY RUN] {cmd}", self._effective_log(log))
        return NotRunResult(status=Status.NotRun, command=cmd, retcode=-1, host_name=self.name)

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
        ``Status.NotRun`` (which does NOT count as ok) with a ``[DRY RUN]``
        diagnostic, so the folded aggregate is NotRun and its ``msg`` names the
        action.

        **Only the STATUS hardens here — the values stay plain, readable
        Results.** A transfer entry's ``value`` is the destination path, which
        this method computes locally from *dest* and the caller's own file
        names; it is never a device measurement, so it is the preview rather
        than the fabrication. Poisoning it the way
        :meth:`_dry_run_result` poisons a command's output would delete the
        dry run's product and leave the hazard untouched.

        A *mode* is parsed here even though nothing is transferred: a typo'd
        ``--mode 789`` is the caller's own input and costs nothing to catch, so
        a dry run should catch it. Backend capability is **not** checked — that
        belongs to the real transfer, which is where the backend is actually
        selected and used.

        **This banner is deliberately NOT folded with the host's standing mode,
        unlike ``_dry_run_result``'s line.** The exemption is a decision, not an
        accident of the signature: ``get``/``put``/``load``/``unload`` take no
        per-call ``log``, but the STANDING mode is a separate input and would
        apply if this folded — a host declared ``log = false`` in lab data
        (coerced to ``QUIET`` by ``HostSpec._coerce_log_bool``) or a monitor
        host pinned to ``NEVER`` (``monitor.factory``) would print nothing. It
        prints anyway because this line is an ANNOUNCEMENT, never a payload: it
        names an action, source files and a destination, and a dry run whose
        output is empty is useless rather than safe. The tension is real — an
        operator who set ``log = false`` did ask for quiet, and the real
        transfer honours that (it runs inside ``SuppressCommandOutput``, which
        this arm returns above) — and it is resolved in favour of the dry run
        having a product. Payloads are the thing that must obey the mode.
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
            src: Result(Status.NotRun, value=dest / src.name, msg=f"[DRY RUN] {action}: {src}")
            for src in files
        }
        # Built here rather than through `aggregate_transfer` so the aggregate
        # `msg` is the single banner an operator reads, not the per-file
        # diagnostics joined; the status it would compute is the same NotRun.
        return Result(
            Status.NotRun,
            value=per_file,
            msg=f"[DRY RUN] {action}: {file_names} -> {dest}{suffix}",
        )

    def _dry_run_power_report(self, action: str, detail: str) -> Result:
        """Announce a power action a dry run declines to take, and return the decline.

        The vocabulary for the verbs that do not go through the command path at
        all: :meth:`reboot` ``hard=True`` drives a
        :class:`~otto.host.power.PowerController` — a PDU or a hypervisor, not
        a shell — so :meth:`_dry_run_result`, ``Status.NotRun`` and every other
        primitive the contract built are nowhere near the line that cycles the
        box. *action* names the verb (``"REBOOT (hard)"``), *detail* says what
        WOULD happen and, explicitly, what did not.

        ``Status.NotRun`` and a plain :class:`~otto.result.Result`, deliberately
        matching ``write_file``'s decline rather than
        :class:`~otto.result.NotRunResult`: nothing was measured, so ``value``
        is honestly ``None`` and the renderer — which reads it in order to
        print — has nothing to detonate on.

        ``is_ok`` False is what a LIBRARY caller acts on
        (``if (await host.reboot()).is_ok:``), and it is deliberately NOT what
        keeps :meth:`reboot` from rebuilding its transports — the early return
        above that gate is. Worth stating, because the follow-up that
        commissioned this guard predicted the opposite: hardening the status
        and leaving the body to run would have left every ACTION below it
        happening, which is the whole defect.

        **The banner is NOT folded with the host's standing log mode**, for the
        same reason :meth:`_dry_run_transfer`'s is not: these verbs take no
        per-call ``log``, so folding would let a host declared ``log = false``
        in lab data print nothing at all. This line is an ANNOUNCEMENT, never a
        payload — a dry run whose output is empty is a bug.
        """
        banner = f"[DRY RUN] {action}: {self.name} — {detail}"
        self._log_command(banner)
        return Result(Status.NotRun, msg=banner)

    def _dry_run_session(self, name: str) -> "HostSession":
        """Announce the session a dry run declines to open, and return the decline.

        The :class:`~otto.result.NotRunResult` philosophy one level up: the
        caller gets a handle that is safe to hold and to hand around, and every
        method that would touch the device declines AT THE POINT OF USE. One
        mental model applies at both layers — ``host.exec`` under a dry run
        answers ``NotRun``, so ``session.run`` does too — and the caller still
        gets the preview, because each declined call announces the command it
        would have sent.

        See :class:`~otto.host.session.DeclinedSession` for the per-method
        contract and for why ``expect`` is the one method that raises.

        SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: the banner is the right
        idea from the version this replaced, which logged
        ``[DRY RUN] open_session(...)`` and then genuinely opened the session —
        the announcement-without-the-suppression inversion, where the log line
        is what makes the hole look handled. On
        :class:`~otto.host.docker_host.DockerContainerHost` that arm ran
        ``_ensure_running()`` immediately after the banner, so a dry run could
        reach ``compose_up`` and start a real container.

        Args:
            name: the session name the caller asked for.

        Returns:
            A :class:`~otto.host.session.DeclinedSession` for *name*.
        """
        from .session import DeclinedSession

        self._log_command(f"[DRY RUN] open_session({name!r}) — no session opened")
        # The login user is CONFIGURATION, not a measurement, so the declined
        # handle reports it exactly as a real one would — read from the same
        # `_login_user()` that `SessionManager._seed_user` stamps a live named
        # session with. `getattr` because `_session_mgr` is a concrete
        # family's field, not `BaseHost`'s; the three `open_session` overrides
        # that reach here all have one.
        mgr = getattr(self, "_session_mgr", None)
        login_user = mgr._login_user() if mgr is not None else ""  # noqa: SLF001 — intra-package read of the manager's configured login user
        # `self.name`, not `self.id`, and deliberately: this identifier is
        # read by humans in a `[DRY RUN]` banner or a CommandNotRunError
        # message, and every other dry-run decline on this class names the
        # host the same way (`_dry_run_result`, `_dry_run_power_report`).
        return DeclinedSession(
            name=name,
            log_command=self._log_command,
            log_output=self._log_output,
            host_id=self.name,
            current_user=login_user,
        )

    ####################
    #  Privilege
    ####################

    def _elevate(self, cmd: str) -> tuple[str, list["Expect"]]:
        """Return *(wrapped_cmd, extra_expects)* to run *cmd* with elevation.

        Default raises — only posix-shell hosts (via the ``PosixPrivilege``
        mixin) can elevate. Embedded/RTOS hosts have no ``sudo``.

        Synchronous by contract, which is why :meth:`_prepare_elevation`
        exists: an implementation needing an awaited answer resolves it there.
        """
        raise NotImplementedError(
            f"sudo/elevation is not supported on '{self.__class__.__name__}'"
        ) from None

    async def _prepare_elevation(self) -> None:
        """Resolve anything :meth:`_elevate` reads. Awaited by ``run(sudo=True)``.

        Default no-op — a host whose ``_elevate`` needs nothing resolved (and a
        host that cannot elevate at all, which still raises from ``_elevate``
        and not from here) does nothing. ``PosixPrivilege`` overrides it to
        resolve the host's :class:`~otto.host.userland.Userland`, whose
        capabilities raise if read before ``resolve()`` has been awaited.

        Separate from ``_elevate`` rather than folded into it because
        ``_elevate`` is synchronous and is called from a comprehension over
        already-resolved commands; this is the single async point above both of
        those call sites.
        """
        return

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

    async def _login(self, user: str | None = None) -> None:
        raise NotImplementedError(
            f"The '{self.__class__.__name__}' class does not support interactive sessions"
        ) from None

    @cli_exposed
    async def login(
        self,
        user: Annotated[
            str | None,
            Opt(
                help="Land the session as this user (containers via docker exec -u; "
                "unix replays login-proxy hops)."
            ),
        ] = None,
    ) -> None:
        """Open an interactive shell bridged to the local terminal.

        Subclasses implement ``_login`` to do the actual protocol
        work. This wrapper exists so CLI and SDK callers have a single
        public entry point.

        stdin and stdout are bridged directly to the remote terminal and the
        session is recorded to the otto log. Press ``Ctrl+]`` to disconnect
        locally without ending the remote session; type ``exit`` or ``logout``
        to end the session normally.

        Under a dry run this announces and returns without connecting. It is
        the ``send`` shape, not the ``expect`` shape: ``login`` returns
        ``None``, so returning early invents no device fact — its product is a
        side effect (a terminal bridged to a real shell), and declining to
        perform it is the whole point. On
        :class:`~otto.host.docker_host.DockerContainerHost` the arm matters
        twice over: ``_login`` calls ``_ensure_running()``, which can reach
        ``compose_up`` and START A CONTAINER. The CLI's ``--dry-run`` seam
        already stops this verb before its body (it declares no preview), so
        the exposed caller is the LIBRARY one.

        Args:
            user: Land the session on this login instead of the
                connection's configured default. Containers implement it via
                ``docker exec -u``; unix hosts replay any login-proxy hops
                needed to reach it (see :mod:`otto.host.login_proxy`). Hosts
                that can do neither raise :exc:`NotImplementedError`.
        """
        if user is not None:
            _validate_user(user)
        if is_dry_run():
            target = f"as {user!r}" if user is not None else "as the configured login user"
            self._log_command(
                f"[DRY RUN] login({self.name}) {target} — no interactive shell opened, "
                f"no connection made"
            )
            return
        await self._login(user)

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
        user: Annotated[
            str | None,
            Opt(
                help="Run as this user — containers only; every other family "
                "refuses a per-call user. On unix, put/get take --user; from "
                "Python use as_user() or exec(user=...). The persistent "
                "channel binds its user when it opens."
            ),
        ] = None,
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
                ``float("inf")`` to opt out of the bound. It bounds the
                COMMANDS and nothing else: with ``sudo=True`` the userland
                resolution below happens above this budget and can add up to
                ``otto.host.userland._RESOLVE_BUDGET_S`` (30s) to the call on a
                host that will not answer probes.
            sudo: If ``True``, each command is rewritten through ``_elevate`` before
                execution, using whichever mechanism this host's userland resolved
                (``sudo`` or ``su``). Resolving that mechanism is a real cost the
                first time — see ``timeout`` above and
                :meth:`otto.host.userland.Userland.resolve`. Hosts that do not
                support elevation at all
                (e.g. embedded/RTOS) raise :exc:`NotImplementedError`; a host whose
                userland offers neither mechanism raises
                :exc:`~otto.host.errors.UnsupportedOnUserlandError` rather than
                emitting a command that cannot work — see ``_elevate``.
            user: Run each command as this user. Containers implement this
                (``docker exec -u``); every other family refuses loudly — a
                persistent session's identity is
                :meth:`~otto.host.privilege.PosixPrivilege.as_user`'s job,
                and the stateless :meth:`exec`/:meth:`put`/:meth:`get` take
                ``user=`` directly on unix. ``None`` means the container's
                declared default, or the connection's own identity elsewhere.
                On containers the persistent channel binds its user at open;
                a later run() naming a different user refuses — close() or
                rebuild_connections() to rebind.

        Returns:
            A :class:`~otto.result.Results` aggregating one :class:`~otto.result.CommandResult`
            per command.

        Raises:
            ~otto.host.errors.UnsupportedOnUserlandError: the host's declared
                shell dialect is ``ash`` and a command's framed line would
                exceed what BusyBox ash's line editor delivers intact — see
                :func:`~otto.host.session.refuse_if_line_editor_would_truncate`.
                The command is refused before anything is sent, because the
                alternative is the device silently running a SHORTER command
                and reporting its success as this one's. :meth:`exec` allocates
                no pty, is not subject to the bound, and is the way to send
                such a command.

        See Also:
            :meth:`exec`: stateless, concurrent-safe alternative for one-off commands.
        """
        if user is not None:
            _validate_user(user)
        timeout = _validate_timeout(timeout)
        default_expects = _normalize_expects(expects)
        if sudo:
            # ABOVE the single-vs-sequence split, not inside either arm. This is
            # the only async point above `_elevate`, which is synchronous and so
            # cannot resolve the capabilities it reads, and there are two
            # `_apply_sudo` sites below — one per arm. Moved into the
            # single-command arm this still reads earlier in the file than the
            # sequence arm's rewrite, so it satisfies any line-number check
            # while `run([...], sudo=True)` raises; that mutant was measured
            # green against an earlier version of the guard. What pins it now is
            # a STATEMENT-POSITION rule plus a behavioural test driving both
            # shapes, in `tests/unit/host/test_privilege.py`.
            await self._prepare_elevation()
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
                user=user,
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
                user=user,
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
        user: "str | None" = None,
    ) -> CommandResult:
        """Per-command runner for the persistent shell session. Subclasses override."""
        raise NotImplementedError from None

    async def exec(
        self,
        cmd: str,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        log: LogMode = LogMode.NORMAL,
        user: str | None = None,
    ) -> CommandResult:
        """Run a single command outside the persistent shell session.

        Validates *timeout* and *user* and delegates to ``_exec_one``, which
        each host family implements. Do not override this method — override
        ``_exec_one``, so the validation cannot be bypassed.

        Args:
            cmd: Shell command to run.
            timeout: Seconds before the command is abandoned. Defaults to
                :data:`DEFAULT_COMMAND_TIMEOUT`; pass ``float("inf")`` for a
                deliberately unbounded command.
            log: Logging disposition for this call.
            user: Run the command as this user. Containers act via
                ``docker exec -u``; ssh-term unix hosts AUTHENTICATE as the
                user — the command runs over that user's own SSH connection,
                so direct-cred users only (a login reachable only through
                proxy hops refuses). Every other family (telnet-term unix,
                embedded, local) refuses loudly. ``None`` means the
                container's declared default, or the connection's own
                identity elsewhere.
        """
        if user is not None:
            _validate_user(user)
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            return self._dry_run_result(cmd, log)
        return await self._exec_one(cmd, timeout=timeout, log=log, user=user)

    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: str | None = None,
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

        Returns:
            The matched text.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run, which issues no
                command for a pattern to match — see
                :func:`refuse_declined_match` for why the empty string this
                replaced was a fabrication and not a safe default.
        """
        timeout = _validate_timeout(timeout)
        if is_dry_run():
            refuse_declined_match(pattern, self.name, self._log_command)
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
        user: str | None = None,
    ) -> Result:
        """Download files from the host to a local directory. Subclasses must override."""
        raise NotImplementedError from None

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        mode: int | str | None = None,
        user: str | None = None,
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

    def _owned_products(self, owner: str | None) -> "list[Product]":
        """Products filtered by owner; ``None`` = all (the pre-owner behavior)."""
        if owner is None:
            return list(self.products)
        return [p for p in self.products if p.owner == owner]

    @cli_exposed
    async def stage(self, owner: str | None = None) -> Result:
        """Stage every product onto this host (transfer/place, no install).

        Iterates :attr:`products` in declaration order, returning the first
        non-ok :class:`~otto.result.Result`; an empty list is a successful no-op.
        *owner* narrows the walk to the products that repo attached, so one
        repo's default actions never touch another's.
        """
        for product in self._owned_products(owner):
            result = await product.stage(cast("Host", self))
            if not result.is_ok:
                # Returned whole: a product's CommandResult carries the retcode
                # and output that the CLI turns into an exit code.
                return result
        return Result(Status.Success)

    @cli_exposed
    async def install(self, stage_only: bool = False, owner: str | None = None) -> Result:
        """Stage, then install every product.

        Calls :meth:`stage` first; returns early if ``stage_only`` is set or the
        stage step failed. Otherwise installs each product in declaration order,
        short-circuiting on the first failure. *owner* scopes both halves.
        Projects may override for cross-product ordering/dependencies.
        """
        stage_result = await self.stage(owner=owner)
        if stage_only or not stage_result.is_ok:
            return stage_result
        for product in self._owned_products(owner):
            result = await product.install(cast("Host", self))
            if not result.is_ok:
                return result
        return Result(Status.Success)

    @cli_exposed
    async def uninstall(
        self,
        get_product_logs: bool = True,
        get_debug_logs: bool = True,
        owner: str | None = None,
    ) -> Result:
        """Gather product logs, uninstall every product, gather debug logs.

        **The order is the contract.** Product logs come off the host BEFORE
        anything is torn down — a lost log set is the frustration this whole
        surface exists to prevent — and debug logs come off AFTER, because
        teardown-time activity is typically exactly what they exist to capture.

        Best-effort throughout: every product is attempted even if one fails,
        and a failed log haul is recorded as a failure but never aborts the
        uninstall (that would strand products on the host over a lost log).
        The first non-ok result seen is what returns. *owner* scopes both the
        product walk and the product-log haul; debug logs are host-level and
        unscoped, which is why the per-repo walk turns them off with
        ``get_debug_logs=False`` rather than filtering them.
        """
        first_failure: Result | None = None

        def note(result: Result) -> None:
            nonlocal first_failure
            if not result.is_ok and first_failure is None:
                first_failure = result

        if get_product_logs:
            note(await self.get_product_logs(owner=owner))
        for product in self._owned_products(owner):
            note(await product.uninstall(cast("Host", self)))
        if get_debug_logs:
            note(await self.get_debug_logs())
        return first_failure if first_failure is not None else Result(Status.Success)

    @cli_exposed(output_dir=False)
    async def is_installed(self, owner: str | None = None) -> bool:
        """Return True iff there is at least one product and all are installed.

        An empty :attr:`products` list is **not installed** (avoids the
        vacuous-truth surprise of ``all([])``) — and the rule applies to the
        *owner*-filtered list, so a repo with nothing on this host is not
        reported installed by another repo's products.
        """
        owned = self._owned_products(owner)
        if not owned:
            return False
        for product in owned:
            if not await product.is_installed(cast("Host", self)):
                return False
        return True

    @cli_exposed(output_dir=False)
    async def is_uninstalled(self, owner: str | None = None) -> bool:
        """Inverse of :meth:`is_installed`."""
        return not await self.is_installed(owner=owner)

    @cli_exposed
    async def cleanup(self, get_product_logs: bool = True, get_debug_logs: bool = True) -> Result:
        """Uninstall, then remove dev tools and toolchain tools (best-effort).

        Strictly more than :meth:`uninstall`, in that order: products first (a
        dev tool may be what a product's uninstall needs), then the tooling.
        The log flags are forwarded to :meth:`uninstall`, which owns the log
        ordering. Project-specific remnant removal belongs in an override.

        HOST-GLOBAL, and takes no ``owner``: its last step removes the
        toolchain tools, which one host shares with every owner on it. A repo
        scoping its own teardown composes ``uninstall(owner=…)`` with
        :meth:`uninstall_dev_tools` instead — an owner-scoped ``cleanup`` would
        be a verb that honours the scope for two of its three steps and takes
        the neighbours' tooling with it on the third.

        Each removal result is reported WHOLE. Repacking one as
        ``Result(status, msg=result.value)`` would read ``value`` on the
        :class:`~otto.result.NotRunResult` a dry run returns, which raises
        :exc:`~otto.result.CommandNotRunError` — a traceback where the contract
        says decline.
        """
        first_failure: Result | None = None

        def note(result: Result) -> None:
            nonlocal first_failure
            if not result.is_ok and first_failure is None:
                first_failure = result

        note(await self.uninstall(get_product_logs=get_product_logs, get_debug_logs=get_debug_logs))
        note(await self.uninstall_dev_tools())
        note(await self.remove_toolchain_tools())
        return first_failure if first_failure is not None else Result(Status.Success)

    @cli_exposed(output_dir=False)
    async def is_clean(self) -> bool:
        """No products installed, no dev tools installed, no toolchain tools present.

        The matching question to :meth:`cleanup`, and it ASKS rather than
        infers: the toolchain probe refuses under a dry run
        (:func:`refuse_declined_fact`) instead of reporting a host clean that
        nobody looked at.
        """
        if not await self.is_uninstalled():
            return False
        for tool in self.dev_tools:
            if await tool.is_installed(cast("Host", self)):
                return False
        return await self.toolchain_tools_absent()

    ####################
    #  Logs
    ####################

    def log_dest(self, dest: "Path | None" = None) -> Path:
        """Local root for this host's retrieved logs: ``<base>/logs/<host-id>``.

        *base* is *dest* when given, else the active command's output
        directory, else the CWD. The subtree below (``product/``, ``debug/``)
        is a documented contract — the mirror of the coverage pipeline's
        per-host-id keying — so consumers may read it by path.
        """
        from ..context import try_get_context  # lazy — host must not import context at import time

        ctx = try_get_context()
        if dest is not None:
            base = dest
        else:
            base = ctx.output_dir if ctx and ctx.output_dir else Path.cwd()
        return base / "logs" / self.id

    @cli_exposed
    async def get_product_logs(
        self, dest: "Path | None" = None, owner: str | None = None
    ) -> Result:
        """Retrieve each product's logs into ``…/logs/<host-id>/product/``.

        Best-effort: every product's :meth:`~otto.host.product.Product.get_logs`
        hook is called even after one fails, and the first failure is what
        returns. The hook need not run anything on the host — an external
        retrieval mechanism is equally welcome — it just has to land its files
        under the directory it is handed.
        """
        target = self.log_dest(dest) / "product"
        target.mkdir(parents=True, exist_ok=True)
        first_failure: Result | None = None
        for product in self._owned_products(owner):
            result = await product.get_logs(cast("Host", self), target)
            if not result.is_ok and first_failure is None:
                first_failure = result
        return first_failure if first_failure is not None else Result(Status.Success)

    @cli_exposed
    async def get_debug_logs(self, dest: "Path | None" = None) -> Result:
        """Fetch :attr:`debug_log_globs` matches into ``…/logs/<host-id>/debug/``.

        An entry with no glob metacharacter is fetched as declared, so a host
        family with no shell needs nothing new. An entry WITH one needs
        :meth:`~otto.host.file_ops.PosixFileOps.glob`; a host class without it
        (embedded, for now) must declare concrete paths or override this
        method — a pattern there fails LOUD rather than being silently
        skipped, because a skipped log set looks exactly like a host that had
        no logs.
        """
        target = self.log_dest(dest) / "debug"
        target.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for entry in self.debug_log_globs:
            if any(ch in entry for ch in "*?["):
                glob = getattr(self, "glob", None)
                if glob is None:
                    return Result(
                        Status.Error,
                        msg=(
                            f"debug_log_globs entry {entry!r} is a glob pattern, but "
                            f"{type(self).__name__} has no glob support — declare "
                            "concrete paths or override get_debug_logs."
                        ),
                    )
                paths.extend(Path(p) for p in await glob(entry))
            else:
                paths.append(Path(entry))
        if not paths:
            # Not an empty get(): several transfer backends report a no-file
            # transfer as a failure, and zero logs is success.
            return Result(Status.Success)
        return await self.get(paths, target)

    @cli_exposed
    async def get_logs(
        self,
        product: bool = True,
        debug: bool = True,
        require_product_logs: bool = False,
        dest: "Path | None" = None,
        owner: str | None = None,
    ) -> Result:
        """Conditionally gather product and debug logs (both by default).

        Zero retrieved logs is success; *require_product_logs* turns an empty
        product-log haul into a failure (there is deliberately no debug twin —
        no case was made for one, and symmetry alone does not buy a flag).

        *require_product_logs* with ``product=False`` is a contradiction and is
        refused up front rather than ignored: the flag pair is expressible from
        the CLI (``get-logs --no-product --require-product-logs``), and a
        requirement that is parsed but unenforceable would exit 0 having
        promised logs nobody went looking for.
        """
        if require_product_logs and not product:
            return Result(
                Status.Error,
                msg=(
                    "require_product_logs cannot be satisfied with product=False: "
                    "the product-log haul it requires is the step being skipped. "
                    "Gather product logs, or drop the requirement."
                ),
            )
        if product:
            product_dir = self.log_dest(dest) / "product"
            # Snapshot BEFORE the haul: the requirement is about what THIS call
            # retrieved, and a reused dest already holds an earlier haul's
            # files. Skipped unless asked for — it is a directory walk.
            before = _log_tree_state(product_dir) if require_product_logs else set()
            result = await self.get_product_logs(dest=dest, owner=owner)
            if not result.is_ok:
                return result
            if require_product_logs and _log_tree_state(product_dir) <= before:
                return Result(
                    Status.Error,
                    msg=f"require_product_logs: no product logs retrieved from {self.id}",
                )
        if debug:
            return await self.get_debug_logs(dest=dest)
        return Result(Status.Success)

    ####################
    #  Tools
    ####################

    def _owned_dev_tools(self, owner: str | None) -> "list[DevTool]":
        """Dev tools filtered by owner; ``None`` = all (the pre-owner behavior).

        The twin of :meth:`_owned_products`, deliberately identical in shape:
        both attachment kinds carry the same ``owner`` stamp, so a reader who
        knows one filter knows the other. ``None`` is "every tool", not "the
        tools with no owner" — the pre-owner callers (``otto host <id>
        install-tools``, :meth:`cleanup`) are host-global and must stay so.
        """
        if owner is None:
            return list(self.dev_tools)
        return [t for t in self.dev_tools if t.owner == owner]

    @cli_exposed
    async def install_dev_tools(self, owner: str | None = None) -> Result:
        """Stage then install every dev tool (declaration order, first failure wins).

        Each tool is carried through both phases before the next one starts, so
        a tool that stages but cannot install stops the walk rather than
        leaving later tools installed on top of a half-placed prerequisite.
        *owner* narrows the walk to the tools that repo attached, exactly as on
        the product verbs: it is what lets one repo's project actions place its
        own tooling on a shared host without touching a neighbour's.
        """
        for tool in self._owned_dev_tools(owner):
            result = await tool.stage(cast("Host", self))
            if not result.is_ok:
                # Returned whole: a tool's CommandResult carries the retcode
                # and output that the CLI turns into an exit code.
                return result
            result = await tool.install(cast("Host", self))
            if not result.is_ok:
                return result
        return Result(Status.Success)

    @cli_exposed
    async def uninstall_dev_tools(self, owner: str | None = None) -> Result:
        """Remove this host's dev tools (best-effort), scoped by *owner*.

        The removal twin of :meth:`install_dev_tools`, and a verb of its own
        rather than a loop inside :meth:`cleanup` for the same reason
        :meth:`remove_toolchain_tools` is one: a caller needs exactly this step
        on its own. The per-repo project actions tear down with
        ``uninstall(owner=…)`` followed by ``uninstall_dev_tools(owner=…)`` --
        never :meth:`cleanup`, whose third step is the host-global toolchain a
        repo must not touch -- and a second copy of the loop over there drifted
        from this one the moment either changed.

        Best-effort, unlike the installation walk: every tool is attempted even
        after one fails, because a tool that refuses to go must not strand the
        rest of the tooling on the host. The first failure is what returns, and
        it is returned WHOLE (see :meth:`cleanup`).
        """
        first_failure: Result | None = None
        for tool in self._owned_dev_tools(owner):
            result = await tool.uninstall(cast("Host", self))
            if not result.is_ok and first_failure is None:
                first_failure = result
        return first_failure if first_failure is not None else Result(Status.Success)

    @cli_exposed
    async def install_toolchain_tools(self) -> Result:
        """Install the toolchain's declared tools (transfer, rename, chown, per tool).

        Default implementation, per tool: ``put`` it with its declared mode,
        then as root ``mv`` it to its declared
        :attr:`~otto.host.toolchain.ToolchainTool.name` if that differs from
        the source basename, and ``chown`` it to its declared user. Projects
        whose toolchain installs need more than that override this — that is
        expected, not a failure of the default.

        **The transfer must stay first.** Under ``--dry-run`` ``put`` returns a
        ``NotRun`` decline and this returns it, which is the only reason the
        verb never reaches ``as_user`` — elevation does not decline, it RAISES
        :exc:`~otto.result.CommandNotRunError`
        (:func:`~otto.host.host.refuse_declined_elevation`). Hoisting the
        elevation above the transfer, or out around the loop, turns a clean
        dry-run decline into a traceback. Pinned by
        ``test_install_toolchain_tools_declines_before_elevating_under_dry_run``.
        """
        for tool in self.toolchain.tools:
            result = await self.put(tool.source, tool.dest, mode=tool.mode)
            if not result.is_ok:
                return result
            # ``put`` lands every file under its SOURCE basename — no transfer
            # backend renames — so a declared name that differs is a rename
            # this verb owes, and nothing can address the tool by that name
            # (chown included) until it is done.
            landed = shlex.quote(str(tool.dest / tool.source.name))
            installed = shlex.quote(str(tool.dest / tool.name))
            async with self.as_user("root"):
                if tool.name != tool.source.name:
                    moved = await self.exec(f"mv {landed} {installed}")
                    if not moved.is_ok:
                        return moved
                chown = await self.exec(f"chown {shlex.quote(tool.user)} {installed}")
                if not chown.is_ok:
                    return chown
        return Result(Status.Success)

    @cli_exposed
    async def remove_toolchain_tools(self) -> Result:
        """Remove the toolchain's declared tools from this host (best-effort).

        The removal twin of :meth:`install_toolchain_tools`, and a step of its
        own rather than a loop inside :meth:`cleanup`, because ONE TOOLCHAIN
        SERVES EVERY OWNER on a host: the per-repo project actions must not
        touch it (a repo tearing it down would take its neighbours' tooling
        with it), so the ``otto.project`` orchestrator sweeps it across the
        fleet exactly once at the end of a lab-level cleanup. Both callers ask
        the same method so the path a tool is removed from cannot drift from
        the path it was installed to.

        Every tool is attempted even after one fails -- one immovable artifact
        must not strand the rest -- and the first failure is returned WHOLE,
        for the same reason :meth:`cleanup` reports removals whole: repacking
        one as ``Result(status, msg=result.value)`` reads ``value`` on the
        :class:`~otto.result.NotRunResult` a dry run returns, which raises
        :exc:`~otto.result.CommandNotRunError`.
        """
        first_failure: Result | None = None
        for tc_tool in self.toolchain.tools:
            # dest/name, NOT dest/source.name: install_toolchain_tools renames
            # each tool to its declared name, and that is what is on the host.
            result = await self.exec(f"rm -f {shlex.quote(str(tc_tool.dest / tc_tool.name))}")
            if not result.is_ok and first_failure is None:
                first_failure = result
        return first_failure if first_failure is not None else Result(Status.Success)

    async def toolchain_tools_absent(self) -> bool:
        """Whether none of the toolchain's declared tools is present on this host.

        The matching question to :meth:`remove_toolchain_tools`, extracted for
        the same reason: it is the host-global half of :meth:`is_clean`, and the
        orchestrator asks it across the fleet without re-asking the per-repo
        product questions that its own walk already covered.

        ASKS rather than infers -- the probe refuses under a dry run
        (:func:`refuse_declined_fact`) instead of reporting an absence nobody
        looked for. Python-only, unlike its removal twin: ``otto host <id>
        is-clean`` is the CLI-shaped question, and this is one term of it.
        """
        for tc_tool in self.toolchain.tools:
            present = await self.exec(f"test -e {shlex.quote(str(tc_tool.dest / tc_tool.name))}")
            refuse_declined_fact(present, asked=f"toolchain_tools_absent({tc_tool.name!r})")
            if present.status.is_ok:
                return False
        return True

    @cli_exposed
    async def install_tools(self, dev: bool = True, toolchain: bool = False) -> Result:
        """Install tool kinds conditionally, dev on and toolchain off by default.

        The asymmetric defaults are the point: dev tools are small and wanted
        on nearly every run, while toolchain artifacts are large and rarely
        needed, so asking for them is deliberate.
        """
        if dev:
            result = await self.install_dev_tools()
            if not result.is_ok:
                return result
        if toolchain:
            return await self.install_toolchain_tools()
        return Result(Status.Success)

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

        **Under a dry run this reports the plan and commands nothing** —
        ``Status.NotRun``, ``is_ok`` False. The same hazard as
        :meth:`reboot` ``hard=True`` and for the same reason: the controller is
        a PDU or a hypervisor, not a shell, so no part of the command path's
        dry-run plumbing is in the way of ``await host.power('off')``. The CLI
        stops above this verb at the ``--dry-run`` seam, which makes the live
        surface the LIBRARY layer — an embedder or an in-process caller holding
        an :class:`~otto.context.OttoContext` with ``dry_run=True``.

        THE TOGGLE ARM CANNOT SAY WHICH DIRECTION IT WOULD GO, and says so.
        Deciding requires ``controller.status()``, which is a device read; a
        preview naming ``'off'`` because that is the likely answer would be a
        fabricated measurement dressed as a plan, and the caller would confirm
        a direction otto never established.

        Raises:
            ValueError: no ``power_control`` on this host, or *state* is not
                ``'on'`` / ``'off'`` / ``None``. Both checks read local values
                only, so a dry run raises them from the same lines: previewing
                a power command that cannot be issued would fabricate
                feasibility. The third ``ValueError`` — a toggle against a
                controller that cannot report state — needs the device and is
                therefore reachable only on a real run.
        """
        from .power import PowerState

        def _with_state(result: Result, commanded: PowerState) -> Result:
            value = commanded if result.is_ok else None
            return Result(result.status, value=value, msg=result.msg)

        # Both checks hoisted above the dry-run arm so there is ONE authority
        # for each error rather than a preview-side copy that can drift.
        controller = self._require_power_control()
        if state not in ("on", "off", None):
            raise ValueError(f"invalid power state {state!r}; expected 'on', 'off', or None")
        if is_dry_run():
            if state is None:
                return self._dry_run_power_report(
                    "POWER (toggle)",
                    f"would read {type(controller).__name__}'s reported state and command "
                    f"the opposite. Not done: current state not read, no power command "
                    f"issued, direction undetermined",
                )
            return self._dry_run_power_report(
                f"POWER ({state})",
                f"would command power {state} via {type(controller).__name__}. "
                f"Not done: no power command issued",
            )
        if state == "on":
            return _with_state(await controller.on(cast("Host", self)), PowerState.ON)
        if state == "off":
            return _with_state(await controller.off(cast("Host", self)), PowerState.OFF)
        current = await controller.status(cast("Host", self))
        if current is None:
            raise ValueError(
                f"power(toggle) on {self.name!r} needs a controller that "
                f"reports status; pass state='on' or 'off'."
            )
        if current is PowerState.ON:
            return _with_state(await controller.off(cast("Host", self)), PowerState.OFF)
        return _with_state(await controller.on(cast("Host", self)), PowerState.ON)

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

        **Under a dry run this reports the plan and does nothing at all** —
        ``Status.NotRun``, ``is_ok`` False. THE HARD ARM IS WHY THE ARM IS AT
        THE TOP: it calls the power controller, which drives a PDU or a
        hypervisor rather than a host command, so none of the dry-run plumbing
        in the command path is anywhere near it and ``otto -n … reboot --hard``
        cycled a real machine. Three more consequences ride on the same guard,
        each of them a real side effect of a run that was supposed to have
        none: ``_soft_reboot`` discards the declined ``run("reboot")`` and
        answers Success, so ``rebuild_connections()`` below tore down every
        cached transport for a host that is still up; and both wait phases dial
        for real (``is_reachable`` → ``verify_connection``), burning the whole
        down/up deadline against a live host. Returning early is the contract's
        third honest behaviour — never run the logic — and it is the only one
        available, because there is no measurement here to decline: the harms
        are the ACTIONS.

        **The arm is a SECOND guard, not the only one, and deliberately so.**
        This verb keeps the CLI's ``--dry-run`` seam default, so
        ``otto -n host <id> reboot`` never reaches this body at all — every
        other verb with a richer preview than the seam's block opts in with
        ``dry_run_preview=True``, and this one does not. The stakes are
        asymmetric in a way no other verb's are: a regressed transfer preview
        mutates a file, a regressed reboot arm power-cycles hardware, on the
        one flag that means "I am not sure". The two guards are genuinely
        independent — the seam reads the typed root options
        (``dry_run_requested``) while this arm reads the active context
        (``is_dry_run``) — so a context-plumbing regression takes out one and
        not the other. The rule: opt in whenever the preview is richer, EXCEPT
        for verbs that touch power.

        Raises:
            ValueError: ``hard=True`` on a host with no ``power_control``. A
                dry run raises it too, from the same line: the check reaches no
                device, and previewing a power cycle that cannot happen would
                fabricate feasibility.
        """
        if is_dry_run():
            if hard:
                # Asked even though nothing will be cycled: it names the
                # controller in the preview, and a host with no power backend
                # must fail here exactly as it would have for real.
                controller = self._require_power_control()
                would = f"would power-cycle via {type(controller).__name__}"
                not_done = "no power cycle issued"
            else:
                would = "would issue the in-shell reboot command"
                not_done = "no reboot command issued"
            if not wait:
                waiting = "no wait"
            elif down_timeout > 0:
                waiting = (
                    f"then wait up to {min(down_timeout, timeout)}s for it to go down "
                    f"and {timeout}s in total for it to come back, polling every "
                    f"{poll_interval}s"
                )
            else:
                waiting = (
                    f"then wait up to {timeout}s for it to come back (down phase "
                    f"skipped), polling every {poll_interval}s"
                )
            return self._dry_run_power_report(
                f"REBOOT ({'hard' if hard else 'soft'})",
                f"{would}, {waiting}. Not done: {not_done}, host not probed, "
                f"cached transports kept",
            )
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
