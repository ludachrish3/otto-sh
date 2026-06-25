"""
Abstract base for network-reached hosts.

``RemoteHost`` is the common ancestor of every host class that talks to a
target across a network — :class:`~otto.host.unix_host.UnixHost` (SSH/Telnet to a bash shell),
:class:`~otto.host.embedded_host.EmbeddedHost` (telnet to an RTOS shell), and any future siblings such
as a Windows-host class. It is deliberately distinct from :class:`~otto.host.local_host.LocalHost`,
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

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..logger import get_otto_logger
from .host import BaseHost

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

    from ..configmodule.lab import Lab
    from ..utils import CommandStatus
    from .connections import ConnectionManager
    from .options import SnmpOptions
    from .power import PowerController
    from .product import Product
    from .session import SessionManager

logger = get_otto_logger()


def make_host_id(
    element: str,
    element_id: int | None,
    board: str | None,
    slot: int | None,
) -> str:
    """Compose a host's ``id`` from its identity fields — the single source of
    the id format, called by ``RemoteHost._generate_id`` and by host_preferences
    selector matching (so a selector regex matches the same string a built host
    reports). ``element_id`` renders as its number; ``board``/``slot`` lower-case
    with a ``_`` join.
    """
    element_id_str = "" if element_id is None else f"{element_id}"
    ne = f"{element.lower()}{element_id_str}"
    if board is None:
        return ne
    slot_str = "" if slot is None else f"{slot}"
    return f"{ne}_{board.lower()}{slot_str}"


OsType = str
"""Profile selector recorded on a host (the ``os_type`` field).

Built-ins: ``unix`` (:class:`~otto.host.unix_host.UnixHost`), ``embedded`` (generic
:class:`~otto.host.embedded_host.EmbeddedHost`), ``zephyr`` (:class:`~otto.host.embedded_host.ZephyrHost`). Custom profiles add
more names. The base *family* (unix vs embedded) is derived from the host
class, not from this string.
"""


class RemoteHost(BaseHost):
    """Abstract base class for any host reached over a network.

    Concrete subclasses (:class:`~otto.host.unix_host.UnixHost`, :class:`~otto.host.embedded_host.EmbeddedHost`) supply the
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

    name: str
    """Human-readable name; auto-generated from ``element``/``board`` if not given."""

    creds: dict[str, str]
    """Users and their respective passwords for this host."""

    resources: set[str]
    """Names of resources required to use this host."""

    log: bool
    """Whether this host logs its output to stdout and log files."""

    user: str | None
    """User with which to log in, or None to use the first entry in ``creds``."""

    element_id: int | None
    """Network element identifier, or None when no disambiguation is needed."""

    board: str | None
    """Board type name, or None."""

    slot: int | None
    """Physical slot number of the board, or None."""

    hop: str | None
    """Host ID of the intermediate hop used to reach this host, or None."""

    os_type: OsType
    """Profile selector recorded on this host (see :data:`OsType`). The base
    *family* (unix vs embedded) is derived from the host class, not this string."""

    os_name: str | None
    """Kernel/OS name (e.g. ``Linux``, ``Zephyr``)."""

    os_version: str | None
    """OS/kernel version string, or None if unspecified."""

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

    interfaces: dict[str, str]
    """Named secondary interface addresses, keyed by interface name (e.g.
    ``{"mgmt": "10.0.0.5", "data": "192.168.1.5"}``). The *primary* address
    stays :attr:`ip`; this map is additive and optional (empty by default).
    Resolve a name (or pass a literal through) with :meth:`address_for`."""

    products: list[Product]
    """Software-under-test deployed to this host (see
    :attr:`~otto.host.host.BaseHost.products`)."""

    power_control: PowerController | None
    """Pluggable power backend (see :attr:`~otto.host.host.BaseHost.power_control`)."""

    # --- Connection-state contract ---------------------------------------
    # Concrete subclasses supply these as real ``@dataclass`` fields (a
    # ``ConnectionManager`` and a ``SessionManager``). Declared here as bare
    # annotations so the shared lifecycle below — ``_connected`` — type-checks
    # against every remote host.
    _connections: ConnectionManager
    _session_mgr: SessionManager
    _lab: Lab | None

    async def verify_connection(self) -> 'CommandStatus':  # pragma: no cover
        raise NotImplementedError from None

    ####################
    #  Connection state / lifecycle
    ####################

    @property
    def _connected(self) -> bool:
        """Whether the host has any current connections or live sessions."""
        return self._session_mgr.has_live_sessions or self._connections.connected

    async def is_reachable(self, timeout: float = 10.0) -> bool:
        """Probe by attempting a connection (no command), bounded by *timeout*."""
        try:
            result = await asyncio.wait_for(self.verify_connection(), timeout)
        except Exception:
            return False
        return result.status.is_ok

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
        if str(dest_dir) in ('', '.'):
            return self.default_dest_dir
        return self.default_dest_dir / dest_dir

    ####################
    #  Naming
    ####################

    def _generate_name(self) -> str:

        if not self.board:
            return f"{self.element}{self._element_id_str}"

        return f"{self.element}{self._element_id_str} {self.board}{self._slot_str}"

    def _generate_id(self) -> str:
        return make_host_id(self.element, self.element_id, self.board, self.slot)

    @property
    def _element_id_str(self) -> str:

        if self.element_id is None:
            return ''

        return f"{self.element_id}"

    @property
    def _slot_str(self) -> str:

        if self.slot is None:
            return ''

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
        return self.interfaces.get(name_or_literal, name_or_literal)

    ####################
    #  Hop transport
    ####################

    def _build_hop_transport(self):
        """Build an ``SshHopTransport`` for reaching this host through its hop.

        The transport wraps a factory coroutine that lazily resolves the hop
        host ID via the config module and opens a dedicated SSH connection to
        it. Each target host gets its own tunnel connection (not shared with
        the hop's own connections).

        For multi-hop chains the transport holds a reference to its parent
        :class:`SshHopTransport`, so ``close()`` cascades down the entire
        chain — every intermediate SSH connection (and its underlying
        asyncio transport) gets closed explicitly. Without that linkage,
        the outermost SSH connection (e.g. carrot in an
        otto→carrot→tomato→pepper chain) is owned only by asyncssh's
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
            raise ValueError(f"_build_hop_transport called on host {self.name!r} with no hop configured")
        host_name = self.name

        # The outer SshHopTransport — its ``_parent`` is set lazily on the
        # first call to ``_create_tunnel`` (when the configmodule is
        # available and we can resolve the hop chain). Linking ``_parent``
        # makes ``close()`` walk the chain so every intermediate SSH
        # connection's asyncio transport gets explicitly closed.
        # placeholder factory is replaced below; needed to satisfy the
        # constructor without doing anything that requires the configmodule.
        async def _placeholder(*args, **kwargs):
            raise RuntimeError("SshHopTransport factory not initialized")
        outer = SshHopTransport(_placeholder)

        async def _create_tunnel(
            _visited: set[str] | None = None,
        ) -> 'SSHClientConnection':
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
            hop_host = cast(RemoteHost, lab.hosts[hop_id])

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
                if outer._parent is None:
                    outer._parent = hop_host._build_hop_transport()
                parent_tunnel = await outer._parent.get_tunnel(_visited=visited)

            user, password = next(iter(hop_host.creds.items())) if hop_host.user is None else (hop_host.user, hop_host.creds[hop_host.user])
            logger.debug(f"Opening SSH tunnel through {hop_id} for {host_name}")
            conn = await _ssh_connect(
                hop_host.ip,
                username=user,
                password=password,
                known_hosts=None,
                tunnel=parent_tunnel,
            )
            return conn

        outer._factory = _create_tunnel
        return outer
