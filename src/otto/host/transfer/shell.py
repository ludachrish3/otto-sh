"""Shell-only file transfer backend for UnixHost -- chunked base64 over exec.

Moves files using nothing but command execution: no ``scp``, no ``nc``, no
``rsync``. Built for the devices at the bottom of the BusyBox matrix, whose
entire toolkit is a POSIX shell plus whatever applets are actually present --
which, on a Tier 2 rootfs old enough to lack ``base64`` entirely, may not
include a decoder at all (see ``ShellFileTransfer._run_put``).

PUT chunks the local file into ``_SHELL_CHUNK_BYTES``-byte plaintext
pieces, base64-encodes each locally, and appends it to a same-directory temp
file via ``printf '%s' '<chunk>' | base64 <flag> >> <temp>``; once every
chunk has landed, an integrity check runs against the TEMP (see below), and
only then does the last step move the temp onto the real destination with
``mv``, so a transfer that dies -- or fails its integrity check -- partway
through never leaves a truncated or wrong file at the real path. See
``ShellFileTransfer._put_one``.

GET is the mirror image with the encode/decode roles swapped, and the
staging moved from the remote side to the local one: the device is asked for
the file's size (:attr:`~otto.host.userland.Userland.stat_size`), then for
each ``_SHELL_CHUNK_BYTES``-byte range via
``dd if=<src> bs=<N> skip=<k> count=1 2>/dev/null | base64`` -- the device
only *encodes*; every decode runs locally, in Python, chunk by chunk. Bytes
land in a same-directory local temp (``<dest>.otto-<unique>``, exactly
PUT's naming); the same integrity check PUT runs then confirms the temp
against the remote source, and ``Path.replace()`` swings it onto the real
destination only once that check passes. See
``ShellFileTransfer._get_one``.

Both directions verify the same way, in
``ShellFileTransfer._verify_integrity``:
:attr:`~otto.host.userland.Userland.checksum` resolving to ``"md5sum"``
means a device-side ``md5sum`` compared against a digest accumulated locally,
chunk by chunk, as bytes are sent or decoded; resolving to ``"absent"``
means a byte-size comparison instead, via the same
:attr:`~otto.host.userland.Userland.stat_size` capability GET's own sizing
already reads. Either way, a mismatch is a failed transfer with the temp
removed -- never a warning, and never a ``Status.Success`` with a note.

Registers ``shell`` into the shared transfer registry on import, and is the
``busybox`` OS profile's default ``transfer`` (``otto.host.os_profile.
_register_builtin_os_profiles``).
"""

import base64
import hashlib
import logging
import shlex
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing_extensions import override

if TYPE_CHECKING:
    from ..connections import ConnectionManager
    from ..userland import Userland

from ...result import CommandResult, Result
from ...utils import Status
from ..errors import UnsupportedOnUserlandError
from .base import TransferContext, TransferProgressFactory, TransferProgressHandler, mark_skipped
from .registry import register_transfer_backend
from .unix_base import UnixFileTransfer

_logger = logging.getLogger(__name__)

_SHELL_CHUNK_BYTES = 4096
"""Bytes of PLAINTEXT per chunk; base64 expands this to 5464 characters.

Conservative, but NOT because it sits comfortably below every plausible line
limit -- it does not. Measured device-side: a single exec argument of >=4 MB
succeeds in the Tier 2 rootfs, so ARG_MAX is nowhere near binding. The real
constraint is transport-side -- telnet line handling and the command frame's
per-line processing -- and NOTHING IN THIS PHASE MEASURES IT, because a
chroot has no transport. One full chunk's command line is over 5500
characters (see ``TestShellChunkLineLength``), which already exceeds the
classic Linux canonical-mode tty line buffer: measured directly in this
worktree via a real pty (``pty.openpty()``, ``ICANON`` set), writing an
8001-byte line returns only 4096 bytes on the first read. Whether a real
telnet session's line discipline imposes that same limit is exactly what
Phase 5's Tier 3 (rootless dropbear) is for measuring; until then, the
honest statement is that this value's wire-level headroom is UNMEASURED and
plausibly negative, not that it was chosen with margin to spare. If that
bites, the failure mode is a wedge or a loud integrity failure (``base64
-d``'s own validation, or the post-chunk md5sum/size check) -- never silent
corruption, since a wire-level split breaks the base64 stream cleanly
rather than producing plausible-looking wrong bytes.
"""


class ShellFileTransfer(UnixFileTransfer):
    """File transfer using nothing but command execution -- no scp, nc, or rsync.

    Every byte moves as base64 text piped through the device's own shell --
    the last resort for a host with no ``scp``, no ``nc``, and no ``rsync``.

    PUT chunks the local file into ``_SHELL_CHUNK_BYTES``-byte plaintext
    pieces; each piece is base64-encoded locally and appended to a
    same-directory temp file (``<dest>.otto-<unique>``); once every chunk has
    landed, an integrity check (see ``_verify_integrity``) runs against
    the temp, and only on a match is it moved onto the real destination with
    ``mv``. See ``_put_one`` for why the temp-then-mv shape is
    load-bearing, not decorative.

    GET asks the device for the source file's size, then pulls it back
    ``_SHELL_CHUNK_BYTES`` at a time via ``dd | base64`` (the device only
    encodes; every decode is local Python), staging the decoded bytes in a
    same-directory local temp; the same integrity check then confirms the
    temp against the remote source, and only on a match does
    ``Path.replace()`` swing it onto the real destination. See
    ``_get_one``.

    Two things are hard dependencies, unlike :class:`~otto.host.transfer.nc.NcFileTransfer`
    (whose ``userland`` is optional -- see its own docstring for why):

    ``exec_cmd``
        Every unix backend needs this; there is no other way to run
        anything on the device.
    ``userland``
        Whether ``base64`` exists at all, and which flag spells "decode",
        can only be known by asking the device -- see
        :attr:`~otto.host.userland.Userland.base64_flag`. Assuming a
        GNU-coreutils spelling breaks on every BusyBox row in the matrix
        (``base64 --decode`` is rejected everywhere it was tested; see
        :attr:`~otto.host.options.UserlandOptions.base64_flag`), and
        assuming BusyBox's ``-d`` breaks GNU hosts the other way. GET reads
        the same object for :attr:`~otto.host.userland.Userland.stat_size`
        -- whether the device sizes a file with ``stat`` or falls back to
        ``wc`` can only be known the same way. Both directions also read
        :attr:`~otto.host.userland.Userland.checksum` for
        ``_verify_integrity`` -- whether the device has ``md5sum`` at
        all, gating a checksum comparison against the weaker byte-size one.

    Both are enforced in :meth:`create`, before any bytes move.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        userland: "Userland",
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(
            connections=connections,
            name=name,
            exec_cmd=exec_cmd,
            max_filename_len=max_filename_len,
        )
        self._userland = userland

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ShellFileTransfer":
        if ctx.connections is None:
            raise ValueError(
                "ShellFileTransfer requires a connections manager on the transfer context"
            )
        if ctx.exec_cmd is None:
            raise ValueError("ShellFileTransfer requires exec_cmd on the transfer context")
        if ctx.userland is None:
            raise ValueError(
                "ShellFileTransfer requires a userland resolver on the transfer context -- "
                "shell put and get must know whether base64 is even present, get must also "
                "know how to size a file, and both must know whether they can verify a "
                "transfer landed intact (md5sum or a size probe) before either emits a "
                "single command"
            )
        return cls(
            connections=ctx.connections,
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            userland=ctx.userland,
            max_filename_len=ctx.max_filename_len,
        )

    # ------------------------------------------------------------------
    # Protocol dispatch (implements BaseFileTransfer's abstract methods)
    # ------------------------------------------------------------------

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Sequential shell PUT: refuse up front, then chunk-and-mv one file at a time.

        Two refusal checks run before the loop -- and before anything else in
        this method -- because each answers a question about the WHOLE
        transfer, not about any one file: if the device has no ``base64``, or
        no way to confirm a PUT landed intact, every file would fail
        identically, and issuing the first file's chunk command anyway would
        read as "we tried and it failed" when nothing was tried at all. See
        :exc:`~otto.host.errors.UnsupportedOnUserlandError`.

        ``base64_flag == "absent"`` is checked first: without it, no chunk
        can even be encoded, so nothing else here can run at all.
        ``checksum == "absent" and stat_size == "absent"`` together is
        checked second: :meth:`_put_one` always verifies its temp before the
        final ``mv`` (see :meth:`_verify_integrity`), and with neither a
        digest tool nor a size probe on the device there is no way to run
        that check at all -- refusing here, before a single chunk is sent,
        keeps that requirement as loud as the ``base64`` one instead of
        surfacing as a per-file failure partway through the first transfer
        attempted. Unhit by the BusyBox matrix (measured: ``md5sum`` and
        ``stat``/``wc`` are present on every row), so this is a unit-only
        branch, like GET's own ``stat_size == "absent"`` refusal below.

        ``resolve()`` is idempotent, serialized, and rate-limited (see
        :meth:`~otto.host.userland.Userland.resolve`), so calling it once per
        ``_run_put`` -- rather than once per file -- is the intended usage,
        matching :meth:`~otto.host.transfer.nc.NcFileTransfer.prepare`.

        Sequential, like :class:`~otto.host.transfer.ftp.FtpFileTransfer`:
        this backend has no concurrency story of its own (every chunk is one
        more exec round trip on the same control path), so a failure stops
        the batch and every file not yet attempted is marked
        ``Status.Skipped`` rather than guessed at.
        """
        await self._userland.resolve()
        flag = self._userland.base64_flag
        if flag == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs base64 on the remote host, but this "
                "host's userland resolved base64_flag='absent' -- no base64 binary was "
                "found there. Nothing was attempted; install base64 on the device or "
                "transfer with a backend this host actually supports."
            )
        checksum = self._userland.checksum
        stat_size = self._userland.stat_size
        if checksum == "absent" and stat_size == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs a way to confirm a PUT landed intact, "
                "but this host's userland resolved checksum='absent' and stat_size='absent' "
                "-- neither md5sum nor a size probe (`stat -c %s` / `wc -c`) answered on "
                "this host. Nothing was attempted; transfer with a backend this host "
                "actually supports."
            )
        per_file: dict[Path, Result] = {}
        for i, src in enumerate(src_files):
            dst = dest_dir / src.name
            handler = progress_factory() if progress_factory is not None else None
            result = await self._put_one(src, dst, flag, checksum, stat_size, handler)
            per_file[src] = result
            if not result.is_ok:
                mark_skipped(per_file, src_files[i + 1 :])
                break
        return per_file

    @override
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: "TransferProgressFactory | None",
    ) -> dict[Path, Result]:
        """Sequential shell GET: refuse up front, then dd|base64-and-decode one file at a time.

        Two refusals run before the loop, for the same reason
        :meth:`_run_put`'s own two refusals do: each answers a question
        about the WHOLE transfer, not about any one file, so answering it
        late would read as "we tried and it failed" when nothing was tried
        at all. GET's two are not the same PAIR as PUT's, though: GET never
        needs the ``checksum``-or-``stat_size`` refusal PUT does, because
        GET's ``checksum == "absent"`` fallback reuses the size it already
        fetched for chunk planning (see :meth:`_verify_integrity`) rather
        than needing a second, independent probe the way PUT's fresh
        query on its temp does -- so GET's ``stat_size == "absent"`` refusal
        below already covers the one case that would otherwise leave GET
        with no way to verify either.

        ``stat_size == "absent"`` is checked first: GET cannot chunk a file
        it cannot measure, and ``bytes_total`` in the progress contract
        needs a real number, not a guess. Measured (this phase's task
        brief, ahead of this task's dispatch): every row of the BusyBox
        matrix resolves ``stat_size`` to ``"stat"``, so no matrix device
        reaches this branch today -- but the value is PROBED, not assumed,
        and a non-BusyBox unix host can answer differently, so the branch
        is real and stays.

        ``base64_flag == "absent"`` is checked second, exactly as in
        :meth:`_run_put` -- the device only encodes for GET, so the same
        binary is required even though no *decode* flag of its own is ever
        emitted remotely (decoding happens locally; see :meth:`_get_one`).

        Sequential, like :meth:`_run_put`: this backend has no concurrency
        story of its own, so a failure stops the batch and every file not
        yet attempted is marked ``Status.Skipped``.
        """
        await self._userland.resolve()
        stat_size = self._userland.stat_size
        if stat_size == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs a way to size a remote file before "
                "it can chunk the read, but this host's userland resolved "
                "stat_size='absent' -- neither `stat -c %s` nor `wc -c` answered on this "
                "host. Nothing was attempted; transfer with a backend this host actually "
                "supports."
            )
        flag = self._userland.base64_flag
        if flag == "absent":
            raise UnsupportedOnUserlandError(
                f"{self._name}: shell transfer needs base64 on the remote host, but this "
                "host's userland resolved base64_flag='absent' -- no base64 binary was "
                "found there. Nothing was attempted; install base64 on the device or "
                "transfer with a backend this host actually supports."
            )
        checksum = self._userland.checksum
        per_file: dict[Path, Result] = {}
        for i, src in enumerate(src_files):
            dst = dest_dir / src.name
            handler = progress_factory() if progress_factory is not None else None
            result = await self._get_one(src, dst, stat_size, checksum, handler)
            per_file[src] = result
            if not result.is_ok:
                mark_skipped(per_file, src_files[i + 1 :])
                break
        return per_file

    # ------------------------------------------------------------------
    # Shell put
    # ------------------------------------------------------------------

    async def _put_one(
        self,
        src: Path,
        dst: Path,
        flag: str,
        checksum: str,
        stat_size: str,
        handler: "TransferProgressHandler | None",
    ) -> Result:
        """PUT one file: chunk, base64-encode, exec, verify, temp-then-mv.

        Stages as ``<dst>.otto-<unique>`` in ``dst``'s own directory --
        deliberately not under ``/tmp``, which is not guaranteed to be the
        same filesystem as the destination, and a cross-filesystem ``mv``
        degrades to a copy, losing the atomicity a same-filesystem rename
        gives the final step. The first chunk creates the temp (``>``);
        every later chunk appends (``>>``) -- successive ``base64 <flag>``
        invocations appending to the same file concatenate correctly,
        IN ORDER (measured, Tier 2 rootfs,
        ``tests/busybox/test_shell_codec_contracts.py::test_chunk_append_reassembles_in_order``,
        the four matrix rows that have a ``base64`` applet at all -- 1.16.1
        has none, so that row returns early and is not part of this
        measurement), so chunk N+1 lands cleanly after chunk N. A source
        file with no bytes at all never enters the chunk loop, so its temp
        is created directly.

        *flag* is emitted verbatim -- whatever
        :attr:`~otto.host.userland.Userland.base64_flag` resolved to, never
        hard-coded. The encoded text is emitted as ``printf '%s' '<chunk>'``
        rather than ``echo <chunk>`` -- the shape
        ``test_chunk_append_reassembles_in_order`` (and
        ``test_a_binary_hostile_payload_survives_the_round_trip``) actually
        measured on-device, not merely a plan description. This is not
        because ``echo`` was untested there: it was, at
        ``test_the_decode_spelling_matches_what_the_matrix_records:205``,
        which runs ``echo aGk= | base64 <flag>`` on-device. What was never
        measured is ``echo`` at 5464 characters (this module's real chunk
        size, versus that test's 4-character probe), or its escape and
        ``-n``-guarding behaviour in general -- both are userland-dependent
        and outside what any test in that suite pins. The literal single quotes
        around ``<chunk>`` need no escaping: the base64 alphabet this
        module's encoder emits is ``[A-Za-z0-9+/=]`` only (verified against
        :func:`base64.b64encode`'s own output, which also never wraps the
        text -- each chunk is one line), so it contains no single quote, no
        other shell metacharacter, and never starts with ``-``. The temp and
        destination paths are not so safe -- ``shlex.quote`` plus a ``--``
        terminator on every command that takes a path as a positional
        argument (``mv``, ``rm``), matching :func:`~otto.host.transfer.base.chmod_command`'s
        precedent, because ``shlex.quote`` alone leaves a leading-dash
        basename like ``-rf`` unquoted and a command without ``--`` would
        then read it as flags.

        Any failure -- a chunk write, the temp's creation, a failed or
        mismatched integrity check, or the final ``mv`` -- removes the temp
        (best-effort; its own failure is logged, not raised, since the file
        to report on has already failed) and returns a failing
        :class:`~otto.result.Result`. A local read error (the source
        vanished, permissions changed) is caught the same way, since by the
        time this runs :meth:`BaseFileTransfer.put_files` has already
        validated the file list -- this is a race, not a precondition otto
        skipped.

        Verification (see :meth:`_verify_integrity`) runs on the TEMP, after
        every chunk has landed and before the ``mv`` -- a mismatch is
        therefore caught before it ever reaches the real destination, not
        reported after the fact against a file the caller may already be
        reading. Every chunk-write byte also updates a running
        :func:`hashlib.md5` locally, so an ``md5sum`` verification costs no
        second local read pass over *src*.
        """
        temp = dst.parent / f"{dst.name}.otto-{uuid.uuid4().hex}"
        quoted_temp = shlex.quote(str(temp))
        try:
            total = src.stat().st_size
            bytes_done = 0
            wrote_any = False
            # MD5 is a corruption check against `md5sum`, the tool name this
            # capability is named for and the only one BusyBox ships -- never
            # a security boundary, so the collision-resistance ruff's S324
            # warns about does not apply here.
            local_digest = hashlib.md5()  # noqa: S324
            with src.open("rb") as f:
                while True:
                    chunk = f.read(_SHELL_CHUNK_BYTES)
                    if not chunk:
                        break
                    encoded = base64.b64encode(chunk).decode("ascii")
                    redirect = ">>" if wrote_any else ">"
                    result = await self._exec_cmd(
                        f"printf '%s' '{encoded}' | base64 {flag} {redirect} {quoted_temp}"
                    )
                    if not result.is_ok:
                        await self._cleanup_temp(quoted_temp)
                        return Result(
                            Status.Error,
                            msg=(
                                f"{src}: writing a chunk to {temp} failed "
                                f"(exit {result.retcode}): {result.value or result.msg}"
                            ),
                        )
                    wrote_any = True
                    bytes_done += len(chunk)
                    local_digest.update(chunk)
                    if handler is not None:
                        handler(str(src), str(dst), bytes_done, total)

            if not wrote_any:
                create_result = await self._exec_cmd(f": > {quoted_temp}")
                if not create_result.is_ok:
                    await self._cleanup_temp(quoted_temp)
                    return Result(
                        Status.Error,
                        msg=(
                            f"{src}: creating empty temp {temp} failed "
                            f"(exit {create_result.retcode}): "
                            f"{create_result.value or create_result.msg}"
                        ),
                    )
                if handler is not None:
                    handler(str(src), str(dst), 0, 0)

            mismatch = await self._verify_integrity(
                temp,
                checksum=checksum,
                stat_size=stat_size,
                local_digest=local_digest,
                local_size=bytes_done,
            )
            if mismatch is not None:
                await self._cleanup_temp(quoted_temp)
                return Result(Status.Error, msg=f"{src}: integrity check failed -- {mismatch}")

            mv_result = await self._exec_cmd(f"mv -- {quoted_temp} {shlex.quote(str(dst))}")
            if not mv_result.is_ok:
                await self._cleanup_temp(quoted_temp)
                return Result(
                    Status.Error,
                    msg=(
                        f"{src}: moving {temp} to {dst} failed "
                        f"(exit {mv_result.retcode}): {mv_result.value or mv_result.msg}"
                    ),
                )
        except OSError as e:
            await self._cleanup_temp(quoted_temp)
            return Result(Status.Error, msg=f"{src}: could not read local file: {e}")
        return Result(Status.Success, value=dst)

    async def _cleanup_temp(self, quoted_temp: str) -> None:
        """Best-effort removal of a staged temp after a failed chunk/create/mv step.

        Fire-and-forget on purpose: the file this belongs to has already
        failed, and a caller reading a two-part failure ("chunk write
        failed" AND "cleanup also failed") learns nothing a debug log
        doesn't already cover. ``rm -f`` so a temp that was never created
        (the very first chunk failed) is not itself an error.
        """
        result = await self._exec_cmd(f"rm -f -- {quoted_temp}")
        if not result.is_ok:
            _logger.debug(
                f"{self._name}: cleanup of {quoted_temp} failed (ignored): "
                f"{result.value or result.msg}"
            )

    # ------------------------------------------------------------------
    # Shell get
    # ------------------------------------------------------------------

    async def _get_one(
        self,
        src: Path,
        dst: Path,
        stat_size: str,
        checksum: str,
        handler: "TransferProgressHandler | None",
    ) -> Result:
        """GET one file: size it, pull+decode chunks, verify, stage locally, replace into place.

        PUT's temp-then-mv shape, mirrored onto the local side rather than
        the remote one: every chunk lands in a same-directory local temp
        (``<dst>.otto-<unique>``), and ``Path.replace()`` swings it onto
        *dst* only once every chunk has arrived. Staying in ``dst``'s own
        directory is not merely the same discipline PUT follows -- for GET
        it is load-bearing in a way PUT's staging choice is not: PUT's
        remote ``mv`` can, on some implementations, degrade to a copy
        across filesystems and still succeed; ``Path.replace()`` has
        no such fallback and raises outright
        (``OSError: [Errno 18] Invalid cross-device link``, measured
        locally) when source and destination are on different filesystems.

        Sizing runs first, via *stat_size* (``"stat"`` or ``"wc"`` --
        ``"absent"`` is refused earlier, in :meth:`_run_get`, before this
        method is ever called). The device only ENCODES: each chunk is
        pulled with ``dd if=<src> bs=<N> skip=<k> count=1 2>/dev/null |
        base64`` and decoded locally, in Python -- no decode flag is ever
        emitted remotely for GET, unlike PUT, where the device does the
        decoding. The number of chunks is ``ceil(total / N)`` -- 0 when
        *src* is empty, in which case the loop below never runs and the
        local temp is created empty (opening it for writing is enough; no
        special-cased remote command is needed the way PUT's empty-file
        branch needs one, because GET's "local file" really is local). The
        size probe and every chunk read are separate exec round trips
        against the same remote path -- *src* can grow or shrink on the
        device in between them (a TOCTOU PUT's own local ``stat()``-then-read
        shares in miniature), and this method does not detect or guard
        against it; a shrink mid-transfer surfaces as one chunk's ``dd``
        reading short, silently under-filling that chunk, not as an error.

        No wrap flag (``-w 0`` or similar) is ever passed to the remote
        ``base64``. Whether the device's ``base64`` wraps its output IS
        measured, on real BusyBox rootfs images (reproduced directly in
        this worktree against ``tests/_fixtures/busybox_rootfs``): encoding
        one :data:`_SHELL_CHUNK_BYTES` (4096-byte) chunk wraps to 72 lines
        of up to 76 columns each (5464 encoded characters) on every row that
        has a ``base64`` applet at all; 1.16.1 has none. Wrapping is not an
        edge case here -- on this matrix, for a full-size chunk, it is the
        *only* case measured. Whatever text comes back is flattened locally
        by stripping ALL whitespace before decoding: the base64 alphabet
        (``[A-Za-z0-9+/=]``) never contains whitespace, so this is lossless
        regardless of how the remote encoder chose to wrap. The flatten step
        is genuinely load-bearing, not merely defensive: decoding uses
        ``base64.b64decode(..., validate=True)``, which raises on ANY
        character outside the alphabet -- including a bare, unstripped
        newline -- rather than the default ``validate=False``, which
        silently *discards* non-alphabet bytes. ``validate=False`` would
        make a stray byte from a wedged transport (leaked stderr, a prompt
        fragment, a truncated read) decode into wrong bytes with no error at
        all; ``validate=True`` turns that into a loud
        :exc:`~otto.result.Result` failure instead, at the cost of requiring
        the flatten step to run first on ordinary wrapped output.

        Any failure -- a chunk read, decoding a chunk's text, a failed or
        mismatched integrity check, or a local write -- removes the local
        temp (best-effort; see :meth:`_cleanup_local_temp`) and returns a
        failing :class:`~otto.result.Result`. The size probe is the one
        exception: its failure (``_remote_size`` returning ``None``) returns
        before the chunk loop or the ``temp.open()`` call ever runs, so
        there is no local temp yet to remove -- see the ``total is None``
        branch below. A source path is a REMOTE path here (unlike PUT's
        *src*, which is local), so there is nothing local to race against
        before the loop starts -- only ``dst``'s directory needs to exist,
        and if it does not, the ``OSError`` from opening the local temp is
        caught the same way every other local write failure is.

        Verification (see :meth:`_verify_integrity`) runs on the local temp,
        after every chunk has been decoded and written and before
        ``Path.replace()`` -- a mismatch never reaches *dst*. On the
        ``checksum == "md5sum"`` path this is a fresh ``md5sum`` round trip;
        on the ``checksum == "absent"`` path it reuses *total*, the size
        this method already fetched to plan the chunk loop, rather than
        re-querying the device -- see :meth:`_verify_integrity` for why that
        does not weaken what the check catches. Every decoded chunk also
        updates a running :func:`hashlib.md5` locally, so an ``md5sum``
        verification costs no second local read pass over the temp.
        """
        temp = dst.parent / f"{dst.name}.otto-{uuid.uuid4().hex}"
        try:
            total = await self._remote_size(src, stat_size)
            if total is None:
                return Result(
                    Status.Error,
                    msg=f"{src}: could not determine the remote file's size (stat/wc probe "
                    "failed or returned unparseable output)",
                )
            num_chunks = (total + _SHELL_CHUNK_BYTES - 1) // _SHELL_CHUNK_BYTES
            bytes_done = 0
            # MD5 is a corruption check against `md5sum`, the tool name this
            # capability is named for and the only one BusyBox ships -- never
            # a security boundary, so the collision-resistance ruff's S324
            # warns about does not apply here.
            local_digest = hashlib.md5()  # noqa: S324
            quoted_if = shlex.quote(f"if={src}")
            with temp.open("wb") as f:
                for k in range(num_chunks):
                    result = await self._exec_cmd(
                        f"dd {quoted_if} bs={_SHELL_CHUNK_BYTES} skip={k} count=1 "
                        "2>/dev/null | base64"
                    )
                    if not result.is_ok:
                        self._cleanup_local_temp(temp)
                        return Result(
                            Status.Error,
                            msg=(
                                f"{src}: reading chunk {k} failed (exit {result.retcode}): "
                                f"{result.value or result.msg}"
                            ),
                        )
                    try:
                        decoded = base64.b64decode(
                            "".join((result.value or "").split()), validate=True
                        )
                    except ValueError as e:
                        self._cleanup_local_temp(temp)
                        return Result(
                            Status.Error, msg=f"{src}: chunk {k} was not valid base64: {e}"
                        )
                    f.write(decoded)
                    bytes_done += len(decoded)
                    local_digest.update(decoded)
                    if handler is not None:
                        handler(str(src), str(dst), bytes_done, total)
            if num_chunks == 0 and handler is not None:
                handler(str(src), str(dst), 0, 0)

            mismatch = await self._verify_integrity(
                src,
                checksum=checksum,
                stat_size=stat_size,
                local_digest=local_digest,
                local_size=bytes_done,
                known_remote_size=total,
            )
            if mismatch is not None:
                self._cleanup_local_temp(temp)
                return Result(Status.Error, msg=f"{src}: integrity check failed -- {mismatch}")

            temp.replace(dst)
        except OSError as e:
            self._cleanup_local_temp(temp)
            return Result(Status.Error, msg=f"{src}: local write failed: {e}")
        return Result(Status.Success, value=dst)

    async def _verify_integrity(
        self,
        remote_path: Path,
        *,
        checksum: str,
        stat_size: str,
        local_digest: "hashlib._Hash",
        local_size: int,
        known_remote_size: int | None = None,
    ) -> str | None:
        """Confirm *remote_path* matches what was sent (PUT) or received (GET).

        Returns ``None`` on a match, or a human-readable reason on a
        mismatch or a failed verification command -- the caller turns
        either into a failing :class:`~otto.result.Result` and removes its
        temp; this method never does either itself, so it has exactly one
        job: decide match or no match, never format the final message.

        ``checksum == "md5sum"``: a device-side ``md5sum -- <remote_path>``,
        compared against *local_digest* (already accumulated chunk-by-chunk
        by the caller, so no second local read pass). The comparison
        lower-cases both sides before comparing -- every ``md5sum`` measured
        on this matrix emits lowercase hex and so does
        :meth:`hashlib.md5.hexdigest`, so this never matters on a real
        device, but a parser that silently depended on that agreement would
        be a latent bug in waiting, not a verified property. Always a FRESH
        command, for both callers -- there is no cached digest to reuse.

        ``checksum == "absent"``: a byte-size comparison against
        *local_size*, using the exact ``stat``/``wc`` spelling
        :attr:`~otto.host.userland.Userland.stat_size` already resolved.
        *known_remote_size*, when given, is used directly instead of a fresh
        :meth:`_remote_size` round trip -- GET's caller passes the total it
        already fetched to plan the chunk loop, because the remote PATH
        (*src*) is the same one that number came from and nothing here
        changes it; PUT has no such number for its temp (nothing sized it
        before this call) and always issues a fresh query. Reusing rather
        than re-querying does not weaken what this check catches: it still
        compares the ANNOUNCED size against what actually got decoded and
        written, which is exactly the "chunk read short, no error raised"
        gap this task closes -- it only forgoes catching a size change that
        happens strictly between the initial probe and this call. That gap
        is real and is narrower than it looks next to the ``md5sum`` arm
        above: that arm issues its device command fresh on every call, with
        no cached digest ever reused, so a change to the remote file
        partway through a GET is still caught there, even on GET. Only
        THIS arm's reuse of *known_remote_size* gives up that freshness,
        and only for GET -- PUT always queries fresh regardless of which
        arm runs, since PUT has no earlier size or digest to reuse in the
        first place. Weaker than a checksum regardless (two files of the
        same size can still differ), and the caller's message says so
        rather than reporting it identically to a hash match.
        """
        quoted = shlex.quote(str(remote_path))
        if checksum == "md5sum":
            result = await self._exec_cmd(f"md5sum -- {quoted}")
            if not result.is_ok:
                return (
                    f"md5sum verification command failed (exit {result.retcode}): "
                    f"{result.value or result.msg}"
                )
            fields = (result.value or "").split()
            remote_digest = fields[0] if fields else ""
            local_hex = local_digest.hexdigest()
            if remote_digest.lower() != local_hex.lower():
                return (
                    f"md5sum mismatch: device reports {remote_digest!r}, "
                    f"local hash is {local_hex!r}"
                )
            return None
        if known_remote_size is not None:
            remote_size = known_remote_size
            # Reused, not freshly asked -- see this method's own docstring for
            # why that does not weaken the check. The message below must say
            # so: this number is what the device reported BEFORE the
            # transfer, not a report happening now, and "device reports"
            # unqualified would misstate which.
            remote_size_desc = "the size fetched before the transfer was"
        else:
            remote_size = await self._remote_size(remote_path, stat_size)
            if remote_size is None:
                return (
                    "could not verify by size (stat/wc probe failed or returned "
                    "unparseable output; this host has no md5sum, so size is the "
                    "only check available)"
                )
            remote_size_desc = "device reports"
        if remote_size != local_size:
            return (
                f"size mismatch (byte-size comparison only -- this host has no "
                f"md5sum): {remote_size_desc} {remote_size} bytes, local is "
                f"{local_size} bytes"
            )
        return None

    async def _remote_size(self, src: Path, stat_size: str) -> int | None:
        """Query *src*'s byte size the way *stat_size* says this host answers it.

        ``"stat"`` -> ``stat -c %s -- <src>``: a plain positional argument,
        so a leading-dash basename needs the ``--`` terminator (matching
        :func:`~otto.host.transfer.base.chmod_command`'s precedent -- verified
        locally that BusyBox 1.36.1's ``stat`` accepts it identically to
        GNU coreutils'). ``"wc"`` -> ``wc -c < <src>``: a shell redirection
        TARGET, which the shell itself resolves rather than passing to
        ``wc`` as an argument, so no ``--`` is needed there.

        Returns ``None``, never raises, on a non-zero exit or unparseable
        output -- the caller turns that into a failing
        :class:`~otto.result.Result` for the one file rather than an
        exception that would abort files already queued behind it.

        Two callers, and both reach this method -- GET's unconditionally,
        PUT's only when ``checksum`` resolves ``"absent"``.
        GET's own chunk-planning sizing (*src* is the remote source, before
        a single chunk has been requested) always calls this directly.
        :meth:`_verify_integrity`'s ``checksum == "absent"`` path calls this
        too, but only when its caller omits *known_remote_size* -- PUT's
        temp has no earlier size to reuse, so PUT's call always lands here;
        GET's call always supplies the *total* it already fetched for chunk
        planning, so GET never reaches this method a second time (see
        :meth:`_verify_integrity`'s docstring for why reusing rather than
        re-querying does not weaken what GET's absent-checksum check
        catches). The name predates that second caller and stays generic on
        purpose -- despite the parameter name, nothing here is GET-specific.
        """
        quoted_src = shlex.quote(str(src))
        cmd = f"stat -c %s -- {quoted_src}" if stat_size == "stat" else f"wc -c < {quoted_src}"
        result = await self._exec_cmd(cmd)
        if not result.is_ok:
            return None
        try:
            return int((result.value or "").strip())
        except ValueError:
            return None

    def _cleanup_local_temp(self, temp: Path) -> None:
        """Best-effort removal of a staged LOCAL temp after a failed size/read/decode/write step.

        The GET-side analogue of :meth:`_cleanup_temp`: fire-and-forget, for
        the same reason -- the file this belongs to has already failed, and
        there is nothing more informative to report by raising a second
        time. Local, not remote, so there is no exec round trip:
        ``Path.unlink(missing_ok=True)`` covers "never created" the way
        PUT's ``rm -f`` does, without an extra command against the device.
        """
        try:
            temp.unlink(missing_ok=True)
        except OSError as e:
            _logger.debug(f"{self._name}: cleanup of local temp {temp} failed (ignored): {e}")


register_transfer_backend("shell", ShellFileTransfer)
