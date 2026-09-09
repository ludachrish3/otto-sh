"""
Abstract base for network-reached hosts.

``RemoteHost`` is the common ancestor of every host class that talks to a
target across a network — :class:`~otto.host.unix_host.UnixHost` (SSH/Telnet to a bash shell),
:class:`~otto.host.embedded_host.EmbeddedHost` (telnet to an RTOS shell), and any future siblings
such as a Windows-host class. It is deliberately distinct from
:class:`~otto.host.local_host.LocalHost`,
which runs commands on the local machine and shares no network plumbing.

History: this name used to belong to the *concrete* SSH/Telnet bash host.
That class is now :class:`~otto.host.unix_host.UnixHost`; ``RemoteHost`` is the abstract parent.
The split makes the OS family of a host explicit (lab data carries an
``os_type`` field) and gives embedded targets a place to live alongside Unix
ones without lying about their shape.

``RemoteHost`` **is** a ``@dataclass(kw_only=True)``, and every field the two
network families share is declared here once, with its type, its docstring and
its default. It used to be deliberately field-less — the field-ordering rule of
dataclass inheritance (no non-default field after a default one) ruled a shared
dataclass base out, so the base carried bare annotations and each concrete
subclass re-declared every field. ``kw_only=True`` (Python 3.10+) removes that
rule for keyword-only fields, so the base can hold the declarations and a
subclass can still take required positional arguments. ``ip`` is the one field
that stays positional (``field(kw_only=False)``), so ``UnixHost("10.0.0.1",
creds)`` keeps working. The base stays UNSLOTTED, matching
:class:`~otto.host.host.BaseHost`: the concrete subclasses are
``@dataclass(slots=True)`` over it, exactly as before.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandResult
from ..utils import Status
from .host import BaseHost, is_dry_run
from .login_proxy import Cred
from .options import TelnetOptions

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from ..config.lab import Lab
    from .command_frame import CommandFrame
    from .connections import ConnectionManager
    from .element import Element
    from .host import Expect
    from .interface import Interface
    from .options import SnmpOptions
    from .session import HostSession, SessionManager
    from .session_setup import SessionSetup
    from .transport import SshHopTransport

logger = logging.getLogger(__name__)


_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """Normalize an identity token into a URL/id-safe slug.

    STABILITY CONTRACT — feeds ``make_host_id``, which in turn feeds
    ``make_link_id`` (static route ids) and tunnel path hops/sentinels;
    changing it re-maps every id and invalidates live tunnel markers. Never
    change the algorithm:

    - lower-case;
    - replace every maximal run of characters outside ``[a-z0-9]`` with a
      single ``-`` (so spaces, ``_``, ``.``, ``:``, ``|``, ``/``, and
      punctuation never reach an id);
    - strip leading/trailing ``-``.

    A value that slugs to ``""`` (all punctuation/whitespace) is invalid — the
    caller reports it as a load error.
    """
    return _SLUG_RUN.sub("-", value.lower()).strip("-")


def make_host_id(element: str, board: str | None, slot: int | None) -> str:
    """Compose a host's ``id`` from its identity fields — the single source of the id format.

    ``slug(element)``, then — only when a board is set — ``_`` + ``slug(board)``
    + ``slot``. The element's ``id`` is data and never appears (spec
    2026-09-05 §2.2). Called by ``RemoteHost._generate_id`` and by
    host_preferences selector matching, so a selector regex matches the same
    string a built host reports.
    """
    if board is None:
        return slug(element)
    slot_str = "" if slot is None else f"{slot}"
    return f"{slug(element)}_{slug(board)}{slot_str}"


OsType = str
"""Profile selector recorded on a host (the ``os_type`` field).

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded`` (generic
:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embedded_host.ZephyrHost`). Custom profiles add
more names. The base *family* (unix vs embedded) is derived from the host
class, not from this string.
"""


@dataclass(kw_only=True)
class RemoteHost(BaseHost):
    """Abstract base class for any host reached over a network.

    Concrete subclasses (:class:`~otto.host.unix_host.UnixHost`,
    :class:`~otto.host.embedded_host.EmbeddedHost`) supply the
    transport-specific session/transfer machinery as ``@dataclass`` fields.
    Do not instantiate this class directly.

    Every field the two network families share is declared below, once, with
    its type, its docstring and its default — the same rule
    :class:`~otto.host.host.BaseHost` follows for the fields all five families
    share. A family class declares only its own fields and, where its policy
    differs, re-declares a base field without a docstring (see
    ``tests/unit/host/test_field_homes.py`` for the allowlist).
    """

    ip: str = field(kw_only=False)
    """IP address of the host."""

    element: "Element" = field(repr=False)
    """The element this host belongs to — see
    :attr:`~otto.host.host.BaseHost.element` for what it carries. REQUIRED
    here, unlike on the base: a networked host always belongs to one. Keyword-
    only, so narrowing it cannot shift the positional order."""

    creds: list[Cred] = field(default_factory=list)
    """Login credentials for this host — one :class:`~otto.host.login_proxy.Cred`
    entry per account, in priority order (the first entry is the default
    login when ``user`` is unset). A proxied entry (``Cred.proxy`` set)
    cannot be reached by direct authentication;
    :meth:`~otto.host.unix_host.UnixHost.cred` /
    :attr:`~otto.host.unix_host.UnixHost.default_cred` and the
    connection-layer chain resolution
    (:func:`~otto.host.login_proxy.resolve_chain`) handle that. Optional on
    a console family whose shell has no login step (a stock Zephyr target);
    :class:`~otto.host.unix_host.UnixHost` makes it required."""

    user: str | None = None
    """User with which to log in, or None to use the first entry in ``creds``."""

    board: str | None = field(default=None, repr=False)
    """Board type name, or None."""

    slot: int | None = field(default=None, repr=False)
    """Physical slot number of the board, or None."""

    site: int | str | None = field(default=None, repr=False)
    """Site the host is installed at (a name or a number), or None."""

    rack: int | str | None = field(default=None, repr=False)
    """Rack within the site (a name or a number), or None."""

    shelf: int | None = field(default=None, repr=False)
    """Shelf / rack position, or None."""

    hop: str | None = None
    """Host ID of the intermediate hop used to reach this host, or None."""

    os_type: OsType = "unix"
    """Profile selector recorded on this host (see :data:`OsType`). The base
    *family* (unix vs embedded) is derived from the host class, not this string;
    each family overrides the value (``embedded``, ``zephyr``, …), and a custom
    profile over one of them records its own name here (e.g. ``ubuntu-22.04``
    over the unix family)."""

    os_name: str | None = None
    """Kernel/OS name (e.g. ``Linux``, ``Zephyr``), or None. A bare embedded
    host carries no OS name; a concrete family sets one."""

    os_version: str | None = None
    """OS/kernel version string, or None if unspecified."""

    hw_version: str | None = None
    """Hardware version description, or None — the board revision, typically.
    Informational; otto never parses it."""

    sw_version: str | None = None
    """Software version the host is DECLARED to run, or None; never a probe's
    observation. On the shared contract rather than
    :class:`~otto.host.unix_host.UnixHost` alone since spec 2026-08-28
    host-inventory §4."""

    term: str = "ssh"
    """Protocol used to issue terminal commands (active member of
    :attr:`valid_terms`)."""

    transfer: str = "scp"
    """Protocol used to transfer files (active member of
    :attr:`valid_transfers`)."""

    valid_terms: list[str] = field(default_factory=lambda: ["ssh", "telnet"])
    """Closed menu of term backends this host supports (active is ``term``)."""

    valid_transfers: list[str] = field(default_factory=lambda: ["scp", "sftp", "ftp", "nc"])
    """Closed menu of transfer backends this host supports (active is ``transfer``)."""

    is_virtual: bool = False
    """Determines whether a host is a VM / emulator (e.g. QEMU) or not."""

    has_bash: bool = True
    """Whether this host has a working ``bash`` a command can be tagged and
    exec'd through (``bash -c 'exec -a …'``). Tunnel discovery
    (:mod:`otto.tunnel.discovery`) scans only ``has_bash`` hosts. Unix hosts
    have bash by default and embedded targets do not; override in ``lab.json``
    for a host that defies its family's norm."""

    command_frame: "CommandFrame | None" = None
    """Shell-framing *dialect* for this host's console — how a command is
    wrapped in sentinels and how output/retcode are parsed back. ``None`` lets
    the :class:`~otto.host.session.SessionManager` use its built-in
    :class:`~otto.host.command_frame.BashFrame`, which is the right answer for
    a bash console; a bare :class:`~otto.host.embedded_host.EmbeddedHost` has
    no dialect to fall back on and fails loud at construction unless a profile,
    a subclass (e.g. :class:`~otto.host.embedded_host.ZephyrHost`) or an
    explicit value supplies one.

    Lab data declares the dialect by string in the ``command_frame`` field
    (resolved in ``__post_init__``). Projects can register custom dialects via
    :func:`otto.host.command_frame.register_command_frame`. The dialect is
    independent of the transport, so it is handed straight to the
    :class:`~otto.host.session.SessionManager`."""

    landing_frame: "CommandFrame | None" = None
    """Dialect of the shell otto lands in when it differs from ``command_frame``;
    ``None`` means the same dialect. Lab data names a registered frame by
    string (resolved in ``__post_init__``); only meaningful with
    ``session_setup``, which manoeuvres from the landing shell to the target."""

    session_setup: "SessionSetup | None" = None
    """Session-setup hook run once per shell session, after the handshake and
    every login-proxy hop, with a real :class:`~otto.host.session.HostSession`.
    Lab data declares it by name or as a ``{"type": name, ...params}`` table
    (resolved in ``__post_init__``). See :mod:`otto.host.session_setup`."""

    default_dest_dir: Path = field(default_factory=Path)
    """Per-host default directory that ``put`` / ``get`` resolve a
    relative or empty ``dest_dir`` against. Lets a fan-out helper like
    ``do_for_all_hosts`` pass one generic destination (``Path()``) and
    have each host land the files where its filesystem actually lives —
    e.g. ``/RAM:`` on a Zephyr FAT target, ``/lfs`` on a Zephyr LittleFS
    target. Defaults to ``Path()``, which preserves the existing
    "relative path lands in the SSH user's home" behavior on Unix; the
    embedded family resolves an empty default to ``filesystem.mount``."""

    max_filename_len: int = 255
    """Upper bound on the basename length (including extension) accepted by
    the target's filesystem. Defaults to ``255`` — the Linux ``NAME_MAX``,
    also the cap for ext4 / XFS / Btrfs / NTFS and the typical LittleFS
    ceiling. Override per-host when the firmware enforces a tighter limit
    (e.g. ``32`` for a Zephyr build that sets ``CONFIG_FS_FATFS_MAX_LFN=32``,
    or ``12`` for a stock FAT 8.3 build without LFN support). ``put`` / ``get``
    reject over-limit names up front with a clear message instead of letting
    the device produce an opaque error like ``-ENOENT`` or ``File name too
    long``."""

    telnet_options: TelnetOptions = field(default_factory=TelnetOptions, repr=False)
    """Connection options for telnet sessions (port, cols/rows, auto-resize, etc.)."""

    snmp: "SnmpOptions | None" = field(default=None, repr=False)
    """Optional per-host SNMP polling config (lab ``snmp`` block), or None. When
    set, otto's monitor collects this host over SNMP instead of by running shell
    commands — not an embedded-only channel; a Unix host may poll a real SNMP
    agent the same way. See :class:`~otto.host.options.SnmpOptions`."""

    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    """Opaque per-host ``metadata`` table from lab data (spec §4); never read by otto."""

    interfaces: dict[str, "Interface"] = field(default_factory=dict, repr=False)
    """Named network devices, keyed by the netdev name (e.g.
    ``{"eth0": Interface(ip="10.0.0.5"), "eth1": Interface(ip="192.168.1.5")}``).
    The *primary* address stays :attr:`ip`; this map is additive and optional
    (empty by default). Resolve a name (or pass a literal through) with
    :meth:`~otto.host.remote_host.RemoteHost.address_for`."""

    log_stdout: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout.
    Commands and their output are still written to the log files unless the
    effective :class:`~otto.logger.mode.LogMode` — this host's ``log``
    composed with the per-command mode — is ``LogMode.NEVER``, which redacts
    them from every sink."""

    _lab: "Lab | None" = field(default=None, compare=False, repr=False)
    """Back-reference to the owning Lab, wired by Lab.add_host. Lets hop
    resolution use self._lab.hosts[...] instead of ambient state."""

    _connection_factory: "type[ConnectionManager] | None" = field(default=None, repr=False)
    """Optional ConnectionManager subclass for dependency injection (e.g. test
    doubles). When None, the real ConnectionManager is used."""

    _connections: "ConnectionManager" = field(init=False, repr=False)
    """Manages the raw transport connection(s) for this host; built by the
    family's ``__post_init__``."""

    _session_mgr: "SessionManager" = field(init=False, repr=False)
    """Manages the persistent shell session(s) for this host; built by the
    family's ``__post_init__``."""

    async def _probe_connection(self) -> None:
        """Open this family's transport channel(s) without running a command.

        The family-specific half of :meth:`verify_connection`: Unix dispatches
        on ``term`` (and warms the FTP control channel when ``transfer`` is
        ``ftp``); embedded opens its single telnet console. Failure is signalled
        by raising — the template's ``except`` turns it into a
        ``Status.Error`` result.
        """
        # The message IS the diagnostic: verify_connection's template converts
        # this raise into a CommandResult whose value is str(e), so a bare
        # NotImplementedError would surface as an EMPTY error message.
        raise NotImplementedError(
            f"{type(self).__name__} must implement _probe_connection"
        ) from None

    async def verify_connection(self) -> CommandResult:
        """Attempt to connect without running any commands.

        Called by :meth:`is_reachable` to decide whether the host is up (the
        live reachability path) and by dry-run mode to validate connectivity.
        The probe itself is family-specific (``_probe_connection``); this
        template owns the logging and the ``CommandResult`` shape.

        **The label follows the invocation, not the file.** Both log lines
        below were unconditionally prefixed ``[DRY RUN]``, which was false on
        the busier of the two call paths: ``otto host <id> reboot --wait``
        with no ``-n`` anywhere dials for real through ``is_reachable`` once
        per poll, and printed ``[DRY RUN] Connection FAILED`` about a live
        socket. Deleting the prefix outright would then be wrong in the other
        direction, because the second caller is ``--dry-run --probe``
        (``otto.cli.probe``), where the connection is the one device contact
        the flag authorises and a reader must be told which mode produced it.
        So the condition is read here — ``is_dry_run()``, the only thing this
        template can honestly know about its caller — and the line says which
        world it is in.

        The dry-run line keeps the ``[DRY RUN]`` token but does NOT stop
        there, because everywhere else in this tree that token marks a thing
        otto DID NOT DO (``no session opened``, ``no elevation attempted``,
        ``Command not executed``). Left bare it would read as "the connection
        was skipped" — the mirror image of the original lie. The clause
        carries the truth, matching ``open_session``'s ``[DRY RUN]
        open_session(...) — no session opened`` shape.

        Neither path goes silent: SUPPRESS THE PAYLOAD, NEVER THE
        ANNOUNCEMENT. A probe whose dial left no trace in the log would be its
        own defect.
        """
        # Read once, above the try: both arms must agree, and a second call
        # inside the ``except`` would be a second chance to disagree.
        dry_run = is_dry_run()
        label = "[DRY RUN] " if dry_run else ""
        note = " — a real connection; no command was run" if dry_run else ""
        try:
            await self._probe_connection()
            self._log_command(f"{label}Connection verified{note}")
            return CommandResult(
                status=Status.Success, value="Connection successful", command="connect", retcode=0
            )
        except Exception as e:  # noqa: BLE001 — verify_connection probes all failure modes
            self._log_command(f"{label}Connection FAILED: {e}{note}")
            return CommandResult(status=Status.Error, value=str(e), command="connect", retcode=1)

    ####################
    #  Connection state / lifecycle
    ####################

    @property
    def _connected(self) -> bool:
        """Whether the host has any current connections or live sessions."""
        return self._session_mgr.has_live_sessions or self._connections.connected

    @override
    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Probe by attempting a connection (no command), bounded by *timeout*."""
        try:
            result = await asyncio.wait_for(self.verify_connection(), timeout)
        except Exception:  # noqa: BLE001 — reachability probe, any failure means unreachable
            return False
        return result.status.is_ok

    @override
    async def close(self) -> None:
        # Sessions first, transports second — and the transports MUST close
        # even when a session refuses to (chaos spec: teardown chain
        # robustness, docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).
        # The session failure still propagates afterwards.
        try:
            await self._session_mgr.close_all()
        finally:
            # NOT teardown_step-wrapped: this close is close()'s own result,
            # not cleanup after some other operation — its loud-failure
            # contract (either chain's failure propagates; the other chain
            # still runs) is pinned by test_unix_host.py's close-chain sweep.
            # ast-grep-ignore: no-awaited-close-in-finally
            await self._connections.close()

    ####################
    #  Session delegation (shared by every remote family)
    ####################

    @override
    async def _run_one(
        self,
        cmd: str,
        timeout: float,
        expects: "list[Expect] | None" = None,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
    ) -> CommandResult:
        """Execute a single command on the host via the **persistent shell session**.

        Called by :meth:`run` for both the single-string and list forms. The session
        is stateful: working directory changes (``cd``), exported environment variables,
        and other shell state persist between calls, just as they would in an
        interactive terminal.

        Limitations:
            - **Sequential only.** The session is a single shell — calling ``run()``
              concurrently from multiple coroutines will corrupt the session output.
              Use :meth:`exec` instead when you need concurrent execution (where
              the family supports it — embedded targets share one console).
            - **Stateful.** Commands affect each other; a ``cd`` in one call changes
              the directory for the next.

        Args:
            cmd: Shell command to run. Passed to the remote shell as-is.
            expects: Optional list of ``(pattern, response)`` tuples for interactive
                prompts (e.g. sudo password, confirmation dialogs). Each pattern is
                matched against output as it arrives; the corresponding response is
                sent automatically.
            timeout: Seconds before the command is considered hung. On expiry,
                Ctrl+C is sent and ``Status.Error`` is returned. Pass
                ``float("inf")`` for a deliberately unbounded command.
            user: Accepted for signature parity with the container family and
                REFUSED — see Raises below. A persistent session already has
                an identity; changing it is
                :meth:`~otto.host.privilege.PosixPrivilege.as_user`'s job.

        Returns:
            A :class:`~otto.result.CommandResult`; ``value`` holds the output.
            Exit code 0 → ``Status.Success``; non-zero → ``Status.Failed``.

        Raises:
            NotImplementedError: *user* is not None. The refusal is the FIRST
                line of the body, above the dry-run arm, so a dry run refuses
                too rather than reporting a decline for a call this family
                could never honour. It names the alternatives: ``as_user`` for
                the session's own identity, and, on unix, the stateless
                ``exec``/``put``/``get``, which take ``user=`` directly.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: run(user=...) is not supported on "
                f"{type(self).__name__} — a persistent session's identity is "
                f"as_user's job (async with host.as_user(...)); on unix, "
                f"exec/put/get accept user= directly"
            ) from None
        if is_dry_run():
            return self._dry_run_result(cmd, log)
        return await self._session_mgr.run_cmd(
            cmd, expects=expects, timeout=timeout, log=self._effective_log(log)
        )

    @override
    async def open_session(self, name: str) -> "HostSession":
        """Open a named persistent shell session.

        Unlike :meth:`~otto.host.host.BaseHost.run`, which uses a single default session,
        this method
        creates an additional named session that can run commands concurrently
        with the default session (or other named sessions).

        The session is established eagerly — any connection errors surface here.
        Call :meth:`~otto.host.session.HostSession.close` when done, or use the async context
        manager protocol::

            async with await host.open_session("monitor") as mon:
                result = await mon.run("stat /tmp/file.bin")

        Args:
            name: Identifier for this session. Reusing an existing name returns
                the existing session if it is still alive, or replaces it if dead.

        Returns:
            A :class:`~otto.host.session.HostSession` proxy exposing ``run``, ``send``,
            ``expect``, and ``close``.

        Under a dry run nothing is dialled and the handle is a
        :class:`~otto.host.session.DeclinedSession` — see
        ``BaseHost._dry_run_session``.

        See Also:
            :meth:`~otto.host.host.BaseHost.exec`: stateless alternative for one-off commands.
            :meth:`~otto.host.host.BaseHost.run`: default persistent session.
        """
        if is_dry_run():
            return self._dry_run_session(name)
        return await self._session_mgr.open_session(name)

    @override
    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the host's persistent session."""
        effective = self._effective_log(log)
        if is_dry_run():
            # The folded mode, not the default NORMAL: a dry run must not put a
            # send on the console that a real run keeps off it. No NEVER guard
            # here on purpose -- `_log_command` returns before it logs on NEVER,
            # and that is the ONE home for the decision; a second copy here
            # reads as redundant and gets deleted, taking the real one's twin
            # with it. Building the f-string first costs nothing that matters:
            # `text` is already a live `str` and `repr` only copies it.
            self._log_command(f"[DRY RUN] send({text!r})", effective)
            return
        await self._session_mgr.send(text, log=effective)

    @override
    async def _expect_one(
        self,
        pattern: str | re.Pattern[str],
        timeout: float,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  Dest dir resolution
    ####################

    def _resolve_dest(self, dest_dir: Path) -> Path:
        """Resolve a caller-supplied destination against ``default_dest_dir``.

        - Absolute paths are returned unchanged (the caller asked for that
          exact location).
        - Empty / ``Path()`` / ``Path('.')`` resolves to ``default_dest_dir``.
        - Any other relative path is joined onto ``default_dest_dir`` so
          ``put(..., dest_dir=Path('subdir'))`` lands under the host's
          natural root.

        Unix hosts whose default is the empty ``Path()`` get the original
        behavior (an empty caller dest stays empty → SCP/SFTP resolve to the
        SSH user's home directory).
        """
        if dest_dir.is_absolute():
            return dest_dir
        if str(dest_dir) in ("", "."):
            return self.default_dest_dir
        return self.default_dest_dir / dest_dir

    ####################
    #  Naming
    ####################

    def _generate_name(self) -> str:
        """Space-joined display label, exactly as written: ``element [board] [slot]``.

        No case change in either direction and no number (spec 2026-09-05
        §2.4) — a label, not an id.
        """
        parts: list[str] = [self.element.name]
        if self.board:
            parts.append(self.board)
            if self.slot is not None:
                parts.append(str(self.slot))
        return " ".join(parts)

    def _generate_id(self) -> str:
        return make_host_id(self.element.name, self.board, self.slot)

    @property
    def _slot_str(self) -> str:

        if self.slot is None:
            return ""

        return f"{self.slot}"

    ####################
    #  Addressing
    ####################

    def address_for(self, name_or_literal: str) -> str:
        """Resolve an interface *name* to its address, or pass a literal through.

        If *name_or_literal* is a key in :attr:`interfaces`, return that
        interface's address; otherwise return the value unchanged (it is taken
        to be a literal address such as :attr:`ip` or an explicit IP). This lets
        a host's ``snmp.address`` name a secondary interface without otto having
        to distinguish names from literals.
        """
        entry = self.interfaces.get(name_or_literal)
        return entry.ip if entry is not None else name_or_literal

    ####################
    #  Hop transport
    ####################

    def _build_hop_transport(self) -> "SshHopTransport":
        """Build an ``SshHopTransport`` for reaching this host through its hop.

        The transport wraps a factory coroutine that lazily resolves the hop
        host ID via the config module and opens a dedicated SSH connection to
        it. Each target host gets its own tunnel connection (not shared with
        the hop's own connections).

        For multi-hop chains the transport holds a reference to its parent
        :class:`SshHopTransport`, so ``close()`` cascades down the entire
        chain — every intermediate SSH connection (and its underlying
        asyncio transport) gets closed explicitly. Without that linkage,
        the outermost SSH connection (e.g. test1 in an
        otto→test1→test2→test3 chain) is owned only by asyncssh's
        tunnel mechanism, never has ``close()`` called on its asyncio
        transport, and leaves a zombie ``_SelectorSocketTransport`` that
        fires ``ResourceWarning`` from ``__del__`` after the test's loop
        closes — which pytest's ``[unraisable]`` plugin then escalates
        into a flake on the next test.

        Cycle detection prevents infinite loops (e.g. A hops through B, B hops through A).
        """
        from asyncssh import connect as _ssh_connect

        from .transport import SshHopTransport

        hop_id = self.hop
        if hop_id is None:
            raise ValueError(
                f"_build_hop_transport called on host {self.name!r} with no hop configured"
            )
        host_name = self.name

        # The outer SshHopTransport — its ``_parent`` is set lazily on the
        # first call to ``_create_tunnel`` (when the config is
        # available and we can resolve the hop chain). Linking ``_parent``
        # makes ``close()`` walk the chain so every intermediate SSH
        # connection's asyncio transport gets explicitly closed.
        # placeholder factory is replaced below; needed to satisfy the
        # constructor without doing anything that requires the config.
        async def _placeholder(*args: object, **kwargs: object) -> NoReturn:  # noqa: ARG001 — required by SshHopTransport factory callback signature (Callable[..., Awaitable[SSHClientConnection]])
            raise RuntimeError("SshHopTransport factory not initialized")

        outer = SshHopTransport(_placeholder)

        async def _create_tunnel(
            _visited: set[str] | None = None,
        ) -> "SSHClientConnection":
            visited = _visited or set()
            if hop_id in visited:
                raise ValueError(f"Circular hop detected: {hop_id!r} already in chain {visited}")
            visited.add(hop_id)

            lab = self._lab
            if lab is None:
                # Standalone host (not added to a Lab): resolve the hop target
                # from the active OttoContext's lab, where it lives. (Hosts loaded
                # via the JSON loader / get_host carry their own _lab; this path
                # supports directly-constructed hosts per the library "FD model".)
                from ..context import try_get_context

                _ctx = try_get_context()
                lab = _ctx.lab if _ctx is not None else None
            if lab is None:
                raise RuntimeError(
                    f"Host {host_name!r} cannot resolve hop {hop_id!r}: the host has no lab "
                    f"back-reference and there is no active OttoContext. Add the host to a Lab "
                    f"(Lab.add_host) or run within `otto.open_context(...)`."
                )
            if hop_id not in lab.hosts:
                raise KeyError(
                    f"hop {hop_id!r} not in lab {lab.name!r}; available: {sorted(lab.hosts)}"
                )
            hop_host = cast("RemoteHost", lab.hosts[hop_id])

            parent_tunnel = None
            if hop_host.hop:
                # Build the parent SshHopTransport lazily on first use and
                # cache it on ``outer._parent`` so close() can walk it.
                # Reusing the cached connection avoids re-tunneling on
                # subsequent calls and gives close() a single object to
                # tear down.  ``get_tunnel`` holds the parent's
                # ``_conn_lock``, which is what prevents concurrent callers
                # of the outer factory from each opening their own parent
                # connection and leaking the race losers.
                if outer._parent is None:  # noqa: SLF001 — intra-package access to SshHopTransport._parent cache
                    outer._parent = hop_host._build_hop_transport()  # noqa: SLF001 — intra-package access to RemoteHost._build_hop_transport
                parent_tunnel = await outer._parent.get_tunnel(_visited=visited)  # noqa: SLF001 — intra-package access to SshHopTransport._parent

            # Same login_target/direct-cred resolution the hop host's own
            # ConnectionManager uses for its transport auth — a proxied
            # login_target resolves to its via-chain's directly-loginable
            # end (the proxy hops themselves are applied post-handshake by
            # the hop host's own session, not here).
            user, password = hop_host._connections.credentials  # noqa: SLF001 — intra-package access to RemoteHost._connections for hop-auth resolution
            logger.debug(f"Opening SSH tunnel through {hop_id} for {host_name}")
            return await _ssh_connect(
                hop_host.ip,
                username=user,
                password=password,
                known_hosts=None,
                tunnel=parent_tunnel,
            )

        outer._factory = _create_tunnel  # noqa: SLF001 — intra-package assignment to SshHopTransport._factory closure
        return outer
