"""Start a program on a fresh pty that is its CONTROLLING terminal, and stop it again.

The one spawner for every test that needs a real terminal: the PTY e2e driver
(``tests/e2e/host/_pty_driver.py``) and the interactive shells of
``tests/_fixtures/interactive_shell.py``. A controlling terminal matters
twice over: the kernel delivers SIGWINCH and job-control signals only to its
foreground process group, and a job-control shell without one complains and
behaves unlike a login shell.
"""

import contextlib
import fcntl
import os
import struct
import subprocess
import sys
import termios
from dataclasses import dataclass
from pathlib import Path

from scripts.process_group import signal_group

# setsid() alone (start_new_session=True) leaves the child with NO controlling
# terminal: an inherited slave fd never acquires one. This shim runs AFTER exec
# in a fresh interpreter (a preexec_fn would run between fork and exec, where a
# lock held by any other thread deadlocks — PLW1509), makes the slave (fd 0)
# the session's controlling terminal, then execs the program in place: same
# pid, so wait() and the group kill reach the program itself. -S skips site
# processing for the shim only; the program sees the environment unchanged.
_CTTY_SHIM = (
    "import fcntl, os, sys, termios; "
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
    "os.execvp(sys.argv[1], sys.argv[1:])"
)

_REAP_TIMEOUT = 5.0
"""How long a SIGKILLed program gets to be reaped; past it something else is wrong."""


def set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the terminal size on *fd*, either side of the pty."""
    # struct winsize { unsigned short ws_row, ws_col, ws_xpixel, ws_ypixel; }
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@dataclass
class ControllingPty:
    """A program running on a pty it controls, and the controller side of that pty."""

    proc: "subprocess.Popen[bytes]"
    controller: int

    def foreground_group(self) -> int:
        """The terminal's foreground process group; 0 when there is none (no ctty, or no leader)."""
        return os.tcgetpgrp(self.controller)

    def close(self) -> None:
        """Kill the program's group and its foreground job's, reap the program, close the pty.

        A job-control shell runs a foreground job in a group of its own, so
        the program's group alone would miss it. The foreground group is
        signalled only when it is a real group other than this process's:
        ``tcgetpgrp`` answers 0 once the session leader has exited, and
        ``killpg(0)`` would signal the caller's own group, the test run.

        A program that is not reaped within :data:`_REAP_TIMEOUT` raises
        :class:`subprocess.TimeoutExpired`, but only when no exception is
        already propagating. Called from a context manager's exit (or a
        ``finally``) while the test's own failure is in flight, the timeout is
        suppressed so that failure is the one reported.
        """
        failing = sys.exc_info()[1] is not None
        try:
            with contextlib.suppress(OSError):
                foreground = self.foreground_group()
                if foreground > 0 and foreground != os.getpgrp():
                    signal_group(foreground)
            if self.proc.poll() is None:
                signal_group(self.proc.pid)  # start_new_session: pgid == pid
            try:
                self.proc.wait(timeout=_REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                if not failing:
                    raise
        finally:
            with contextlib.suppress(OSError):
                os.close(self.controller)


def spawn_on_controlling_pty(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: "Path | str | None" = None,
    onlcr: "bool | None" = None,
    cols: "int | None" = None,
    rows: "int | None" = None,
) -> ControllingPty:
    """Start *argv* on a fresh pty that becomes its controlling terminal.

    *argv[0]* is resolved on *env*'s ``PATH``. The terminal is configured
    before the program exists, so it inherits the settings the way a login
    shell inherits a console's: *onlcr* sets or clears ``onlcr`` (``None``
    leaves the default), and *cols* with *rows* set the size.
    """
    controller, terminal = os.openpty()
    try:
        if onlcr is not None:
            attrs = termios.tcgetattr(terminal)
            attrs[1] = attrs[1] | termios.ONLCR if onlcr else attrs[1] & ~termios.ONLCR
            termios.tcsetattr(terminal, termios.TCSANOW, attrs)
        if cols is not None and rows is not None:
            set_winsize(terminal, cols, rows)
        proc = subprocess.Popen(
            [sys.executable, "-S", "-c", _CTTY_SHIM, *argv],
            stdin=terminal,
            stdout=terminal,
            stderr=terminal,
            start_new_session=True,
            cwd=None if cwd is None else str(cwd),
            env=env,
            close_fds=True,
        )
    except BaseException:
        os.close(controller)
        raise
    finally:
        os.close(terminal)
    return ControllingPty(proc, controller)
