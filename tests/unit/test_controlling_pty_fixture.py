"""The controlling-pty spawner stops a program that is already gone without signalling the test run.

``ControllingPty.close`` signals the terminal's foreground group as well as
the program's, to reach a job a shell ran in a group of its own. Once the
session leader has exited, ``tcgetpgrp`` answers 0, and ``killpg(0)`` would
SIGKILL this process's own group: the pytest worker. Every group ``close``
signals is recorded, and only a safe one is forwarded, so a regression fails
the test instead of killing the run it is part of.
"""

import contextlib
import os
import subprocess
from collections.abc import Callable

import pytest

from otto.utils import wait_for
from tests._fixtures import controlling_pty
from tests._fixtures.controlling_pty import spawn_on_controlling_pty
from tests._fixtures.interactive_shell import interactive_shell


@pytest.fixture
def signalled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The groups ``close`` signals; one that is not safe to signal is recorded and dropped."""
    groups: list[int] = []
    forward = controlling_pty.signal_group

    def record(pgid: int, *args: object) -> None:
        groups.append(pgid)
        if pgid > 0 and pgid != os.getpgrp():
            forward(pgid, *args)

    monkeypatch.setattr(controlling_pty, "signal_group", record)
    return groups


def _all_safe(groups: list[int]) -> bool:
    return all(pgid > 0 and pgid != os.getpgrp() for pgid in groups)


def test_closing_a_pty_whose_program_exited_unreaped_spares_the_caller(signalled: list[int]):
    """The leader exited and is not reaped yet: the foreground group reads 0, and close skips it."""
    terminal = spawn_on_controlling_pty(["sh", "-c", "exit 0"], env=dict(os.environ))
    os.waitid(os.P_PID, terminal.proc.pid, os.WEXITED | os.WNOWAIT)  # exited, still a zombie
    assert terminal.foreground_group() == 0, "the hazard this test exists for is not reproduced"
    terminal.close()
    assert terminal.proc.returncode == 0
    assert _all_safe(signalled), signalled


def test_an_interactive_shell_that_exits_is_stopped_cleanly(signalled: list[int]):
    """A tail that runs ``exit`` ends the shell; leaving the context reaps it and nothing else."""
    with interactive_shell("sh") as shell:
        shell.send("exit\n")
        wait_for(
            lambda: shell.terminal.foreground_group() == 0,
            5,
            interval=0.01,
            on_timeout="the shell never exited",
        )
    assert shell.terminal.proc.returncode == 0
    assert _all_safe(signalled), signalled


def _unreapable(
    monkeypatch: pytest.MonkeyPatch,
) -> "tuple[controlling_pty.ControllingPty, Callable[..., int]]":
    """A pty whose program ``close`` cannot reap in time, and the real reap for the test's end."""
    terminal = spawn_on_controlling_pty(["sh", "-c", "exit 0"], env=dict(os.environ))
    os.waitid(os.P_PID, terminal.proc.pid, os.WEXITED | os.WNOWAIT)  # exited, so not killed
    reap = terminal.proc.wait

    def never(timeout: "float | None" = None) -> int:
        raise subprocess.TimeoutExpired(terminal.proc.args, timeout or 0)

    monkeypatch.setattr(terminal.proc, "wait", never)
    return terminal, reap


def test_a_reap_timeout_is_raised_when_nothing_else_is_failing(monkeypatch: pytest.MonkeyPatch):
    """With no exception in flight, a program ``close`` cannot reap is an error of its own."""
    terminal, reap = _unreapable(monkeypatch)
    with pytest.raises(subprocess.TimeoutExpired):
        terminal.close()
    assert reap(timeout=5) == 0


def test_a_reap_timeout_never_masks_the_tests_own_failure(monkeypatch: pytest.MonkeyPatch):
    """Closed while a failure propagates, ``close`` lets that failure through, not its timeout."""
    terminal, reap = _unreapable(monkeypatch)
    with pytest.raises(AssertionError, match="the test's own failure"):
        _fail_inside(terminal)
    assert reap(timeout=5) == 0


def _fail_inside(terminal: "controlling_pty.ControllingPty") -> None:
    """Fail the way a test does, inside a context manager whose exit closes *terminal*."""
    with contextlib.closing(terminal):
        raise AssertionError("the test's own failure")
