"""
Docker container host.

A :class:`~otto.host.docker_host.DockerContainerHost` satisfies the otto
:class:`~otto.host.host.Host` protocol by
delegating most operations through a *parent* host that runs the docker
daemon. ``exec`` becomes ``parent.exec("docker exec ...")``;
``get`` / ``put`` are two-step ``docker cp`` via the parent's filesystem,
staged under the parent's private staging root, which needs a
:class:`~otto.host.unix_host.UnixHost` parent (its login names the root);
``login`` opens a PTY-backed ``docker exec -it`` over the parent's
existing SSH connection.

``run`` (and ``open_session`` / ``send`` / ``expect``) use a persistent
``docker exec -it <ctr> sh`` session multiplexed on the parent's SSH
connection — shell state (``cd``, env vars, shell vars) persists across
calls, matching :class:`~otto.host.local_host.LocalHost` and
:class:`~otto.host.unix_host.UnixHost`. ``exec`` stays stateless and concurrent-safe.

Persistent-shell support requires an SSH-based :class:`~otto.host.unix_host.UnixHost` parent.
Local-host parents and telnet parents are rejected at session-open time —
the per-call ``exec`` path still works against any parent.
"""

import asyncio
import re
import shlex
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import (
    dataclass,
    field,
    replace,
)
from pathlib import Path
from typing import Annotated

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import Arg, Exclude, Opt, Status, cli_exposed
from .capability_grid import HostCapabilities, SessionIdentity, UserSupport
from .connections import teardown_step
from .errors import (
    ContainerNotRunningError,
    HostUnreachableError,
    MountNotFoundError,
    StagingRefusedError,
)
from .file_ops import PosixFileOps
from .host import (
    CONCURRENT_HELP,
    DEFAULT_COMMAND_TIMEOUT,
    BaseHost,
    CredentialExpect,
    Host,
    ShellCommand,
    _validate_user,
    is_dry_run,
    refuse_declined_fact,
)
from .identity import ROOT_LOGIN
from .mount import Mount, mount_for, mount_for_parent, translate
from .privilege import PosixPrivilege
from .session import Expect, SessionManager, ShellSession, _DockerSshSession
from .shell_quote import path_operand, shell_operand
from .transfer.registry import connection_protocol


def stack_containers_command(
    project: str,
    *,
    all: bool = False,  # noqa: A002 -- docker's flag name
) -> str:
    """Build the ``docker ps`` command listing compose project *project*'s container ids.

    Matches by the compose project label. *all* adds ``-a`` so a stopped
    container is listed too.
    """
    flags = "-aq" if all else "-q"
    return f"docker ps {flags} --filter label=com.docker.compose.project={shlex.quote(project)}"


def compose_container_probe(
    project: str,
    service: str,
    *,
    all: bool = False,  # noqa: A002 -- docker's flag name
) -> str:
    """Build the ``docker ps`` command that finds one compose service's container id.

    :func:`stack_containers_command`, narrowed by the service label.
    ``oneoff=False`` keeps a ``docker compose run`` one-off, which carries the
    same two labels, from being taken for the service's container. *all* adds
    ``-a`` so a stopped container is found too.
    """
    return (
        f"{stack_containers_command(project, all=all)} "
        f"--filter label=com.docker.compose.service={shlex.quote(service)} "
        "--filter label=com.docker.compose.oneoff=False"
    )


def _caller_pairs(
    expects: "list[Expect | CredentialExpect] | None", host: str
) -> "list[Expect] | None":
    """Return *expects* as the caller's own pairs, refusing an elevation answer.

    A container's ``exec`` never builds one (its ``sudo`` is ``-u root``), and
    the parent's public ``exec`` takes caller pairs only. One arriving here
    means a new route started elevating through the container without a way
    to carry its answer, which must fail loudly rather than drop it.
    """
    if expects is None:
        return None
    pairs: list[Expect] = []
    for item in expects:
        if isinstance(item, CredentialExpect):
            raise TypeError(
                f"{host}: an elevation password answer cannot be forwarded through "
                "the parent's exec; a container elevates with `docker exec -u root`"
            )
        pairs.append(item)
    return pairs


# ---------------------------------------------------------------------------
# The private docker staging root on a parent
# ---------------------------------------------------------------------------

_STAGING_ROOT_PREFIX = "/tmp/otto-docker-"  # noqa: S108 — deliberate staging path

_TRANSFER_STAGING = ".transfer"
"""The directory under the staging root that holds each container transfer's files.

A leading dot, which no compose project name can start with
(``[a-z0-9][a-z0-9_-]*``), so a project's cleanup never removes a transfer in
flight."""

_PRIVATE_MODE = 0o700
"""The staging root's exact mode: its owner alone may list, read or write it."""

_OWNER_SEARCH = 0o100
"""The mode bit that lets a directory's owner reach what is under it."""


def staging_root(login: str) -> Path:
    """Return *login*'s private docker staging root on a parent: ``/tmp/otto-docker-<login>``.

    Pure, so a dry run can name it without contacting the parent. The one
    place the root's path is spelled: every docker file otto stages on a
    parent lives under it (build contexts and archives, compose files and
    their sidecars, the env launcher, each container transfer), and
    :func:`ensure_private_root` creates or verifies it before anything is
    written there. The root is per account, so two people who share one
    login on a parent share one root.

    Raises:
        ~otto.host.errors.StagingRefusedError: *login* is empty, or holds a
            ``/``, whitespace or a control character, so it cannot be one
            path segment.
    """
    if not login or "/" in login or any(c.isspace() or not c.isprintable() for c in login):
        raise StagingRefusedError(
            f"the login {login!r} cannot name a docker staging directory: it must be "
            "non-empty, with no '/', whitespace or control character"
        )
    return Path(f"{_STAGING_ROOT_PREFIX}{login}")


def parent_staging_root(parent: "Host") -> Path:
    """Return the private docker staging root of *parent*'s login, without contacting it.

    The login is the account the parent's commands run as, read from its
    creds. Commands and uploads must run as that one account: the root is
    mode 0700, so a file uploaded as one account into a root another account
    created could be neither written nor read back. A parent reached through
    a login proxy (or with a per-protocol cred for its uploads) is therefore
    refused, naming both accounts.

    Raises:
        ~otto.host.errors.StagingRefusedError: *parent* is not a unix host,
            no cred names its command account, its commands and its
            connection or uploads would run as different accounts, or the
            login cannot name a directory (:func:`staging_root`).
    """
    # Function-scope: unix_host is the heavier module, and only a staging caller needs it.
    from .unix_host import UnixHost

    if not isinstance(parent, UnixHost):
        raise StagingRefusedError(
            f"{parent.id}: docker files are staged only on a unix host parent, whose "
            f"login names its private staging root; this parent is a {type(parent).__name__}"
        )
    connections = parent._connections  # noqa: SLF001 — intra-package: the parent's own creds
    session = connections.login_target
    if not session:
        raise StagingRefusedError(
            f"{parent.id}: no cred names the account its commands run as, so its "
            f"private docker staging root has no name; declare one in its creds"
        )
    # A backend with no connection of its own uploads through the session,
    # whose connection authenticates as the term's cred.
    upload_protocol = connection_protocol(parent.transfer) or parent.term
    for protocol, role in [(parent.term, "it logs in"), (upload_protocol, "it uploads")]:
        cred = connections.transport_cred_for(protocol)
        login = cred.login if cred is not None else ""
        if login != session:
            raise StagingRefusedError(
                f"{parent.id}: docker staging needs commands and uploads to run as one "
                f"account, but its commands run as {session!r} while {role} as "
                f"{login!r}. Declare {session!r}, the account that runs docker, as a "
                f"direct cred on {parent.id}, with no login proxy."
            )
    return staging_root(session)


@dataclass(frozen=True)
class PathOwnership:
    """One path's type, owner and permission bits, as the parent reported them."""

    kind: str
    """``directory``, ``regular file``, ``symbolic link``, or another file type's name."""

    uid: int
    """The owner's numeric UID."""

    mode: int
    """The permission bits, the setuid, setgid and sticky bits included."""


@dataclass(frozen=True)
class OwnershipProbe:
    """What one :func:`probe_ownership` read: the session's UID and each path's ownership."""

    uid: int
    """The numeric UID the parent's command session runs as."""

    paths: "dict[Path, PathOwnership | None]"
    """Each readable path's ownership; ``None`` for a path that is not there."""

    unreadable: "list[Path]"
    """Paths that are there, or may be, but whose type, owner and mode could not be read.

    Unknown is never read as absent: an unsearchable parent directory hides a
    path exactly as a missing one would, so only a searchable parent that
    lacks the path makes it missing."""

    def missing(self, path: Path) -> bool:
        """Return whether *path* reads as absent (not merely unreadable).

        A path whose parent directory is itself invisible (behind a
        directory the session cannot search) reads absent too, so a caller
        takes a path for absent only after trusting every directory above it
        (:func:`~otto.docker.staging.trusted_legacy_staging`).
        """
        return path not in self.unreadable and self.paths.get(path) is None

    def problem(self, path: Path, *, kind: str, private: bool) -> "str | None":
        """Return why *path* cannot be trusted, or ``None`` when it can.

        Trusted means its type, owner and mode could be read, it is there, it
        is a *kind* (a symbolic link never is), the session's UID owns it, and
        its mode is exactly 0700 when *private*, otherwise writable by neither
        group nor others and, for a directory, searchable by its owner (an
        unsearchable directory hides what is under it, so nothing below it
        could be read).
        """
        if path in self.unreadable:
            return "its type, owner and mode cannot be read"
        found = self.paths.get(path)
        if found is None:
            return "it does not exist"
        if found.kind != kind:
            return f"it is a {found.kind}, not a {kind}"
        if found.uid != self.uid:
            return f"it is owned by UID {found.uid}, not by this session's UID {self.uid}"
        if private and found.mode != _PRIVATE_MODE:
            return f"its mode is {found.mode:04o}, not {_PRIVATE_MODE:04o}"
        if not private and found.mode & 0o022:
            return f"its mode {found.mode:04o} lets group or others write to it"
        if kind == "directory" and not found.mode & _OWNER_SEARCH:
            return f"its mode {found.mode:04o} does not let its owner search it"
        return None


# One line per path. A path that is there (a dangling link included) is read
# by `stat -c`, or by `ls -ldn` where `stat` is missing or refuses `-c` (it is
# optional in a BusyBox build); neither follows a symbolic link, so a link
# reports itself. `missing` only when the parent directory can be searched and
# lacks the path, or is itself absent; an unsearchable parent, or a path
# neither tool can read, is `unreadable`, never `missing`.
_OWNERSHIP_OF_P = (
    'd="${p%/*}/"; '
    'if [ -e "$p" ] || [ -L "$p" ]; then '
    'stat -c \'%F %u %a\' "$p" 2>/dev/null || ls -ldn "$p" 2>/dev/null || echo unreadable; '
    'elif [ -d "$d" ] && [ -x "$d" ]; then echo missing; '
    'elif [ -e "$d" ]; then echo unreadable; '
    "else echo missing; fi"
)
_STAT_ANSWER = re.compile(r"(?P<kind>[a-z][a-z ]*?) (?P<uid>\d+) (?P<mode>[0-7]{1,4})")
_LS_ANSWER = re.compile(r"(?P<type>[-bcdlps])(?P<perms>[-rwxsStT]{9})\S*\s+\d+\s+(?P<uid>\d+)\s")
_LS_KIND = {
    "-": "regular file",
    "d": "directory",
    "l": "symbolic link",
    "b": "block special file",
    "c": "character special file",
    "p": "fifo",
    "s": "socket",
}
_LS_SPECIAL_BITS = [(2, "sS", 0o4000), (5, "sS", 0o2000), (8, "tT", 0o1000)]


def _ownership_command(paths: "list[Path]", create: "Path | None") -> str:
    """Build :func:`probe_ownership`'s command: an optional create, the UID, a line per path."""
    made = f"mkdir -m {_PRIVATE_MODE:04o} {path_operand(create)} 2>/dev/null; " if create else ""
    operands = " ".join(path_operand(p) for p in paths)
    return f"{made}id -u; for p in {operands}; do {_OWNERSHIP_OF_P}; done"


def _read_ownership_line(line: str) -> "PathOwnership | None":
    """Read one ``stat -c '%F %u %a'`` or ``ls -ldn`` line, or ``None`` if it is neither."""
    stat_form = _STAT_ANSWER.fullmatch(line)
    if stat_form is not None:
        kind = stat_form["kind"]
        return PathOwnership(
            kind="regular file" if kind == "regular empty file" else kind,
            uid=int(stat_form["uid"]),
            mode=int(stat_form["mode"], 8),
        )
    ls_form = _LS_ANSWER.match(line)
    if ls_form is None:
        return None
    perms = ls_form["perms"]
    # r, w, x and the lowercase s/t (special bit AND execute) grant the bit at
    # their position; '-' and the uppercase S/T (special bit only) do not.
    mode = sum(1 << (8 - i) for i, mark in enumerate(perms) if mark not in "-ST")
    mode |= sum(bit for i, marks, bit in _LS_SPECIAL_BITS if perms[i] in marks)
    return PathOwnership(kind=_LS_KIND[ls_form["type"]], uid=int(ls_form["uid"]), mode=mode)


def _read_ownership(output: str, paths: "list[Path]") -> "OwnershipProbe | None":
    """Read :func:`_ownership_command`'s answer, or ``None`` when it is not one."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != len(paths) + 1 or not lines[0].isdigit():
        return None
    found: "dict[Path, PathOwnership | None]" = {}
    unreadable: "list[Path]" = []
    for path, line in zip(paths, lines[1:], strict=True):
        if line == "missing":
            found[path] = None
            continue
        if line == "unreadable":
            unreadable.append(path)
            continue
        ownership = _read_ownership_line(line)
        if ownership is None:
            return None
        found[path] = ownership
    return OwnershipProbe(uid=int(lines[0]), paths=found, unreadable=unreadable)


async def probe_ownership(
    parent: "Host", paths: "list[Path]", *, create: "Path | None" = None
) -> OwnershipProbe:
    """Read the session's UID and each of *paths*' type, owner and mode, in one command.

    The one stat-and-trust read behind both :func:`ensure_private_root` and
    the read-back of older staging
    (:func:`~otto.docker.staging.trusted_legacy_staging`). *create*, when
    given, is first made with ``mkdir -m 0700``: atomic, its mode exact
    whatever the umask, and a no-op when the path exists, so what is read
    afterwards is the same whether this call made it or found it.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run.
        ~otto.host.errors.HostUnreachableError: the command never completed.
        ~otto.host.errors.StagingRefusedError: the answer is not one otto can
            read, so nothing about the paths is known.
    """
    where = ", ".join(str(p) for p in paths)
    result = await parent.exec(_ownership_command(paths, create))
    refuse_declined_fact(result, asked=f"probe_ownership({where})")
    if result.timed_out:
        raise HostUnreachableError(
            f"{parent.id}: reading the type, owner and mode of {where} never completed"
        )
    probe = _read_ownership(str(result.value), paths)
    if probe is None:
        raise StagingRefusedError(
            f"{parent.id}: could not read the type, owner and mode of {where}; "
            f"the parent answered {str(result.value).strip()!r}"
        )
    return probe


def _shared_tmp_problem(probe: OwnershipProbe, tmp: Path) -> "str | None":
    """Return why *tmp* cannot hold a private root, or ``None`` when it is root-owned and sticky."""
    if tmp in probe.unreadable:
        return "cannot be read (its type, owner and mode)"
    found = probe.paths.get(tmp)
    if found is None:
        return "does not exist"
    if found.kind != "directory":
        return f"is a {found.kind}, not a directory"
    if found.uid != 0:
        return f"is owned by UID {found.uid}, not by root"
    if not found.mode & 0o1000:
        return f"does not have the sticky bit set (its mode is {found.mode:04o})"
    return None


async def ensure_private_root(parent: "Host") -> Path:
    """Create *parent*'s private docker staging root, or verify the one there, and return it.

    Runs before anything is written under the root, and before staging there
    is read back. The root (:func:`parent_staging_root`) is made with
    ``mkdir -m 0700``. Whether this call made it or found it, otto goes on
    only if ``/tmp`` is a root-owned directory with the sticky bit set (so no
    other account can rename or replace the root) and the root is a directory,
    not a symbolic link, owned by the session's UID with mode exactly 0700.
    Anything else is refused, naming the path; otto never repairs or adopts
    it. Interference by the same account or by root is out of scope.

    Raises:
        ~otto.host.errors.StagingRefusedError: the parent or its root cannot
            be trusted (see above and :func:`parent_staging_root`).
        ~otto.host.errors.HostUnreachableError: the check never completed.
        ~otto.result.CommandNotRunError: this is a dry run.
    """
    root = parent_staging_root(parent)
    tmp = root.parent
    probe = await probe_ownership(parent, [tmp, root], create=root)
    shared = _shared_tmp_problem(probe, tmp)
    if shared is not None:
        raise StagingRefusedError(
            f"{parent.id}: {tmp} {shared}, so another account could rename or replace "
            f"{root}; otto stages no docker files there"
        )
    if probe.missing(root):
        raise StagingRefusedError(
            f"{parent.id}: the docker staging root {root} could not be created"
        )
    problem = probe.problem(root, kind="directory", private=True)
    if problem is not None:
        raise StagingRefusedError(
            f"{parent.id}: the docker staging root {root} cannot be trusted: {problem}. "
            f"otto never repairs or adopts it; remove it on {parent.id} (as its owner or "
            f"as root) and run again"
        )
    return root


@dataclass(slots=True, kw_only=True)
class DockerContainerHost(PosixPrivilege, PosixFileOps, BaseHost):
    """A Docker container exposed as a first-class otto host.

    Construction is normally done by :mod:`otto.docker.compose` after a
    successful ``docker compose up``; tests instantiate it directly with a
    mocked parent.

    A container host never starts its container. One registered from the
    declared settings before its stack is up (a placeholder, its
    ``container_id`` empty) resolves the running container's id on first
    use, and raises :class:`~otto.host.errors.ContainerNotRunningError` when
    there is none; bring the stack up first (``otto docker compose up``).
    """

    capabilities = HostCapabilities(
        run_user=UserSupport.chown,
        exec_user=UserSupport.chown,
        put_user=UserSupport.chown,
        get_user=UserSupport.ignored,
        show_progress=False,
        session_identity=SessionIdentity.bound_at_open,
        transfer=(
            "the parent host's own backend for the staging leg, then `docker cp` "
            "across the container boundary, `--concurrent` governing the "
            "staging leg"
        ),
        note=(
            "`user=` defaults to the service's declared user, and falls back to "
            "the image's own `USER` when neither is set."
        ),
    )
    """What this family promises. See :class:`~otto.host.capability_grid.HostCapabilities`."""

    parent: "Host" = field(kw_only=False)
    """The lab host running the docker daemon. Owns auth, hop chain, and
    the SSH connection used to reach the daemon. Typed as
    :class:`~otto.host.host.Host` (the protocol) so the type-system surface stays narrow,
    but ``run`` / ``open_session`` / ``send`` / ``expect`` / ``login``
    additionally require an SSH-based :class:`~otto.host.unix_host.UnixHost` at runtime —
    they open a persistent ``docker exec`` channel on the parent's
    asyncssh connection. ``exec`` and file transfer work against any
    parent."""

    container_id: str = field(kw_only=False)
    """Docker container id or unique name. Resolved by
    :func:`otto.docker.compose.compose_up` via
    ``docker compose -p <proj> ps -q <service>``."""

    project: str = field(kw_only=False)
    """Owning project name (the project's settings ``name``). Combined with
    *parent* and *service* to form the host id."""

    service: str = field(kw_only=False)
    """Compose service name (e.g. ``api``)."""

    compose_project: str = field(kw_only=False)
    """The ``-p`` value passed to ``docker compose`` for this stack. Stored
    so other commands (``logs``, ``ps``, ``down``) can scope correctly."""

    name: str = field(default="", init=False)

    is_virtual: bool = field(default=True, init=False)
    """Containers are always virtual by definition."""

    user: "str | None" = None
    """Declared default access user for this container (``users = {...}`` in
    ``[[docker.composes]]``): the identity ``exec``/``run``/``login``/``put``
    act as when the call doesn't name one. ``None`` defers to the image's
    ``USER``. Values go to docker verbatim (``root``, ``1000``, ``1000:1000``,
    ``name:group``)."""

    mounts: "list[Mount]" = field(default_factory=list, repr=False)
    """Directories this container shares with :attr:`parent`, derived from
    ``docker inspect`` when the stack came up (:mod:`otto.docker.mounts`).

    In-process state: the bring-up that filled this table filled it in ITS
    process. A placeholder registered by a later invocation has an empty one
    even while the stack is up, and nothing re-resolves it the way
    ``_ensure_running`` re-resolves a container id.

    Empty on a placeholder, and empty on a container that genuinely mounts
    nothing -- deliberately not a tri-state. No ordinary caller behaves
    differently between the two, so distinguishing them in the TYPE would
    tax every call site to serve one error path; :meth:`parent_path`'s
    refusal makes the distinction there instead, where it is wanted.
    """

    _pending_run_user: "str | None" = field(default=None, init=False, repr=False)
    """Per-call ``user`` of the ``run()`` attempt CURRENTLY in flight.

    A parameter for the imminent open and nothing more: ``_run_one`` sets it
    once the container is known to be up, and clears it in a ``finally`` the
    moment the attempt ends — success or failure alike. It is deliberately not
    a memory of "the last user asked for": an attempt that never opened a
    channel must leave nothing behind for the NEXT opener to pick up, and the
    next opener may be ``send``/``expect``, which have no ``user`` parameter to
    correct a stale one with. ``None`` resolving to the declared :attr:`user`
    via ``_effective_user`` is what makes those openers land on the declared
    default exactly as a plain ``run()`` would."""

    _bound_run_user: "str | None" = field(default=None, init=False, repr=False)
    """User the default run channel ACTUALLY opened as. Meaningless unless
    ``_run_user_bound`` — ``None`` is itself a valid binding (image USER)."""

    _run_user_bound: bool = field(default=False, init=False, repr=False)
    """Whether an open of the default run channel has been recorded.

    Written ONLY by :meth:`_record_run_channel_open`, which the channel itself
    calls once its transport is up; cleared by ``close``/``rebuild_connections``.
    Nothing that merely *intends* to reach the channel may set it."""

    _ensure_lock: asyncio.Lock = field(init=False, repr=False)
    """Serializes :meth:`_ensure_running` so concurrent accesses to a
    placeholder probe for it one at a time and resolve its id once."""

    def __post_init__(self) -> None:
        parent_id = getattr(self.parent, "id", getattr(self.parent, "name", "localhost"))
        self.id = f"{parent_id}.{self.project}.{self.service}".lower()
        self.name = f"{parent_id}:{self.service}"
        self._session_mgr = self._build_session_mgr()
        self._ensure_lock = asyncio.Lock()

    def _build_session_mgr(self) -> SessionManager:
        """Build a fresh SessionManager wired to this host.

        The underlying transport is a ``docker exec -it`` channel multiplexed
        on the parent's SSH connection; opening is lazy and gated on the parent
        being an SSH-based :class:`~otto.host.unix_host.UnixHost`.

        Called from :meth:`__post_init__` and :meth:`rebuild_connections`.
        """

        def _make_session(*, record: bool = True) -> ShellSession:
            from .unix_host import UnixHost

            if not (isinstance(self.parent, UnixHost) and self.parent.term == "ssh"):
                term = getattr(self.parent, "term", None)
                raise NotImplementedError(
                    f"DockerContainerHost persistent shell requires an SSH-based "
                    f"UnixHost parent; got {type(self.parent).__name__}"
                    + (f" with term={term!r}" if term is not None else "")
                    + ". Use exec() with chained `&&` commands instead, or "
                    "configure an SSH-based parent."
                )
            return _DockerSshSession(
                # Through the parent's claiming accessor: the channel rides the
                # parent's SSH connection, so the parent must own this loop too.
                conn_provider=self.parent._live_connections().ssh,  # noqa: SLF001 — intra-package access to parent host's connection manager
                container_id_getter=lambda: self.container_id,
                user_getter=self._user_for_an_imminent_open,
                on_open=self._record_run_channel_open if record else None,
            )

        return SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=_make_session,
            # Named auxiliary sessions open as the same user the run channel
            # would open as right now, but record nothing — see
            # :meth:`open_session`.
            named_session_factory=lambda: _make_session(record=False),
            exec_factory=self._exec_via_parent,
            creds=[],
            host_id=self.id,
        )

    ####################
    #  Command execution
    ####################

    async def _ensure_running(self) -> None:
        """Make sure ``self.container_id`` points at a running container.

        When the host was created from declared settings (e.g. by
        :func:`register_declared_container_hosts` at lab-load time of any
        new ``otto`` invocation), ``container_id`` is initially the empty
        placeholder string. The compose-up that registered the *real*
        container id lives in another process's memory and isn't visible
        here.

        Resolve lazily: ask the parent for any container labeled with
        ``com.docker.compose.project={self.compose_project}`` and
        ``com.docker.compose.service={self.service}``. If found, cache the
        id on ``self``. If not, raise: this method never starts anything, and
        the error names the explicit ways to start it. A cached id is not
        probed again, so a container that stopped after its id was cached
        fails from the ``docker exec`` itself.

        The probe has three outcomes that fail, and each raises something
        different (``_resolve_for_use`` maps them): it answered with no id (the
        stack is not up), it ran and failed (docker itself could not answer),
        or the parent could not complete it (the transport, not the container,
        is the problem).

        The probe still asks the docker daemon, **so every caller of this
        method owes a dry run an arm above its own call**, and the list of them
        is the invariant: ``_docker_exec`` (via ``_exec_via_parent``),
        ``put`` and ``get`` return a decline, as do the base's ``_run_one``,
        ``send`` and ``open_session`` above ``_prepare_live_session``;
        ``_expect_one``'s caller (``BaseHost.expect``) raises above it;
        ``login`` announces and returns. A new caller that forgets meets the
        probe's own refusal (``refuse_declined_fact``) instead of a decline,
        but only while the id is unresolved: a cached id is not probed, so once
        it is resolved the caller's own dry-run arm is the only guard.
        This paragraph asserted the invariant before it was true:
        ``open_session`` logged ``[DRY RUN] open_session(...)`` and then called
        this method anyway, and ``login`` had no dry-run arm at all (both fixed
        2026-08-15).

        Raises:
            ~otto.host.errors.ContainerNotRunningError: the probe answered and
                no running container was found for this placeholder, or the
                probe ran on the parent and failed (the message carries
                Docker's own text).
            ~otto.host.errors.HostUnreachableError: the parent could not
                complete the probe. A transport the parent cannot open at all
                raises out of its ``exec`` and propagates unchanged.
            ~otto.result.CommandNotRunError: this is a dry run and the
                container id is not already cached (from the probe).
        """
        # Claimed before the lock: ``_ensure_lock`` is per-loop state, which
        # the rebuild a claim from another loop makes replaces.
        self._claim_loop()
        if self.container_id:
            return

        async with self._ensure_lock:
            # Double-checked: another waiter may have resolved it while we
            # were blocked on the lock.
            if self.container_id:
                return

            cid = await self._resolve_for_use()
            if not cid:
                raise ContainerNotRunningError(
                    f"Container {self.id!r} is not running: no running container was "
                    f"found for service {self.service!r} of compose project "
                    f"{self.compose_project!r}, and otto does not start containers on "
                    f"use. Bring its stack up first: from the shell, "
                    f"`otto docker compose up {self.project}` (a use-case); from Python, "
                    f"`otto.docker.deploy({self.project!r})` (a use-case) or "
                    f"`otto.docker.compose_up(...)` (a project's own compose files), for "
                    f"example in a session-scoped fixture."
                )
            self.container_id = cid

    async def is_running(self) -> bool:
        """Whether the backing container is live right now — side-effect-free.

        Like ``_ensure_running`` it never starts anything; unlike it, it
        answers rather than raising: it is the read-only liveness probe for
        paths that must not require docker at all (tunnel discovery and
        manage — a declared-but-down container trivially carries no
        processes). A placeholder whose container turns out to be running
        caches the resolved id, so subsequent ``exec`` calls skip the probe.

        Side-effect-free is not the same as dry-run-safe: it is a ``bool``
        answering a question about a device, which is exactly the shape a
        dry run has no honest value for, and ``False`` is the actionable
        fiction ("the container is down"). It therefore declines, from
        ``_resolve_container_id``. otto's own two callers no longer
        depend on that — ``otto.tunnel.discovery._device_running`` refuses
        one level above — so this is a LIBRARY-surface backstop, for a caller
        outside otto that has no funnel of its own.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run and the
                container id is not already cached.
        """
        if self.container_id:
            return True
        cid = await self._resolve_container_id(log=LogMode.QUIET)
        if cid:
            self.container_id = cid
            return True
        return False

    async def _probe_container(self, log: LogMode = LogMode.NORMAL) -> CommandResult:
        """Ask the parent for this service's running container; return its raw answer.

        A dry run's decline cannot be read as any of the probe's answers:
        :meth:`is_running` would report a container down that was never asked
        about, and ``_ensure_running`` would raise an error that says the
        same. The refusal is here rather than in each caller because the
        fabrication would be born here -- one funnel, two callers, no drift.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run, so nothing
                about this container's state was measured.
        """
        result = await self.parent.exec(
            compose_container_probe(self.compose_project, self.service), log=log
        )
        refuse_declined_fact(result, asked=f"is_running({self.id})")
        return result

    async def _resolve_container_id(self, log: LogMode = LogMode.NORMAL) -> str:
        """Return the running container id for this service, or ``""``.

        ``""`` is this method's whole vocabulary for "not running", and every
        probe that is not ok folds into it: :meth:`is_running` answers rather
        than raising, and tunnel discovery relies on a container whose docker
        cannot answer reading as down.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run (from the probe).
        """
        result = await self._probe_container(log)
        if result.status.is_ok and result.value.strip():
            return result.value.strip().splitlines()[0]
        return ""

    async def _resolve_for_use(self) -> str:
        """Return the running container id, ``""`` when the probe found none, or raise.

        Unlike :meth:`_resolve_container_id`, this tells the probe's failures
        apart, because each has a different remedy. What ``parent.exec``
        reports, and where each lands:

        * an ok status: the probe answered. Its first line is the id; no
          output is ``""``, and ``_ensure_running`` raises "no running
          container was found".
        * ``Status.Failed``: the command RAN and exited non-zero (the ssh
          exec channel, ``SessionManager.exec_on``; a pooled or default shell
          session, ``ShellSession.run_cmd``; a local subprocess,
          ``LocalHost._exec_subprocess``). The daemon is down, there is no
          docker binary, or there is no permission on the socket:
          :class:`~otto.host.errors.ContainerNotRunningError` carrying the
          probe, its exit and Docker's own text (stderr is merged into it).
        * ``Status.Error`` (always the status when ``timed_out`` is set): the
          command never completed, from a timeout or a session lost
          mid-command in the same three producers, or a local subprocess that
          reported no return code: :class:`~otto.host.errors.HostUnreachableError`.
        * A transport the parent cannot open at all RAISES out of its
          ``exec`` (``OSError`` / ``ConnectionError``, the contract
          :func:`~otto.host.errors.exec_or_raise` is written against). It is
          not caught here and reaches the caller unchanged.

        Any other status that is not ok counts as a failure the command
        reported; ``Status.NotRun`` never gets this far, because the probe
        refuses it.

        Raises:
            ~otto.host.errors.ContainerNotRunningError: the probe ran and failed.
            ~otto.host.errors.HostUnreachableError: the parent could not
                complete the probe.
            ~otto.result.CommandNotRunError: this is a dry run (from the probe).
        """
        result = await self._probe_container()
        probe = compose_container_probe(self.compose_project, self.service)
        parent = self.parent.id
        if result.status is Status.Error or result.timed_out:
            raise HostUnreachableError(
                f"Container {self.id!r}: its parent {parent!r} could not complete the "
                f"probe for its running container (`{probe}`): "
                f"{result.value.strip() or result.status.name}"
            )
        if not result.status.is_ok:
            raise ContainerNotRunningError(
                f"Container {self.id!r} could not be checked: the probe for its running "
                f"container failed on parent {parent!r}. `{probe}` exited "
                f"{result.retcode} ({result.status.name}): "
                f"{result.value.strip() or '(no output)'}. otto does not start "
                f"containers on use. Check docker on {parent!r}, then bring the "
                f"stack up: from the shell, `otto docker compose up {self.project}`; "
                f"from Python, `otto.docker.deploy({self.project!r})` or "
                f"`otto.docker.compose_up(...)`."
            )
        if result.value.strip():
            return result.value.strip().splitlines()[0]
        return ""

    def _effective_user(self, user: "str | None") -> "str | None":
        """Per-call beats declared; neither → ``None`` (the image's ``USER`` prevails)."""
        return user if user is not None else self.user

    @override
    def _ambient_user(self) -> "str | None":
        """Report a switch made on the container's persistent channel.

        The container's ``as_user`` is real — ``PosixPrivilege`` runs ``su``
        on the in-container shell and records it on the session manager — so
        an enclosing ``async with container.as_user("app")`` must reach
        ``exec`` as well, which runs on a fresh ``docker exec`` of its own and
        would otherwise land on the DECLARED default while ``run`` ran as
        ``app``. ``None`` (no default channel, or one still running as the
        user it opened as) leaves :meth:`_effective_user` to apply the
        declared default exactly as before.
        """
        return self._live_session_mgr().ambient_user

    def mount_for(self, container_path: "str | Path") -> "Mount | None":
        """Return the mount covering *container_path*, or ``None`` if it is not shared.

        The predicate form -- use it to ASK. :meth:`parent_path` is for
        callers that have already established the path is shared.
        """
        return mount_for(self.mounts, container_path)

    def parent_path(self, container_path: "str | Path") -> Path:
        """Return where *container_path* lives on :attr:`parent`.

        Raises:
            ~otto.host.errors.MountNotFoundError: the path is not under any
                mount this container is known to have.
        """
        found = mount_for(self.mounts, container_path)
        if found is None:
            raise MountNotFoundError(self._no_mount_message(container_path, on_parent=False))
        return translate(found, container_path, to_parent=True)

    def container_path(self, parent_path: "str | Path") -> Path:
        """Return where a parent-side *parent_path* appears inside this container.

        Raises:
            ~otto.host.errors.MountNotFoundError: the path is not under any
                mount this container is known to have.
        """
        found = mount_for_parent(self.mounts, parent_path)
        if found is None:
            raise MountNotFoundError(self._no_mount_message(parent_path, on_parent=True))
        return translate(found, parent_path, to_parent=False)

    def _no_mount_message(self, path: "str | Path", *, on_parent: bool = False) -> str:
        """Say which of the three "no match" situations this actually is.

        An empty table reads identically whether otto never looked or the
        container really shares nothing, and the two send a reader to
        completely different places -- so the message, which is the only
        consumer that cares, branches on the one fact that separates them.

        *on_parent* selects which side of each :class:`~otto.host.mount.Mount`
        the non-empty branch names. :meth:`container_path` searches the
        PARENT-side paths (it is translating a parent path INTO the
        container), so it must list those, not the container-side paths
        :meth:`parent_path` searched -- naming the wrong side points a reader
        at directories that were never checked against *path* at all.

        The placeholder branch names TWO causes, not one. An empty
        ``container_id`` says only that no bring-up has run *in this process*:
        the table is in-memory state built by whichever invocation ran the
        compose-up, and a later ``otto`` invocation re-registers the container
        as a placeholder with an empty table even while the stack is up. A
        remedy line reading "run `otto docker compose up` first" would then instruct a
        reader to do the thing they had already done.
        """
        if self.mounts:
            key = (lambda m: m.parent_path) if on_parent else (lambda m: m.container_path)
            named = ", ".join(str(key(m)) for m in self.mounts)
            return (
                f"{self.id}: {path} is not under any of this container's shared "
                f"directories ({named})"
            )
        if not self.container_id:
            return (
                f"{self.id}: {path} cannot be translated -- no mount table has been read "
                f"for this container. Mounts are read from the daemon at bring-up and are "
                f"held in memory by the process that brought the stack up, so either the "
                f"stack is not up, or it was brought up by a different otto invocation. "
                f"Bring it up from this process (`otto docker compose up`, or deploy its use-case) "
                f"to populate the table."
            )
        return (
            f"{self.id}: {path} cannot be translated -- this container reports no shared "
            f"directories. Either its compose service declares no bind or volume mounts, "
            f"or the mount inspection at bring-up failed (a warning would have been "
            f"logged then), or this host was registered as a placeholder and had its "
            f"container id resolved lazily, so no mount inspection ran in this process."
        )

    def _user_for_an_imminent_open(self) -> "str | None":
        """Return the user a channel opening RIGHT NOW must run as.

        A LIVE default channel's own binding wins. That is what keeps a named
        auxiliary session landing beside the run channel rather than somewhere
        else: ``_pending_run_user`` is scoped to a single ``run()`` attempt and
        is already cleared by the time ``open_session`` is called, so reading it
        alone would open the named session as the declared default while the run
        channel sits on another user. Falling back to the pending intent covers
        the case this is really for — the run channel itself opening, which by
        definition happens when no live one exists.
        """
        if self._run_channel_is_bound:
            return self._bound_run_user
        return self._effective_user(self._pending_run_user)

    def _record_run_channel_open(self, user: "str | None") -> None:
        """Record that the default run channel just opened as *user*.

        THE ONLY WRITER of the bind record, and it is called from
        :meth:`~otto.host.session._DockerSshSession.open_transport` — i.e. by the open
        itself, once the transport is up. Recording at the point of INTENT
        instead (in ``_run_one``, before ``run_cmd``) is what the first cut of
        this feature did, and it was wrong twice over: a channel that
        ``send()``/``expect()`` opened was never recorded at all, so a
        subsequent ``run(user=...)`` cheerfully "bound" a user the live shell
        was not running as; and an open that failed left a record that refused
        the next legitimate call.
        """
        self._bound_run_user = user
        self._run_user_bound = True

    def _forget_run_channel_binding(self) -> None:
        """Drop the bind record and the pending intent — the channel is gone.

        Both ``close`` and ``rebuild_connections`` end with no default channel,
        so the next open is free to pick any user; clearing the intent as well
        returns the host to its DECLARED default rather than silently carrying
        the last per-call user across a teardown.
        """
        self._pending_run_user = None
        self._bound_run_user = None
        self._run_user_bound = False

    @property
    def _run_channel_is_bound(self) -> bool:
        """Whether a LIVE default run channel exists whose user was recorded.

        Both halves matter. Without the record, a channel could be live with
        no idea what it opened as. Without liveness, a channel whose handshake
        failed after its transport came up — or one whose ``close`` raised
        partway — would keep refusing calls against a shell that is gone.
        """
        mgr = self._live_session_mgr()  # claims first: a stale manager's liveness is a dead loop's
        return self._run_user_bound and mgr.has_live_default_session

    async def _docker_exec(
        self, cmd: str, *, interactive: bool = False, user: "str | None" = None
    ) -> str:
        """Build the ``docker exec`` invocation that runs *cmd* inside the container."""
        await self._ensure_running()
        flags = "-i" if not interactive else "-it"
        u = f" -u {shlex.quote(user)}" if user is not None else ""
        return f"docker exec {flags}{u} {self.container_id} sh -c {shlex.quote(cmd)}"

    @override
    def _refuse_exec_sudo(self) -> None:
        # Nothing to refuse: sudo on a container is `docker exec -u root`.
        # user+sudo is refused in `exec` below, where both values are known.
        return

    @override
    @cli_exposed
    async def exec(
        self,
        cmd: Annotated[
            "str | ShellCommand",
            Arg(
                name="COMMAND",
                help="One command, quoted as one arg; join steps with && on a POSIX shell.",
            ),
        ],
        expects: Annotated["Expect | list[Expect] | None", Exclude] = None,
        timeout: Annotated[
            float,
            Opt(help="Seconds before the command is abandoned; use inf for unbounded.", min=0.0),
        ] = DEFAULT_COMMAND_TIMEOUT,
        log: Annotated[LogMode, Exclude] = LogMode.NORMAL,
        sudo: bool = False,
        user: Annotated[
            "str | None",
            Opt(
                help="Run as this user — a direct or proxy login. Which families "
                "accept it, and how, is declared per family in `otto host` docs."
            ),
        ] = None,
    ) -> CommandResult:
        """See :meth:`~otto.host.host.BaseHost.exec`.

        On a container ``sudo`` is ``docker exec -u root`` — the parent's
        exec channel has no pty to answer a ``sudo -S`` prompt on, and root
        in the container is what elevation means there. ``user`` and
        ``sudo`` together refuse: both choose the ``-u``.

        Explicit two-argument ``super()`` — see ``_mkdir_all`` for why a
        bare ``super()`` raises on this ``@dataclass(slots=True)`` class.

        Mirrors :meth:`~otto.host.host.BaseHost.exec`'s ``@cli_exposed``
        stamp and CLI overlays exactly, so ``otto host <id> exec`` renders
        the same options on the container family (an override is a fresh
        function object; the marker does not inherit through it). See
        ``tests/unit/cli/test_dynamic_host_commands.py`` for the pin.
        """
        if sudo:
            if user is not None:
                raise NotImplementedError(
                    f"{self.name}: exec(user=..., sudo=True) — on a container both "
                    f"choose the user; pass one"
                ) from None
            return await super(DockerContainerHost, self).exec(
                cmd, expects=expects, timeout=timeout, log=log, user=ROOT_LOGIN
            )
        return await super(DockerContainerHost, self).exec(
            cmd, expects=expects, timeout=timeout, log=log, user=user
        )

    @override
    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
        *,
        expects: "list[Expect | CredentialExpect] | None" = None,
        needs_shell: bool = False,
    ) -> CommandResult:
        """Run a single command in the container via the parent.

        Stateless and concurrent-safe — each call spawns a fresh
        ``docker exec``. ``run()`` is the stateful counterpart that
        preserves shell state across calls. *needs_shell* is accepted here
        for signature parity — the parent's own ``exec`` decides whether it
        needs a shell to honour *expects*. ``docker exec -i`` keeps stdin
        open, so the parent's pooled shell can answer a prompt across it.
        """
        return await self._exec_via_parent(cmd, timeout, log=log, user=user, expects=expects)

    async def _exec_via_parent(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
        expects: "list[Expect | CredentialExpect] | None" = None,
    ) -> CommandResult:
        """Wrap *cmd* in ``docker exec`` and dispatch through the parent.

        Declines a dry run HERE rather than relabelling the parent's decline
        below, for three reasons that all point the same way. The parent
        returns a :class:`~otto.result.NotRunResult` whose ``value`` raises by
        contract, and ``dataclasses.replace`` reads every init field — so the
        relabel line, whose only job is cosmetic, would be where the contract
        fired. The parent's decline also names the ``docker exec`` WRAPPER, and
        a decline that names a command the caller never issued is the wrong
        error however it is delivered. And ``_docker_exec`` below calls
        ``_ensure_running``, which resolves the container on the daemon — a
        dry run must reach neither.

        Public ``exec``/``run`` short-circuit above this in ``BaseHost``, so
        today nothing arrives here under a dry run. This method is also
        ``SessionManager``'s ``exec_factory``, which is reached by a different
        route, and a seam that dispatches to a device answers for itself.
        SessionManager's exec_factory reaches here without a user — the
        declared default applies, which is the spec's effective-user rule,
        not an accident. It also reaches here without *expects*: that route
        already runs on a pooled shell of its own, so the default ``None``
        is correct there too.

        *expects* forwards to the parent's own ``exec``, which answers
        prompts on its pooled shell — ``docker exec -i`` (never ``-t``) keeps
        stdin open across that hop so a response written there reaches the
        container. They are the caller's own pairs only: a container elevates
        with ``docker exec -u root`` (see :meth:`exec`), so no elevation
        password answer exists on this route to forward.
        """
        if is_dry_run():
            return self._dry_run_result(cmd, log)
        pairs = _caller_pairs(expects, self.name)
        wrapped = await self._docker_exec(cmd, user=self._effective_user(user))
        result = await self.parent.exec(
            wrapped, timeout=timeout, log=self._effective_log(log), expects=pairs
        )
        # Replace the wrapped command in the result so callers see what they
        # asked for, not the docker-exec wrapper. `replace` rather than a field
        # list so new CommandResult fields are carried through automatically.
        return replace(result, command=cmd)

    @override
    @contextmanager
    def _run_user_binding(self, user: "str | None") -> "Iterator[None]":
        """Declare the user this ``run`` intends; the channel open binds it.

        The channel's user is BOUND, not per-call: ``docker exec -u`` is
        settled when the channel opens and cannot be renegotiated on a live
        shell. So this binds nothing — it declares an INTENT
        (:attr:`_pending_run_user`) and lets the open do the binding, via
        :meth:`_record_run_channel_open`. ``send()`` and ``expect()`` open the
        very same channel and set no intent, which resolves to the declared
        :attr:`user`; whichever call gets there first, the record describes the
        shell that is actually running.

        Against that record: if a LIVE channel opened as a different user than
        this call asks for, refuse — the alternative is silently answering as
        somebody else. If no channel is live, there is nothing to conflict
        with, so the call proceeds and its open binds. ``None`` (the image's
        ``USER``) is a binding like any other. :meth:`close` and
        :meth:`rebuild_connections` drop the channel and the record together.

        The intent is scoped to THIS attempt: set once the container is known
        to be up (the base's ``_prepare_live_session`` ran first), and dropped
        in a ``finally`` whether the command succeeded, failed, or never
        reached a shell. An attempt that dies before its channel opens must
        leave nothing behind, because the next thing to open the channel may
        be ``send``/``expect``, which take no ``user`` and so cannot correct a
        stale one — the residue would open the channel as a user this caller
        never successfully obtained and then refuse every plain ``run()``
        against it.
        """
        effective = self._effective_user(user)
        if self._run_channel_is_bound and effective != self._bound_run_user:
            raise RuntimeError(
                f"{self.name}: the persistent run channel is bound to user "
                f"{self._bound_run_user!r} and this call asked for {effective!r} — "
                f"close() or rebuild_connections() to rebind"
            )
        self._pending_run_user = user
        try:
            yield
        finally:
            self._pending_run_user = None

    @override
    async def _prepare_live_session(self) -> None:
        """Resolve the container before any live session arm touches it.

        The base's dry-run arm runs first, which is the whole point of the
        hook: the previous shape logged the ``[DRY RUN]`` line and then ran
        ``_ensure_running()`` anyway, so a dry run could ask the daemon for
        the container's id (and, while otto still started containers on use,
        START ONE).
        """
        await self._ensure_running()

    ####################
    #  Interactive shell
    ####################

    @override
    def _refuse_console_verb(self, verb: str) -> None:
        raise ValueError(
            f"{self.name}: {verb} applies to console hosts only (a container has no console)"
        )

    @override
    async def run_login(self, user: "str | None" = None, force: bool = False) -> None:
        """Open an interactive shell inside the container via the parent's SSH conn.

        ``user`` lands the shell as that identity (``docker exec -u``); the
        declared per-service default applies when the call names none, and
        the image's ``USER`` when neither is set. ``force`` is a console
        reset, and a container has no console: it raises :exc:`ValueError`
        before anything else runs.
        """
        if force:
            self._refuse_console_verb("--force")
        # Importing here to keep this module importable without asyncssh.
        from .interact import run_ssh_login
        from .unix_host import UnixHost

        if not isinstance(self.parent, UnixHost):
            raise NotImplementedError(
                f"DockerContainerHost.login() requires an SSH-based parent host; "
                f"got parent of type {type(self.parent).__name__}."
            )
        if self.parent.term != "ssh":
            raise NotImplementedError(
                f"DockerContainerHost.login() requires parent.term == 'ssh'; "
                f"got {self.parent.term!r}. Telnet parents cannot tunnel an "
                f"interactive docker exec."
            )
        await self._ensure_running()

        conn = await self.parent._live_connections().ssh()  # noqa: SLF001 — intra-package access to parent host's connection manager
        effective = self._effective_user(user)
        u = f" -u {shlex.quote(effective)}" if effective is not None else ""
        # /bin/sh is universal in Linux containers; users can override by
        # running `docker exec` directly if they want bash.
        cmd = f"docker exec -it{u} {shlex.quote(self.container_id)} /bin/sh"
        await run_ssh_login(conn=conn, host_name=self.name, command=cmd)

    ####################
    #  File transfer
    ####################

    @staticmethod
    def _stage_dir(root: Path, container_id: str) -> Path:
        """Per-CALL staging directory on the parent filesystem, named for its container.

        Under the parent's private staging root (:func:`staging_root`), which
        :func:`ensure_private_root` verifies first. Two overlapping transfers
        to one container must not remove each other's files in ``finally``.
        Each is a direct child of one shared directory, so removing it removes
        everything the call staged: a per-container parent left one empty
        directory behind per container ever transferred to (#698), and
        removing that parent once empty raced a second transfer between its
        two ``mkdir`` steps.
        """
        return root / _TRANSFER_STAGING / f"{container_id}-{uuid.uuid4().hex}"

    @override
    @cli_exposed(success="Transfer complete.", dry_run_preview=True)
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
        user: Annotated[
            str | None,
            Opt(
                help="chown landed files to this user inside the container "
                "(defaults to the declared service user)."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
        concurrent: Annotated[bool, Opt(help=CONCURRENT_HELP)] = True,
    ) -> Result:
        """Upload local files into the container.

        *show_progress* governs the STAGING leg — the ``parent.put`` to the
        parent host, an ordinary transfer that renders per-file bars — and is
        forwarded there untouched; the ``docker cp`` step is opaque and has
        no progress of its own. Declared on the
        :class:`~otto.host.host.Host` protocol: without the keyword a caller
        suppressing bars for a bulk transfer through the protocol (the
        coverage fetcher does) raised ``TypeError`` on this family alone.

        Two-step: ``parent.put`` to a per-call staging dir under the parent's
        private staging root, then ``docker cp`` from there into the
        container. The root is verified first (:func:`ensure_private_root`),
        and one otto cannot trust raises
        :class:`~otto.host.errors.StagingRefusedError` before anything is
        sent. The staging dir is cleaned up
        unconditionally so a failed transfer doesn't leak. Every file whose
        staging leg succeeded is copied into the container; a file that failed
        staging keeps that verdict and its siblings still land.

        *mode* is applied with a ``chmod`` **inside the container** after the
        copies land — deliberately not by stamping the staging copy and
        trusting ``docker cp`` to preserve it, which would put undocumented
        third-party behaviour in the trust path.

        *user*, once resolved through ``_effective_user`` (falling back to
        the declared service :attr:`user` when omitted), chowns the landed
        files to that identity after they land — ``chown`` itself runs as
        root, since the image's default user may not own what ``docker cp``
        just placed. ``chown <user>`` accepts every form ``chown`` itself
        accepts, verbatim: a name, a UID, or the ``UID:GID``/``name:group``
        owner:group spelling. A chown failure never fails silently: every
        landed file flips to an error entry naming the file, the user, and
        the reason.

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.put`.

        ``recursive`` transfers each directory among the sources as a tree;
        see :ref:`recursive-transfers`. *concurrent* is forwarded to the
        staging leg; the ``docker cp`` leg runs one command per file --
        see :ref:`concurrent-transfers`.
        """
        from .transfer import aggregate_transfer, chmod_command, parse_file_mode

        files = self._as_sources(src_files)
        if user is not None:
            _validate_user(user)
        if recursive:
            return await self._put_tree(
                files,
                dest_dir,
                mode=mode,
                user=user,
                show_progress=show_progress,
                concurrent=concurrent,
            )
        if is_dry_run():
            return self._dry_run_transfer("PUT", files, dest_dir, mode)
        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            return aggregate_transfer({f: Result(Status.Error, msg=mode_check.msg) for f in files})
        resolved_mode: int | None = mode_check.value  # ty: ignore[unsound-assignment] — Result.value is Any by design (payload slot); retired by the Result[T] workstream
        effective = self._effective_user(user)
        await self._ensure_running()

        stage = self._stage_dir(await ensure_private_root(self.parent), self.container_id)
        # Bound before the try: nothing evaluated inside the finally may
        # raise ahead of the teardown guard and mask the body's exception.
        host_name = self.name
        try:
            mkdir = await self.parent.exec(f"mkdir -p {path_operand(stage)}")
            if not mkdir.status.is_ok:
                msg = f"failed to create staging dir on parent: {mkdir.value}"
                return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in files})

            stage_result = await self.parent.put(
                files, stage, show_progress=show_progress, concurrent=concurrent
            )
            staged_entries = stage_result.value if isinstance(stage_result.value, dict) else {}
            per_file: dict[Path, Result] = {}
            for f in files:
                staged_entry = staged_entries.get(
                    f, Result(Status.Error, msg=stage_result.msg or "staging failed")
                )
                if staged_entry.status is not Status.Success:
                    # The staging leg's own verdict IS this file's entry --
                    # Error and Skipped alike; its siblings that DID stage are
                    # still copied below. Tested against Success rather than
                    # `is_ok` deliberately: `Skipped.is_ok` is True, and the
                    # one Skipped a transfer still produces (the sibling of a
                    # directory source refused by a non-recursive put) names a
                    # file whose bytes never reached the staging dir, so an
                    # `is_ok` test would docker cp a path that does not exist.
                    per_file[f] = staged_entry
                    continue
                staged = stage / f.name
                # Unbounded on purpose: this command's duration IS the
                # transfer into the container, which scales with the file's
                # size — a wall-clock bound here is meaningless (see nc.py).
                cp = await self.parent.exec(
                    f"docker cp {path_operand(staged)} "
                    f"{shell_operand(f'{self.container_id}:{dest_dir}')}",
                    timeout=float("inf"),
                )
                if not cp.status.is_ok:
                    per_file[f] = Result(Status.Error, msg=f"docker cp failed: {cp.value}")
                else:
                    per_file[f] = Result(Status.Success, value=dest_dir / f.name)
            # chown BEFORE chmod: chown-ing an already-chmod'd file clears
            # setuid/setgid (S_ISUID/S_ISGID) on most filesystems, which
            # would silently defeat a `mode="4755"` + `user=...` put. Running
            # chown first means the mode landed last is the one that sticks.
            # A side effect, still loud: a file whose chown just failed is no
            # longer Status.Success, so the chmod pass below skips it rather
            # than setting a mode on a file with admittedly-wrong ownership.
            if effective is not None:
                await self._chown_landed_as_root(per_file, effective)
            if resolved_mode is not None:
                landed = [
                    r.value for r in per_file.values() if r.status is Status.Success and r.value
                ]
                if landed:
                    if effective is not None:
                        # The effective user may not own what docker cp landed;
                        # only root can be relied on to chmod it.
                        wrapped = await self._docker_exec(
                            chmod_command(resolved_mode, landed), user=ROOT_LOGIN
                        )
                        chmod = await self.parent.exec(wrapped, timeout=float("inf"))
                    else:
                        # self.exec runs INSIDE the container (parent.exec would
                        # chmod the staging copy, about to be deleted).
                        chmod = await self.exec(chmod_command(resolved_mode, landed))
                    if not chmod.status.is_ok:
                        for f, r in per_file.items():
                            if r.status is Status.Success:
                                per_file[f] = Result(
                                    Status.Error,
                                    value=r.value,
                                    msg=(
                                        f"{f}: transferred, but setting mode "
                                        f"0o{resolved_mode:o} failed: "
                                        f"{chmod.value or f'chmod exited {chmod.retcode}'}"
                                    ),
                                )
            return aggregate_transfer(per_file)
        finally:
            with teardown_step(host_name, "staging-dir removal"):
                await self.parent.exec(self._unstage_command(stage))

    async def _chown_landed_as_root(self, per_file: "dict[Path, Result]", effective: str) -> None:
        """Chown every landed file to *effective*, run as root inside the container.

        Root performs the chown because the effective user may not own what
        ``docker cp`` just placed. Mutates *per_file* in place: a failed
        chown is never silent — every still-``Success`` entry flips to
        ``Error``, naming the file, the user, and the reason.
        """
        landed = [r.value for r in per_file.values() if r.status is Status.Success and r.value]
        if not landed:
            return
        paths = " ".join(path_operand(p) for p in landed)
        wrapped = await self._docker_exec(
            f"chown {shell_operand(effective)} {paths}", user=ROOT_LOGIN
        )
        chown = await self.parent.exec(wrapped, timeout=float("inf"))
        if chown.status.is_ok:
            return
        for f, r in per_file.items():
            if r.status is Status.Success:
                per_file[f] = Result(
                    Status.Error,
                    value=r.value,
                    msg=(
                        f"{f}: transferred, but chown to {effective!r} failed: "
                        f"{chown.value or f'chown exited {chown.retcode}'}"
                    ),
                )

    @override
    @cli_exposed(success="Download complete.", dry_run_preview=True)
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(variadic=True, elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
        user: Annotated[
            str | None,
            Opt(
                help="Accepted for interface uniformity; reads are ownership-indifferent, "
                "so containers ignore it."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
        concurrent: Annotated[bool, Opt(help=CONCURRENT_HELP)] = True,
    ) -> Result:
        """Download files from the container to the local machine.

        *show_progress* is forwarded to the staging leg (``parent.get`` from
        the parent host to the local machine) — see :meth:`put`.

        Two-step: ``docker cp`` from the container into a per-call staging dir
        under the parent's private staging root (verified first, as
        :meth:`put` does), then ``parent.get`` to the local dir. Every file
        ``docker cp`` staged is fetched; one file's ``docker cp`` failure is
        its own entry.

        *user* is accepted and validated but otherwise ignored: a read never
        changes what owns anything, so there is nothing to chown (spec §4).

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.get`.

        ``recursive`` transfers each directory among the sources as a tree;
        see :ref:`recursive-transfers`. *concurrent* is forwarded to the
        staging leg; the ``docker cp`` leg runs one command per file --
        see :ref:`concurrent-transfers`.
        """
        from .transfer import aggregate_transfer

        files = self._as_sources(src_files)
        if user is not None:
            _validate_user(user)
        if recursive:
            return await self._get_tree(
                files, dest_dir, user=user, show_progress=show_progress, concurrent=concurrent
            )
        if is_dry_run():
            return self._dry_run_transfer("GET", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(await ensure_private_root(self.parent), self.container_id)
        # Bound before the try: nothing evaluated inside the finally may
        # raise ahead of the teardown guard and mask the body's exception.
        host_name = self.name
        try:
            mkdir = await self.parent.exec(f"mkdir -p {path_operand(stage)}")
            if not mkdir.status.is_ok:
                msg = f"failed to create staging dir on parent: {mkdir.value}"
                return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in files})

            per_file: dict[Path, Result] = {}
            staged_paths: list[Path] = []
            for f in files:
                staged = stage / f.name
                # Unbounded on purpose: this command's duration IS the
                # transfer out of the container, which scales with the file's
                # size — a wall-clock bound here is meaningless (see nc.py).
                cp = await self.parent.exec(
                    f"docker cp {shell_operand(f'{self.container_id}:{f}')} {path_operand(staged)}",
                    timeout=float("inf"),
                )
                if not cp.status.is_ok:
                    # One file's docker cp failure is that file's own entry;
                    # the files that DID stage are still fetched below.
                    per_file[f] = Result(Status.Error, msg=f"docker cp failed: {cp.value}")
                else:
                    staged_paths.append(staged)

            # parent.get keys its per-file dict by the staged paths; re-key it
            # back to the container source paths (as passed) so the caller sees
            # the keys it handed in.
            if staged_paths:
                parent_result = await self.parent.get(
                    staged_paths, dest_dir, show_progress=show_progress, concurrent=concurrent
                )
                staged_map = parent_result.value if isinstance(parent_result.value, dict) else {}
                fallback = Result(parent_result.status, msg=parent_result.msg)
                for f in files:
                    if f not in per_file:
                        per_file[f] = staged_map.get(stage / f.name, fallback)
            return aggregate_transfer({f: per_file[f] for f in files})
        finally:
            with teardown_step(host_name, "staging-dir removal"):
                await self.parent.exec(self._unstage_command(stage))

    @staticmethod
    def _unstage_command(stage: Path) -> str:
        """Remove one call's staging dir, and with it everything the call staged."""
        return f"rm -rf {path_operand(stage)}"

    @override
    async def _mkdir_all(self, paths: "list[Path]", *, user: str | None = None) -> Result:
        """Create the tree's directories as root inside the container.

        *user* is accepted for interface uniformity but ignored — like
        :meth:`get`'s *user*, spec §4: ``docker cp`` lands files as root
        regardless of the declared service user, so a directory the service
        user could not create would still be the one ``docker cp`` writes
        into, and mkdir must run as root for the same reason the
        post-transfer chmod does.

        Explicit two-argument ``super()``, not the zero-argument form: this
        class is ``@dataclass(slots=True)``, which rebuilds the class object
        to add ``__slots__`` and leaves this method's compiled ``__class__``
        cell pointing at the pre-rebuild class — a bare ``super()`` here
        raises ``TypeError: super(type, obj): obj must be an instance or
        subtype of type`` at call time. Naming the class explicitly resolves
        it fresh through the module's own name, which is the rebuilt one.
        """
        return await super(DockerContainerHost, self)._mkdir_all(paths, user=ROOT_LOGIN)

    @override
    def _rebuild_connections(self) -> None:
        """Drop any persistent session so the next call reopens it.

        This family's override of the ``_rebuild_connections`` hook that
        :meth:`~otto.host.host.BaseHost.rebuild_connections` calls: how the
        host moves to another event loop (``_claim_loop`` reaches it when a
        loop other than the one its managers last served claims it). The container
        host doesn't own any raw transport (the parent does), but its
        ``_session_mgr`` may hold a ``ShellSession`` whose ``asyncssh``
        process is bound to the old loop. Replacing the manager forces lazy
        re-opens against the parent's SSH connection, which the parent
        claims (and rebuilds, if stale) on the same loop. ``_ensure_lock``
        is replaced too, for the same reason.

        The run channel's user binding goes with the channel — a fresh
        manager opens a fresh ``docker exec``, so the next ``run()`` is free
        to name any user.
        """
        self._session_mgr = self._build_session_mgr()
        self._forget_run_channel_binding()
        # A lock binds to the loop that first contends it; a rebuild is how
        # a host moves to a fresh loop, so it starts a fresh lock.
        self._ensure_lock = asyncio.Lock()

    ####################
    #  Cleanup
    ####################

    @override
    async def _close(self) -> None:
        """Tear down the persistent session.

        The parent's underlying connection is owned by the parent and is not
        closed here — but this host *must* close before its parent so the
        session's docker exec channel can drain cleanly.

        Clearing the run channel's user binding is part of the teardown: the
        next ``run()`` reopens a channel and may bind a different user. It
        happens in a ``finally`` because a teardown that fails half-way still
        leaves no channel this host may claim to know the user of — the
        alternative is a raising ``close()`` that also poisons every later
        ``run(user=...)`` with a refusal. A close that finishes after the host
        reconnected (a rebuild or an abandonment since it started) leaves the
        binding alone: it belongs to the new channel.
        """
        generation, session_mgr = self._generation, self._session_mgr
        try:
            await session_mgr.close_all()
        finally:
            if self._generation == generation:  # a later channel's binding is not this close's
                self._forget_run_channel_binding()


__all__ = ["DockerContainerHost"]
