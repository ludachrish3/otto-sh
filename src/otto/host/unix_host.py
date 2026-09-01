"""
Unix host class.

Unix hosts (Linux being the concrete kernel today; macOS/BSD trivially
compatible) accessed over the network via SSH or Telnet, with bash as the
remote shell. Manages two responsibilities:

- Command execution

  - SSH (via asyncssh)
  - telnet (via telnetlib3)

- File transfers

  - SCP (via asyncssh)
  - SFTP (via asyncssh)
  - FTP (via aioftp)
  - netcat (via commands on the client and host)

All kinds of host connections, for command execution and for file transfers, should be able to
establish a connection first, keep it open, and then use it for multiple commands and transfers.
Host connections can be explicitly closed by calling the `.close()` method (or via ``async with``),
and any still-open connections are closed automatically when the host's context scope exits.

The `.run()` method runs a single command (str) or a list of commands. Depending on the `.term`
value the correct connection type (ssh or telnet) is used without being specified as an argument.

The `.put()` and `.get()` methods both take a single file or a list of files. Depending on the
`.transfer` value the correct connection type (scp, sftp, ftp, or netcat) is used without being
specified as an argument.

History: this class was originally named ``RemoteHost``. With the introduction of
:class:`~otto.host.embedded_host.EmbeddedHost` for bare-metal/RTOS targets, ``RemoteHost`` is
now an abstract base for any network-reached host and the bash-on-SSH/Telnet concrete class lives
here as ``UnixHost``.
"""

# TODO: Consider having a single function that takes a connection, and does the lower level asyncio stuff  # noqa: E501 — TODO comment
# For example, run could dynamically dispatch to _runSshCmds(), which would pass along the _ssh_conn member  # noqa: E501 — TODO comment
# Then the _ssh_conn would be the connection used in an "async with" block to issue the command.
# Main problem here is that eash library uses its own method names to run commands and put/get files
# Possibly make the homegrown TelnetClient class mirror asyncssh? that could really help with design symmetry.  # noqa: E501 — TODO comment
import asyncio
import logging
import socket
from dataclasses import (
    dataclass,
    field,
    replace,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    cast,
)

from typing_extensions import override

if TYPE_CHECKING:
    from ..config.lab import Lab

from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import (
    Arg,
    Exclude,
    Opt,
    Status,
    WaitTimeoutError,
    cli_exposed,
    wait_for_async,
)
from .capability import IMPAIRER_RESOLVER, TERM_RESOLVER, TRANSFER_RESOLVER
from .command_frame import CommandFrame, build_command_frame
from .connections import (
    ConnectionManager,
    TermContext,
    build_term_backend,
    teardown_step,
)
from .dev_tool import DevTool
from .errors import UnsupportedOnUserlandError
from .file_ops import PosixFileOps
from .host import (
    Host,
    SuppressCommandOutput,
    is_dry_run,
)
from .interact import run_ssh_login, run_telnet_login
from .interface import Interface
from .inventory_ref import InventoryRef
from .lab_info import LabInfo
from .login_proxy import Cred, LoginProxyError, cred_for, resolve_chain
from .options import (
    FtpOptions,
    NcOptions,
    ScpOptions,
    SftpOptions,
    SnmpOptions,
    SshOptions,
    TelnetOptions,
    UserlandOptions,
)
from .power import PowerController, power_control_from_spec
from .privilege import PosixPrivilege
from .product import Product
from .remote_host import OsType, RemoteHost
from .session import (
    SessionManager,
)
from .telnet import TelnetClient
from .toolchain import Toolchain
from .transfer import (
    TransferContext,
    UnixFileTransfer,
    build_transfer_backend,
)
from .userland import (
    APPLET_ABSENT,
    APPLET_PRESENT,
    Userland,
    applet_capability,
    refuse_if_gapped,
)

logger = logging.getLogger(__name__)

_RECOVERY_PROBE_TIMEOUT = 10.0
"""Per-attempt bound for the post-reboot shell probe (`exec "true"`)."""

GNU_SHUTDOWN = "shutdown -h now"
"""The coreutils/systemd spelling, and still the default when nothing says otherwise.

Exactly what ``UnixHost.shutdown`` emitted, unconditionally, before
:func:`shutdown_command` existed -- which is the whole reason it is the fallback
rather than :data:`BUSYBOX_POWEROFF`. A host whose applet batch could not be
asked gets this and is therefore UNCHANGED by the probe; falling back the other
way would quietly re-spell the command on every host otto has ever run against,
which is not something any measurement here asked for.
"""

BUSYBOX_POWEROFF = "poweroff"
"""The spelling a BusyBox device actually has. Measured, not inferred.

``shutdown`` is absent and ``poweroff`` present on all five matrix artifacts --
1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 -- recorded per row by
``tests/integration/busybox_bed/test_applet_userland.py`` and by the ``shutdown-command``
record in :data:`~otto.host.userland.GAPS`. Not a BusyBox-only spelling: a GNU
host normally has ``poweroff`` too, so this arm is only ever reached on a device
that answered "no ``shutdown``".
"""


def shutdown_command(userland: "Userland", *, host: str = "") -> str:
    """Return the shutdown spelling *userland* actually has.

    **The gap registry's fourth product call site, and the first that FIXES a
    surface rather than declining it.** The other three --
    :func:`otto.host.session.refuse_if_line_editor_would_truncate`,
    :func:`otto.host.daemon.refuse_if_launch_wrapper_needs_bash` and
    :func:`otto.host.file_ops.refuse_if_base64_is_absent` -- all answer a
    measured device limitation by refusing. This one answers it by asking the
    device which spelling it has and emitting that, so a BusyBox host is shut
    down instead of being told otto cannot.

    THREE OUTCOMES:

    * ``shutdown`` present -- :data:`GNU_SHUTDOWN`, unchanged from what otto
      emitted before this function existed. A GNU host takes this arm and
      nothing about it moves;
    * ``shutdown`` absent, ``poweroff`` present -- :data:`BUSYBOX_POWEROFF`.
      Every matrix row lands here;
    * both measured absent -- refused through
      :func:`~otto.host.userland.refuse_if_gapped`, so the message, the
      evidence and the docs anchor are the ``shutdown-command`` record's and
      not a second, drifting copy. Downgrading that record to ``untested``
      stops the refusal and the caller falls back to :data:`GNU_SHUTDOWN` --
      the CALLER decides this host is in the measured class, the TABLE decides
      whether that class is refused at all.

    **WHY THE CHOICE READS THE VALUE ALONE WHILE THE REFUSAL ASKS
    :meth:`~otto.host.userland.Userland.is_settled` FIRST.** The two
    neighbouring guards do the opposite of the first half of that and the
    difference is deliberate, not an inconsistency. ``is_settled`` draws the
    line itself: *a consumer that DEGRADES may read the value alone --
    degrading on a guess costs a weaker mode. A consumer that REFUSES has to
    ask this first.* Picking a spelling is a degrade: the worst a guess buys is
    a command the device does not have, which is precisely the situation otto
    was in before this function existed. Refusing is not: an applet batch that
    could not be ASKED must never become a verdict that the device has neither
    spelling, or an sshd at its ``MaxSessions`` ceiling turns a healthy host
    into an un-shutdownable one.

    That gate is belt-and-braces TODAY and is written anyway, because what
    makes it redundant is a value that can change. ``_UNASKABLE_DEFAULTS`` maps
    every applet to :data:`~otto.host.userland.APPLET_PRESENT`, so an unasked
    batch currently reads as "``shutdown`` is there", takes the first arm, and
    emits what otto always emitted. Flip that default to ``absent`` and the
    unsettled host would fall through to the refusal with nothing measured --
    which is what the ``is_settled`` calls stop, and what
    ``test_an_unsettled_absence_degrades_rather_than_refusing`` holds by
    flipping exactly that default.

    **COSTS NOTHING EXTRA ON THIS PATH.** Reading an applet needs a resolved
    userland, and ``UnixHost.shutdown`` issues its command with ``sudo=True``,
    which makes ``Host.run`` await ``PosixPrivilege._prepare_elevation`` --
    i.e. the same :meth:`~otto.host.userland.Userland.resolve` -- before it
    sends anything. Resolving up front only moves that round earlier in the
    same call; ``resolve()`` is idempotent on what is settled, so the second
    one costs a lock acquisition and nothing on the wire.

    Args:
        userland: the host's RESOLVED capabilities.
            :meth:`~otto.host.userland.Userland.resolve` must have been awaited
            -- :meth:`~otto.host.userland.Userland.has_applet` raises otherwise,
            deliberately, rather than answering from an empty table.
        host: the host's name, for the refusal message only. Never changes the
            verdict.

    Returns:
        The command to emit.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: the device was measured to
            have neither spelling and the ``shutdown-command`` record is
            ``measured-broken``. Nothing was attempted.
    """
    if userland.has_applet("shutdown") == APPLET_PRESENT:
        return GNU_SHUTDOWN
    if userland.has_applet("poweroff") == APPLET_PRESENT:
        return BUSYBOX_POWEROFF
    if _measured_absent(userland, "shutdown") and _measured_absent(userland, "poweroff"):
        refuse_if_gapped("shutdown-command", host=host, attempted=GNU_SHUTDOWN)
    return GNU_SHUTDOWN


def _measured_absent(userland: "Userland", applet: str) -> bool:
    """Whether *applet* is absent AND that absence was declared or measured.

    The ``is_settled``-first shape :meth:`~otto.host.userland.Userland.has_applet`
    documents for a consumer that refuses. *applet* is validated twice against
    the same closed list -- once by ``has_applet`` and once by
    :func:`~otto.host.userland.applet_capability` on the way into
    :meth:`~otto.host.userland.Userland.is_settled` -- so a typo raises here
    rather than becoming a condition that quietly never fires.
    """
    return (
        userland.is_settled(applet_capability(applet))
        and userland.has_applet(applet) == APPLET_ABSENT
    )


@dataclass(slots=True)
class UnixHost(PosixPrivilege, PosixFileOps, RemoteHost):
    """Unix host accessed via SSH or Telnet, with bash as the remote shell."""

    ip: str
    """IP address of the host."""

    creds: list[Cred]
    """Login credentials for this host — one :class:`~otto.host.login_proxy.Cred`
    entry per account, in priority order (the first entry is the default
    login when ``user`` is unset). A proxied entry (``Cred.proxy`` set)
    cannot be reached by direct authentication; :meth:`cred` /
    :attr:`default_cred` and the connection-layer chain resolution
    (:func:`~otto.host.login_proxy.resolve_chain`) handle that."""

    element: str = field(repr=False)
    """Network element to which this host belongs."""

    os_type: OsType = "unix"
    """Default profile selector for a bare :class:`UnixHost`. A custom
    unix-based profile (e.g. ``ubuntu-22.04``) records its own name here."""

    os_name: str | None = "Linux"
    """Kernel/OS name. Defaults to ``Linux`` (the concrete Unix kernel today)."""

    os_version: str | None = None
    """OS/kernel version string, or None if unspecified."""

    name: str = ""
    """Human readable name to represent the host. Automatically generated if not provided."""

    user: str | None = None
    """User with which to log in. If not provided, the first entry in `creds` will be used."""

    element_id: int | None = field(default=None, repr=False)
    """Network element identifier to which this host belongs.
    None indicates there are no other NEs of this type and a number is not needed."""

    board: str | None = field(default=None, repr=False)
    """Name of the board type to which this host belongs."""

    slot: int | None = field(default=None, repr=False)
    """Phyiscal slot number of the board to which this host belongs."""

    site: int | str | None = field(default=None, repr=False)
    """Site the host is installed at (a name or a number)."""

    rack: int | str | None = field(default=None, repr=False)
    """Rack within the site (a name or a number)."""

    shelf: int | None = field(default=None, repr=False)
    """Shelf / rack position."""

    hw_version: str | None = None
    """Hardware version description."""

    sw_version: str | None = None
    """Software version description."""

    term: str = "ssh"
    """Protocol used to issue terminal commands."""

    is_virtual: bool = False
    """Determines whether a host is a VM or not."""

    has_bash: bool = True
    """Whether this host has a working ``bash`` a command can be tagged and
    exec'd through (``bash -c 'exec -a …'``). Tunnel discovery
    (:mod:`otto.tunnel.discovery`) scans only ``has_bash`` hosts. Unix hosts have
    bash by default; override to ``False`` in ``lab.json`` for a host that
    defies the norm."""

    docker_capable: bool = False
    """Whether this host can run Docker containers (i.e., has a docker daemon
    and the configured user can talk to it). Containers declared by projects
    are scheduled onto docker-capable hosts; non-capable hosts are skipped."""

    roles: list[str] = field(default_factory=list)
    """Role tags this lab assigned to the host ("edge", "builder"). Docker
    use-case fragments name a role; placement resolves it to the unique
    docker-capable host carrying the tag (otto.docker.resolve)."""

    transfer: str = "scp"
    """Protocol used to transfer files."""

    valid_terms: list[str] = field(default_factory=lambda: ["ssh", "telnet"])
    """Closed menu of term backends this host supports (active is ``term``)."""

    valid_transfers: list[str] = field(default_factory=lambda: ["scp", "sftp", "ftp", "nc"])
    """Closed menu of transfer backends this host supports (active is ``transfer``)."""

    impairer: str = "netem"
    """Active impairer used for link-impairment placements on this host."""

    valid_impairers: list[str] = field(default_factory=lambda: ["netem"])
    """Closed menu of impairers this host supports (active is ``impairer``)."""

    default_dest_dir: Path = field(default_factory=Path)
    """Default landing directory for ``put`` / ``get`` when the caller
    supplies an empty or relative ``dest_dir``. Defaults to ``Path()``,
    which preserves the existing behavior — SCP/SFTP resolve a relative
    destination against the SSH user's home directory. Override per-host
    to land transfers in a fixed location regardless of the caller's
    argument. See :attr:`~otto.host.remote_host.RemoteHost.default_dest_dir`."""

    max_filename_len: int = 255
    """Upper bound on the basename length (including extension) accepted by
    the target's filesystem. Defaults to ``255`` — the Linux ``NAME_MAX``,
    also the cap for ext4 / XFS / Btrfs / NTFS. Lower it for hosts on a
    tighter filesystem; see :attr:`~otto.host.remote_host.RemoteHost.max_filename_len` for details.
    Over-limit names are rejected by :meth:`put` / :meth:`get` with a
    self-explaining error instead of an opaque ``File name too long``
    midway through the transfer."""

    ssh_options: SshOptions = field(default_factory=SshOptions, repr=False)
    """Connection options for SSH sessions (port, timeout, known_hosts,
    port-forwarding rules, etc.)."""

    telnet_options: TelnetOptions = field(default_factory=TelnetOptions, repr=False)
    """Connection options for telnet sessions (port, cols/rows, auto-resize, etc.)."""

    sftp_options: SftpOptions = field(default_factory=SftpOptions, repr=False)
    """Connection options for SFTP file transfers."""

    scp_options: ScpOptions = field(default_factory=ScpOptions, repr=False)
    """Connection options for SCP file transfers."""

    ftp_options: FtpOptions = field(default_factory=FtpOptions, repr=False)
    """Connection options for FTP file transfers (port, encoding, FTPS, etc.)."""

    nc_options: NcOptions = field(default_factory=NcOptions, repr=False)
    """Connection options for netcat file transfers (nc executable, port
    strategy, listener check, etc.)."""

    userland_options: UserlandOptions = field(default_factory=UserlandOptions, repr=False)
    """Declared answers about this host's userland — which elevation mechanism
    it has, which ``timeout`` convention its applet speaks, and so on. Every
    field defaults to ``None``, meaning "ask the device"; a declared value wins
    outright and skips the probe.

    Not a per-protocol table like its neighbours. These are facts about the
    DEVICE, so one table answers for every backend, and ``_userland()`` turns
    it into the single :class:`~otto.host.userland.Userland` this host's
    consumers share. See
    ``docs/superpowers/specs/2026-08-11-busybox-host-support-design.md``."""

    command_frame: CommandFrame | None = None
    """Shell-framing dialect for this host's bash console. ``None`` (the
    default) lets the :class:`~otto.host.session.SessionManager` use its
    built-in :class:`~otto.host.command_frame.BashFrame`, preserving the
    historical behavior exactly. Lab data may name a registered frame by string
    (resolved in ``__post_init__``); a profile or subclass may supply an
    instance. Promoted to a common field in Phase A so any host can declare its
    dialect — see :attr:`~otto.host.embedded_host.EmbeddedHost.command_frame`."""

    shell_history: bool = False
    """Whether otto's commands are recorded in this host's shell history.

    Defaults False: otto neutralizes ``HISTFILE`` on every interactive shell
    it opens, so automation traffic doesn't bury a human's own history on a
    shared lab box. Set True where otto's commands should stay auditable from
    the shell's own history file.

    Suppression is best-effort and silent — see
    :meth:`~otto.host.command_frame.BashFrame.quiet_history` for the payload
    and why it neutralizes ``HISTFILE`` rather than clearing ``HISTSIZE``. It
    covers the persistent interactive sessions otto opens (SSH, telnet,
    including shells reached through a login proxy); ``exec``-channel commands
    were never recorded in the first place, and ``otto login`` deliberately
    leaves a human's shell alone."""

    snmp: SnmpOptions | None = field(default=None, repr=False)
    """Optional SNMP polling config (lab ``snmp`` block). When set, otto's
    monitor collects this host's metrics over SNMP instead of running shell
    commands. SNMP monitoring is not embedded-only — a Unix host may use it to
    poll a real SNMP agent. See :class:`~otto.host.options.SnmpOptions`."""

    hop: str | None = None
    """Host ID of the intermediate hop used to reach this host, or None for direct connection."""

    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    """Opaque per-host ``metadata`` from lab data. Never interpreted by otto."""

    element_metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    """Opaque ``metadata`` of this host's element; a per-host copy (loader-stamped)."""

    resources: frozenset[str] = field(default_factory=frozenset, repr=False)
    """This host's own reservation identifiers — a slot; a copy of the spec's set.
    See :attr:`~otto.host.remote_host.RemoteHost.resources`."""

    element_resources: frozenset[str] = field(default_factory=frozenset, repr=False)
    """The element's reservation identifiers; stamped by the loader, never by the
    spec. See :attr:`~otto.host.remote_host.RemoteHost.element_resources`."""

    lab_info: LabInfo = field(default_factory=LabInfo, repr=False)
    """The resolved lab this host came from (loader-stamped, like ``source_lab``)."""

    inventory_ref: InventoryRef = field(default_factory=InventoryRef, repr=False)
    """Inventory provenance; empty unless this host was resolved from a record."""

    debug_log_globs: list[str] = field(default_factory=list)
    """Remote paths/glob patterns ``get_debug_logs`` fetches. Default empty.
    See :attr:`~otto.host.host.BaseHost.debug_log_globs`."""

    interfaces: dict[str, Interface] = field(default_factory=dict, repr=False)
    """Named network devices
    (see :attr:`~otto.host.remote_host.RemoteHost.interfaces`).
    Resolve with :meth:`~otto.host.remote_host.RemoteHost.address_for`."""

    products: list["Product"] = field(default_factory=list)
    """Software-under-test deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.products`."""

    dev_tools: list["DevTool"] = field(default_factory=list)
    """Repo-internal tooling deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.dev_tools`."""

    power_control: "PowerController | None" = None
    """Pluggable power backend. Lab data declares it by string (a config-free
    controller type) or a ``[power]`` table (``{type, on_cmd, off_cmd, ...}``);
    ``__post_init__`` coerces it to an instance. None → power()/reboot(hard=True)
    fail loud. See :attr:`~otto.host.host.BaseHost.power_control`."""

    log: LogMode = field(default=LogMode.NORMAL, repr=False)
    """Standing per-host logging disposition. ``QUIET`` keeps this host's command
    I/O in ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere
    (warnings/errors are unaffected)."""

    log_stdout: bool = field(default=True, repr=False)
    """Determines whether this host should log its output to stdout.
    Commands and their output are still logged to log files if `log` is `True`."""

    toolchain: Toolchain = field(default_factory=Toolchain, repr=False)
    """Toolchain associated with this host's products.  Used by the
    coverage pipeline to select the correct ``gcov`` and ``lcov``
    binaries.  Defaults to system-installed tools."""

    _lab: "Lab | None" = field(default=None, compare=False, repr=False, kw_only=True)
    """Back-reference to the owning Lab, wired by Lab.add_host. Lets hop
    resolution use self._lab.hosts[...] instead of ambient state."""

    id: str = field(init=False, repr=False)
    """Unique identifier for this host."""

    logical_index: int | None = field(default=None, init=False, repr=False)
    """Lab-scoped position among same-``slug(element)`` siblings (1-based, by
    ``element_id`` ascending), stamped by ``Lab._assign_logical_indices``;
    ``None`` when the element is unique in the lab. Display/CLI sugar only —
    never stored, hashed, or used as a correlation key."""

    _name_overridden: bool = field(default=False, init=False, repr=False)
    """True when ``name`` was supplied at construction (an override); such names
    are never regenerated by the lab-assembly pass."""

    _connection_factory: type[ConnectionManager] | None = field(default=None, init=True, repr=False)
    """Optional ConnectionManager subclass for dependency injection (e.g. test doubles).
    When None, the real ConnectionManager is used."""

    _connections: ConnectionManager = field(init=False, repr=False)
    """Manages all raw transport connections for this host."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages persistent shell sessions for this host."""

    _file_transfer: UnixFileTransfer = field(init=False, repr=False)
    """Handles all file transfer protocols for this host."""

    _userland_cache: Userland | None = field(default=None, init=False, repr=False, compare=False)
    """Backing store for :meth:`_userland` — see there for why it is built on
    demand rather than by a ``default_factory``."""

    ####################
    #  Privilege
    ####################

    @override
    def _sudo_password(self) -> str | None:
        """Return the current user's password, used for ``sudo -S``."""
        c = cred_for(self.creds, self.current_user)
        return c.password if c else None

    @override
    def _userland(self) -> Userland:
        """Return this host's one :class:`~otto.host.userland.Userland`, building it on first ask.

        **One per host instance, never rebuilt.** Every consumer — the
        elevation path in :class:`~otto.host.privilege.PosixPrivilege`, the
        netcat backend's listener cap — reads through this, so the probe round
        is paid once for the host rather than once per reader. That matters
        more than it sounds: ``resolve()`` serializes its callers, and the
        heaviest consumer fans out over files against a server that REFUSES
        excess channels rather than queueing them.

        **Built here rather than by a ``default_factory``,** which is what the
        sibling ``*_options`` fields use. A ``Userland`` is not inert data: it
        needs a ``run`` callable bound to this host, and a field default has no
        access to ``self``. Building it on demand also means a subclass that
        replaces ``__post_init__`` cannot leave the seam uninitialized, and a
        ``dataclasses.replace`` copy — which skips ``init=False`` fields — gets
        its own rather than inheriting one whose ``run`` still points at the
        original instance.

        The lambda is deliberate, and matches :meth:`_build_file_transfer`'s:
        binding ``self.exec`` eagerly would freeze out a test (or a subclass)
        that swaps the method afterwards.
        """
        if self._userland_cache is None:
            self._userland_cache = Userland(
                self.userland_options,
                lambda *a, **kw: self.exec(*a, **kw),  # noqa: PLW0108 — late-bind self for monkeypatching
            )
        return self._userland_cache

    def cred(self, login: str) -> Cred:
        """Return the cred entry for *login*; loud lookup listing known logins."""
        found = cred_for(self.creds, login)
        if found is None:
            known = ", ".join(c.login for c in self.creds) or "<none>"
            raise LoginProxyError(f"{self.name}: no cred for login {login!r}. Known: {known}")
        return found

    @property
    def default_cred(self) -> Cred | None:
        """First cred entry — the default login user."""
        return self.creds[0] if self.creds else None

    def __post_init__(self) -> None:

        self.id = self._generate_id()
        self._name_overridden = bool(self.name)
        if not self._name_overridden:
            self.name = self._generate_name()  # no lab context yet -> no number

        # Lab JSON serializes ``default_dest_dir`` as a string; coerce so
        # ``_resolve_dest`` can use Path arithmetic uniformly.
        if not isinstance(self.default_dest_dir, Path):
            self.default_dest_dir = Path(self.default_dest_dir)

        # Lab JSON declares the frame dialect by name; coerce a string to the
        # registered instance. None is left as-is (SessionManager applies bash).
        if isinstance(self.command_frame, str):
            self.command_frame = build_command_frame(self.command_frame)

        self.power_control = power_control_from_spec(self.power_control)

        TERM_RESOLVER.validate_choice(self.valid_terms, self.term)
        TRANSFER_RESOLVER.validate_choice(self.valid_transfers, self.transfer)
        IMPAIRER_RESOLVER.validate_choice(self.valid_impairers, self.impairer)

        self._connections = self._build_connections()
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            creds=self.creds,
            host_id=self.id,
            shell_history=self.shell_history,
        )
        self._file_transfer = self._build_file_transfer()

    @property
    def _creds(self) -> tuple[str, str | None]:
        """Provide the (username, password) pair from creds. Delegates to ConnectionManager."""
        return self._connections.credentials

    ####################
    #  Connection
    ####################

    def rebuild_connections(self) -> None:
        """Recreate the ConnectionManager and dependents.

        Useful after changing ``hop`` or when the host must reconnect on a
        new event loop (e.g. after ``pytest.main()`` returns and coverage
        collection starts in a fresh ``asyncio.run()``).
        """
        self._connections = self._build_connections()
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            creds=self.creds,
            host_id=self.id,
            shell_history=self.shell_history,
        )
        self._file_transfer = self._build_file_transfer()

    def _build_connections(self) -> ConnectionManager:
        """Construct the connection backend for the current ``term`` via the registry seam.

        Honors the ``_connection_factory`` test override. Shared by ``__post_init__`` /
        ``rebuild_connections`` (and the override-copy seam, via ``dataclasses.replace``) so a
        custom term backend builds the right class.
        """
        hop_transport = self._build_hop_transport() if self.hop else None
        term_ctx = TermContext(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term=self.term,
            name=self.name,
            hop=hop_transport,
            ssh_options=self.ssh_options,
            telnet_options=self.telnet_options,
            sftp_options=self.sftp_options,
            ftp_options=self.ftp_options,
        )
        conn_cls = self._connection_factory or build_term_backend(self.term)
        return conn_cls.create(term_ctx)

    def _build_file_transfer(self) -> UnixFileTransfer:
        """Construct the transfer backend for the current ``transfer`` via the registry seam.

        Uses ``self._connections``. Shared by ``__post_init__`` / ``rebuild_connections``
        (and the override-copy seam, via ``dataclasses.replace``) so a custom
        transfer backend builds the right class.
        """
        return cast(
            "UnixFileTransfer",
            build_transfer_backend(self.transfer).create(
                TransferContext(
                    transfer=self.transfer,
                    host_name=self.name,
                    connections=self._connections,
                    nc_options=self.nc_options,
                    scp_options=self.scp_options,
                    userland=self._userland(),
                    get_local_ip=lambda: self._get_local_ip(),  # noqa: PLW0108 — late-bind self for monkeypatching
                    exec_cmd=lambda *a, **kw: self.exec(*a, **kw),  # noqa: PLW0108 — late-bind self for monkeypatching
                    # Asked of the session manager, never predicted from
                    # `self.term`: whether `exec` types into a line-disciplined
                    # shell is that object's decision (a proxied login routes
                    # through the pooled path on ssh too), and the framing it
                    # subtracts is its frame's.
                    exec_line_budget=lambda: self._session_mgr.exec_line_budget,
                    max_filename_len=self.max_filename_len,
                )
            ),
        )

    def _get_local_ip(self) -> str:
        """Return the local IP address used to reach this host, via OS routing lookup.

        Opens an unconnected UDP socket and uses the OS routing table to determine
        which local interface would be used to reach ``self.ip``. No packets are sent.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.ip, 80))
            return s.getsockname()[0]

    @override
    async def _probe_connection(self) -> None:
        """Open this host's term channel (ssh or telnet); warm FTP when configured."""
        if self.term == "ssh":
            await self._connections.ssh()
        else:
            await self._connections.telnet()

        if self.transfer == "ftp":
            await self._connections.ftp()

    ####################
    #  Command execution
    ####################

    # TODO: Make sync versions of cmd and file methods that just wraps the async def

    @override
    async def _login(self, user: str | None = None) -> None:
        """Open an interactive shell on this host, bridged to the local terminal.

        Dispatches on ``self.term``:

        - **ssh**: reuses the cached ``SSHClientConnection`` (asyncssh
          multiplexes channels, so opening a PTY-backed process on an
          existing connection is cheap). Works transparently through
          configured hops because the connection is already tunneled.
        - **telnet**: builds a *dedicated* :class:`TelnetClient` for
          this session with ``auto_window_resize=True`` and opens it in
          ``interactive=True`` mode so the remote shell echoes the
          user's keystrokes back (the normal connect path sends
          ``DONT ECHO`` to silence echoes for non-interactive capture).
          The cached telnet client, if any, is not reused — it may
          already be in non-echo mode. Hop tunnels are honored via the
          same port-forward helper the regular telnet path uses.

        ``user``: land the interactive session on this login
        instead of ``self._connections.login_target``, replaying any
        login-proxy hops (:func:`~otto.host.login_proxy.resolve_chain`)
        over the bridge after authentication but before the stdin/stdout
        pumps start (see :mod:`otto.host.interact`). Both transports
        authenticate as the resolved DIRECT cred, which must match the
        login the cached SSH connection (or the login_target-derived
        telnet client) already authenticates as — building a fresh
        connection under a different direct login is out of scope, so a
        mismatch raises :class:`~otto.host.login_proxy.LoginProxyError`
        rather than silently proxying from the wrong account.
        """
        target = user if user is not None else self._connections.login_target
        direct, hops = resolve_chain(self.creds, target)

        if self.term == "ssh":
            via_login, _ = self._connections.credentials
            if direct.login != via_login:
                raise LoginProxyError(
                    f"{self.name}: login is authenticated as {via_login!r}, "
                    f"but --user {target!r} resolves to a direct login of "
                    f"{direct.login!r}; starting a fresh connection as "
                    f"{direct.login!r} is not supported."
                )
            conn = await self._connections.ssh()
            await run_ssh_login(
                conn=conn,
                host_name=self.name,
                proxy_hops=hops,
                via_login=via_login,
                host_id=self.id,
            )
            return

        login_user, password = self._connections.credentials
        if direct.login != login_user:
            raise LoginProxyError(
                f"{self.name}: login is authenticated as {login_user!r}, but "
                f"--user {target!r} resolves to a direct login of "
                f"{direct.login!r}; starting a fresh connection as "
                f"{direct.login!r} is not supported."
            )
        interactive_options = replace(self.telnet_options, auto_window_resize=True)
        remote_port = interactive_options.port
        if self._connections.has_tunnel:
            local_port = await self._connections._forward_port(remote_port)  # noqa: SLF001 — intra-package access to HostConnections._forward_port for tunnel setup
            connect_host = "localhost"
            connect_port: int | None = local_port
        else:
            connect_host = self._connections.ip
            connect_port = None  # TelnetClient will use options.port

        client = TelnetClient(
            host=connect_host,
            user=login_user,
            password=password or "",
            options=interactive_options,
            connect_port=connect_port,
            prompt=None,
        )
        host_name = self.name
        try:
            await client.connect(interactive=True)
            await run_telnet_login(
                client=client,
                host_name=self.name,
                proxy_hops=hops,
                via_login=login_user,
                host_id=self.id,
            )
        finally:
            with teardown_step(host_name, "interactive telnet client close"):
                await client.close()

    @override
    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: str | None = None,
    ) -> CommandResult:
        """Run a single command concurrent-safely, independent of the persistent shell.

        Unlike :meth:`~otto.host.host.BaseHost.run`, this method is **concurrent-safe**: multiple
        ``exec()`` calls can run simultaneously via ``asyncio.gather()`` or
        ``asyncio.create_task()`` without corrupting each other or the
        persistent shell session.

        Key differences from ``run()``:

        +------------------+------------------------------+----------------------------+
        | Property         | ``run()``                    | ``exec()``                 |
        +==================+==============================+============================+
        | Shell state      | Persistent (cd, env persist) | Stateless (fresh each call)|
        | Concurrency      | Sequential only              | Safe for asyncio.gather()  |
        | Expect support   | Yes                          | No                         |
        | Connection cost  | Reuses existing session      | Reuses cached exec pool    |
        | Best for         | Multi-step workflows, state  | One-off / parallel cmds    |
        +------------------+------------------------------+----------------------------+

        Implementation details:

        - **SSH**: runs via ``SSHClientConnection.create_process()`` — a
          lightweight exec channel on the existing TCP connection.  No new
          TCP handshake or authentication needed.
        - **Telnet**: telnet has no stateless exec primitive, so otto keeps
          a free-list pool of dedicated internal shell sessions.  Serial
          callers reuse one session (one TCP+auth handshake amortized over
          all calls); concurrent callers each pull their own session off
          the free-list, opening a new one if none are free.  This preserves
          the independence guarantee while avoiding the 1-2 s handshake on
          every call.

        Args:
            cmd: Shell command to run. Shell operators (``<``, ``>``, ``|``) work on
                SSH because asyncssh wraps the command in a shell; on telnet the
                command runs through the login shell of the new session.
            timeout: Seconds before the command is considered hung.
                Defaults to :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`;
                pass ``float("inf")`` for a deliberately unbounded command such
                as a netcat listener awaiting a connection.

        Returns:
            A :class:`~otto.result.CommandResult`; ``value`` holds the output.

        See Also:
            :meth:`~otto.host.host.BaseHost.run`: stateful, sequential alternative
            with expect support.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: exec(user=...) is not supported on UnixHost — "
                f"run(sudo=True) elevates whole commands; a per-call unix user "
                f"is a ledgered follow-up"
            ) from None
        return await self._session_mgr.exec(cmd, timeout=timeout, log=self._effective_log(log))

    ####################
    #  File transfer
    ####################

    @override
    @cli_exposed(success="Download complete.", dry_run_preview=True)
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(
                variadic=True,
                elem_type=Path,
                help="Remote file(s) to download.",
                remote_path="any",
            ),
        ],
        dest_dir: Path,
        user: Annotated[
            str | None,
            Opt(help="Not supported on this host type — containers only."),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> Result:
        """Transfer files from remote host to the local machine."""
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: get(user=...) is not supported on UnixHost — "
                f"transfer ownership follows the connection's own identity"
            ) from None
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.get_files(src_files, dest_dir, show_progress)

    # TODO: Look into a way to batch a single list of files that goes to different hosts
    # The main use case is lists of products or tools. These are the same binaries, and
    # go to multiple hosts. It would be most efficient if they could all be done in a
    # single asyncio.gather() rather than multiple.
    @override
    @cli_exposed(success="Transfer complete.", dry_run_preview=True)
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Annotated[Path, Arg(remote_path="dir")],
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
        user: Annotated[
            str | None,
            Opt(help="Not supported on this host type — containers only."),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
    ) -> Result:
        """Transfer files from local machine to remote host.

        *mode* sets the permission bits on the uploaded files — an ``int``
        (``0o755``) from Python, or a string always read as octal (``"755"``,
        ``"0755"``, ``"0o755"``). It is applied in one batched ``chmod`` after
        the bytes land, whichever unix backend (scp/sftp/ftp/nc) carried them.
        """
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: put(user=...) is not supported on UnixHost — "
                f"transfer ownership follows the connection's own identity"
            ) from None
        if not isinstance(src_files, list):
            src_files = [src_files]
        dest_dir = self._resolve_dest(dest_dir)
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir, mode)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.put_files(src_files, dest_dir, show_progress, mode)

    ####################
    #  Kernel modules
    ####################

    @cli_exposed(output_dir=False)
    async def lsmod(self) -> Result:
        """List the kernel modules currently loaded on the host.

        ``value`` holds the module names (``list[str]``); a failed read is a
        non-ok :class:`~otto.result.Result` instead of a silent empty list.
        """
        return await self._loaded_modules()

    async def _loaded_modules(self) -> Result:
        """Read loaded module names from ``/proc/modules`` — the source ``lsmod`` formats.

        World-readable (no sudo), no ``lsmod`` binary dependency; column
        one is the module name, already ``-``→``_`` normalized by the kernel.
        ``value`` is the module list, empty (``Error``) when the read fails;
        a dry run DECLINES instead (``Status.NotRun``, ``value`` raises).
        ``log=LogMode.QUIET`` keeps the (potentially long) module dump out of
        the console (still recorded in verbose.log).

        The dry-run arm used to answer ``Result(Status.Skipped, value=[])``,
        and that empty list was fabricated device data of the worst kind: a
        caller asking "is module X loaded?" was told **no** by a machine
        nobody contacted, and ``Skipped.is_ok`` is True so nothing downstream
        could tell. It goes through :meth:`~otto.host.host.BaseHost._dry_run_result`
        rather than hand-building a decline, which keeps the ``[DRY RUN]``
        announcement (at the same ``QUIET`` the real read uses) and names the
        exact command that was not issued.
        """
        if is_dry_run():
            return self._dry_run_result("cat /proc/modules", LogMode.QUIET)
        result = await self.exec("cat /proc/modules", log=LogMode.QUIET)
        if not result.status.is_ok:
            return Result(
                Status.Error, value=[], msg=f"reading /proc/modules failed: {result.value.strip()}"
            )
        mods = [line.split()[0] for line in result.value.splitlines() if line.strip()]
        return Result(Status.Success, value=mods)

    @cli_exposed(success="Module loaded.")
    async def load(
        self,
        file: Annotated[Path, Arg(help="Kernel module .ko to insert.")],
        name: Annotated[str | None, Opt(help="Module name; defaults to the file stem.")] = None,
        dest_dir: Annotated[Path, Exclude] = Path("/tmp"),  # noqa: S108 — deliberate staging path
        show_progress: Annotated[bool, Exclude] = False,
    ) -> Result:
        """Insert a kernel module: stage the .ko to the host, then ``insmod`` it.

        ``put`` lands the .ko on the target (as the login/transfer user); the
        ``insmod`` runs in the shell session — under ``sudo`` unless the session
        is already root (Spec A's ``current_user``). The staged file is removed
        afterward (the module lives in kernel memory once inserted). ``name``
        defaults to the file stem (``-``→``_``) and is used in error text.
        """
        resolved = (name or file.stem).replace("-", "_")
        dest = dest_dir / file.name
        put_result = await self.put(file, dest_dir, show_progress=show_progress)
        if not put_result.is_ok:
            return Result(put_result.status, msg=f"staging {file} failed: {put_result.msg}")
        need_sudo = self.current_user != "root"
        result = await self.run(f"insmod {self._q(dest)}", sudo=need_sudo)
        await self.rm(dest, force=True)  # best-effort cleanup
        if result.status.is_ok:
            return Result(Status.Success)
        return Result(Status.Error, msg=f"insmod {resolved} failed: {result.only.value.strip()}")

    @cli_exposed(success="Module unloaded.")
    async def unload(
        self,
        name: Annotated[str, Arg(help="Module name to remove.")],
    ) -> Result:
        """Remove a kernel module (``rmmod``).

        Idempotent: removing a module that is not resident succeeds without running ``rmmod``
        (mirrors :meth:`~otto.host.embedded_host.EmbeddedHost.unload`).
        """
        resolved = name.replace("-", "_")
        # A failed module read yields value=[] -> treated as not-resident
        # (old-behavior parity; lsmod carries the failure channel).
        #
        # The `not is_dry_run()` half is what keeps the residency logic from
        # RUNNING AT ALL under a dry run — the third of the three honest
        # behaviours, and the only one available here. Reading `.value` would
        # raise (`_loaded_modules` declines), and before it declined the
        # fabricated empty list made a dry-run unload short-circuit to Success
        # without ever naming an `rmmod`.
        if not is_dry_run() and resolved not in (await self._loaded_modules()).value:
            return Result(Status.Success)
        need_sudo = self.current_user != "root"
        result = await self.run(f"rmmod {self._q(resolved)}", sudo=need_sudo)
        if result.status.is_ok:
            return Result(Status.Success)
        return Result(Status.Error, msg=f"rmmod {resolved} failed: {result.only.value.strip()}")

    ####################
    #  Power / reboot
    ####################

    @override
    async def _soft_reboot(self) -> Result:
        # UNREACHABLE UNDER A DRY RUN, and this method is not safe there: the
        # `Success` below is returned whatever `run` answered, so a dry run's
        # `NotRun` decline becomes a reported reboot. `BaseHost.reboot`'s
        # dry-run arm returns above the only call site, which is why nothing
        # here checks `is_dry_run()` — a future caller that reaches this from a
        # dry run needs its own arm, not a fix here (the tolerance below is
        # deliberate and must not become a decline).
        #
        # Issuing `reboot` races the connection teardown: on a fast host the
        # transport can drop before the command's round-trip completes, and
        # that failure is indistinguishable from the host obeying quickly.
        # Tolerate it — reboot(wait=True)'s down-wait is the loud check for
        # "the command never actually took".
        #
        # `timeout=10.0` bounds the COMMAND, not the call. `sudo=True` makes
        # `run` await the host's userland resolution first, above the budget,
        # so on a host that refuses probes this line can take up to 30s + 10s
        # where before the userland layer it took 10s. Once per host object,
        # and again only after `_RETRY_COOLDOWN_S`; the recovery deadline
        # `reboot(wait=True)` applies afterwards is unaffected. See
        # `PosixPrivilege._prepare_elevation`.
        try:
            await self.run("reboot", sudo=True, timeout=10.0)
        except UnsupportedOnUserlandError:
            # NOT the disconnect race, and the difference is the whole point of
            # the refusal. This one is raised BEFORE anything is sent: otto knew
            # the command could not work and declined to emit it. Swallowing it
            # into the Success below would report a reboot on a host that was
            # never rebooted — and `wait` defaults to False, so nothing
            # downstream would ever notice. The generic handler below caught it
            # until this clause was added; enforced tree-wide by
            # `tests/unit/host/test_privilege.py`.
            raise
        except Exception as e:  # noqa: BLE001 — expected issue-race disconnect; the down-wait disambiguates
            logger.debug(f"{self.name}: connection dropped while issuing reboot ({e})")
        return Result(Status.Success)

    @override
    async def _confirm_recovered(self, deadline: float, poll_interval: float, /) -> bool:
        # "Accepts a connection" is not "booted": early-boot sshd (or a
        # socket-activated stub) can accept and then stall immediately after.
        # Recovery = one clean command round-trip on the fresh post-rebuild
        # connection, retried until the deadline; a raising probe (refused,
        # reset mid-handshake) is just "not yet", never an error.
        async def shell_answered() -> bool:
            try:
                result = await self.exec("true", timeout=_RECOVERY_PROBE_TIMEOUT, log=LogMode.QUIET)
            except Exception:  # noqa: BLE001 — probe failure means "not booted yet"; the deadline is the arbiter
                return False
            return result.status.is_ok

        # probe_first: a deadline that has already elapsed by the time we get
        # here (e.g. the up-wait consumed the whole budget landing right at
        # the edge) must not fail the gate unprobed — one clean round-trip is
        # a recovery no matter what the clock says.
        loop = asyncio.get_running_loop()
        try:
            await wait_for_async(
                shell_answered,
                max(0.0, deadline - loop.time()),
                interval=poll_interval,
                probe_first=True,
                on_timeout=f"{self.name!r} shell never answered before the recovery deadline",
            )
        except WaitTimeoutError:
            return False
        return True

    @override
    @cli_exposed
    async def shutdown(self) -> Result:
        """Power this host off from its own shell, in the spelling it has.

        :func:`shutdown_command` asks the device: ``shutdown -h now`` where that
        applet exists, ``poweroff`` where it does not (every BusyBox matrix row).

        A dropped connection is SUCCESS — issuing a power-off command races the
        transport being torn down — but a completed round trip that exits
        non-zero is :attr:`~otto.utils.Status.Failed`, because the device
        answered and it said no.

        **Under a dry run this reports the plan and sends nothing**, with
        ``is_ok`` False. The status check below reads ``Status.Failed`` alone,
        so a declined round trip — ``Results.collect`` folds the single
        ``NotRunResult`` to ``NotRun`` — fell straight through to
        ``Result(Status.Success)``: a FABRICATED POWER-OFF, and the worst place
        in the file for one, because ``shutdown`` has no wait behind it the way
        ``reboot(wait=True)`` does and nothing downstream would ever notice.

        Raises:
            ~otto.host.errors.UnsupportedOnUserlandError: the device has neither
                spelling, or this userland offers no elevation. Nothing was sent
                in either case.
        """
        if is_dry_run():
            # ABOVE the resolution, and the spelling is deliberately named as a
            # CHOICE rather than as a fact. A dry run settles no capability
            # (`Userland._send` declines), so the applets read their assumed
            # defaults and `shutdown_command` would degrade to the GNU spelling
            # — reporting that as the command otto WILL send would be a
            # fabricated measurement about a device nobody contacted.
            return self._dry_run_power_report(
                "SHUTDOWN",
                f"would power the host off from its own shell "
                f"({GNU_SHUTDOWN!r} or {BUSYBOX_POWEROFF!r}, whichever the device "
                f"has). Not done: no command issued, no applet probe, the host "
                f"stays up",
            )
        # THE SPELLING IS THE DEVICE'S, not otto's. `shutdown` is absent on
        # every BusyBox matrix row and `poweroff` is present on all five, so a
        # hard-coded `shutdown -h now` is a command a whole class of device
        # cannot run -- while swapping in a hard-coded `poweroff` would silently
        # re-spell the command on every host otto already works against.
        # `shutdown_command` asks instead.
        #
        # Resolved HERE rather than left to `run(sudo=True)`: that call awaits
        # the same resolution through `PosixPrivilege._prepare_elevation`, but
        # it does so AFTER the command has been chosen, and the choice is what
        # needs the answer. Same round, moved earlier; `resolve()` is
        # idempotent once settled, so the one `run` does costs a lock and
        # nothing on the wire.
        userland = self._userland()
        await userland.resolve()
        command = shutdown_command(userland, host=self.name)
        try:
            result = await self.run(command, sudo=True, timeout=10.0)
        except UnsupportedOnUserlandError:
            # NOT the disconnect race, exactly as `_soft_reboot` argues: this
            # is raised BEFORE anything is sent (no elevation on this
            # userland), so swallowing it would report a shutdown on a host
            # that is still running.
            raise
        except Exception as e:  # noqa: BLE001 — expected issue-race disconnect; see below
            logger.debug(f"{self.name}: connection dropped while issuing {command} ({e})")
            return Result(Status.Success)
        # THE RESULT IS READ, WHICH `_soft_reboot` DOES NOT DO, and the
        # asymmetry is deliberate rather than shutdown being stricter than
        # reboot. Both tolerate the same race: issuing a power-off command
        # kills the transport under it, and otto reports that as
        # `Status.Error` (a timeout, an EOF or a lost connection --
        # `SessionManager.run_cmd` turns all three into Error, it does not
        # raise) or as the exception caught above. Neither is evidence the
        # device disobeyed, so neither fails the call.
        #
        # `Status.Failed` is a different animal and is the one this reads: the
        # round trip COMPLETED and the shell handed back a non-zero exit
        # (`retcode != 0` is the only thing that produces it). The device
        # answered, and it said no -- `shutdown: not found` at 127, or sudo
        # denied. Reporting that as Success is the silent-success class that
        # cost otto an expire timer and a zeroed destination file, and here it
        # is worse than at `_soft_reboot`: `reboot(wait=True)` has a down-wait
        # that catches a reboot which never took, and `shutdown()` has no wait
        # and no second check at all. Nothing downstream would ever notice.
        if result.status is Status.Failed:
            return Result(
                Status.Failed,
                msg=(
                    f"{self.name}: {command!r} exited non-zero and the host is "
                    f"still running: {result.only.value.strip()}"
                ),
            )
        return Result(Status.Success)
