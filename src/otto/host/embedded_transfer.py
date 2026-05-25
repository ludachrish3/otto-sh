"""
Console file transfer for embedded hosts.

:class:`~otto.host.unixHost.UnixHost`'s :class:`~otto.host.transfer.FileTransfer`
dispatches ``scp``/``sftp``/``ftp``/``netcat`` — none of which exist on a
bare-metal/RTOS target. :class:`EmbeddedFileTransfer` is the embedded analogue:
it speaks only the device's own shell, and an :class:`~otto.host.embeddedHost.
EmbeddedHost` delegates ``get``/``put`` to it exactly as ``UnixHost`` delegates
to ``FileTransfer``.

Backends
--------
The backend is a *typed, declared-per-host* choice (the host's ``transfer``
field), never a fixed mechanism: embedded systems share no universal file
transfer protocol, so otto standardizes on a pluggable backend instead. Adding
a future backend (YMODEM, MCUmgr, a vendor mechanism) is then additive.

- ``console`` (default) — the Zephyr ``fs`` shell. ``get`` runs ``fs read`` and
  decodes the hexdump; ``put`` runs chunked ``fs write`` calls. Requires the
  target firmware to enable ``CONFIG_FILE_SYSTEM_SHELL`` over a mounted
  filesystem. If it does not, ``get``/``put`` fail with a clear error
  (:data:`_FS_ABSENT_MSG`) rather than hanging or writing garbage.
- ``tftp`` — reserved (see :class:`~otto.host.options.TftpOptions`); the body
  raises :class:`NotImplementedError` (deferred).

Why console transfer is slow
----------------------------
Every byte crosses the shell as ~3 characters of hex text (``fs write``) or is
read back inside a hexdump (``fs read``), one bounded command at a time. That
is fine for test artifacts and configuration files — kilobytes — but it is not
a path for firmware images; use TFTP (once implemented) for bulk data.

``show_progress`` is accepted for signature parity with ``FileTransfer`` but
Phase 5 does not render a progress bar — a known, documented gap.
"""

import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .transfer import (
    BaseFileTransfer,
    TransferProgressFactory,
    TransferProgressHandler,
)

logger = getOttoLogger()

EmbeddedTransferType = Literal['console', 'tftp']
"""File-transfer backend for an embedded host. ``console`` uses the device
shell's ``fs`` commands; ``tftp`` is reserved and not yet implemented."""

# Bytes per `fs write` invocation. Each byte costs three characters of hex
# ("XX ") on the command line, so the chunk size must stay within the target's
# `CONFIG_SHELL_CMD_BUFF_SIZE` and `CONFIG_SHELL_ARGC_MAX`. 32 is comfortably
# inside the values otto-overlay.conf sets (512-byte buffer, 64 args).
_WRITE_CHUNK = 32

# `fs read` emits 16 bytes per hexdump line.
_HEXDUMP_COLS = 16

# A hexdump line: an 8-hex-digit offset, then the hex/ascii columns.
_HEX_LINE_RE = re.compile(r'^\s*([0-9A-Fa-f]{8})\s\s(.*)$')

# The shell rejects an absent `fs` command as `fs: command not found` (the
# ZephyrSession unknown-command behavior). Distinguishes "no fs shell" from an
# ordinary file-level failure (a bad path, a full disk).
_FS_ABSENT_RE = re.compile(r'\bfs:\s*command not found', re.IGNORECASE)

_FS_ABSENT_MSG = (
    "console file transfer requires the Zephyr fs shell "
    "(CONFIG_FILE_SYSTEM_SHELL) over a mounted filesystem; the target's "
    "shell has no 'fs' command"
)

# Generous ceiling for `fs read` of a whole file (a large hexdump is slow to
# stream); tight ceilings for the small, bounded write/remove commands.
_READ_TIMEOUT = 60.0
_WRITE_TIMEOUT = 15.0
_RM_TIMEOUT = 10.0


class EmbeddedFileTransfer(BaseFileTransfer):
    """File transfer for an embedded host, over the device shell only.

    Subclasses :class:`~otto.host.transfer.BaseFileTransfer`, inheriting
    its ``put_files`` / ``get_files`` API (filename-length validation,
    shared Rich progress acquisition). Implements the abstract
    ``_run_put`` / ``_run_get`` against the device's ``fs`` shell. The
    shell command runner is injected as ``exec_cmd`` so the class is
    testable against a fake shell with no real connection.
    """

    def __init__(
        self,
        transfer: EmbeddedTransferType,
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
        mount_cmd: str | None = None,
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(name=name, max_filename_len=max_filename_len)
        self.transfer = transfer
        self._exec_cmd = exec_cmd
        # `mount_cmd`: an optional ``fs mount …`` command to run once before
        # the first transfer. Needed for filesystems Zephyr cannot auto-mount
        # via ``zephyr,fstab`` (notably FAT — fstab only supports LittleFS in
        # 3.7 LTS). ``None`` for auto-mounting or no-FS targets.
        # ``_mount_done`` makes the call idempotent within a host's lifetime;
        # a "already mounted" error from a real ``fs mount`` is silently
        # accepted by ``_ensure_mounted``.
        self._mount_cmd = mount_cmd
        self._mount_done = False

    # ------------------------------------------------------------------
    # BaseFileTransfer hooks
    # ------------------------------------------------------------------

    async def _run_get(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        """Transfer files off the embedded target — sequential (single console).

        ``_console_get_one`` reads the file in a single ``fs read`` command,
        so per-byte progress isn't feasible; the handler is invoked once at
        completion to satisfy the "files complete to 100%" contract."""
        if self.transfer == 'tftp':
            raise NotImplementedError(
                "TFTP transfer for embedded hosts is not yet implemented"
            ) from None
        await self._ensure_mounted()
        for src in srcFiles:
            handler = progress_factory() if progress_factory is not None else None
            status, err = await self._console_get_one(src, destDir, handler)
            if not status.is_ok:
                return status, err
        return Status.Success, ''

    async def _run_put(
        self,
        srcFiles: list[Path],
        destDir: Path,
        progress_factory: TransferProgressFactory | None,
    ) -> tuple[Status, str]:
        """Transfer local files onto the embedded target — sequential.

        ``_console_put_one`` writes in 32-byte chunks (``_WRITE_CHUNK``), so
        the handler is invoked after each chunk for genuine per-byte
        progress — much finer than asyncssh's 256 KB SCP block, fitting the
        slowness of console transfer."""
        if self.transfer == 'tftp':
            raise NotImplementedError(
                "TFTP transfer for embedded hosts is not yet implemented"
            ) from None
        await self._ensure_mounted()
        for src in srcFiles:
            handler = progress_factory() if progress_factory is not None else None
            status, err = await self._console_put_one(src, destDir, handler)
            if not status.is_ok:
                return status, err
        return Status.Success, ''

# ------------------------------------------------------------------
    # console backend — get
    # ------------------------------------------------------------------

    async def _console_get_one(
        self, src: Path, destDir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> tuple[Status, str]:
        """Read one file off the target via ``fs read`` and write it locally.

        ``fs read`` is a single monolithic command — no chunk loop — so the
        progress handler is invoked exactly once at completion (signalling
        file done at 100%). Per-byte progress for GET would require chunked
        ``fs read <path> <offset> <length>``; deferred."""
        src_path = src.as_posix()
        logger.debug(f"{self._name}: fs read {src_path} -> {destDir}")
        result = await self._exec_cmd(f'fs read {src_path}', timeout=_READ_TIMEOUT)

        if self._fs_unavailable(result):
            return Status.Error, _FS_ABSENT_MSG
        if not result.status.is_ok:
            return Status.Error, (
                f"fs read {src_path} failed (retcode={result.retcode}): "
                f"{result.output.strip()}"
            )

        try:
            data = self._decode_hexdump(result.output)
        except ValueError as e:
            return Status.Error, f"fs read {src_path}: {e}"

        dest = destDir / src.name
        dest.write_bytes(data)
        if progress_handler is not None:
            progress_handler(src_path, dest.as_posix(), len(data), len(data))
        return Status.Success, ''

    # ------------------------------------------------------------------
    # console backend — put
    # ------------------------------------------------------------------

    async def _console_put_one(
        self, src: Path, destDir: Path,
        progress_handler: TransferProgressHandler | None = None,
    ) -> tuple[Status, str]:
        """Write one local file onto the target via chunked ``fs write``.

        Emits a progress event after each successful 32-byte chunk write,
        plus a final event at file completion (``bytes_done == bytes_total``).
        Empty files emit a single ``(0, 0)`` event so the bar appears and
        immediately completes."""
        data = src.read_bytes()
        dest_path = (destDir / src.name).as_posix()
        src_str = str(src)
        total = len(data)
        logger.debug(
            f"{self._name}: fs write {src} -> {dest_path} ({total} bytes)"
        )

        # `fs write` seeks to an offset but never truncates, so a shorter new
        # file would leave a stale tail behind. Remove the destination first;
        # a "not found" failure here is expected and ignored.
        await self._exec_cmd(f'fs rm {dest_path}', timeout=_RM_TIMEOUT)

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
                f'fs write {dest_path} -o 0', timeout=_WRITE_TIMEOUT,
            )
            status, err = self._check_write(result, dest_path, 0)
            if status.is_ok and progress_handler is not None:
                progress_handler(src_str, dest_path, 0, 0)
            return status, err

        for offset in range(0, total, _WRITE_CHUNK):
            chunk = data[offset:offset + _WRITE_CHUNK]
            hexbytes = ' '.join(f'{b:02x}' for b in chunk)
            result = await self._exec_cmd(
                f'fs write {dest_path} -o {offset} {hexbytes}',
                timeout=_WRITE_TIMEOUT,
            )
            status, err = self._check_write(result, dest_path, offset)
            if not status.is_ok:
                return status, err
            if progress_handler is not None:
                progress_handler(src_str, dest_path, offset + len(chunk), total)
        return Status.Success, ''

    def _check_write(
        self, result: CommandStatus, dest_path: str, offset: int,
    ) -> tuple[Status, str]:
        """Classify an ``fs write`` result into a transfer status."""
        if self._fs_unavailable(result):
            return Status.Error, _FS_ABSENT_MSG
        if not result.status.is_ok:
            return Status.Error, (
                f"fs write {dest_path} at offset {offset} failed "
                f"(retcode={result.retcode}): {result.output.strip()}"
            )
        return Status.Success, ''

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_mounted(self) -> None:
        """Run the optional ``mount_cmd`` once per host lifetime.

        Filesystems Zephyr cannot auto-mount via ``zephyr,fstab`` (FAT, in
        3.7 LTS) need otto to issue ``fs mount fat <path>`` before any
        ``fs read/write``. A re-mount on an already-mounted filesystem is
        an expected error (e.g. ``-EBUSY``) and is silently accepted —
        the goal is "the FS is mounted by the time we return", not "we
        just mounted it now".
        """
        if self._mount_cmd is None or self._mount_done:
            return
        # Whether the command succeeds (first mount) or fails (already
        # mounted), the post-condition is the same. Errors from a missing
        # `fs` command surface naturally on the next `fs read`/`fs write`.
        await self._exec_cmd(self._mount_cmd, timeout=_WRITE_TIMEOUT)
        self._mount_done = True

    @staticmethod
    def _fs_unavailable(result: CommandStatus) -> bool:
        """True when the target's shell has no ``fs`` command at all.

        Keyed on the shell's unknown-command echo, so it is distinct from an
        ordinary file-level failure (a missing path, a full filesystem).
        """
        return bool(_FS_ABSENT_RE.search(result.output))

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
            if '\t' in rest:
                hex_field = rest.split('\t', 1)[0]
            else:
                hex_field = rest[:_HEXDUMP_COLS * 3]
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
