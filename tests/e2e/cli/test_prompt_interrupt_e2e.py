"""Ctrl+C at an interactive prompt interrupts the command there (#1003).

Each test drives the real ``otto`` binary on a controlling pseudo-terminal and
types a real ``^C`` byte, so the terminal's line discipline turns it into
SIGINT for the foreground process group, exactly as a person's keypress does.
The prompt must not survive it: the command exits 130 promptly, before doing
any of the work its answers would have authorised.

Before #1003, the SIGINT handler of an ``async def`` command was a loop
callback (``loop.add_signal_handler``), and a prompt blocks the loop's only
thread in ``input()``. The callback therefore ran only after the person
answered every prompt, by which time ``otto init`` had scaffolded the project.
"""

from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import CHECKOUT_ROOT
from tests.e2e.host._pty_driver import InteractiveOttoSession

pytestmark = pytest.mark.hostless

_PROJECT1 = CHECKOUT_ROOT / "tests" / "project1"
"""The sample project that declares the ``unix`` lab."""

_CTRL_C = b"\x03"
_PROMPT_TIMEOUT = 30.0
"""Generous: the first prompt waits on otto's start-up under a loaded runner."""
_EXIT_TIMEOUT = 15.0
"""The interrupt must end the command without any further input. Bounded well
above the 10 s teardown deadline so a run that took the force path still
reports its exit code rather than a hang."""


def _interrupt_at(sess: InteractiveOttoSession, prompt: bytes) -> int:
    """Wait for *prompt*, type Ctrl+C, and return the exit code (no other input)."""
    sess.expect(prompt, timeout=_PROMPT_TIMEOUT)
    sess.send(_CTRL_C)
    return sess.wait(timeout=_EXIT_TIMEOUT)


def test_ctrl_c_at_inits_first_prompt_exits_130_and_writes_nothing(tmp_path: Path) -> None:
    """The first question (the product name) is interrupted on the spot."""
    project = tmp_path / "widget"
    project.mkdir()
    with InteractiveOttoSession(["init", "--path", str(project)], xdir=tmp_path / "xdir") as sess:
        code = _interrupt_at(sess, b"Product name")
    assert code == 130
    assert list(project.iterdir()) == []


def test_ctrl_c_at_an_area_confirm_exits_130_and_writes_nothing(tmp_path: Path) -> None:
    """A later question, after earlier answers were given, still writes nothing.

    ``init`` collects every answer before it writes, so an interrupt at any
    question leaves the directory as it was.
    """
    project = tmp_path / "widget"
    project.mkdir()
    with InteractiveOttoSession(
        ["init", "--name", "widget", "--path", str(project)], xdir=tmp_path / "xdir"
    ) as sess:
        sess.expect(b"Version", timeout=_PROMPT_TIMEOUT)
        sess.sendline("")
        code = _interrupt_at(sess, b"area?")
    assert code == 130
    assert list(project.iterdir()) == []


def test_ctrl_c_at_tunnel_remove_all_confirm_exits_130_without_reaping(tmp_path: Path) -> None:
    """``tunnel remove --all`` stops at its question; nothing is reaped.

    The reap would contact the lab's hosts, and none is running here, so the
    proof that the interrupt landed AT the prompt is the exit code with no
    answer typed: before #1003 the command waited for one.
    """
    with InteractiveOttoSession(
        ["--lab", "unix", "tunnel", "remove", "--all"],
        xdir=tmp_path / "xdir",
        roots=_PROJECT1,
    ) as sess:
        code = _interrupt_at(sess, b"Reap ALL otto tunnels?")
    assert code == 130
