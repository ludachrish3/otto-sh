"""PTY-backed subprocess driver for end-to-end tests of interactive otto commands.

``InteractiveOttoSession`` spawns ``otto`` as a subprocess with a fresh
pseudo-terminal wired to its stdin/stdout/stderr and set as its controlling
terminal (via a post-exec ``TIOCSCTTY`` shim — see ``__enter__``; without a
controlling terminal the kernel delivers resize SIGWINCH to nobody). That
gives the child process a real TTY, so code paths guarded by
``sys.stdin.isatty()`` (raw mode, SIGWINCH forwarding, the stdin worker
thread inside :mod:`otto.host.interact`) run for real under test instead of
being skipped.

The driver is intentionally generic: it knows nothing about ``host login``
specifically. Any otto subcommand that expects an interactive terminal —
including the planned ``--no-log`` toggle for ``host login`` — can reuse it.
"""

from __future__ import annotations

import contextlib
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

from typing_extensions import Self

from tests.e2e._otto_subprocess import OTTO_BIN, PROJECT_ROOT, otto_subprocess_env


class InteractiveOttoSession:
    """Spawn an ``otto`` subprocess on a fresh PTY and drive its I/O.

    Example::

        with InteractiveOttoSession(
            ["-l", "unix", "host", "test1", "login"],
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
        sut_dirs: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._argv = [str(OTTO_BIN), *argv]
        self._xdir = Path(xdir)
        self._cols = cols
        self._rows = rows
        self._sut_dirs = sut_dirs
        self._extra_env = extra_env
        self._master_fd: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = bytearray()

    def __enter__(self) -> Self:
        # The shim below makes exec failures surface as a traceback on the
        # PTY (Popen itself always succeeds — python exists), which reads as
        # an expect() timeout three layers from the cause. Name the one
        # recurring shape up front instead.
        if not os.access(OTTO_BIN, os.X_OK):
            raise RuntimeError(
                f"otto binary missing or not executable at {OTTO_BIN} — "
                "run `uv sync` in this worktree"
            )
        self._xdir.mkdir(parents=True, exist_ok=True)

        master_fd, slave_fd = pty.openpty()
        self._set_winsize(slave_fd, self._cols, self._rows)

        # ``sut_dirs=None`` omits ``OTTO_SUT_DIRS`` (this driver has no default
        # SUT repo); TERM is inherited so the child sees the same terminal type
        # the suite runs under (the root conftest pins it to ``dumb``).
        env = otto_subprocess_env(
            xdir=self._xdir,
            sut_dirs=self._sut_dirs,
            term=os.environ.get("TERM", "xterm"),
            extra_env=self._extra_env,
        )
        # setsid() alone (start_new_session=True) leaves the child with NO
        # controlling terminal — an inherited slave fd never acquires one —
        # and the kernel delivers resize SIGWINCH only to the controlling
        # terminal's foreground process group, i.e. to nobody. This shim
        # runs AFTER exec in the fresh child interpreter (a preexec_fn
        # would run between fork and exec, where a lock held by any other
        # thread deadlocks — PLW1509), makes the slave (fd 0) the session's
        # controlling terminal, then execs otto in place — same pid, so
        # wait()/killpg semantics are unchanged and resize() is actually
        # seen by otto's install_sigwinch handler.
        ctty_shim = (
            "import fcntl, os, sys, termios; "
            "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            "os.execvp(sys.argv[1], sys.argv[1:])"
        )
        # -S skips site processing for the SHIM only (its coverage bootstrap
        # would be discarded by the exec anyway); otto's own interpreter runs
        # site normally and still sees the env.
        self._proc = subprocess.Popen(
            [sys.executable, "-S", "-c", ctty_shim, *self._argv],
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
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
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

    @property
    def foreground_pgid(self) -> int:
        """The slave tty's foreground process group — 0 when the child has NO ctty.

        The load-bearing precondition for :meth:`resize`: the kernel delivers
        SIGWINCH only to the controlling terminal's foreground process group,
        so a child that never acquired this PTY as its controlling terminal
        (the pre-shim driver's permanent state) receives nothing, ever.
        """
        assert self._master_fd is not None
        return os.tcgetpgrp(self._master_fd)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY. The kernel delivers SIGWINCH to the child session."""
        assert self._master_fd is not None
        self._cols = cols
        self._rows = rows
        self._set_winsize(self._master_fd, cols, rows)

    def expect(
        self,
        pattern: bytes | re.Pattern[bytes],
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
                raise TimeoutError(
                    f"select() failed while waiting for {compiled.pattern!r}: {exc}"
                ) from exc
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
