"""
Embedded (bare-metal / RTOS) host class.

An :class:`~otto.host.embedded_host.EmbeddedHost` is a network-reached target whose "OS" is a
real-time kernel or bare-metal firmware rather than a POSIX system — Zephyr is the first
concrete example. It is exposed through the *same* :class:`~otto.host.host.Host`
API as :class:`~otto.host.unix_host.UnixHost` (``run``/``exec``/``send``/
``expect``/``put``/``get``) so test code does not care whether a target is a
Linux box or a microcontroller.

What makes an embedded target different from a Unix host:

- **One console.** A Zephyr device exposes a *single* shell over telnet. There
  is no second channel to run commands out-of-band, so ``exec`` shares
  the one persistent session with ``run`` and is therefore **not**
  concurrency-safe (it is on :class:`~otto.host.unix_host.UnixHost`). For the
  same reason, prefer the default session (``run``) over ``open_session`` —
  a second named session opens a second telnet connection to the device,
  which most RTOS shell backends do not accept concurrently.
- **No bash.** No ``$?``, no command substitution, no ``scp``/``ftp``/``nc``.
  Command framing and file transfer cannot reuse the Unix machinery.
- **Telnet only.** The shell is reached over telnet (optionally through an SSH
  hop), never SSH directly.

Command execution requires a *command frame*: a
:class:`~otto.host.command_frame.CommandFrame` instance
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
:class:`~otto.host.transfer.EmbeddedFileTransfer`, which speaks the
device shell only (the ``console`` backend uses Zephyr's ``fs`` commands).
The interactive bridge (``_login``) currently raises
:class:`NotImplementedError`.
"""

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, NoReturn, cast

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import Arg, Exclude, Opt, Status, cli_exposed
from .binary_loader import BinaryLoader
from .capability import TERM_RESOLVER, TRANSFER_RESOLVER
from .capability_grid import HostCapabilities, SessionIdentity, UserSupport
from .command_frame import CommandFrame, ZephyrFrame
from .connections import ConnectionManager
from .embedded_filesystem import EmbeddedFileSystem, NoFileSystem
from .host import (
    DEFAULT_COMMAND_TIMEOUT,
    Host,
    SuppressCommandOutput,
    is_dry_run,
    refuse_declined_fact,
)
from .power import power_control_from_spec
from .remote_host import OsType, RemoteHost
from .session import (
    SessionManager,
)
from .transfer import (
    EmbeddedFileTransfer,
    TransferContext,
    _acquire_shared_progress,
    build_transfer_backend,
    make_rich_progress_handler,
)

logger = logging.getLogger(__name__)

# Readiness-handshake ceiling for an embedded telnet console. The Zephyr shell
# under QEMU can take a few seconds after the TCP connection opens before it
# starts reading input, so the marker handshake needs a more generous ceiling
# than the bash default (3 s). Passed to the SessionManager as ``init_timeout``.
_EMBEDDED_INIT_TIMEOUT = 15.0


@dataclass(slots=True, kw_only=True)
class EmbeddedHost(RemoteHost):
    """OS-agnostic bare-metal / RTOS host reached over telnet.

    :class:`EmbeddedHost` carries no OS-specific defaults. A ``command_frame``
    must be supplied — either via a profile, a subclass (e.g.
    :class:`ZephyrHost`), or an explicit constructor argument — or construction
    raises ``ValueError`` (fail loud). :class:`ZephyrHost` is the in-tree
    concrete subclass and worked example.

    Every field this class shares with the unix family lives on
    :class:`~otto.host.remote_host.RemoteHost`; what follows is the embedded
    family's own, plus the value-policy overrides below.
    """

    capabilities = HostCapabilities(
        run_user=UserSupport.refused,
        exec_user=UserSupport.refused,
        put_user=UserSupport.refused,
        get_user=UserSupport.refused,
        show_progress=True,
        session_identity=SessionIdentity.none,
        transfer_family="embedded",
        note=(
            "A serial console has no user to switch to, and transfer ownership "
            "follows the connection's own identity. Refuses `put`/`get` with "
            "`--recursive`."
        ),
    )
    """What this family promises. :class:`ZephyrHost` inherits it unchanged --
    a Zephyr target answers ``user=`` no differently from any other embedded
    one. See :class:`~otto.host.capability_grid.HostCapabilities`."""

    os_type: OsType = "embedded"

    has_bash: bool = False

    term: str = "telnet"

    transfer: str = "console"

    valid_terms: list[str] = field(default_factory=lambda: ["telnet"])

    valid_transfers: list[str] = field(default_factory=lambda: ["console"])

    filesystem: EmbeddedFileSystem = field(default_factory=NoFileSystem)
    """On-device filesystem variant — e.g. :class:`~otto.host.embedded_filesystem.FatRamFileSystem`,
    :class:`~otto.host.embedded_filesystem.LittleFsFileSystem`, or
    :class:`~otto.host.embedded_filesystem.NoFileSystem` (the default).
    Carries the mount path, the optional ``fs mount`` command, and the
    command-formation hooks the transfer code and the embedded monitor's
    disk parser drive. See :mod:`otto.host.embedded_filesystem`.

    Lab data declares the variant by string in the ``filesystem`` field;
    the host factory resolves the string to a class. Projects can
    register custom variants via
    :func:`otto.host.embedded_filesystem.register_filesystem`."""

    loader: BinaryLoader | None = None
    """Binary-load strategy for this target's runtime (e.g. Zephyr LLEXT).
    Unlike ``command_frame`` it is *optional* — many embedded hosts never load
    binaries. Lab data declares it by string in the ``loader`` field (e.g.
    ``"llext-hex"``); ``__post_init__`` resolves the string to an instance.
    ``load()`` / ``unload()`` fail loud (``ValueError``) when it is None. Projects
    register custom loaders via
    :func:`otto.host.binary_loader.register_binary_loader`."""

    _file_transfer: EmbeddedFileTransfer = field(init=False, repr=False)
    """Handles ``get``/``put`` over the device shell for this host."""

    def __post_init__(self) -> None:

        self.id = self._generate_id()
        if not self.name:
            self.name = self._generate_name()

        # Lab JSON serializes ``filesystem`` as a string; the host factory
        # resolves it to a class instance for declared hosts, but a directly-
        # constructed EmbeddedHost may still pass a string here. Coerce.
        if isinstance(self.filesystem, str):
            from .embedded_filesystem import build_filesystem

            self.filesystem = build_filesystem(self.filesystem)

        # Same for ``command_frame`` and ``landing_frame`` — lab JSON declares
        # both dialects by name, one shared import serving both coercions.
        if isinstance(self.command_frame, str) or isinstance(self.landing_frame, str):
            from .command_frame import build_command_frame

            if isinstance(self.command_frame, str):
                self.command_frame = build_command_frame(self.command_frame)
            if isinstance(self.landing_frame, str):
                self.landing_frame = build_command_frame(self.landing_frame)

        if self.session_setup is not None:
            from .session_setup import session_setup_from_spec  # off the startup graph on purpose

            self.session_setup = session_setup_from_spec(self.session_setup)

        # Same for ``loader`` — lab JSON declares the binary-load strategy by
        # name. Optional, so no fail-loud here (load()/unload() check at call).
        if isinstance(self.loader, str):
            from .binary_loader import build_binary_loader

            self.loader = build_binary_loader(self.loader)

        self.power_control = power_control_from_spec(self.power_control)

        # A bare 'embedded' host carries no shell-framing dialect. Fail loud
        # rather than silently inheriting one, so a misconfigured non-Zephyr
        # host is caught at construction, not at first command.
        if self.command_frame is None:
            raise ValueError(
                f"EmbeddedHost {self.name!r} has no command_frame. A bare "
                f"'embedded' host carries no shell-framing dialect. Set os_type "
                f'to a profile that supplies one (e.g. "zephyr"), or pass an '
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

        TERM_RESOLVER.validate_choice(self.valid_terms, self.term)
        TRANSFER_RESOLVER.validate_choice(self.valid_transfers, self.transfer)

        # An RTOS telnet shell has no login step — force ``login=False`` so the
        # connection never blocks waiting for a ``login:`` prompt that the
        # device will never send.
        factory = self._connection_factory or ConnectionManager
        self._connections = factory(
            ip=self.ip,
            creds=self.creds,
            user=self.user,
            term=self.term,
            name=self.name,
            hop=hop_transport,
            telnet_options=replace(self.telnet_options, login=False, single_client_console=True),
        )
        self._session_mgr = SessionManager(
            connections=self._connections,
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            command_frame=self.command_frame,
            init_timeout=_EMBEDDED_INIT_TIMEOUT,
            # This family was the only one omitting it, so every session-layer
            # message that names a host — `perform_switch`'s proxy errors, a
            # dry run's `CommandNotRunError` — said `host ''` here. `self.id`
            # rather than `self.name` to match the three sibling families
            # (unix, local, docker) and the field's own name; `__post_init__`
            # assigns it above, so it is populated by this line.
            host_id=self.id,
            landing_frame=self.landing_frame,
            session_setup=self.session_setup,
        )
        self._file_transfer = cast(
            "EmbeddedFileTransfer",
            build_transfer_backend(self.transfer).create(
                TransferContext(
                    transfer=self.transfer,
                    host_name=self.name,
                    exec_cmd=lambda *a, **kw: self._run_one(*a, **kw),  # noqa: PLW0108 — late-bind self for monkeypatching
                    filesystem=self.filesystem,
                    max_filename_len=self.max_filename_len,
                )
            ),
        )

    ####################
    #  Connection
    ####################

    @override
    async def _probe_connection(self) -> None:
        """Open the single telnet console — the embedded connect probe."""
        await self._connections.telnet()

    ####################
    #  Command execution
    ####################

    @override
    async def _login(self, user: str | None = None) -> None:
        """Open an interactive shell bridged to the local terminal.

        Not yet implemented for embedded hosts — the telnet bridge for a
        login-less RTOS shell lands in a later phase. ``user`` is accepted
        for signature parity with :meth:`~otto.host.host.BaseHost._login`
        but embedded hosts have no login-proxy chain to replay.
        """
        raise NotImplementedError(
            "Interactive sessions for embedded hosts are not yet implemented"
        ) from None

    @override
    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: str | None = None,
    ) -> CommandResult:
        """Run a single command on the embedded host.

        Unlike :meth:`~otto.host.host.BaseHost.exec`, this is **not** concurrency-safe: an
        embedded target exposes a single console with no stateless exec
        primitive, so ``exec`` runs on the same persistent session as
        ``run``. It exists for API parity; use ``run`` for stateful
        workflows.

        The *user* refusal is :meth:`_refuse_exec_user`'s, not this method's,
        so it lands above ``exec``'s dry-run arm.
        """
        return await self._session_mgr.run_cmd(cmd, timeout=timeout, log=self._effective_log(log))

    @override
    def _refuse_exec_user(self, user: str) -> None:
        """Refuse ``exec(user=...)``: a serial console has no user to switch to."""
        raise NotImplementedError(
            f"{self.name}: exec(user=...) is not supported on EmbeddedHost — "
            f"a serial console has no user to switch to"
        ) from None

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
                help="Not supported on this host type — containers chown, "
                "unix hosts authenticate as the user."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
    ) -> Result:
        """Transfer files from the embedded host to the local machine.

        Delegates to :class:`~otto.host.transfer.EmbeddedFileTransfer`,
        which speaks the device shell (the ``console`` backend uses Zephyr's
        ``fs`` commands). Transfers are sequential — an embedded target has a
        single console.

        ``recursive`` is not supported: see :ref:`recursive-transfers`.
        """
        if recursive:
            raise NotImplementedError(
                f"{self.name}: get(recursive=True) is not supported on EmbeddedHost — "
                f"a console transfer of a tree needs its own measurement; transfer files one by one"
            ) from None
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: get(user=...) is not supported on EmbeddedHost — "
                f"transfer ownership follows the connection's own identity"
            ) from None
        if not isinstance(src_files, list):
            src_files = [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", src_files, dest_dir)
        with SuppressCommandOutput(host=cast("Host", self)):
            return await self._file_transfer.get_files(src_files, dest_dir, show_progress)

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
                help="Not supported on this host type — containers chown, "
                "unix hosts authenticate as the user."
            ),
        ] = None,
        show_progress: Annotated[bool, Exclude] = True,
        recursive: Annotated[bool, Opt(short="-r", help="Recurse into directory sources.")] = False,
    ) -> Result:
        """Transfer files from the local machine to the embedded host.

        Delegates to :class:`~otto.host.transfer.EmbeddedFileTransfer`
        (the ``console`` backend writes via Zephyr's chunked ``fs write``).
        Transfers are sequential — an embedded target has a single console.

        ``dest_dir`` is resolved against :attr:`default_dest_dir` so a
        generic ``Path()`` from a fan-out caller lands on the host's
        mounted filesystem (e.g. ``/RAM:`` on a FAT target) rather than on
        Zephyr's bare ``/``, which has no FS and rejects opens with
        ``-ENOENT``.

        A non-``None`` *mode* is **rejected before any bytes move**: an
        embedded filesystem (FAT, LittleFS) has no permission bits to set, so
        accepting one would be a silent lie. The parameter exists on the
        signature only so the failure names this host and backend rather than
        surfacing as an unknown-argument error.

        ``recursive`` is not supported: see :ref:`recursive-transfers`.
        """
        if recursive:
            raise NotImplementedError(
                f"{self.name}: put(recursive=True) is not supported on EmbeddedHost — "
                f"a console transfer of a tree needs its own measurement; transfer files one by one"
            ) from None
        if user is not None:
            raise NotImplementedError(
                f"{self.name}: put(user=...) is not supported on EmbeddedHost — "
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
    #  File operations
    ####################

    @cli_exposed(output_dir=False)
    async def exists(self, path: "str | Path") -> bool:
        """Return ``True`` when *path* exists on the device (via ``fs ls``).

        Raises:
            ~otto.result.CommandNotRunError: under a dry run, which asked the
                device nothing — see
                :func:`~otto.host.host.refuse_declined_fact`.
        """
        result = await self._run_one(
            self.filesystem.ls_command(str(path)), timeout=DEFAULT_COMMAND_TIMEOUT
        )
        refuse_declined_fact(result, asked=f"exists({str(path)!r})")
        return result.status.is_ok

    @cli_exposed(output_dir=False)
    async def ls(self, path: "Annotated[str | Path, Arg()]" = ".", all: bool = False) -> list[str]:  # noqa: A002, ARG002 — A002: CLI-exposed param name; ARG002: required by UnixHost.ls override signature
        """List entry names in *path* via the device ``fs ls`` former.

        Raises:
            ~otto.result.CommandNotRunError: under a dry run — see
                :func:`~otto.host.host.refuse_declined_fact`.
        """
        result = await self._run_one(
            self.filesystem.ls_command(str(path)), timeout=DEFAULT_COMMAND_TIMEOUT
        )
        refuse_declined_fact(result, asked=f"ls({str(path)!r})")
        if not result.status.is_ok:
            return []
        return [line for line in result.value.splitlines() if line]

    @cli_exposed
    async def rm(
        self,
        path: "str | Path",
        recursive: bool = False,  # noqa: ARG002 — required by UnixHost.rm override signature (flags not supported on embedded)
        force: bool = False,  # noqa: ARG002 — required by UnixHost.rm override signature (flags not supported on embedded)
    ) -> Result:
        """Remove *path* via the device ``fs rm`` former (flags ignored)."""
        result = await self._run_one(
            self.filesystem.rm_command(str(path)), timeout=DEFAULT_COMMAND_TIMEOUT
        )
        return Result(result.status, msg=result.value)

    def _no_fileop(self, name: str) -> NoReturn:
        raise NotImplementedError(
            f"{name}() is not supported on embedded host {self.name!r}; the "
            f"device shell has no equivalent. Use get()/put() for reads/writes."
        ) from None

    async def mkdir(self, path: "str | Path", parents: bool = True) -> Result:  # noqa: ARG002 — required by UnixHost.mkdir override signature (always raises)
        """Not supported — embedded targets have no shell ``mkdir`` equivalent."""
        self._no_fileop("mkdir")

    async def cp(
        self,
        src: "str | Path",  # noqa: ARG002 — required by UnixHost.cp override signature (always raises)
        dst: "str | Path",  # noqa: ARG002 — required by UnixHost.cp override signature (always raises)
        recursive: bool = False,  # noqa: ARG002 — required by UnixHost.cp override signature (always raises)
    ) -> Result:
        """Not supported — embedded targets have no shell ``cp`` equivalent."""
        self._no_fileop("cp")

    async def mv(self, src: "str | Path", dst: "str | Path") -> Result:  # noqa: ARG002 — required by UnixHost.mv override signature (always raises)
        """Not supported — embedded targets have no shell ``mv`` equivalent."""
        self._no_fileop("mv")

    async def read_file(self, path: "str | Path") -> str:  # noqa: ARG002 — required by UnixHost.read_file override signature (always raises)
        """Not supported — use :meth:`get` to retrieve files from an embedded target."""
        self._no_fileop("read_file")

    async def write_file(
        self,
        path: "str | Path",  # noqa: ARG002 — required by UnixHost.write_file override signature (always raises)
        data: str,  # noqa: ARG002 — required by UnixHost.write_file override signature (always raises)
        append: bool = False,  # noqa: ARG002 — required by UnixHost.write_file override signature (always raises)
    ) -> Result:
        """Not supported — use :meth:`put` to send files to an embedded target."""
        self._no_fileop("write_file")

    ####################
    #  Binary load
    ####################

    @cli_exposed(success="Binary loaded.", dry_run_preview=True)
    async def load(
        self,
        file: Annotated[Path, Arg(help="Binary to load into the device runtime.")],
        name: Annotated[str, Arg(help="Name to register the loaded binary under.")],
        show_progress: Annotated[bool, Exclude] = False,
        timeout: Annotated[float, Exclude] = 120.0,
    ) -> Result:
        """Load a binary into the device runtime via the host's binary loader.

        Distinct from :meth:`put` (a *file* transfer to a mounted filesystem):
        ``load`` pushes a binary into the target's loader (e.g. Zephyr LLEXT's
        ``llext load_hex``), with no destination file. The payload is read from
        *file*, formatted into the device command by the loader, and sent with
        ``log=LogMode.NEVER`` so the (large) encoded payload never reaches the
        console or log. Returns a :class:`~otto.result.Result`; ``msg`` carries
        the device's failure text on error.

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
                    cmd,
                    timeout=timeout,
                    log=LogMode.NEVER,
                    write_progress=_wp,
                )
        else:
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout, log=LogMode.NEVER)
        ok, reason = loader.check_loaded(result.value)
        if ok:
            return Result(Status.Success)
        return Result(Status.Error, msg=f"load {name} from {file} failed: {reason}")

    @cli_exposed(success="Binary unloaded.", dry_run_preview=True)
    async def unload(
        self,
        name: Annotated[str, Arg(help="Name of the binary to unload.")],
        timeout: Annotated[float, Exclude] = 20.0,
    ) -> Result:
        """Unload *name* from the device runtime, draining to full eviction.

        Some loaders (LLEXT) refcount a resident binary, so one unload may only
        decrement it. ``unload`` loops the loader's unload command until
        :meth:`~otto.host.binary_loader.BinaryLoader.is_fully_unloaded` reports
        the binary gone (bounded by ``loader.max_unload_rounds``). Idempotent:
        unloading something not loaded succeeds on the first round. Returns a
        :class:`~otto.result.Result`; fails loud (``ValueError``) if no loader
        is declared.
        """
        loader = self._require_loader()
        if is_dry_run():
            return self._dry_run_transfer("UNLOAD", [], Path(name))
        cmd = loader.unload_command(name)
        last = ""
        for _ in range(loader.max_unload_rounds):
            result = await self._session_mgr.run_cmd(cmd, timeout=timeout)
            last = result.value
            if loader.is_fully_unloaded(result.value):
                return Result(Status.Success)
        return Result(
            Status.Error,
            msg=(
                f"{name} still resident after {loader.max_unload_rounds} "
                f"unload rounds: {last.strip()}"
            ),
        )


@dataclass(slots=True, kw_only=True)
class ZephyrHost(EmbeddedHost):
    """A Zephyr RTOS host — the concrete, registered embedded host.

    This is the worked example for shipping a host subclass: it declares nothing
    of its own and re-states only the Zephyr-specific field VALUES that
    :class:`EmbeddedHost` does not assume, and is registered under
    ``os_type: "zephyr"`` via
    :func:`otto.host.os_profile.register_host_class`. External repositories
    register their own ``EmbeddedHost``/``UnixHost`` subclasses the same way
    (from an init module listed in ``.otto/settings.toml``), and may layer
    per-build ``OsProfile`` data bundles over them.
    """

    os_type: OsType = "zephyr"

    os_name: str | None = "Zephyr"

    command_frame: CommandFrame = field(default_factory=ZephyrFrame)

    ####################
    #  Power / reboot
    ####################

    @override
    async def _soft_reboot(self) -> Result:
        # UNREACHABLE UNDER A DRY RUN, and unsafe there: the `Success` is
        # returned whatever `run` answered, so a dry run's `NotRun` decline
        # would be reported as a reboot. `BaseHost.reboot`'s dry-run arm
        # returns above the only call site — a future caller reaching this
        # under a dry run needs its own arm. Same note as
        # `UnixHost._soft_reboot`.
        await self.run("kernel reboot cold", timeout=10.0)
        return Result(Status.Success)
