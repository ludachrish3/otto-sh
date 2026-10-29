"""The two ways a host command fails, as separate types.

Every package that drives a host through :meth:`~otto.host.host.Host.exec` had
one ``RuntimeError`` for both, and the difference is the one a caller acts on:
a host that cannot be reached (or never answered) is an infrastructure problem
to report and move past, while a command that RAN and failed is a result about
the system under test.

There is a THIRD outcome that is not defined here: ``CommandNotRunError``, for
a command a dry run declined to issue, lives in ``otto.result`` next to the
only thing that raises it (``NotRunResult.value``). It cannot live here —
``otto.result`` would have to import this module to raise it, and this module
imports ``CommandResult`` from ``otto.result``, so the edge is a circular
import at runtime and a layering inversion in ``tach.toml`` (``otto.host``
depends on ``otto.result``, never the reverse).

The two classes are PEERS, not parent and child — neither implies the other,
and a caller catching one must not silently catch the other. Both keep
``RuntimeError`` so the ``except (ValueError, RuntimeError)`` clauses in
``otto.cli`` (where typer's vendored click makes ``typer.Exit`` a
``RuntimeError`` too) keep working unchanged.

:func:`exec_or_raise` is the control flow those types exist to express. It
takes the two classes as arguments rather than hard-coding them, because the
sequence is what repeats and the taxonomy is the caller's: ``otto.link`` names
its own :class:`~otto.link.manage.LinkHostUnreachableError` /
:class:`~otto.link.manage.LinkCommandFailedError` so an ``except`` around an
impair can mean "link work failed" rather than "some host call failed", and it
gets the same three checks by passing them in. Its siblings share the same
core: :func:`exec_or_unreachable` (the same run, a non-ok result returned),
:func:`root_run_or_unreachable` (a privileged command sent as given,
elevated unless the session already acts as root) and :func:`root_run`
(that, a timeout returned).
"""

import enum
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ..errors import OttoError
from ..logger.mode import LogMode
from ..result import CommandResult
from .identity import is_root

if TYPE_CHECKING:
    # Typing only, and deliberately so: `otto.host.userland` imports THIS
    # module at runtime to raise the class below, so a runtime import back
    # would be a cycle. `UnsupportedOnUserlandError.for_gap` reads four
    # attributes off the record and never constructs one.
    from .userland import Gap


class HostUnreachableError(OttoError, RuntimeError):
    """Transport to the host failed, or the command never completed.

    A timeout belongs here rather than with :class:`HostCommandError`: the
    command produced no verdict, so nothing about the system under test was
    learned.
    """


class HostCommandError(OttoError, RuntimeError):
    """The command ran on the host and reported failure.

    The host was reachable and answered; the answer was a non-ok status (or
    output the caller cannot use).
    """


class PasswordRefusedError(HostCommandError):
    """The elevation prompt came back after otto answered it: the password was refused.

    Raised instead of typing the password a second time or waiting out the
    command's timeout. The session has interrupted the command and probed
    the shell before this is raised; a session the probe could not confirm
    is discarded, so the next command on the host gets a working shell.
    The message names the account and the host, never the password.
    """


class StagingRefusedError(HostCommandError):
    """otto will not stage docker files on, or run them from, a directory it cannot trust.

    Raised before anything is written or run there: the private staging root
    is not a directory owned by the session's account with mode 0700, ``/tmp``
    is not root-owned and sticky, older staging could have been written by
    another account, or the parent's commands and uploads would run as two
    different accounts. The message names the path or the accounts, and the
    fix.
    """


class RawLandingError(OttoError, RuntimeError):
    """A framed operation was attempted in a ``raw`` landing.

    The ``raw`` landing dialect has no command framing at all: it exists so a
    session-setup hook can drive a console whose landing state answers no
    frame (a boot menu, an autoboot countdown) with ``send``/``expect``
    alone, and then enter the target frame. ``run()`` before ``enter_frame()``
    is the hook author's bug, and this names it rather than timing out.
    """


class SessionSetupError(OttoError, ConnectionError):
    """A session setup hook failed, or left no shell that answers the frame.

    A ``ConnectionError`` so callers that already treat "the session never
    came up" as unreachable keep doing so; ``SessionManager`` catches it by
    name ahead of its transport-race retry, because a hook failure is not a
    race and must not consume the retry.
    """


class HostKeyChangedError(OttoError, ConnectionError):
    """The known-hosts file holds a different key for this host ID than the one offered.

    Raised by :meth:`~otto.host.known_hosts.KnownHosts.record` when, under the
    file lock, another process has recorded a different key for the alias
    since this connection checked. Its message states the fact only; the
    advice is :class:`HostKeyRefusedError`'s, which every connection turns it
    into. A ``ConnectionError`` so callers that treat "the session never came
    up" as unreachable keep doing so.
    """


class HostKeyNotCertifiedError(OttoError, ConnectionError):
    """The host is known by its certificate authority, and the key about to be recorded is plain.

    Raised by :meth:`~otto.host.known_hosts.KnownHosts.record` when, under the
    file lock, an ``@cert-authority`` line for the host ID has appeared since
    this connection checked: a host known by its CA is never recorded by a
    plain key. Its message states the fact only; the advice is
    :class:`HostKeyRefusedError`'s. A ``ConnectionError`` so callers that
    treat "the session never came up" as unreachable keep doing so.
    """


class HostKeyRevokedError(OttoError, ConnectionError):
    """The known-hosts file revokes the key this connection is about to record.

    Raised by :meth:`~otto.host.known_hosts.KnownHosts.record`, which judges
    the file through the same lookup as the handshake: a key revoked after the
    handshake read the file (by another process, or by hand) is refused before
    any credential is sent, never recorded. A ``ConnectionError`` so callers
    that treat "the session never came up" as unreachable keep doing so.
    """


class KnownHostsUnwritableError(OttoError, ConnectionError):
    """A key had to be recorded, or an entry removed, and the known-hosts file cannot be written.

    The message names the file and both fixes: point ``known_hosts`` at a
    writable path, or turn checking off with ``verify_host_key = false``. A
    read-only file whose entries already match is never written, so it never
    raises this.
    """


class KnownHostsError(OttoError, ConnectionError):
    """The known-hosts file cannot be read or holds a line otto does not support.

    An X.509 subject line is refused with its line number, as is a line
    asyncssh cannot parse; the message names the file.
    """


def forget_key_command(host_id: str) -> str:
    """Return ``otto host <id> forget-key``, the command that removes *host_id*'s entries.

    The one place the command is spelled, for every message that advises it.
    """
    return f"otto host {host_id} forget-key"


@dataclass(frozen=True)
class ForgetResult:
    """What :meth:`~otto.host.known_hosts.KnownHosts.forget` did, and the message that reports it.

    ``str()`` of this is what both ``otto host <id> forget-key`` and a
    library caller show, so the wording is decided here once. It is a
    :class:`~otto.result.CliReport`: the CLI prints that same text.

    It lives beside :func:`forget_key_command` rather than in
    :mod:`otto.host.known_hosts` because ``UnixHost.forget_key`` names it in
    its signature, which the CLI resolves at runtime, and that module imports
    asyncssh. Import it from :mod:`otto.host.known_hosts`.
    """

    alias: str
    """The host ID whose entries were removed."""

    removed: int
    """How many lines were removed."""

    kept_shared: list[str]
    """Lines that name the host but were left in place, because they also name
    other hosts (or a wildcard), or are ``@cert-authority`` / ``@revoked`` lines."""

    path: Path
    """The known-hosts file that was edited."""

    @override
    def __str__(self) -> str:
        if self.removed:
            noun = "entry" if self.removed == 1 else "entries"
            lines = [f"removed {self.removed} {noun} for {self.alias} from {self.path}"]
        else:
            lines = [f"no entry for {self.alias} in {self.path}"]
        lines.extend(f"kept: {line}" for line in self.kept_shared)
        return "\n".join(lines)

    def __otto_cli_report__(self) -> str:
        """Return :func:`str` of this result, the text ``forget-key`` prints."""
        return str(self)


class HostKeyRefusal(enum.Enum):
    """Why a host key was refused, which decides the advice the refusal gives."""

    CHANGED = "changed"
    """The file holds a different key for the host ID; forget-key is the fix
    when the host's key really changed."""

    REVOKED = "revoked"
    """The key is revoked in the file. forget-key keeps ``@revoked`` lines, so
    it is no fix and is not advised."""

    NOT_CERTIFIED = "not certified"
    """An ``@cert-authority`` line covers the host ID, so only certificates that
    CA signed are trusted for it, and the host presented a plain key. forget-key
    keeps ``@cert-authority`` lines, so it is no fix and is not advised."""

    UNTRUSTED = "untrusted"
    """The key is not trusted for another reason (a certificate no
    ``@cert-authority`` line trusts); the reason says which."""

    FILE = "file"
    """The file cannot be read or written; the reason names the fix."""


class HostKeyRefusedError(OttoError, ConnectionError):
    """otto refused an SSH connection over its host key, before any credential was sent.

    The one error every host-key refusal becomes, whatever caused it: a key
    that differs from the file's entry for the host ID (including a key of
    another type), a revoked key, a certificate no ``@cert-authority`` line
    trusts, a key that had to be recorded in a file that cannot be written,
    or a file that cannot be read. Its message is built here and nowhere
    else, so ``otto run``, ``otto test``, the host verbs and the survey all
    show the same words.

    *host_id* is the host whose key was refused: on a hopped connection whose
    hop leg failed, the hop's ID, not the target's. A ``ConnectionError`` so
    callers that treat "the session never came up" as unreachable keep
    doing so.
    """

    def __init__(
        self, *, host_id: str, address: str, path: str, reason: str, kind: HostKeyRefusal
    ) -> None:
        self.host_id = host_id
        """The otto host ID whose key was refused."""
        self.address = address
        """The address otto dialled for that host."""
        self.path = path
        """The known-hosts file the check read."""
        self.reason = reason
        """What was wrong, in asyncssh's or the known-hosts store's words."""
        self.kind = kind
        """Why the key was refused, which decides the advice in the message."""
        self.hint = forget_key_command(host_id)
        """The command that removes the host's entries so the next connection
        records its new key; the message advises it only for a changed key."""
        message = f"SSH host key for {host_id} ({address}) refused: {reason}"
        if kind is HostKeyRefusal.CHANGED:
            message += (
                f". {path} holds a different key for {host_id}; if the host's key "
                f"really changed, run `{self.hint}`"
            )
        elif kind is HostKeyRefusal.REVOKED:
            message += f". The key is revoked in {path}"
        elif kind is HostKeyRefusal.NOT_CERTIFIED:
            message += (
                f". An @cert-authority line in {path} covers {host_id}, but the host "
                "presented a plain key, not a certificate signed by that CA; have the "
                "host present a certificate signed by that CA, or remove the "
                "@cert-authority line to accept a new key on first use again"
            )
        super().__init__(message)


class ConsoleError(OttoError, ConnectionError):
    """The serial console behind a telnet server did not reach a login otto can perform.

    Every message names the host, the server host ID, the port and the dial
    mode, then says which of five distinct states the line was in — stuck at
    a password prompt, busy (the server closed the connection), silent,
    already logged in (quoting the last bytes seen, ANSI-stripped), or a
    refused login — because each has a different fix. A ``ConnectionError``
    so callers that treat "the session never came up" as unreachable keep
    doing so.
    """


class ContainerNotRunningError(OttoError, RuntimeError):
    """A declared container host was used, and no running container for it could be found.

    otto never starts a container because it was used: a container host
    registered from the declared settings (a placeholder, its container id not
    yet known) asks its parent for the running container with a ``docker ps``
    probe. It raises this for two outcomes: nothing is running (the probe
    answered with no id), or the probe failed (it ran on the parent and
    exited non-zero; the message then carries Docker's own text). A parent
    that could not complete the probe at all is a transport failure and
    raises :class:`HostUnreachableError` instead. The message names the
    container and each explicit way to
    bring its stack up: ``otto docker compose up`` from the shell, or
    :func:`~otto.docker.deployment.deploy` / :func:`~otto.docker.compose.compose_up`
    from Python.

    A ``RuntimeError``, the root every failure on this path already had, so an
    existing ``except RuntimeError`` still catches it.
    """


class MountNotFoundError(OttoError, ValueError):
    """A container path could not be translated to a parent-side path.

    ``ValueError`` rather than ``LookupError``: :func:`otto.host.mount.translate`
    already raises a plain ``ValueError`` for a path outside the mount it was
    given, and rooting this the same way lets a caller's single ``except
    ValueError`` catch both failure modes of the same translation.
    """


# Spec: docs/superpowers/specs/2026-08-11-busybox-host-support-design.md
class UnsupportedOnUserlandError(OttoError, RuntimeError):
    """The host's userland provides no way to do what otto was asked to do.

    A THIRD outcome, and the reason it is neither
    :class:`HostUnreachableError` nor :class:`HostCommandError`: nothing was
    attempted, so there is neither a transport problem nor a verdict about the
    system under test. otto knows in advance that the command it would emit
    cannot work — a host that resolved ``elevation`` to ``"none"`` has no sudo
    and no su — and says so at the call site instead of shipping a command that
    fails somewhere less informative.

    Raising rather than degrading is the whole point on a privilege path. A
    quiet fallback to sudo on a host without sudo LOOKS like it worked: the
    wrapped command runs, the shell answers ``sudo: not found``, and the
    failure is attributed to whatever the caller was actually doing.

    ``RuntimeError``, matching :class:`HostCommandError` and
    :class:`HostUnreachableError`, so the ``except (ValueError, RuntimeError)``
    clauses that already bracket host work keep catching it.

    THE REGISTRY HAS LANDED, and this class stayed put. An earlier version of
    this note said the class was EXPECTED TO MOVE, next to the userland gap
    registry described in the BusyBox host-support spec. What
    the registry actually needed was a RENDERING SURFACE, which is
    :meth:`for_gap` below, and moving the class to get one would have dragged
    every ``except`` clause and every import in this package along with it for
    no gain. So the dependency runs the other way: ``otto.host.userland``
    imports this class to raise it, and this class knows a
    :class:`~otto.host.userland.Gap` only as a shape it can format.

    Both ways of raising it are legitimate and they answer different
    questions. :meth:`for_gap` renders what otto MEASURED about a whole class
    of userland ("BusyBox has no ``shutdown`` applet"). A caller's own
    f-string renders what THIS host answered a probe ("resolved
    ``elevation='none'``") — see ``PosixPrivilege._elevate`` and
    ``ShellFileTransfer.run_put``, which are probe-driven and stay that way.
    A registry record cannot say the second thing and a probe cannot say the
    first.
    """

    @classmethod
    def for_gap(
        cls, gap: "Gap", *, host: str = "", attempted: str = "", observed: str = ""
    ) -> "UnsupportedOnUserlandError":
        """Build the refusal for a declared *gap*, rendered from the record.

        The spec's first consumer of the registry: "a named
        ``UnsupportedOnUserlandError`` renders its message from the record
        (surface, why, docs anchor) instead of surfacing a bare ``sudo: not
        found``".

        Everything in the message comes from the record except *host*,
        *attempted* and *observed*, which the caller supplies because the
        record cannot know them. That is the point — an operator who hits this
        gets the same four facts (what broke, why, what proved it, where to
        read more) wherever it fires, and a docs page rendered from the same
        record cannot disagree with it.

        ``Nothing was attempted`` leads, because it is the one thing this
        exception means that the other two host errors do not: no command was
        sent, so nothing was learned about the system under test.

        *observed* REPLACES THAT LEAD, and exists because one caller cannot
        say it. ``otto.host.transfer.sftp.open_sftp_or_attribute`` translates a
        failure that has ALREADY HAPPENED — whether the device serves sftp is
        answerable only by opening the subsystem, so there is no fact to
        pre-check and the operation is its own probe. For that site "nothing
        was attempted" would be false in the message's first clause, so the
        caller passes what it watched instead and the record supplies the rest
        unchanged. Empty for every other caller, which is why every existing
        message is byte-identical to what it was before this parameter
        existed. *attempted* is dropped when *observed* is given: the two
        answer the same question (what otto was doing) and *observed* answers
        it with an outcome attached.

        TAKES A ``measured-broken`` RECORD ONLY, and does not check. The
        message says "otto has measured ``<surface>`` as broken" and prints
        ``MEASURED: <measured_on>``, both of which are false for an
        ``untested`` record -- whose ``measured_on`` is empty by the
        :class:`~otto.host.userland.Gap` invariant, so the rendering would read
        ``MEASURED: .`` The firing rule lives in
        :func:`~otto.host.userland.refuse_if_gapped`, which is what decides
        that a record refuses at all, and every raise today goes through it, so
        this is a precondition on direct callers rather than a reachable bug.
        A caller doing its own dispatch must check
        :attr:`~otto.host.userland.Gap.refuses` first.
        """
        who = f"{host}: " if host else ""
        what = f" ({attempted})" if attempted else ""
        lead = observed or f"nothing was attempted{what}"
        return cls(
            f"{who}{lead} — otto has measured `{gap.surface}` as "
            f"broken on this class of userland. {gap.reason}. MEASURED: {gap.measured_on}. "
            f"QUEUED FOR: {gap.queued_for}. See {gap.docs_anchor}."
        )


class CoverageToolMissingError(OttoError, RuntimeError):
    """The gcov the coverage data needs is not installed on the otto host.

    Raised before lcov runs, for data whose stamp needs a reader nothing
    configured covers: a GCC major other than the system gcov's, read by
    ``gcov-<major>``, or one of llvm-cov's stamps (``402*``, ``408*``,
    ``B11*``), read by ``llvm-cov gcov``. For a product the
    message names the host, the product, the stamp, the tool and the package
    that ships it; for a unit directory it names the tier, the directory and
    the tier's ``toolchain`` setting, as it does for a unit capture's default
    ``gcov`` or ``lcov`` missing from ``PATH``. Nothing falls through to a
    tool geninfo would refuse a moment later.

    It lives here, not beside ``otto.coverage.errors``'
    ``CoverageToolVersionError``, because the raiser is in ``otto.host`` and
    ``otto.coverage`` depends on ``otto.host``, never the reverse.
    """


async def _reached(
    host: Any, cmd: str, call: "Awaitable[Any]", unreachable: type[OttoError]
) -> Any:
    """Await *call*, the run of *cmd* on *host*; a transport failure raises *unreachable*.

    The first of the three checks, shared by every runner here: the
    transport can fail before anything runs.
    """
    try:
        return await call
    except (OSError, ConnectionError) as e:
        raise unreachable(f"host {host.id!r} unreachable running {cmd!r}: {e!r}") from e


def _finished(
    host: Any, cmd: str, result: CommandResult, timeout: float, unreachable: type[OttoError]
) -> CommandResult:
    """Return *result*, unless *cmd* hit *timeout* without completing: that raises *unreachable*."""
    if result.timed_out:
        raise unreachable(
            f"host {host.id!r} unreachable running {cmd!r}: timed out after {timeout}s"
        )
    return result


async def exec_or_unreachable(
    host: Any,
    cmd: str,
    *,
    timeout: float,
    log: LogMode = LogMode.QUIET,
    unreachable: type[OttoError] = HostUnreachableError,
) -> CommandResult:
    """Run one *cmd* on *host*; only a down host or a timed-out command raises.

    A command that completes with a non-ok status is RETURNED: for a caller
    whose failure is a verdict rather than the end of the run.
    :func:`exec_or_raise` is this plus the non-ok check.

    Raises:
        HostUnreachableError: the transport raised, or the command hit
            *timeout* without completing. (Or *unreachable*, if given.)
    """
    result = await _reached(host, cmd, host.exec(cmd, timeout=timeout, log=log), unreachable)
    return _finished(host, cmd, result, timeout, unreachable)


async def exec_or_raise(
    host: Any,
    cmd: str,
    *,
    timeout: float,
    log: LogMode = LogMode.QUIET,
    unreachable: type[OttoError] = HostUnreachableError,
    failed: type[OttoError] = HostCommandError,
) -> CommandResult:
    """Run one read-only *cmd* on *host*, raising instead of returning a bad result.

    Three checks in the order the information arrives, and the order is the
    substance: the transport can fail before anything runs, the command can
    fail to finish, and only then can it finish badly. Collapsing the first two
    into the third is what produced the review's headline defect — a reachable
    host reported as unreachable.

    *unreachable* and *failed* let a package raise its OWN pair while keeping
    this sequence and these messages; the defaults are the shared ones.

    Raises:
        HostUnreachableError: the transport raised, or the command hit
            *timeout* without completing. (Or *unreachable*, if given.)
        HostCommandError: the command completed with a non-ok status. (Or
            *failed*, if given.)
    """
    result = await exec_or_unreachable(host, cmd, timeout=timeout, log=log, unreachable=unreachable)
    if not result.is_ok:
        raise failed(f"{cmd!r} failed on {host.id!r}: {result.msg or result.value}")
    return result


async def root_run(
    host: Any,
    cmd: str,
    *,
    timeout: float,
    log: LogMode = LogMode.QUIET,
    unreachable: type[OttoError] = HostUnreachableError,
    named: str | None = None,
) -> CommandResult:
    """Run privileged *cmd* on *host*'s shell, sudo'd unless the session already acts as root.

    *cmd* is sent exactly as given: otto elevates the caller's command, never
    a wrapper shell of its own, so a sudoers rule naming that command keeps
    matching. A caller whose command is a compound script (a ``;`` list, a
    pipe, a redirection) makes it one word itself first
    (:func:`otto.check.fingerprint.one_command`). Elevated exactly when the
    session user is not root (:func:`~otto.host.identity.is_root`).

    *named* is the command an error names, when the caller wrapped its own
    text into *cmd*; it defaults to *cmd*. A non-ok result and a timeout are
    both RETURNED, for a probe that reads a timeout as its answer;
    :func:`root_run_or_unreachable` raises on the timeout.

    Raises:
        HostUnreachableError: the transport raised. (Or *unreachable*, if given.)
    """
    elevate = not is_root(host.current_user)
    results = await _reached(
        host,
        cmd if named is None else named,
        host.run(cmd, sudo=elevate, timeout=timeout, log=log),
        unreachable,
    )
    return results[0]


async def root_run_or_unreachable(
    host: Any,
    cmd: str,
    *,
    timeout: float,
    log: LogMode = LogMode.QUIET,
    unreachable: type[OttoError] = HostUnreachableError,
    named: str | None = None,
) -> CommandResult:
    """:func:`root_run`, where a command still running after *timeout* also raises.

    A non-ok result is still returned: a rejected privileged command is a
    verdict the caller judges, not a transport failure.

    Raises:
        HostUnreachableError: the transport raised, or the command hit
            *timeout* without completing. (Or *unreachable*, if given.)
    """
    label = cmd if named is None else named
    result = await root_run(
        host, cmd, timeout=timeout, log=log, unreachable=unreachable, named=label
    )
    return _finished(host, label, result, timeout, unreachable)
