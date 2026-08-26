"""SFTP file transfer backend for UnixHost.

Registers ``sftp`` into the shared transfer registry on import.

THE ONE GAPPED SURFACE OTTO DOES NOT PRE-CHECK, and the absence is a decision
rather than an omission. ``sftp-transfer`` in
:data:`~otto.host.userland.GAPS` is ``measured-broken`` like ``scp-transfer``
beside it, and the two are wired completely differently:
:func:`~otto.host.transfer.scp.refuse_if_scp_is_absent` declines before opening
a connection, while :func:`open_sftp_or_attribute` below opens the subsystem,
lets it fail, and translates the failure.

The reason is that there is nothing here to ask. Whether a device serves sftp
is a property of its SSH SERVER's subsystem configuration, and every fact otto
could read instead answers wrongly in the expensive direction: ``sftp-server``
is not on ``PATH`` even on a healthy Debian host, its absolute path differs
across distros and is compiled into dropbear rather than configured, and the
daemon is not the authority either -- packaged dropbear serves sftp perfectly
well on a machine that provides the binary. A pre-check built on any of them
refuses hosts that work, which is the one mistake the gap registry is ordered
to avoid. The definitive test is opening the subsystem, and that is the
operation.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from asyncssh import SFTPClient

    from ..connections import ConnectionManager

import logging

from typing_extensions import override

from ...result import CommandResult, Result
from ...utils import Status
from ..userland import refuse_if_gapped
from .base import (
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
)
from .progress import _make_sftp_progress
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)


async def open_sftp_or_attribute(
    connections: "ConnectionManager", *, host: str = "", attempted: str = ""
) -> "SFTPClient":
    """Open the SFTP subsystem, or fail in the gap registry's words instead of asyncssh's.

    **The gap registry's sixth product call site, and the first that does not
    prevent anything.** Every other consumer of
    :func:`~otto.host.userland.refuse_if_gapped` decides in advance that a
    device cannot do the thing and declines before emitting it. This one cannot:
    see the module docstring for why no pre-check exists that does not refuse
    working hosts. So the subsystem is opened, and what the registry supplies is
    the SENTENCE the caller gets when it does not start.

    WHAT AN OPERATOR GOT BEFORE THIS EXISTED, measured on 2026-08-14 against the
    since-retired rig -- a BusyBox root served over dropbear:
    ``asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a total of 4 expected
    bytes``, out of ``UnixHost.put``, in 22ms. Prompt, residue-free and
    correctly not blamed on any file -- and naming nothing an operator can act
    on. It does not say sftp, it does not say the device, and
    it reads like a truncated connection, so the diagnosis it invites is that
    the link is unreliable. What it costs is a debugging session on a host whose
    fix is one line of lab config.

    WHY IT IS SAFE TO ATTRIBUTE, which is the whole argument for doing this
    after the fact rather than before. This arm runs only when the subsystem has
    ALREADY failed to start, so there is no host it can turn away: a device with
    an sftp-server serves the handshake, this function returns the client, and
    nothing here is reached. The ``busybox`` profile lists ``sftp`` in
    ``valid_transfers`` deliberately for exactly that device, and this guard
    keys on nothing about the profile, the ``os_type`` or the userland.

    WHAT IT CATCHES, AND WHAT IT DELIBERATELY DOES NOT. Only
    :exc:`~asyncssh.sftp.SFTPConnectionLost`, which is what asyncssh raises when
    the subsystem channel opens and then closes before the SFTP version
    exchange -- the measured shape, produced by dropbear execing an
    ``sftp-server`` that is not there (exit 127) and equally by one that is not
    executable (exit 126).

    :exc:`~asyncssh.misc.ChannelOpenError` is NOT caught, and that is the
    interesting half. It is the other way an sftp session fails to start --
    measured 2026-08-14 against an in-process asyncssh server that refuses the
    session, giving ``asyncssh.misc.ChannelOpenError: Session refused`` -- and
    it is also exactly what an sshd at its ``MaxSessions`` ceiling answers, on
    any host, sftp-server or not. Attributing it to a missing subsystem would
    reintroduce the false-absent mistake in message form: an operator whose
    device serves sftp fine would be told their userland cannot. So the narrower
    catch is the honest one, and a device whose daemon refuses the session
    keeps getting asyncssh's error.

    THE TABLE STILL DECIDES. The verdict and the message come from
    :func:`~otto.host.userland.refuse_if_gapped`, so downgrading the record to
    ``untested`` does not merely soften this -- it puts asyncssh's own exception
    back in the caller's hands, unwrapped, via the bare ``raise`` below. That is
    the same downgrade behaviour every other consumer has, and it is what makes
    the record the authority here rather than this function.

    Args:
        connections: the host's manager, whose ``sftp()`` opens (and caches) the
            client.
        host: the host id, for the message. Decoration only.
        attempted: what the caller was doing, for the message. Decoration only.

    Returns:
        The live :class:`~asyncssh.SFTPClient`, on any host whose subsystem
        starts.

    Raises:
        ~otto.host.errors.UnsupportedOnUserlandError: the subsystem closed
            before the SFTP handshake and ``sftp-transfer`` is declared
            measured-broken. It is raised from inside the ``except`` block, so
            asyncssh's own exception stays attached as ``__context__`` and the
            traceback prints both -- the record's sentence for the operator, the
            byte count underneath it for whoever has to read asyncssh.
        asyncssh.sftp.SFTPConnectionLost: the same failure, re-raised untouched,
            when the record does not refuse.
    """
    # DEFERRED, and not for speed. `tests/unit/host/test_lazy_network_imports.py`
    # holds asyncssh out of the import graph that merely DISCOVERING host classes
    # walks -- it is a runtime dependency, and this module is imported by the
    # transfer registry on every `otto --help`. A module-level
    # `from asyncssh.sftp import SFTPConnectionLost` reds that guard. By the time
    # control is here the library is loaded anyway: `connections.sftp()` cannot
    # have raised this without it.
    from asyncssh.sftp import SFTPConnectionLost

    try:
        return await connections.sftp()
    except SFTPConnectionLost as exc:
        refuse_if_gapped(
            "sftp-transfer",
            host=host,
            observed=(
                f"the sftp subsystem was opened and the device closed it before the SFTP "
                f"handshake{f' ({attempted})' if attempted else ''}, giving "
                f"`{type(exc).__name__}: {exc}`"
            ),
        )
        raise


# The block size otto asks asyncssh to read and write in, and therefore the
# stride of the progress events asyncssh reports back. asyncssh's own default
# is `-1`, which lets the SERVER choose the maximum it will negotiate, so the
# stride would be unknowable and no promise could be made about it. 16 KiB is
# asyncssh's own historical default, and the bed measures what pinning it costs.
_SFTP_BLOCK_SIZE = 16384


class SftpFileTransfer(UnixFileTransfer):
    """SFTP file transfer backend for UnixHost.

    Inherits ``put_files`` / ``get_files`` from
    :class:`~otto.host.transfer.base.BaseFileTransfer` and unix scaffolding
    (``_connections``, ``_exec_cmd``, ``_warmup_for_transfer``) from
    :class:`~otto.host.transfer.unix_base.UnixFileTransfer`; implements
    ``_run_put`` / ``_run_get`` directly for the SFTP protocol.
    """

    host_families = frozenset({"unix"})

    # True by CONSTRUCTION rather than by observation: this declaration and the
    # two `block_size=` arguments `_get_files_sftp` / `_put_files_sftp` hand to
    # asyncssh all read ONE module constant, so they cannot drift.
    #
    # The calls pass `_SFTP_BLOCK_SIZE` rather than reading this attribute back,
    # and that is a TYPE constraint, not a style choice: a `ProgressGranularity`
    # arm is `int | None` because a `None` arm is legal for backends that emit one
    # event at completion, and asyncssh declares `block_size: int`. Reading the
    # declaration at the call site hands it an `int | None` and fails typecheck.
    # `test_sftp_passes_its_declared_block_size_to_asyncssh` is what keeps the
    # kwarg and this declaration equal -- it asserts them against each other, so
    # the agreement is measured rather than assumed.
    progress_granularity = ProgressGranularity(put=_SFTP_BLOCK_SIZE, get=_SFTP_BLOCK_SIZE)

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )

    @override
    @classmethod
    def create(cls, ctx: TransferContext) -> "SftpFileTransfer":
        """Build the backend from *ctx*, rejecting what this protocol cannot do without.

        Two fields are required and ``userland`` deliberately is not: nothing
        here reads a probe, because this surface has no fact to probe for -- see
        :func:`~otto.host.transfer.sftp.open_sftp_or_attribute`.

        The annotation is the live class rather than the quoted name, and it is
        not interchangeable: ``docs/api/host/transfer_sftp.rst`` renders this
        method under ``nitpicky``, where the quoted form arrived as a bare
        ``TransferContext`` that resolved to no target and failed the ``-W``
        build. The class is imported at runtime here anyway, so evaluating it
        costs nothing.
        """
        if ctx.connections is None:
            raise ValueError(
                "SftpFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("SftpFileTransfer requires exec_cmd on the transfer context")
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            max_filename_len=ctx.max_filename_len,
        )

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        sftp_conn = await open_sftp_or_attribute(
            self._connections, host=self._name, attempted=f"GET {len(src_files)} file(s)"
        )
        return await self._get_files_sftp(sftp_conn, src_files, dest_dir, progress_factory)

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> dict[Path, Result]:
        sftp_conn = await open_sftp_or_attribute(
            self._connections, host=self._name, attempted=f"PUT {len(src_files)} file(s)"
        )
        return await self._put_files_sftp(sftp_conn, src_files, dest_dir, progress_factory)

    async def _get_files_sftp(
        self,
        sftp_conn: "SFTPClient",
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        async def _get_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP get {src} -> {dest_dir}")
            await sftp_conn.get(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
                block_size=_SFTP_BLOCK_SIZE,
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_get_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file

    async def _put_files_sftp(
        self,
        sftp_conn: "SFTPClient",
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None = None,
    ) -> dict[Path, Result]:
        async def _put_one(src: Path) -> Result:
            _progress = (
                _make_sftp_progress(progress_factory()) if progress_factory is not None else None
            )
            _logger.debug(f"{self._name}: SFTP put {src} -> {dest_dir}")
            await sftp_conn.put(
                str(src),
                str(dest_dir / src.name),
                progress_handler=_progress,
                block_size=_SFTP_BLOCK_SIZE,
            )
            return Result(Status.Success, value=dest_dir / src.name)

        gathered = await asyncio.gather(
            *(_put_one(src) for src in src_files), return_exceptions=True
        )
        per_file: dict[Path, Result] = {}
        for src, outcome in zip(src_files, gathered, strict=True):
            if isinstance(outcome, BaseException):
                per_file[src] = Result(Status.Error, msg=f"{src}: {outcome}")
            else:
                per_file[src] = outcome
        return per_file


register_transfer_backend("sftp", SftpFileTransfer)
