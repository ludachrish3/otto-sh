"""
Embedded (bare-metal / RTOS) host class.

An :class:`EmbeddedHost` is a network-reached target whose "OS" is a real-time
kernel or bare-metal firmware rather than a POSIX system — Zephyr is the first
concrete example. It is exposed through the *same* :class:`~otto.host.host.Host`
API as :class:`~otto.host.unix_host.UnixHost` (``run``/``oneshot``/``send``/
``expect``/``put``/``get``) so test code does not care whether a target is a
Linux box or a microcontroller.

What makes an embedded target different from a Unix host:

- **One console.** A Zephyr device exposes a *single* shell over telnet. There
  is no second channel and no stateless exec primitive, so ``oneshot`` shares
  the one persistent session with ``run`` and is therefore **not**
  concurrency-safe (it is on :class:`UnixHost`).
- **No bash.** No ``$?``, no command substitution, no ``scp``/``ftp``/``nc``.
  Command framing and file transfer cannot reuse the Unix machinery.
- **Telnet only.** The shell is reached over telnet (optionally through an SSH
  hop), never SSH directly.

Command execution requires a *command frame*: a :class:`CommandFrame` instance
that frames each command for the target's RTOS shell over the plain telnet
transport and parses the output/return-code back. There is **no default frame**
— a bare :class:`EmbeddedHost` raises ``ValueError`` at construction if none
is supplied (fail loud). The frame is provided by:

- a registered :class:`~otto.host.os_profile.OsProfile` data bundle (e.g. a
  ``command_frame`` key in an ``[os_profiles.<name>]`` settings table), or
- a concrete subclass that re-declares the default, or
- an explicit constructor argument.

:class:`ZephyrHost` is the in-tree concrete class: it subclasses
:class:`EmbeddedHost` and declares :class:`~otto.host.command_frame.ZephyrFrame`
as the default ``command_frame`` (along with ``os_type='zephyr'`` and
``os_name='Zephyr'``). Zephyr-specific framing and OS naming live on
:class:`ZephyrHost`, not on the base class.

File transfer (``get``/``put``) is delegated to
:class:`~otto.host.embedded_transfer.EmbeddedFileTransfer`, which speaks the
device shell only (the ``console`` backend uses Zephyr's ``fs`` commands).
The interactive bridge (``_interact``) currently raises
:class:`NotImplementedError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from ..configmodule.lab import Lab

from ..logger import get_otto_logger
from ..utils import CommandStatus, Status
from .binary_loader import BinaryLoader
from .command_frame import CommandFrame, ZephyrFrame
from .connections import ConnectionManager
from .embedded_filesystem import EmbeddedFileSystem, NoFileSystem
from .embedded_transfer import EmbeddedFileTransfer
from .host import Host, SuppressCommandOutput, is_dry_run
from .options import SnmpOptions, TelnetOptions
from .remote_host import OsType, RemoteHost
from .repeat import RepeatRunner
from .session import (
    Expect,
    HostSession,
    SessionManager,
)
from .toolchain import Toolchain
from .transfer import (
    TransferContext,
    _acquire_shared_progress,
    build_transfer_backend,
    make_rich_progress_handler,
)

logger = get_otto_logger()

# Readiness-handshake ceiling for an embedded telnet console. The Zephyr shell
# under QEMU can take a few seconds after the TCP connection opens before it
# starts reading input, so the marker handshake needs a more generous ceiling
# than the bash default (3 s). Passed to the SessionManager as ``init_timeout``.
_EMBEDDED_INIT_TIMEOUT = 15.0


@dataclass(slots=True)
class EmbeddedHost(RemoteHost):
    """OS-agnostic bare-metal / RTOS host reached over telnet.

    :class:`EmbeddedHost` carries no OS-specific defaults. A ``command_frame``
    must be supplied — either via a profile, a subclass (e.g.
    :class:`ZephyrHost`), or an explicit constructor argument — or construction
    raises ``ValueError`` (fail loud). :class:`ZephyrHost` is the in-tree
    concrete subclass and worked example.
    """

    ip: str
    """IP address of the host's telnet shell."""

    element: str = field(repr=False)
    """Network element to which this host belongs."""

    os_type: OsType = 'embedded'
    """Default profile selector for a bare :class:`EmbeddedHost`. Subclasses
    (e.g. :class:`ZephyrHost`) override this to their registered name."""

    os_name: Optional[str] = None
    """Kernel/OS name, or None. A bare ``embedded`` host carries no OS name;
    a concrete subclass (e.g. :class:`ZephyrHost`) sets it."""

    os_version: Optional[str] = None
    """OS/kernel version string, or None if unspecified."""

    name: str = None  # type: ignore
    """Human readable name to represent the host. Automatically generated if not provided."""

    creds: dict[str, str] = field(default_factory=dict)
    """Users and their respective passwords. Optional — the Zephyr telnet shell
    backend has no login step, so this is empty for a stock Zephyr target."""

    user: Optional[str] = None
    """User with which to log in, if the shell requires one. Usually unset."""

    element_id: Optional[int] = field(default=None, repr=False)
    """Network element identifier to which this host belongs."""

    board: Optional[str] = field(default=None, repr=False)
    """Name of the board type to which this host belongs."""

    slot: Optional[int] = field(default=None, repr=False)
    """Physical slot number of the board to which this host belongs."""

    is_virtual: bool = False
    """Determines whether a host is a VM/emulator (e.g. QEMU) or not."""

    transfer: str = 'console'
    """File-transfer backend. ``console`` (default) drives the device shell's
    ``fs`` commands; ``tftp`` is reserved and not yet implemented."""

    filesystem: EmbeddedFileSystem = field(default_factory=NoFileSystem)
    """On-device filesystem variant — e.g. :class:`FatRamFileSystem`,
    :class:`LittleFsFileSystem`, or :class:`NoFileSystem` (the default).
    Carries the mount path, the optional ``fs mount`` command, and the
    command-formation hooks the transfer code and the embedded monitor's
    disk parser drive. See :mod:`otto.host.embedded_filesystem`.

    Lab data declares the variant by string in the ``filesystem`` field;
    the storage factory resolves the string to a class. Projects can
    register custom variants via
    :func:`otto.host.embedded_filesystem.register_filesystem`."""

    command_frame: Optional[CommandFrame] = None
    """Shell-framing *dialect* for this target's console — how a command is
    wrapped in sentinels and how output/retcode are parsed back. There is NO
    default: a bare ``embedded`` host carries no dialect, so a frame is
    *required* — supplied either by a profile/subclass (e.g.
    :class:`ZephyrHost`) or as an explicit value. A frame-less
    :class:`EmbeddedHost` fails loud at construction.

    Lab data declares the dialect by string in the ``command_frame`` field
    (e.g. a Zephyr 2.7 build that reports its retcode inline would name a
    project-registered frame); the storage factory resolves the string to an
    instance. Projects can register custom dialects via
    :func:`otto.host.command_frame.register_command_frame`. The dialect is
    independent of the transport, so it is handed straight to the
    :class:`~otto.host.session.SessionManager`."""

    loader: Optional[BinaryLoader] = None
    """Binary-load strategy for this target's runtime (e.g. Zephyr LLEXT).
    Unlike ``command_frame`` it is *optional* — many embedded hosts never load
    binaries. Lab data declares it by string in the ``loader`` field (e.g.
    ``"llext-hex"``); ``__post_init__`` resolves the string to an instance.
    ``load()`` / ``unload()`` fail loud (``ValueError``) when it is None. Projects
    register custom loaders via
    :func:`otto.host.binary_loader.register_binary_loader`."""

    default_dest_dir: Path = field(default_factory=Path)
    """Default landing directory for ``put`` / ``get`` when the caller
    supplies an empty or relative ``dest_dir``. When left at the default
    (an empty ``Path()``), ``__post_init__`` resolves it to
    ``filesystem.mount`` so generic fan-out callers like
    :func:`do_for_all_hosts` don't have to branch on host type. Override
    in lab data to land transfers somewhere other than the FS root. See
    :attr:`RemoteHost.default_dest_dir`."""

    max_filename_len: int = 255
    """Upper bound on the basename length (including extension) accepted by
    the target's filesystem. Defaults to ``255`` — the Linux ``NAME_MAX``,
    also the typical LittleFS ceiling. Override per-host when the firmware
    enforces a tighter limit (e.g. ``32`` for a Zephyr build that sets
    ``CONFIG_FS_FATFS_MAX_LFN=32`` / ``CONFIG_FS_LITTLEFS_NAME_MAX=32``,
    or ``12`` for a stock FAT 8.3 build without LFN support). See
    :attr:`RemoteHost.max_filename_len`."""

    telnet_options: TelnetOptions = field(default_factory=TelnetOptions, repr=False)
    """Connection options for the telnet shell (port, cols/rows, etc.)."""

    snmp: Optional[SnmpOptions] = field(default=None, repr=False)
    """Optional SNMP polling config (lab ``snmp`` block). When set, otto's
    monitor collects this host's metrics over SNMP — a separate channel from
    the single telnet console — instead of running shell commands. See
    :class:`~otto.host.options.SnmpOptions`."""

    toolchain: Toolchain = field(default_factory=Toolchain, repr=False)
    """Cross-toolchain for this bed's products.  Used by the coverage pipeline
    to select the correct ``gcov``/``lcov``.  The host is the test bed, so it
    owns the toolchain matching its target ABI — a Zephyr 3.7 bed and a 4.4 bed
    declare different SDKs.  Defaults to system-installed tools."""

    hop: Optional[str] = None
    """Host ID of the intermediate SSH hop used to reach this host, or None."""

    resources: set[str] = field(default_factory=set[str])
    """Names of resources required to use this host."""

    interfaces: dict[str, str] = field(default_factory=dict, repr=False)
    """Named secondary interface addresses (see :attr:`RemoteHost.interfaces`).
    Resolve with :meth:`address_for`."""

    log: bool = field(default=True, repr=False)
    """Whether this host should log its output to stdout and log files."""

    log_stdout: bool = field(default=True, repr=False)
    """Whether this host should log its output to stdout."""

    _lab: "Lab | None" = field(default=None, compare=False, repr=False, kw_only=True)
    """Back-reference to the owning Lab, wired by Lab.add_host. Lets hop
    resolution use self._lab.hosts[...] instead of ambient state."""

    id: str = field(init=False, repr=False)
    """Unique identifier for this host."""

    _connection_factory: type[ConnectionManager] | None = field(default=None, init=True, repr=False)
    """Optional ConnectionManager subclass for dependency injection (test doubles)."""

    _connections: ConnectionManager = field(init=False, repr=False)
    """Manages the raw telnet transport for this host."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Manages periodic background command tasks for this host."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages the persistent shell session for this host."""

    _file_transfer: EmbeddedFileTransfer = field(init=False, repr=False)
    """Handles ``get``/``put`` over the device shell for this host."""

    def __post_init__(self) -> None:

        self.id = self._generate_id()
        if self.name is None:
            self.name = self._generate_name()

        # Lab JSON serializes ``filesystem`` as a string; the storage factory
        # resolves it to a class instance for declared hosts, but a directly-
        # constructed EmbeddedHost may still pass a string here. Coerce.
        if isinstance(self.filesystem, str):
            from .embedded_filesystem import build_filesystem
            self.filesystem = build_filesystem(self.filesystem)

        # Same for ``command_frame`` — lab JSON declares the dialect by name.
        if isinstance(self.command_frame, str):
            from .command_frame import build_command_frame
            self.command_frame = build_command_frame(self.command_frame)

        # Same for ``loader`` — lab JSON declares the binary-load strategy by
        # name. Optional, so no fail-loud here (load()/unload() check at call).
        if isinstance(self.loader, str):
            from .binary_loader import build_binary_loader
            self.loader = build_binary_loader(self.loader)

        # A bare 'embedded' host carries no shell-framing dialect. Fail loud
        # rather than silently inheriting one, so a misconfigured non-Zephyr
        # host is caught at construction, not at first command.
        if self.command_frame is None:
            raise ValueError(
                f"EmbeddedHost {self.name!r} has no command_frame. A bare "
                f"'embedded' host carries no shell-framing dialect. Set os_type "
                f"to a profile that supplies one (e.g. \"zephyr\"), or pass an "
                f"explicit command_frame."
            )

        # Lab JSON serializes ``default_dest_dir`` as a string; coerce so
        # callers can use Path arithmetic uniformly. When the field was left
        # at its empty default and the filesystem declares a mount, fall back
        # to that mount so fan-out callers land on the FS root automatically.
        if not isinstance(self.default_dest_dir, Path):
            self.default_dest_dir = Path(self.default_dest_dir)
        if self.default_dest_dir == Path() and self.filesystem.mount is not None:
            self.default_dest_dir = Path(self.filesystem.mount)

        hop_transport = self._build_hop_transport() if self.hop else None

        # An RTOS telnet shell has no login step — force ``login=False`` so the
        # connection never blocks waiting for a ``login:`` prompt that the
        # device will never send.
        factory = self._connection_factory or ConnectionManager
        self._connections = factory(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term='telnet',
            name=self.name,
            hop=hop_transport,
            telnet_options=replace(self.telnet_options, login=False, single_client_console=True),
        )
        self._repeater = RepeatRunner(run_cmds=self.run)
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            init_timeout=_EMBEDDED_INIT_TIMEOUT,
        )
        self._file_transfer = cast(EmbeddedFileTransfer, build_transfer_backend(self.transfer).create(
            TransferContext(
                transfer=self.transfer,
                host_name=self.name,
                exec_cmd=lambda *a, **kw: self._run_one(*a, **kw),
                filesystem=self.filesystem,
                max_filename_len=self.max_filename_len,
            )
        ))

    ####################
    #  Connection
    ####################

    async def verify_connection(self) -> CommandStatus:
        """Attempt to open the telnet shell without running commands (dry-run)."""
        try:
            await self._connections.telnet()
            self._log_command("[DRY RUN] Connection verified")
            return CommandStatus(command="connect", output="Connection successful", status=Status.Success, retcode=0)
        except Exception as e:
            self._log_command(f"[DRY RUN] Connection FAILED: {e}")
            return CommandStatus(command="connect", output=str(e), status=Status.Error, retcode=1)

    async def close(self) -> None:
        await self._repeater.stop_all()
        await self._session_mgr.close_all()
        await self._connections.close()

    ####################
    #  Command execution
    ####################

    async def _interact(self) -> None:
        """Open an interactive shell bridged to the local terminal.

        Not yet implemented for embedded hosts — the telnet bridge for a
        login-less RTOS shell lands in a later phase.
        """
        raise NotImplementedError(
            "Interactive sessions for embedded hosts are not yet implemented"
        ) from None

    async def _run_one(
        self,
        cmd: str,
        expects: list[Expect] | None = None,
        timeout: float | None = 10.0,
        log: bool = True,
    ) -> CommandStatus:
        """Execute a single command on the embedded host via the persistent shell session.

        Like :meth:`UnixHost._run_one`, the session is stateful and **sequential
        only** — the embedded target has a single console, so concurrent
        ``run()`` calls would corrupt the session.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, expects=expects, timeout=timeout, log=log)

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
        log: bool = True,
    ) -> CommandStatus:
        """Run a single command on the embedded host.

        Unlike :meth:`UnixHost.oneshot`, this is **not** concurrency-safe: an
        embedded target exposes a single console with no stateless exec
        primitive, so ``oneshot`` runs on the same persistent session as
        :meth:`run`. It exists for API parity; use :meth:`run` for stateful
        workflows.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._session_mgr.run_cmd(cmd, timeout=timeout, log=log)

    async def open_session(self, name: str) -> HostSession:
        """Open a named persistent shell session.

        Note: an embedded target has a single console. Opening a second named
        session opens a second telnet connection to the device, which most
        RTOS shell backends do not accept concurrently. Prefer the default
        session via :meth:`run`.
        """
        if is_dry_run():
            self._log_command(f"[DRY RUN] open_session({name!r})")
        return await self._session_mgr.open_session(name)

    async def send(self, text: str) -> None:
        """Send raw text to the host's persistent session."""
        if is_dry_run():
            self._log_command(f"[DRY RUN] send({text!r})")
            return
        await self._session_mgr.send(text)

    async def expect(
        self,
        pattern: str | re.Pattern[str],
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in the host's session output stream."""
        if is_dry_run():
            self._log_command("[DRY RUN] expect() skipped — pattern would never match without a live connection")
            return ""
        return await self._session_mgr.expect(pattern, timeout)

    def _require_loader(self) -> BinaryLoader:
        """Return this host's binary loader, or fail loud if none is declared."""
        if self.loader is None:
            raise ValueError(
                f"EmbeddedHost {self.name!r} has no binary loader. Declare a "
                f"'loader' (e.g. \"llext-hex\") in the host's profile/lab data, "
                f"or pass an explicit loader, before calling load()/unload()."
            )
        return self.loader

    ####################
    #  File transfer
    ####################

    async def get(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        """Transfer files from the embedded host to the local machine.

        Delegates to :class:`~otto.host.embedded_transfer.EmbeddedFileTransfer`,
        which speaks the device shell (the ``console`` backend uses Zephyr's
        ``fs`` commands). Transfers are sequential — an embedded target has a
        single console.
        """
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        with SuppressCommandOutput(host=cast(Host, self)):
            return await self._file_transfer.get_files(src_files, dest_dir, show_progress)

    async def put(
        self,
        src_files: list[Path] | Path,
        dest_dir: Path,
        show_progress: bool = True,
    ) -> tuple[Status, str]:
        """Transfer files from the local machine to the embedded host.

        Delegates to :class:`~otto.host.embedded_transfer.EmbeddedFileTransfer`
        (the ``console`` backend writes via Zephyr's chunked ``fs write``).
        Transfers are sequential — an embedded target has a single console.

        ``dest_dir`` is resolved against :attr:`default_dest_dir` so a
        generic ``Path()`` from a fan-out caller lands on the host's
        mounted filesystem (e.g. ``/RAM:`` on a FAT target) rather than on
        Zephyr's bare ``/``, which has no FS and rejects opens with
        ``-ENOENT``.
        """
        if not isinstance(src_files, list):
            src_files = [src_files]
        dest_dir = self._resolve_dest(dest_dir)
        if is_dry_run():
            return self._dry_run_transfer("PUT", src_files, dest_dir)
        with SuppressCommandOutput(host=cast(Host, self)):
            return await self._file_transfer.put_files(src_files, dest_dir, show_progress)

    ####################
    #  Binary load
    ####################

    async def load(
        self,
        file: Path,
        name: str,
        show_progress: bool = False,
        timeout: float | None = 120.0,
    ) -> tuple[Status, str]:
        """Load a binary into the device runtime via the host's binary loader.

        Distinct from :meth:`put` (a *file* transfer to a mounted filesystem):
        ``load`` pushes a binary into the target's loader (e.g. Zephyr LLEXT's
        ``llext load_hex``), with no destination file. The payload is read from
        *file*, formatted into the device command by the loader, and sent with
        ``log=False`` so the (large) encoded payload never reaches the console
        or log. Returns ``(Status, str)`` like :meth:`put`/:meth:`get`; the
        ``str`` carries the device's failure text on error.

        ``show_progress`` is **off by default** (the bar only renders in
        interactive / ``otto run``; under ``otto test`` output is captured). When
        enabled it drives a transfer-style Rich bar from the paced telnet write
        of the payload — the only measurable progress (the device's relocation
        emits no incremental signal). Fails loud (``ValueError``) if the host
        declares no loader.
        """
        loader = self._require_loader()
        if is_dry_run():
            return self._dry_run_transfer("LOAD", [file], Path(name))
        payload = file.read_bytes()
        cmd = loader.load_command(name, payload)
        if show_progress:
            async with _acquire_shared_progress() as progress:
                handler = make_rich_progress_handler(progress, self.name)

                def _wp(done: int, total: int) -> None:
                    handler(str(file), f"{self.name}:{name}", done, total)

                result = await self._session_mgr.run_cmd(
                    cmd, timeout=timeout, log=False, write_progress=_wp,
                )
        else:
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout, log=False)
        ok, reason = loader.check_loaded(result.output)
        if ok:
            return Status.Success, ""
        return Status.Error, f"load {name} from {file} failed: {reason}"

    async def unload(
        self,
        name: str,
        timeout: float | None = 20.0,
    ) -> tuple[Status, str]:
        """Unload *name* from the device runtime, draining to full eviction.

        Some loaders (LLEXT) refcount a resident binary, so one unload may only
        decrement it. ``unload`` loops the loader's unload command until
        :meth:`~otto.host.binary_loader.BinaryLoader.is_fully_unloaded` reports
        the binary gone (bounded by ``loader.max_unload_rounds``). Idempotent:
        unloading something not loaded succeeds on the first round. Returns
        ``(Status, str)``; fails loud (``ValueError``) if no loader is declared.
        """
        loader = self._require_loader()
        if is_dry_run():
            return self._dry_run_transfer("UNLOAD", [], Path(name))
        cmd = loader.unload_command(name)
        last = ""
        for _ in range(loader.max_unload_rounds):
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout)
            last = result.output
            if loader.is_fully_unloaded(result.output):
                return Status.Success, ""
        return Status.Error, (
            f"{name} still resident after {loader.max_unload_rounds} unload "
            f"rounds: {last.strip()}"
        )


@dataclass(slots=True)
class ZephyrHost(EmbeddedHost):
    """A Zephyr RTOS host — the concrete, registered embedded host.

    This is the worked example for shipping a host subclass: it re-declares the
    Zephyr-specific field defaults that :class:`EmbeddedHost` no longer assumes,
    and is registered under ``os_type: "zephyr"`` via
    :func:`otto.host.os_profile.register_host_class`. External repositories
    register their own ``EmbeddedHost``/``UnixHost`` subclasses the same way
    (from an init module listed in ``.otto/settings.toml``), and may layer
    per-build ``OsProfile`` data bundles over them.
    """

    os_type: OsType = 'zephyr'
    """Profile selector recorded on the host. ``zephyr`` for this class."""

    os_name: Optional[str] = 'Zephyr'
    """Kernel/OS name — ``Zephyr`` for this class."""

    command_frame: CommandFrame = field(default_factory=ZephyrFrame)
    """Stock Zephyr ``retval`` shell framing (3.7 / 4.4 LTS)."""
