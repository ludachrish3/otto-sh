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

``RemoteHost`` is intentionally **not** a dataclass. The concrete subclasses
are ``@dataclass(slots=True)`` and the field-ordering rules of dataclass
inheritance (no non-default field after a default one) make a shared dataclass
base awkward. Instead this base owns the *behavior* shared by every remote
host — host naming and the ``SshHopTransport`` machinery — and declares, as
bare annotations, the instance attributes those shared methods rely on. Each
concrete subclass supplies the real ``@dataclass`` fields.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandResult
from ..utils import Status
from .host import BaseHost, is_dry_run
from .login_proxy import Cred

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from ..config.lab import Lab
    from .connections import ConnectionManager
    from .dev_tool import DevTool
    from .host import Expect
    from .interface import Interface
    from .inventory_ref import InventoryRef
    from .lab_info import LabInfo
    from .options import SnmpOptions
    from .power import PowerController
    from .product import Product
    from .session import HostSession, SessionManager
    from .toolchain import Toolchain
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


def make_host_id(
    element: str,
    element_id: int | None,
    board: str | None,
    slot: int | None,
) -> str:
    """Compose a host's ``id`` from its identity fields — the single source of the id format.

    Called by ``RemoteHost._generate_id`` and by host_preferences selector
    matching (so a selector regex matches the same string a built host reports).
    ``element``/``board`` are slugged (§ ``slug``); the only structural
    delimiter is ``_`` between the element-slug and the board-slug. A simple
    ``[a-z0-9]`` element slugs to itself, so its id is byte-identical to the
    pre-slug format.
    """
    element_id_str = "" if element_id is None else f"{element_id}"
    ne = f"{slug(element)}{element_id_str}"
    if board is None:
        return ne
    slot_str = "" if slot is None else f"{slot}"
    return f"{ne}_{slug(board)}{slot_str}"


OsType = str
"""Profile selector recorded on a host (the ``os_type`` field).

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded`` (generic
:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr``
(:class:`~otto.host.embedded_host.ZephyrHost`). Custom profiles add
more names. The base *family* (unix vs embedded) is derived from the host
class, not from this string.
"""


class RemoteHost(BaseHost):
    """Abstract base class for any host reached over a network.

    Concrete subclasses (:class:`~otto.host.unix_host.UnixHost`,
    :class:`~otto.host.embedded_host.EmbeddedHost`) supply the
    transport-specific session/transfer machinery as ``@dataclass`` fields.
    Do not instantiate this class directly.

    The bare annotations below are the instance-attribute *contract* every
    concrete subclass must satisfy. They carry no values, so they create no
    slots and do not participate in the subclasses' ``@dataclass`` field
    collection — they exist purely so the shared methods here (and callers
    holding a ``RemoteHost``-typed reference) type-check.
    """

    # Keep slots harmony with the concrete dataclass subclasses, whose
    # ``@dataclass(slots=True)`` would otherwise produce instances that mix
    # ``__slots__`` with the inherited ``__dict__`` from this base.
    __slots__ = ()

    # --- Shared instance-attribute contract ------------------------------
    ip: str
    """IP address of the host."""

    element: str
    """Network element to which this host belongs."""

    id: str
    """Unique identifier for this host."""

    logical_index: int | None
    """Lab-scoped position among same-``slug(element)`` siblings (1-based, by
    ``element_id`` ascending), stamped by ``Lab._assign_logical_indices``;
    ``None`` when the element is unique in the lab. Display/CLI sugar only —
    never stored, hashed, or used as a correlation key."""

    name: str
    """Human-readable name; auto-generated from ``element``/``board`` if not given."""

    _name_overridden: bool
    """True when ``name`` was supplied at construction (an override); such names
    are never regenerated by the lab-assembly pass."""

    creds: list[Cred]
    """Login credentials for this host — see
    :attr:`~otto.host.unix_host.UnixHost.creds`."""

    metadata: dict[str, Any]
    """Opaque per-host ``metadata`` table from lab data (spec §4); never read by otto."""

    element_metadata: dict[str, Any]
    """Opaque ``metadata`` of the element this host belongs to — a per-host copy."""

    resources: frozenset[str]
    """This host's own reservation identifiers — a slot (spec 2026-08-28
    three-level-reservations §3); empty for containers and ``local``. The lab's
    are on :attr:`lab_info`."""

    element_resources: frozenset[str]
    """The reservation identifiers of this host's ELEMENT (spec 2026-08-28
    three-level-reservations §3) — a per-host copy stamped by the loader,
    exactly like :attr:`element_metadata`; never a lab-entry field."""

    lab_info: "LabInfo"
    """The resolved lab (see :class:`~otto.host.lab_info.LabInfo`), stamped by the loader."""

    inventory_ref: "InventoryRef"
    """Inventory provenance (see :class:`~otto.host.inventory_ref.InventoryRef`); empty for an
    inline host."""

    debug_log_globs: list[str]
    """Remote paths/glob patterns ``get_debug_logs`` fetches (see
    :attr:`~otto.host.host.BaseHost.debug_log_globs`)."""

    log: LogMode
    """Standing per-host logging disposition. ``QUIET`` keeps command I/O in
    ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere."""

    user: str | None
    """User with which to log in, or None to use the first entry in ``creds``."""

    element_id: int | None
    """Network element identifier, or None when no disambiguation is needed."""

    board: str | None
    """Board type name, or None."""

    slot: int | None
    """Physical slot number of the board, or None."""

    site: int | str | None
    """Site the host is installed at (a name or a number), or None."""

    rack: int | str | None
    """Rack within the site (a name or a number), or None."""

    shelf: int | None
    """Shelf / rack position, or None."""

    hop: str | None
    """Host ID of the intermediate hop used to reach this host, or None."""

    os_type: OsType
    """Profile selector recorded on this host (see :data:`OsType`). The base
    *family* (unix vs embedded) is derived from the host class, not this string."""

    os_name: str | None
    """Kernel/OS name (e.g. ``Linux``, ``Zephyr``)."""

    os_version: str | None
    """OS/kernel version string, or None if unspecified."""

    hw_version: str | None
    """Hardware version description, or None. Informational — otto never parses it."""

    sw_version: str | None
    """Software version the host is DECLARED to run, or None; never a probe's
    observation. On the shared contract rather than
    :class:`~otto.host.unix_host.UnixHost` alone since spec 2026-08-28
    host-inventory §4."""

    default_dest_dir: Path
    """Per-host default directory that ``put`` / ``get`` resolve a
    relative or empty ``dest_dir`` against. Lets a fan-out helper like
    ``do_for_all_hosts`` pass one generic destination (``Path()``) and
    have each host land the files where its filesystem actually lives —
    e.g. ``/RAM:`` on a Zephyr FAT target, ``/lfs`` on a Zephyr LittleFS
    target. Defaults to ``Path()`` on Unix, which preserves the existing
    "relative path lands in the SSH user's home" behavior."""

    snmp: "SnmpOptions | None"
    """Optional per-host SNMP polling config (lab ``snmp`` block), or None. When
    set, otto's monitor collects this host over SNMP instead of by running shell
    commands. Declared on both concrete subclasses; see
    :class:`~otto.host.options.SnmpOptions`."""

    max_filename_len: int
    """Upper bound on the basename length (including extension) accepted by
    the target's filesystem. Defaults to ``255`` on every concrete subclass
    — the Linux ``NAME_MAX``, also the cap for ext4 / XFS / Btrfs / NTFS
    and the typical LittleFS ceiling. Override per-host when the firmware
    enforces a tighter limit (e.g. ``32`` for a Zephyr build that sets
    ``CONFIG_FS_FATFS_MAX_LFN=32``, or ``12`` for a stock FAT 8.3 build
    without LFN support). ``put`` / ``get`` reject over-limit names up
    front with a clear message instead of letting the device produce an
    opaque error like ``-ENOENT`` or ``File name too long``."""

    interfaces: dict[str, "Interface"]
    """Named network devices, keyed by the netdev name (e.g.
    ``{"eth0": Interface(ip="10.0.0.5"), "eth1": Interface(ip="192.168.1.5")}``).
    The *primary* address stays :attr:`ip`; this map is additive and optional
    (empty by default). Resolve a name (or pass a literal through) with
    :meth:`address_for`."""

    products: "list[Product]"
    """Software-under-test deployed to this host (see
    :attr:`~otto.host.host.BaseHost.products`)."""

    dev_tools: "list[DevTool]"
    """Repo-internal tooling deployed to this host (see
    :attr:`~otto.host.host.BaseHost.dev_tools`)."""

    toolchain: "Toolchain"
    """Toolchain for this host's products (see
    :attr:`~otto.host.host.BaseHost.toolchain`)."""

    power_control: "PowerController | None"
    """Pluggable power backend (see :attr:`~otto.host.host.BaseHost.power_control`)."""

    # --- Connection-state contract ---------------------------------------
    # Concrete subclasses supply these as real ``@dataclass`` fields (a
    # ``ConnectionManager`` and a ``SessionManager``). Declared here as bare
    # annotations so the shared lifecycle below — ``_connected`` — type-checks
    # against every remote host.
    _connections: "ConnectionManager"
    _session_mgr: "SessionManager"
    _lab: "Lab | None"

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
                REFUSED — see Raises below.

        Returns:
            A :class:`~otto.result.CommandResult`; ``value`` holds the output.
            Exit code 0 → ``Status.Success``; non-zero → ``Status.Failed``.

        Raises:
            NotImplementedError: *user* is not None. The refusal is the FIRST
                line of the body, above the dry-run arm, so a dry run refuses
                too rather than reporting a decline for a call this family
                could never honour.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: run(user=...) is not supported on "
                f"{type(self).__name__} — the persistent shell has no "
                f"user-switching semantics"
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
        """Space-joined, ORIGINAL-CASE display label: ``element [logical] [board] [slot]``.

        The number is the lab-scoped ``logical_index`` (never the raw
        ``element_id``), present only when the element repeats in the lab. Parts
        are omitted when absent. This is a label, not an id — case is preserved.
        """
        parts: list[str] = [self.element]
        if self.logical_index is not None:
            parts.append(str(self.logical_index))
        if self.board:
            parts.append(self.board)
            if self.slot is not None:
                parts.append(str(self.slot))
        return " ".join(parts)

    def _generate_id(self) -> str:
        return make_host_id(self.element, self.element_id, self.board, self.slot)

    @property
    def _element_id_str(self) -> str:

        if self.element_id is None:
            return ""

        return f"{self.element_id}"

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
