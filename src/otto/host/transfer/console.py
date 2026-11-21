"""Console file transfer backend for embedded hosts.

``otto.host.transfer.registry`` registers it as ``console``, by reference.
"""

import errno
import logging
import os
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typing_extensions import final, override

from ...result import CommandResult, Result
from ...utils import Status
from ..embedded_filesystem import EmbeddedFileSystem
from .base import (
    ProgressGranularity,
    TransferContext,
    TransferProgressFactory,
    TransferProgressHandler,
)
from .embedded_base import EmbeddedFileTransfer


def _label_errno(retcode: int) -> str:
    """Render a signed-errno retcode as ``-N (-NAME, description)``.

    Zephyr's errnos are POSIX-aligned (its ``errno.h`` is BSD-derived,
    same numeric values as Linux), so Python's stdlib :mod:`errno` and
    :func:`os.strerror` are an authoritative source for the symbolic
    name and human-readable description. Positive retcodes are rendered
    unchanged (no errno mapping applies). Unknown negative codes fall
    back to ``-N`` so the message stays readable on an unrecognized
    value rather than throwing.
    """
    if retcode >= 0:
        return str(retcode)
    code = -retcode
    name = errno.errorcode.get(code)
    if name is None:
        return str(retcode)
    return f"{retcode} (-{name}, {os.strerror(code)})"


logger = logging.getLogger(__name__)


def _failed_message(command: str, result: CommandResult) -> str:
    """Format a failed device command as ``<cmd> failed (retcode=<label>): <output>``."""
    return f"{command} failed (retcode={_label_errno(result.retcode)}): {result.value.strip()}"


@dataclass(frozen=True)
class _PassOutcome:
    """What one write pass of a console put left behind.

    ``no_space`` and ``trunc_failed`` say why a failed pass failed, read from the raw
    command results, so the caller decides the one fallback without parsing *result*'s message.
    """

    result: Result
    reported: int
    no_space: bool = False
    trunc_failed: bool = False


# Bytes per `fs write` invocation. Each byte costs three characters of hex
# ("XX ") on the command line, so the chunk size must stay within the target's
# `CONFIG_SHELL_CMD_BUFF_SIZE` and `CONFIG_SHELL_ARGC_MAX`. 32 is comfortably
# inside the values otto-overlay.conf sets (512-byte buffer, 64 args).
_WRITE_CHUNK = 32

# The errno the device shell prints at the end of a failed filesystem command's line: `(-28)`.
_ERRNO_TAIL_RE = re.compile(r"\s\((-\d+)\)\s*$")

# `fs read` emits 16 bytes per hexdump line.
_HEXDUMP_COLS = 16

# A hexdump line: an 8-hex-digit offset, then the hex/ascii columns.
_HEX_LINE_RE = re.compile(r"^\s*([0-9A-Fa-f]{8})\s\s(.*)$")

_NO_FILESYSTEM_MSG = (
    "console file transfer requires an on-device filesystem; this host's "
    "lab-data 'filesystem' is 'none' (NoFileSystem). Declare a real "
    "filesystem (e.g. 'fat-ram', 'littlefs') in lab data, or register a "
    "custom EmbeddedFileSystem subclass via register_filesystem()."
)

# Generous ceiling for `fs read` of a whole file (a large hexdump is slow to
# stream); tight ceilings for the small, bounded write/remove commands.
_READ_TIMEOUT = 60.0
_WRITE_TIMEOUT = 15.0
_RM_TIMEOUT = 10.0


@final
class ConsoleFileTransfer(EmbeddedFileTransfer):
    """File transfer for an embedded host, over the device shell only.

    Subclasses :class:`~otto.host.transfer.EmbeddedFileTransfer`, inheriting
    its ``put_files`` / ``get_files`` API (filename-length validation,
    shared Rich progress acquisition). Implements the abstract
    ``run_put`` / ``run_get`` against the device's ``fs`` shell. The
    shell command runner is injected as ``exec_cmd`` so the class is
    testable against a fake shell with no real connection.
    """

    progress_granularity = ProgressGranularity(
        put=_WRITE_CHUNK,
        get=None,
        note=(
            "get is one `fs read` command on the Zephyr shell -- the bytes arrive "
            "in a single reply and the one event arrives when it completes"
        ),
    )

    # Narrow the inherited _exec_cmd type: console transfer always requires
    # a live exec callable, never None.
    _exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]]

    def __init__(
        self,
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandResult]],
        filesystem: EmbeddedFileSystem | None = None,
        max_filename_len: int = 255,
        transfer: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            exec_cmd=exec_cmd,
            filesystem=filesystem,
            max_filename_len=max_filename_len,
            transfer=transfer,
        )

    @override
    @classmethod
    def create(cls, ctx: "TransferContext") -> "ConsoleFileTransfer":
        if ctx.exec_cmd is None:
            raise ValueError("ConsoleFileTransfer requires exec_cmd on the transfer context")
        return cls(
            name=ctx.host_name,
            exec_cmd=ctx.exec_cmd,
            filesystem=ctx.filesystem,
            max_filename_len=ctx.max_filename_len,
        )

    # ------------------------------------------------------------------
    # BaseFileTransfer hooks
    # ------------------------------------------------------------------

    @override
    async def run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        """Transfer files off the embedded target — one at a time (single console).

        ``_console_get_one`` reads the file in a single ``fs read`` command,
        so per-byte progress isn't feasible; the handler is invoked once at
        completion to satisfy the "files complete to 100%" contract. A single
        console means
        :attr:`~otto.host.transfer.BaseFileTransfer.concurrency_limit` stays
        one and ``concurrent=True`` is a no-op, but every file is attempted;
        a failure is its own entry.
        """
        if not self._filesystem.supports_transfer:
            return {src: Result(status=Status.Error, msg=_NO_FILESYSTEM_MSG) for src in src_files}
        await self._ensure_mounted()

        async def _get_one(src: Path) -> Result:
            handler = progress_factory() if progress_factory is not None else None
            return await self._console_get_one(src, dest_dir, handler)

        return await self.dispatch_per_file(src_files, transfer_one=_get_one, concurrent=concurrent)

    @override
    async def run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        *,
        progress_factory: TransferProgressFactory | None,
        concurrent: bool = True,
    ) -> dict[Path, Result]:
        """Transfer local files onto the embedded target — one at a time.

        ``_console_put_one`` writes in 32-byte chunks (``_WRITE_CHUNK``), so
        the handler is invoked after each chunk for genuine per-byte
        progress — much finer than asyncssh's 256 KB SCP block, fitting the
        slowness of console transfer. One console means ``concurrent=True``
        is a no-op here, exactly as in :meth:`run_get`; every file is
        attempted; a failure is its own entry.
        """
        if not self._filesystem.supports_transfer:
            return {src: Result(status=Status.Error, msg=_NO_FILESYSTEM_MSG) for src in src_files}
        await self._ensure_mounted()

        async def _put_one(src: Path) -> Result:
            handler = progress_factory() if progress_factory is not None else None
            return await self._console_put_one(src, dest_dir, handler)

        return await self.dispatch_per_file(src_files, transfer_one=_put_one, concurrent=concurrent)

    # ------------------------------------------------------------------
    # console backend — get
    # ------------------------------------------------------------------

    async def _console_get_one(
        self,
        src: Path,
        dest_dir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> Result:
        """Read one file off the target via ``fs read`` and write it locally.

        ``fs read`` is a single monolithic command — no chunk loop — so the
        progress handler is invoked exactly once at completion (signalling
        file done at 100%). Per-byte progress for GET would require chunked
        ``fs read <path> <offset> <length>``; deferred. Returns a
        :class:`~otto.result.Result` carrying ``value=dest`` on success.
        """
        src_path = src.as_posix()
        read_cmd = self._filesystem.read_command(src_path)
        logger.debug(f"{self._name}: {read_cmd} -> {dest_dir}")
        result = await self._exec_cmd(read_cmd, timeout=_READ_TIMEOUT)

        if not result.status.is_ok:
            return Result(
                status=Status.Error,
                msg=_failed_message(read_cmd, result),
            )

        try:
            data = self._decode_hexdump(result.value)
        except ValueError as e:
            return Result(status=Status.Error, msg=f"{read_cmd}: {e}")

        dest = dest_dir / src.name
        dest.write_bytes(data)
        if progress_handler is not None:
            progress_handler(src_path, dest.as_posix(), len(data), len(data))
        return Result(status=Status.Success, value=dest)

    # ------------------------------------------------------------------
    # console backend — put
    # ------------------------------------------------------------------

    async def _console_put_one(
        self,
        src: Path,
        dest_dir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> Result:
        """Write one local file onto the target via chunked ``fs write``.

        Emits a progress event after each successful 32-byte chunk write,
        plus a final event at file completion (``bytes_done == bytes_total``).
        Empty files emit a single ``(0, 0)`` event so the bar appears and
        immediately completes. Returns a :class:`~otto.result.Result` carrying
        ``value=<dest path>`` on success.

        The file is written and then truncated to its length. When a write reports no space or
        the truncate fails for any reason, the put falls back once to removing the destination
        and writing the whole file again with no truncate; a filesystem whose ``trunc_command``
        returns ``None`` takes that same pass from the start. A put cancelled over an existing
        file can leave the new bytes followed by the old file's tail: put again or remove the
        file.
        """
        data = src.read_bytes()
        dest = dest_dir / src.name
        dest_path = dest.as_posix()
        logger.debug(f"{self._name}: fs write {src} -> {dest_path} ({len(data)} bytes)")

        # A failing pass leaves a partial file on the device, which blocks any retry on a
        # capacity-bound filesystem (the half-written bytes still consume space), so a failed
        # put removes it best-effort; a failure there is logged, never propagated. A raised
        # Exception does the same; cancellation is left alone.
        try:
            outcome = await self._put_with_fallback(data, str(src), dest_path, progress_handler)
        except Exception:
            await self._cleanup_partial(dest_path)
            raise
        if outcome.result.is_ok:
            return Result(status=Status.Success, value=dest)
        await self._cleanup_partial(dest_path)
        return outcome.result

    async def _put_with_fallback(
        self,
        data: bytes,
        src_str: str,
        dest_path: str,
        progress_handler: TransferProgressHandler | None,
    ) -> _PassOutcome:
        """Write then truncate; on no space or a failed truncate take the one fallback pass."""
        trunc_cmd = self._filesystem.trunc_command(dest_path, length=len(data))
        if trunc_cmd is None:
            return await self._remove_then_write(
                data, src_str, dest_path, progress_handler, -1, tolerate_remove_failure=True
            )
        outcome = await self._write_pass(
            data, src_str, dest_path, progress_handler, -1, trunc_cmd=trunc_cmd
        )
        if outcome.result.is_ok or not (outcome.no_space or outcome.trunc_failed):
            return outcome
        # The old file's space is still held, which is what a copy-on-write filesystem needs
        # freed; a shell without a truncate fails it. After a remove the file is new and
        # needs no truncate. The writes replay from offset 0 but the bar must not go back.
        why = "filesystem full" if outcome.no_space else f"truncate failed ({outcome.result.msg})"
        logger.debug(
            f"{self._name}: {dest_path}: {why}, removing the destination and writing it once more"
        )
        # This put already wrote the file, so it exists and its remove must succeed.
        return await self._remove_then_write(
            data,
            src_str,
            dest_path,
            progress_handler,
            outcome.reported,
            tolerate_remove_failure=False,
        )

    async def _remove_then_write(
        self,
        data: bytes,
        src_str: str,
        dest_path: str,
        progress_handler: TransferProgressHandler | None,
        reported: int,
        *,
        tolerate_remove_failure: bool,
    ) -> _PassOutcome:
        """Remove the destination, then write the whole file with no truncate.

        The one pass shared by the fallback and by a filesystem with no truncate. A failed
        remove fails the pass unless *tolerate_remove_failure*: a put that starts here
        (no truncate) may find no file to remove, while the fallback follows this put's own
        write, so the file exists.
        """
        rm_cmd = self._filesystem.rm_command(dest_path)
        removed = await self._exec_cmd(rm_cmd, timeout=_RM_TIMEOUT)
        if not removed.status.is_ok and not tolerate_remove_failure:
            failed = Result(status=Status.Error, msg=_failed_message(rm_cmd, removed))
            return _PassOutcome(failed, reported)
        return await self._write_pass(
            data, src_str, dest_path, progress_handler, reported, trunc_cmd=None
        )

    async def _write_pass(
        self,
        data: bytes,
        src_str: str,
        dest_path: str,
        progress_handler: TransferProgressHandler | None,
        reported: int,
        *,
        trunc_cmd: str | None,
    ) -> _PassOutcome:
        """Write *data* to *dest_path* from offset 0, then run *trunc_cmd* when given.

        ``fs write`` seeks to an offset but never truncates, so a longer previous file
        would keep a stale tail; the truncate after the last write removes it without a
        remove-first, because ``fs rm`` of an absent file fails on the device and the
        device logs that failure to the console about a second later, where a close can
        land on it (#972).

        *reported* is the highest ``bytes_done`` reported so far (-1: none, so an empty
        file's one ``(0, 0)`` event still fires).
        """
        total = len(data)

        # Zephyr 3.7's `fs write` takes `<path> [-o <offset>] <byte>...`. The
        # `-o <offset>` flag is **required** — without it every positional
        # argument after the path (including any bare integer we intended as
        # the offset) is interpreted as a literal byte, so `fs write foo 0
        # 41 42` writes the four bytes 00, 41, 42, ... rather than starting
        # at offset 0 with bytes 41, 42, ... Live-verified against Zephyr
        # 3.7.2 on qemu_x86.

        # An empty file: a single zero-byte `fs write -o 0` still creates it
        # via the underlying `fs_open(... O_CREAT | O_WRITE)`.
        offsets = range(0, total, _WRITE_CHUNK) if data else range(1)
        for offset in offsets:
            chunk = data[offset : offset + _WRITE_CHUNK]
            hexbytes = " ".join(f"{b:02x}" for b in chunk)
            result = await self._exec_cmd(
                self._filesystem.write_command(dest_path, offset=offset, hexbytes=hexbytes),
                timeout=_WRITE_TIMEOUT,
            )
            check = self._check_write(result, dest_path, offset)
            if not check.is_ok:
                return _PassOutcome(check, reported, no_space=self._is_no_space(result, dest_path))
            done = offset + len(chunk)
            if progress_handler is not None and done > reported:
                progress_handler(src_str, dest_path, done, total)
            reported = max(reported, done)

        if trunc_cmd is not None:
            result = await self._exec_cmd(trunc_cmd, timeout=_WRITE_TIMEOUT)
            if not result.status.is_ok:
                failed = Result(
                    status=Status.Error,
                    msg=_failed_message(trunc_cmd, result),
                )
                return _PassOutcome(failed, reported, trunc_failed=True)
        return _PassOutcome(Result(status=Status.Success), reported)

    @staticmethod
    def _is_no_space(result: CommandResult, dest_path: str) -> bool:
        """Report whether a failed ``fs write`` says ``-ENOSPC``.

        The shell's own retcode is ``-ENOEXEC`` whenever a builtin fails, so the
        filesystem's errno is read from the output, line by line: the failure's own line
        names the destination and ends with ``(-28)``. Other log lines the device prints
        around it neither hide that line nor count, because a line without the destination
        path is not the failure's; a bare ``-28`` retcode is accepted too. The raw command
        result is read, never the formatted message, which holds the destination path.
        """
        if result.retcode == -errno.ENOSPC:
            return True
        for line in result.value.splitlines():
            tail = _ERRNO_TAIL_RE.search(line)
            if tail is not None and dest_path in line and int(tail.group(1)) == -errno.ENOSPC:
                return True
        return False

    async def _cleanup_partial(self, dest_path: str) -> None:
        """Best-effort removal of a half-written destination file after a mid-transfer failure.

        Errors are swallowed: the caller is already
        returning the real put failure, and the cleanup is purely an
        attempt to leave the device's filesystem recoverable for retry.
        """
        try:
            await self._exec_cmd(self._filesystem.rm_command(dest_path), timeout=_RM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup of partial transfer, non-fatal
            logger.debug(
                f"{self._name}: cleanup of {dest_path} failed after a transfer error: {exc!r}"
            )

    def _check_write(
        self,
        result: CommandResult,
        dest_path: str,
        offset: int,
    ) -> Result:
        """Classify an ``fs write`` result into a transfer :class:`~otto.result.Result`."""
        if not result.status.is_ok:
            return Result(
                status=Status.Error,
                msg=_failed_message(f"fs write {dest_path} at offset {offset}", result),
            )
        return Result(status=Status.Success)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_mounted(self) -> None:
        """Ensure the filesystem is mounted, probing before mounting.

        Filesystems Zephyr cannot auto-mount via ``zephyr,fstab`` (FAT, in
        3.7 LTS) need otto to issue ``fs mount fat <path>`` before any
        ``fs read/write``. An already-mounted filesystem is detected with the
        read-only ``fs statvfs`` probe FIRST — never by letting the mount
        fail: Zephyr 3.7's shell k_mallocs the mount-point string and LEAKS
        it on every failed mount (fixed upstream in 4.4 with an early -EBUSY
        guard + ``k_free``), so the old always-mount-and-accept-the-error
        approach bled the 16 KB system heap dry one otto host object at a
        time on long-lived FAT beds.
        """
        mount_cmd = self._filesystem.mount_cmd
        if mount_cmd is None or self._mount_done:
            return
        probe_cmd = self._filesystem.statvfs_command()
        if probe_cmd is not None:
            probe = await self._exec_cmd(probe_cmd, timeout=_WRITE_TIMEOUT)
            if probe.status == Status.Success:
                self._mount_done = True
                return
        # Unmounted (or no statvfs to probe with): mount for real. Whether
        # the command succeeds (first mount) or fails (already mounted, only
        # possible on the no-statvfs fallback), the post-condition is the
        # same. Errors from a missing `fs` command surface naturally on the
        # next `fs read`/`fs write`.
        await self._exec_cmd(mount_cmd, timeout=_WRITE_TIMEOUT)
        self._mount_done = True

    @staticmethod
    def _decode_hexdump(output: str) -> bytes:
        r"""Decode a Zephyr ``fs read`` hexdump back into bytes.

        Each line is ``<8-hex offset>  <space-separated hex bytes>\\t<ascii>``,
        16 bytes per line. The **offset is authoritative**: bytes are placed at
        it and the lines are reassembled in offset order, so a dropped or
        duplicated line is caught (raising :class:`ValueError`) rather than
        silently corrupting the result. The ASCII gutter — separated by a tab,
        or by a fixed-width column when no tab is present — is never read.
        """
        chunks: dict[int, bytes] = {}
        for raw in output.splitlines():
            m = _HEX_LINE_RE.match(raw.rstrip())
            if not m:
                continue
            offset = int(m.group(1), 16)
            rest = m.group(2)
            # The shell separates the hex column from the ASCII gutter with a
            # tab; fall back to the fixed hex-field width if a tab is absent.
            hex_field = rest.split("\t", 1)[0] if "\t" in rest else rest[: _HEXDUMP_COLS * 3]
            tokens = hex_field.split()
            try:
                chunks[offset] = bytes(int(t, 16) for t in tokens)
            except ValueError:
                continue

        out = bytearray()
        for offset in sorted(chunks):
            if offset != len(out):
                raise ValueError(
                    f"hexdump has a gap or overlap — expected the next line "
                    f"at offset {len(out)}, got {offset}"
                )
            out.extend(chunks[offset])
        return bytes(out)
