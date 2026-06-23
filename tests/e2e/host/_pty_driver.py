"""PTY-backed subprocess driver for end-to-end tests of interactive otto commands.

``InteractiveOttoSession`` spawns ``otto`` as a subprocess with a fresh
pseudo-terminal wired to its stdin/stdout/stderr and set as its controlling
terminal. That gives the child process a real TTY, so code paths guarded by
``sys.stdin.isatty()`` (raw mode, SIGWINCH forwarding, the stdin worker
thread inside :mod:`otto.host.interact`) run for real under test instead of
being skipped.

The driver is intentionally generic: it knows nothing about ``host login``
specifically. Any otto subcommand that expects an interactive terminal —
including the planned ``--no-log`` toggle for ``host login`` — can reuse it.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Optional, Union

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_BOOTSTRAP = PROJECT_ROOT / "tests" / "_coverage_bootstrap"
COVERAGERC = PROJECT_ROOT / ".coveragerc"
OTTO_BIN = Path(sys.executable).parent / "otto"


def otto_subprocess_env(xdir: Path, *, sut_dirs: Optional[Path] = None) -> dict[str, str]:
    """Build the env dict for an ``otto`` subprocess under test.

    Mirrors the pattern used by :mod:`tests.unit.cov.test_coverage_e2e`:
    inherits ``PATH``/``HOME``, points ``OTTO_XDIR`` at a test-private
    directory, and prepends the coverage bootstrap to ``PYTHONPATH`` so the
    child's line execution is captured via ``coverage.process_startup()``.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "TERM": os.environ.get("TERM", "xterm"),
        "OTTO_XDIR": str(xdir),
        "COVERAGE_PROCESS_START": str(COVERAGERC),
        "PYTHONPATH": os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep),
    }
    if sut_dirs is not None:
        env["OTTO_SUT_DIRS"] = str(sut_dirs)
    return env


class InteractiveOttoSession:
    """Spawn an ``otto`` subprocess on a fresh PTY and drive its I/O.

    Example::

        with InteractiveOttoSession(
            ["-l", "veggies", "host", "carrot_seed", "login"],
            xdir=tmp_path / "xdir",
        ) as sess:
            sess.expect(b"Press Ctrl+] to disconnect", timeout=10)
            sess.sendline("echo hello")
            sess.expect(b"hello", timeout=5)
            sess.disconnect()
            assert sess.wait(timeout=10) == 0
    """

    _ESCAPE_BYTE = b"\x1d"  # Ctrl+] — matches interact._ESCAPE_BYTE

    def __init__(
        self,
        argv: list[str],
        *,
        xdir: Path,
        cols: int = 80,
        rows: int = 24,
        sut_dirs: Optional[Path] = None,
    ) -> None:
        self._argv = [str(OTTO_BIN), *argv]
        self._xdir = Path(xdir)
        self._cols = cols
        self._rows = rows
        self._sut_dirs = sut_dirs
        self._master_fd: Optional[int] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._buf = bytearray()

    def __enter__(self) -> "InteractiveOttoSession":
        self._xdir.mkdir(parents=True, exist_ok=True)

        master_fd, slave_fd = pty.openpty()
        self._set_winsize(slave_fd, self._cols, self._rows)

        env = otto_subprocess_env(self._xdir, sut_dirs=self._sut_dirs)
        self._proc = subprocess.Popen(
            self._argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            cwd=str(PROJECT_ROOT),
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Write raw bytes to the child's stdin."""
        assert self._master_fd is not None
        os.write(self._master_fd, data)

    def sendline(self, line: str) -> None:
        """Send a text line terminated with ``\\r`` (what a TTY sends on Enter)."""
        self.send(line.encode("utf-8") + b"\r")

    def disconnect(self) -> None:
        """Send the Ctrl+] escape that ends an :mod:`otto.host.interact` session."""
        self.send(self._ESCAPE_BYTE)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY. The kernel delivers SIGWINCH to the child session."""
        assert self._master_fd is not None
        self._cols = cols
        self._rows = rows
        self._set_winsize(self._master_fd, cols, rows)

    def expect(
        self,
        pattern: Union[bytes, re.Pattern[bytes]],
        *,
        timeout: float,
    ) -> bytes:
        """Read from the PTY until ``pattern`` appears in the accumulated buffer.

        Returns everything read up to and including the first match. Raises
        :class:`TimeoutError` with the full accumulated buffer in the message
        if ``timeout`` elapses first — helpful for diagnosing flaky e2e runs.
        """
        assert self._master_fd is not None
        deadline = time.monotonic() + timeout
        compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(re.escape(pattern))

        while True:
            match = compiled.search(self._buf)
            if match:
                consumed = bytes(self._buf[: match.end()])
                del self._buf[: match.end()]
                return consumed

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for {compiled.pattern!r} after {timeout}s.\n"
                    f"Buffer so far ({len(self._buf)} bytes):\n"
                    f"{bytes(self._buf)!r}"
                )

            try:
                readable, _, _ = select.select([self._master_fd], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise TimeoutError(f"select() failed while waiting for {compiled.pattern!r}: {exc}")
            if not readable:
                continue
            try:
                chunk = os.read(self._master_fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                # EOF: give any buffered bytes one last chance to match.
                match = compiled.search(self._buf)
                if match:
                    consumed = bytes(self._buf[: match.end()])
                    del self._buf[: match.end()]
                    return consumed
                raise TimeoutError(
                    f"Child closed the PTY before {compiled.pattern!r} appeared.\n"
                    f"Buffer so far ({len(self._buf)} bytes):\n"
                    f"{bytes(self._buf)!r}"
                )
            self._buf.extend(chunk)

    def drain(self, duration: float = 0.2) -> bytes:
        """Read everything available within ``duration`` seconds. For diagnostics."""
        assert self._master_fd is not None
        deadline = time.monotonic() + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([self._master_fd], [], [], remaining)
            if not readable:
                break
            try:
                chunk = os.read(self._master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._buf.extend(chunk)
        out = bytes(self._buf)
        self._buf.clear()
        return out

    def wait(self, timeout: float) -> int:
        """Wait for the child to exit and return its exit code."""
        assert self._proc is not None
        return self._proc.wait(timeout=timeout)

    @property
    def pid(self) -> int:
        assert self._proc is not None
        return self._proc.pid

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _set_winsize(fd: int, cols: int, rows: int) -> None:
        # struct winsize { unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel; }
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
