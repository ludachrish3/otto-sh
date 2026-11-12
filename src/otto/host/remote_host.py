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
rule for keyword-only fields, so the base can hold the declarations. Every
field is keyword-only, ``ip`` included: ``UnixHost(ip="10.0.0.1", creds=creds)``.
The base stays UNSLOTTED, matching
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

from ..models.base import Cred
from ..result import CommandResult
from ..utils import Status
from .connections import LOGINLESS, ConsoleEndpoint, connect_and_describe
from .construction import RuntimeOnly
from .errors import ConsoleError
from .host import PROBE_TIMEOUT_S, BaseHost, _ProbeAnswer, is_dry_run
from .options import ConsoleOptions, SshOptions, TelnetOptions

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from ..labs.loader import Lab
    from .command_frame import CommandFrame
    from .connections import ConnectionManager
    from .element import Element
    from .interface import Interface
    from .options import SnmpOptions
    from .session_setup import SessionSetup
    from .transport import _SshHopTransport

logger = logging.getLogger(__name__)

_FRESH_CLOSE_S = 10.0
"""Bound on closing a fresh probe's throwaway manager: SSH teardown awaits
``wait_closed()`` without a bound of its own, so a stalled close would
otherwise eat the down window."""


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

    Every portion is slugged and the portions are joined by ``-``:
    ``slug(element)``, then — only when a board is set — ``slug(board)``, then
    — only with a board — the slot. ``test1``, ``test1-bb``, ``test1-bb-0``.
    A slot of ``0`` is a slot. The element's ``id`` is data and never appears.
    Called by ``RemoteHost._generate_id`` and the factory; host_preferences
    selectors fullmatch the id it produces. Two different declarations CAN compose one id (element
    ``a-b`` and element ``a`` + board ``b``); ``Lab.add_host`` refuses that,
    naming both.
    """
    portions = [slug(element)]
    if board is not None:
        portions.append(slug(board))
        if slot is not None:
            portions.append(str(slot))
    return "-".join(portions)


def make_host_name(element: str, board: str | None, slot: int | None) -> str:
    """Compose a host's display ``name`` from its identity fields: ``element [board] [slot]``.

    Space-joined, exactly as written — no case change and no slug — because a
    name is a label, not an id. The board appears only when it is set and
    non-empty, and the slot only WITH a board (a slot alone is ignored, as in
    :func:`make_host_id`). A slot of ``0`` is a slot.
    """
    parts = [element]
    if board:
        parts.append(board)
        if slot is not None:
            parts.append(str(slot))
    return " ".join(parts)


def describe_identity(element: str, board: str | None, slot: int | None, ip: str | None) -> str:
    """Name a host by its declaration, for a collision message: what the user wrote, not the id.

    ``element 'x' board 'cpu' slot 1 (10.0.0.2)`` — board, slot and address
    each appear only when set. Two declarations that compose one id (see
    :func:`make_host_id`) are told apart by exactly these fields.
    """
    parts = [f"element {element!r}"]
    if board is not None:
        parts.append(f"board {board!r}")
    if slot is not None:
        parts.append(f"slot {slot}")
    return " ".join(parts) + (f" ({ip})" if ip else "")


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

    ip: str
    """IP address of the host."""

    element: "Element" = field(  # ty: ignore[invalid-mutable-override] — REQUIRED by design: a remote host always has an element; BaseHost admits None only for container/local
        repr=False,
        metadata={
            "otto": RuntimeOnly(reason="the factory's element argument", supplied_by="element")
        },
    )
    """The element this host belongs to — see
    :attr:`~otto.host.host.BaseHost.element` for what it carries. REQUIRED
    here, unlike on the base: a networked host always belongs to one. Keyword-
    only, so narrowing it cannot shift the positional order."""

    creds: list[Cred] = field(default_factory=list)
    """Login credentials for this host — one :class:`~otto.host.login_proxy.Cred`
    entry per account, in list order: a protocol logs in as the first entry
    that applies to it (unscoped, or scoped to that protocol). A proxied entry
    (``Cred.proxy`` set) cannot be reached by direct authentication;
    :meth:`~otto.host.unix_host.UnixHost.cred` /
    :attr:`~otto.host.unix_host.UnixHost.default_cred` and the
    connection-layer chain resolution
    (:func:`~otto.host.login_proxy.resolve_chain`) handle that. Empty by
    default: an entry with no creds connects loginless (a console, or a stock
    Zephyr target whose shell has no login step).
    :class:`~otto.host.unix_host.UnixHost` takes it as its second positional
    parameter."""

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

    console_options: ConsoleOptions = field(default_factory=ConsoleOptions, repr=False)
    """Address and login behaviour of this host's serial console for the
    ``console`` term (server host ID, port, dial mode, prompt patterns).
    See :class:`~otto.host.options.ConsoleOptions`."""

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

    _lab: "Lab | None" = field(default=None, compare=False, repr=False)
    """Back-reference to the owning Lab, wired by Lab.add_host. Lets hop
    resolution use self._lab.hosts[...] instead of ambient state."""

    _connections: "ConnectionManager" = field(init=False, repr=False)
    """Manages the raw transport connection(s) for this host; built by the
    family's ``__post_init__``."""

    def _build_connections(self) -> "ConnectionManager":
        """Construct a new connection manager for this host from its fields.

        The one manager factory per family: construction, rebuild and the
        fresh down probe (:meth:`is_reachable` with ``fresh=True``) all call it.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _build_connections"
        ) from None

    async def _probe_with(self, conns: "ConnectionManager") -> None:
        """Open this family's transport channel(s) on *conns* without running a command.

        The family-specific dial dispatch: Unix dispatches on ``term`` (and
        warms the FTP control channel when ``transfer`` is ``ftp``); embedded
        opens its single console. Failure is signalled by raising. Both the
        live probe (:meth:`_probe_connection`) and the fresh probe call it.
        """
        # The message IS the diagnostic: verify_connection's template converts
        # this raise into a CommandResult whose value is str(e), so a bare
        # NotImplementedError would surface as an EMPTY error message.
        raise NotImplementedError(f"{type(self).__name__} must implement _probe_with") from None

    @override
    def probe_bound(self, *, timeout: float = PROBE_TIMEOUT_S) -> float:
        """*timeout*, lifted on the ``console`` term to its connect allowance.

        Only a console needs it: it names a running shell or a silent line
        after a full ``login_timeout``. Ssh and telnet keep the hard cut, so a
        large ``connect_timeout`` cannot stretch one probe past a fast
        reboot's down window. Sizing the allowance claims the live manager's
        loop; when another live loop owns the host
        (:class:`~otto.host.loop_owner.HostLoopError`) that is logged at debug
        and *timeout* comes back unlifted — the probe then meets the same
        claim and reads as unreachable. Any other failure propagates.
        """
        if self.term != "console":
            return timeout
        from .loop_owner import HostLoopError  # off the startup graph, as _claim_loop does

        try:
            return max(timeout, self._live_connections().connect_allowance())
        except HostLoopError:
            logger.debug("%s: the probe bound could not be sized", self.name, exc_info=True)
            return timeout

    async def _probe_live(self, timeout: float) -> Exception | None:
        """Probe the live manager once, bounded by *timeout*: ``None``, or why it failed.

        The one live probe: :meth:`is_reachable` (``fresh=False``) and
        ``reboot``'s up phase (:meth:`_probe_up`) both come here. A bound
        that cuts the probe answers with the ``TimeoutError``.
        """
        try:
            return await asyncio.wait_for(self._verify_outcome(), timeout)
        except Exception as exc:  # noqa: BLE001 — reachability probe, any failure means unreachable
            return exc

    async def _probe_fresh(self, timeout: float) -> Exception | None:
        """Probe once through a throwaway manager, bounded by *timeout*: ``None``, or why not.

        The line is released first, and the throwaway is closed (bounded by
        ``_FRESH_CLOSE_S``, never raising) before returning; the live
        manager's caches are untouched. ``is_reachable(fresh=True)`` and
        ``reboot``'s down phase (:meth:`_probe_down`) both come here.
        """
        await self._release_probe_line()
        conns = self._build_connections()
        try:
            await asyncio.wait_for(self._probe_with(conns), timeout)
        except Exception as exc:  # noqa: BLE001 — reachability probe, any failure means unreachable
            return exc
        else:
            return None
        finally:
            try:
                await asyncio.wait_for(conns.close(), _FRESH_CLOSE_S)
            except Exception as exc:  # noqa: BLE001 — a throwaway's teardown must never raise out
                logger.debug("%s: fresh probe close did not finish: %r", self.name, exc)

    @override
    async def _probe_up(self, timeout: float) -> _ProbeAnswer:
        return _ProbeAnswer.of(await self._probe_live(timeout))

    @override
    async def _probe_down(self, timeout: float) -> _ProbeAnswer:
        return _ProbeAnswer.of(await self._probe_fresh(timeout))

    async def _release_probe_line(self) -> None:
        """Release a single-client line before a probe dials; default no-op.

        A console server serves one client, so a family whose cached client
        holds it must let go first or the new dial is refused.
        """

    async def _probe_connection(self) -> None:
        """Open this host's channel(s) on the live manager, after releasing the probe line.

        The family-specific half of :meth:`verify_connection`; the template's
        ``except`` turns a raise into a ``Status.Error`` result.
        """
        await self._release_probe_line()
        await self._probe_with(self._live_connections())

    async def verify_connection(self) -> CommandResult:
        """Attempt to connect without running any commands.

        Called by dry-run mode to validate connectivity. The probe and its log
        lines are ``_verify_outcome``, which the live reachability path
        (:meth:`is_reachable`, ``reboot``'s up phase) runs too; the probe
        itself is family-specific (``_probe_connection``), and this template
        owns the ``CommandResult`` shape.

        **The label follows the invocation, not the file.** Both log lines
        were unconditionally prefixed ``[DRY RUN]``, which was false on
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
        failure = await self._verify_outcome()
        if failure is None:
            return CommandResult(
                status=Status.Success, value="Connection successful", command="connect", retcode=0
            )
        return CommandResult(status=Status.Error, value=str(failure), command="connect", retcode=1)

    async def _verify_outcome(self) -> Exception | None:
        """:meth:`verify_connection`'s probe and log lines: ``None``, or the failure."""
        # Read once, above the try: both arms must agree, and a second call
        # inside the ``except`` would be a second chance to disagree.
        dry_run = is_dry_run()
        label = "[DRY RUN] " if dry_run else ""
        note = " — a real connection; no command was run" if dry_run else ""
        try:
            await self._probe_connection()
        except Exception as e:  # noqa: BLE001 — verify_connection probes all failure modes
            self._log_command(f"{label}Connection FAILED: {e}{note}")
            return e
        self._log_command(f"{label}Connection verified{note}")
        return None

    ####################
    #  Connection state / lifecycle
    ####################

    @property
    def connections(self) -> "ConnectionManager":
        """The manager behind this host's sessions.

        In-package readers -- the protocol survey opens throwaway transports
        through a copy's manager -- use this; nothing outside the host package
        should need it. Reading it claims the running loop, as every
        connection-touching step does (see ``_live_connections``).
        """
        return self._live_connections()

    def _live_connections(self) -> "ConnectionManager":
        """Return the connection manager, after claiming the running loop.

        See :meth:`~otto.host.host.BaseHost._claim_loop`: a manager bound to a
        closed loop is rebuilt first, and one owned by another live loop
        raises :class:`~otto.host.loop_owner.HostLoopError`.
        """
        self._claim_loop()
        return self._connections

    @property
    def _connected(self) -> bool:
        """Whether the host has any current connections or live sessions."""
        return self._session_mgr.has_live_sessions or self._connections.connected

    @override
    async def is_reachable(self, *, timeout: float = PROBE_TIMEOUT_S, fresh: bool = False) -> bool:
        """Probe by attempting a connection (no command), bounded by *timeout*.

        *timeout* is a hard bound. A caller that must let the term finish its
        own answer passes :meth:`probe_bound` (a console names a running
        shell or a silent line only after a full ``login_timeout``).

        With *fresh* the probe line is released, a throwaway manager is built
        by ``_build_connections()`` and probed, and it is closed (bounded by
        ``_FRESH_CLOSE_S``, never raising) before returning; the live
        manager's caches are untouched. ``reboot``'s down phase uses it so a
        transport cached by an early login cannot read a dead host as up (#435).
        Any failure is unreachable, a console line at a running shell
        included: on a console that keeps its link through a reboot, shutdown
        and boot output look the same as a shell.
        """
        if fresh:
            return await self._probe_fresh(timeout) is None
        return await self._probe_live(timeout) is None

    @override
    async def _close(self) -> None:
        # Sessions first, transports second — and the transports MUST close
        # even when a session refuses to (chaos spec: teardown chain
        # robustness, docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).
        # The session failure still propagates afterwards.
        # Taken before the first await: a reconnect while this close runs
        # replaces the attributes, and this close must not touch the new managers.
        session_mgr, connections = self._session_mgr, self._connections
        try:
            await session_mgr.close_all()
        finally:
            # NOT teardown_step-wrapped: this close is close()'s own result,
            # not cleanup after some other operation — its loud-failure
            # contract (either chain's failure propagates; the other chain
            # still runs) is pinned by test_unix_host.py's close-chain sweep.
            # ast-grep-ignore: no-awaited-close-in-finally
            await connections.close()

    ####################
    #  Session delegation (shared by every remote family)
    ####################

    @override
    def _ambient_user(self) -> str | None:
        return self._live_session_mgr().ambient_user

    @override
    def _refuse_run_user(self, user: str) -> None:
        """Refuse ``run(user=...)``: this family has no user to switch to.

        Only a family with no user to switch to reaches this: the unix family
        honours *user* by wrapping the whole call in
        :meth:`~otto.host.privilege.PosixPrivilege.as_user` and calling back in
        with ``user=None``. The message names the CONCRETE class, so an
        embedded host's refusal says so.
        """
        raise NotImplementedError(
            f"{self.name}: run(user=...) is not supported on "
            f"{type(self).__name__} — this family has no user to switch to"
        ) from None

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
        return make_host_name(self.element.name, self.board, self.slot)

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

    def address_for(self, *, name_or_literal: str) -> str:
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

    def _lab_host(self, host_id: str, *, role: str) -> "RemoteHost":
        """Resolve *host_id* to a lab host, naming *role* (``hop``, ``console server``) on failure.

        Resolved from this host's own lab back-reference, else the installed
        peer-host resolver (the installed context's lab, read live) -- the
        lookup the tunnel factory performs for a hop and the console term
        performs for its console server.
        """
        from ..invocation import installed_resolver

        lab = self._lab if self._lab is not None else installed_resolver()
        if lab is None:
            raise RuntimeError(
                f"Host {self.name!r} cannot resolve {role} {host_id!r}: the host has no lab "
                f"back-reference and there is no active OttoContext. Add the host to a Lab "
                f"(Lab.add_host) or run within `otto.open_context(...)`."
            )
        if host_id not in lab.hosts:
            raise KeyError(
                f"{role} {host_id!r} not in lab {lab.name!r}; available: {sorted(lab.hosts)}"
            )
        return cast("RemoteHost", lab.hosts[host_id])

    def hop_host(self) -> "RemoteHost | None":
        """Return the hop's host object, or ``None`` when this host has no hop.

        Shared with the tunnel factory, so the protocol survey observes from
        the same hop.
        """
        if self.hop is None:
            return None
        return self._lab_host(self.hop, role="hop")

    def _build_hop_transport(self, hop_id: str | None = None) -> "_SshHopTransport":
        """Build an ``_SshHopTransport`` for reaching this host through its hop.

        *hop_id* names the host to tunnel into; ``None`` (the default) means
        this host's own ``hop``. The console term passes its console server's
        id, so the same recursion, cred lookup and cycle check reach it.

        The transport wraps a factory coroutine that lazily resolves the hop
        host ID via the config module and opens a dedicated SSH connection to
        it. Each target host gets its own tunnel connection (not shared with
        the hop's own connections). The hop host's ``ssh_options`` kwargs
        (kex, MAC, ciphers, host-key algs, ``extra``) are honoured on that
        connection exactly as they would be on the hop's own session, but its
        structured forwards and ``post_connect`` hook are not applied — a
        tunnel is a transport to the hop, not a session on it, so a hop
        record with a fixed ``local_forwards`` port does not try to rebind
        it once per hopped target.

        For multi-hop chains the transport holds a reference to its parent
        ``_SshHopTransport``, so ``close()`` cascades down the entire
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
        from .transport import _SshHopTransport

        hop_id = self.hop if hop_id is None else hop_id
        if hop_id is None:
            raise ValueError(
                f"_build_hop_transport called on host {self.name!r} with no hop configured"
            )
        host_name = self.name

        # The outer _SshHopTransport — its ``_parent`` is set lazily on the
        # first call to ``_create_tunnel`` (when the config is
        # available and we can resolve the hop chain). Linking ``_parent``
        # makes ``close()`` walk the chain so every intermediate SSH
        # connection's asyncio transport gets explicitly closed.
        # placeholder factory is replaced below; needed to satisfy the
        # constructor without doing anything that requires the config.
        async def _placeholder(*args: object, **kwargs: object) -> NoReturn:  # noqa: ARG001 — required by _SshHopTransport factory callback signature (Callable[..., Awaitable[SSHClientConnection]])
            raise RuntimeError("_SshHopTransport factory not initialized")

        outer = _SshHopTransport(_placeholder)

        async def _create_tunnel(
            _visited: set[str] | None = None,
        ) -> "SSHClientConnection":
            visited = _visited or set()
            if hop_id in visited:
                raise ValueError(f"Circular hop detected: {hop_id!r} already in chain {visited}")
            visited.add(hop_id)

            hop_host = self._lab_host(hop_id, role="hop")

            parent_tunnel = None
            if hop_host.hop:
                # Build the parent _SshHopTransport lazily on first use and
                # cache it on ``outer._parent`` so close() can walk it.
                # Reusing the cached connection avoids re-tunneling on
                # subsequent calls and gives close() a single object to
                # tear down.  ``get_tunnel`` holds the parent's
                # ``_conn_lock``, which is what prevents concurrent callers
                # of the outer factory from each opening their own parent
                # connection and leaking the race losers.
                if outer._parent is None:  # noqa: SLF001 — intra-package access to _SshHopTransport._parent cache
                    outer._parent = hop_host._build_hop_transport()  # noqa: SLF001 — intra-package access to RemoteHost._build_hop_transport
                parent_tunnel = await outer._parent.get_tunnel(_visited=visited)  # noqa: SLF001 — intra-package access to _SshHopTransport._parent

            # A hop is always an SSH tunnel, so it resolves against ssh
            # (spec 2026-09-13 cred-scope §4) regardless of the hop host's
            # own active term — a proxied ssh pick resolves to its
            # via-chain's directly-loginable end (the proxy hops themselves
            # are applied post-handshake by the hop host's own session, not
            # here).
            cred = hop_host._connections.transport_cred_for(protocol="ssh") or LOGINLESS  # noqa: SLF001 — intra-package access to RemoteHost._connections for hop-auth resolution
            # The hop host's OWN ssh_options: its port, algorithm lists, keys
            # and timeouts apply to the tunnel exactly as they would to a
            # direct login. A RemoteHost family without the field (an
            # embedded console can never be a hop) falls back to defaults.
            # Its structured forwards and post_connect hook are NOT applied
            # here (apply_post_connect=False below) — those belong to the
            # hop host's own session; applying them per tunnel would try to
            # rebind a fixed local_forwards port once per hopped target.
            hop_options = getattr(hop_host, "ssh_options", None) or SshOptions()
            kwargs: dict[str, Any] = {"username": cred.login, "tunnel": parent_tunnel}
            tag = f"{host_name} via {hop_id}"
            logger.debug(f"Opening SSH tunnel through {hop_id} for {host_name}")
            # The hop's own ID is the alias: its key is checked and recorded
            # under it, and a refusal on this leg names the hop, not the target.
            return await connect_and_describe(
                tag,
                hop_host.ip,
                kwargs,
                hop_options,
                password=cred.password,
                alias=hop_host.id,
                apply_post_connect=False,
            )

        outer._factory = _create_tunnel  # noqa: SLF001 — intra-package assignment to _SshHopTransport._factory closure
        return outer

    def console_endpoint(self) -> ConsoleEndpoint:
        """Where the ``console`` term dials, resolved from the SERVER's host record.

        ``dial: ssh``: an SSH hop transport INTO the server (built exactly as
        ``hop: <server>`` would be, so a multi-hop server chain and its cred
        resolution come for free); the connection manager forwards
        ``localhost:port`` through it. ``dial: direct``: the server's ``ip``
        and, when the server itself sits behind a hop, a hop transport built
        from the server's own ``hop`` (a fresh one, owned by this endpoint).
        The host's own ``hop`` is never consulted here.

        Raises :class:`~otto.host.errors.ConsoleError` when the server is
        this host itself, and ``KeyError`` (naming this host) when the server
        is not in the lab.
        """
        opts = self.console_options
        if opts.server == self.id:
            raise ConsoleError(
                f"{self.name}: console {opts.server}:{opts.port} (dial={opts.dial}) names "
                f"this host {self.id!r} as its own console server; set "
                f"console_options.server to the lab host that runs the telnet server"
            )
        try:
            server = self._lab_host(opts.server, role="console server")
        except KeyError as exc:
            raise KeyError(f"{self.name}: {exc.args[0]}") from None
        if opts.dial == "ssh":
            return ConsoleEndpoint(
                server_ip=server.ip, hop=self._build_hop_transport(hop_id=opts.server)
            )
        server_hop = server._build_hop_transport() if server.hop else None  # noqa: SLF001 — intra-package access to RemoteHost._build_hop_transport
        return ConsoleEndpoint(server_ip=server.ip, hop=server_hop)
