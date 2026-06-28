"""Console file transfer backend for embedded hosts.

Registers ``console`` into the shared transfer registry on import.
"""

import errno
import os
import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from typing_extensions import override

from ...logger import get_otto_logger
from ...utils import CommandStatus, Status
from ..embedded_filesystem import EmbeddedFileSystem
from .base import TransferContext, TransferProgressFactory, TransferProgressHandler
from .embedded_base import EmbeddedFileTransfer
from .registry import register_transfer_backend


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


logger = get_otto_logger()

# Bytes per `fs write` invocation. Each byte costs three characters of hex
# ("XX ") on the command line, so the chunk size must stay within the target's
# `CONFIG_SHELL_CMD_BUFF_SIZE` and `CONFIG_SHELL_ARGC_MAX`. 32 is comfortably
# inside the values otto-overlay.conf sets (512-byte buffer, 64 args).
_WRITE_CHUNK = 32

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


class ConsoleFileTransfer(EmbeddedFileTransfer):
    """File transfer for an embedded host, over the device shell only.

    Subclasses :class:`~otto.host.transfer.EmbeddedFileTransfer`, inheriting
    its ``put_files`` / ``get_files`` API (filename-length validation,
    shared Rich progress acquisition). Implements the abstract
    ``_run_put`` / ``_run_get`` against the device's ``fs`` shell. The
    shell command runner is injected as ``exec_cmd`` so the class is
    testable against a fake shell with no real connection.
    """

    # Narrow the inherited _exec_cmd type: console transfer always requires
    # a live exec callable, never None.
    _exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]]

    def __init__(
        self,
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
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
        assert ctx.exec_cmd is not None
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
    async def _run_get(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        """Transfer files off the embedded target — sequential (single console).

        ``_console_get_one`` reads the file in a single ``fs read`` command,
        so per-byte progress isn't feasible; the handler is invoked once at
        completion to satisfy the "files complete to 100%" contract.
        """
        if not self._filesystem.supports_transfer:
            return Status.Error, _NO_FILESYSTEM_MSG
        await self._ensure_mounted()
        for src in src_files:
            handler = progress_factory() if progress_factory is not None else None
            status, err = await self._console_get_one(src, dest_dir, handler)
            if not status.is_ok:
                return status, err
        return Status.Success, ""

    @override
    async def _run_put(
        self,
        src_files: list[Path],
        dest_dir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        """Transfer local files onto the embedded target — sequential.

        ``_console_put_one`` writes in 32-byte chunks (``_WRITE_CHUNK``), so
        the handler is invoked after each chunk for genuine per-byte
        progress — much finer than asyncssh's 256 KB SCP block, fitting the
        slowness of console transfer.
        """
        if not self._filesystem.supports_transfer:
            return Status.Error, _NO_FILESYSTEM_MSG
        await self._ensure_mounted()
        for src in src_files:
            handler = progress_factory() if progress_factory is not None else None
            status, err = await self._console_put_one(src, dest_dir, handler)
            if not status.is_ok:
                return status, err
        return Status.Success, ""

    # ------------------------------------------------------------------
    # console backend — get
    # ------------------------------------------------------------------

    async def _console_get_one(
        self,
        src: Path,
        dest_dir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> tuple[Status, str]:
        """Read one file off the target via ``fs read`` and write it locally.

        ``fs read`` is a single monolithic command — no chunk loop — so the
        progress handler is invoked exactly once at completion (signalling
        file done at 100%). Per-byte progress for GET would require chunked
        ``fs read <path> <offset> <length>``; deferred.
        """
        src_path = src.as_posix()
        read_cmd = self._filesystem.read_command(src_path)
        logger.debug(f"{self._name}: {read_cmd} -> {dest_dir}")
        result = await self._exec_cmd(read_cmd, timeout=_READ_TIMEOUT)

        if not result.status.is_ok:
            return Status.Error, (
                f"{read_cmd} failed (retcode={_label_errno(result.retcode)}): "
                f"{result.output.strip()}"
            )

        try:
            data = self._decode_hexdump(result.output)
        except ValueError as e:
            return Status.Error, f"{read_cmd}: {e}"

        dest = dest_dir / src.name
        dest.write_bytes(data)
        if progress_handler is not None:
            progress_handler(src_path, dest.as_posix(), len(data), len(data))
        return Status.Success, ""

    # ------------------------------------------------------------------
    # console backend — put
    # ------------------------------------------------------------------

    async def _console_put_one(
        self,
        src: Path,
        dest_dir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> tuple[Status, str]:
        """Write one local file onto the target via chunked ``fs write``.

        Emits a progress event after each successful 32-byte chunk write,
        plus a final event at file completion (``bytes_done == bytes_total``).
        Empty files emit a single ``(0, 0)`` event so the bar appears and
        immediately completes.
        """
        data = src.read_bytes()
        dest_path = (dest_dir / src.name).as_posix()
        src_str = str(src)
        total = len(data)
        logger.debug(f"{self._name}: fs write {src} -> {dest_path} ({total} bytes)")

        # `fs write` seeks to an offset but never truncates, so a shorter new
        # file would leave a stale tail behind. Remove the destination first;
        # a "not found" failure here is expected and ignored.
        await self._exec_cmd(self._filesystem.rm_command(dest_path), timeout=_RM_TIMEOUT)

        # Zephyr 3.7's `fs write` takes `<path> [-o <offset>] <byte>...`. The
        # `-o <offset>` flag is **required** — without it every positional
        # argument after the path (including any bare integer we intended as
        # the offset) is interpreted as a literal byte, so `fs write foo 0
        # 41 42` writes the four bytes 00, 41, 42, ... rather than starting
        # at offset 0 with bytes 41, 42, ... Live-verified against Zephyr
        # 3.7.2 on qemu_x86.

        # An empty file: a single zero-byte `fs write -o 0` still creates it
        # via the underlying `fs_open(... O_CREAT | O_WRITE)`.
        if not data:
            result = await self._exec_cmd(
                self._filesystem.write_command(dest_path, 0, ""),
                timeout=_WRITE_TIMEOUT,
            )
            status, err = self._check_write(result, dest_path, 0)
            if status.is_ok and progress_handler is not None:
                progress_handler(src_str, dest_path, 0, 0)
            return status, err

        for offset in range(0, total, _WRITE_CHUNK):
            chunk = data[offset : offset + _WRITE_CHUNK]
            hexbytes = " ".join(f"{b:02x}" for b in chunk)
            result = await self._exec_cmd(
                self._filesystem.write_command(dest_path, offset, hexbytes),
                timeout=_WRITE_TIMEOUT,
            )
            status, err = self._check_write(result, dest_path, offset)
            if not status.is_ok:
                # Partial file left on the device blocks any retry on a
                # capacity-bound filesystem (the half-written bytes still
                # consume space). Best-effort `fs rm` so the next attempt
                # starts from a clean slate; a failure here is logged but
                # not propagated — the caller already has the real error.
                await self._cleanup_partial(dest_path)
                return status, err
            if progress_handler is not None:
                progress_handler(src_str, dest_path, offset + len(chunk), total)
        return Status.Success, ""

    async def _cleanup_partial(self, dest_path: str) -> None:
        """Best-effort removal of a half-written destination file after a
        mid-transfer failure. Errors are swallowed: the caller is already
        returning the real put failure, and the cleanup is purely an
        attempt to leave the device's filesystem recoverable for retry.
        """
        try:
            await self._exec_cmd(self._filesystem.rm_command(dest_path), timeout=_RM_TIMEOUT)
        except Exception as exc:
            logger.debug(
                f"{self._name}: cleanup `fs rm {dest_path}` failed after a transfer error: {exc!r}"
            )

    def _check_write(
        self,
        result: CommandStatus,
        dest_path: str,
        offset: int,
    ) -> tuple[Status, str]:
        """Classify an ``fs write`` result into a transfer status."""
        if not result.status.is_ok:
            return Status.Error, (
                f"fs write {dest_path} at offset {offset} failed "
                f"(retcode={_label_errno(result.retcode)}): "
                f"{result.output.strip()}"
            )
        return Status.Success, ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_mounted(self) -> None:
        """Run the filesystem's optional ``mount_cmd`` once per host lifetime.

        Filesystems Zephyr cannot auto-mount via ``zephyr,fstab`` (FAT, in
        3.7 LTS) need otto to issue ``fs mount fat <path>`` before any
        ``fs read/write``. A re-mount on an already-mounted filesystem is
        an expected error (e.g. ``-EBUSY``) and is silently accepted —
        the goal is "the FS is mounted by the time we return", not "we
        just mounted it now".
        """
        mount_cmd = self._filesystem.mount_cmd
        if mount_cmd is None or self._mount_done:
            return
        # Whether the command succeeds (first mount) or fails (already
        # mounted), the post-condition is the same. Errors from a missing
        # `fs` command surface naturally on the next `fs read`/`fs write`.
        await self._exec_cmd(mount_cmd, timeout=_WRITE_TIMEOUT)
        self._mount_done = True

    @staticmethod
    def _decode_hexdump(output: str) -> bytes:
        """Decode a Zephyr ``fs read`` hexdump back into bytes.

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
            if "\t" in rest:
                hex_field = rest.split("\t", 1)[0]
            else:
                hex_field = rest[: _HEXDUMP_COLS * 3]
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


register_transfer_backend("console", ConsoleFileTransfer)
