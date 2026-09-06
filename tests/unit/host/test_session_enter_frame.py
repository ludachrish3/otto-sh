"""ShellSession.enter_frame: a second handshake on the SAME transport.

Uses MockSession from test_session.py: its ``_open`` replaces the output
reader, so a reply fed BEFORE ``enter_frame`` is lost if frame entry reopens
the transport — the property the first test pins.
"""

import asyncio
import logging
import re

import pytest

from otto.host.app_shell import AppShellActiveError
from otto.host.command_frame import BashFrame, RawFrame, ZephyrFrame
from otto.host.errors import RawLandingError
from otto.host.session_setup import SessionSetupError
from tests.unit.host.test_session import MockSession

# The `landed` fixture (a MockSession past its bash landing handshake) lives
# in tests/unit/host/conftest.py, shared with test_host_session_setup_handle.py.

# An interactive shell's prompt. It is printed as soon as the previous command
# finishes, so with echo off it is already pending when the next command's
# output arrives and leads that output's line.
_PS1 = "vagrant@fake:~$ "


class _CountingSession(MockSession):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.opens = 0

    async def _open(self) -> None:
        self.opens += 1
        await super()._open()


@pytest.mark.asyncio
async def test_enter_frame_never_reopens_the_transport():
    s = _CountingSession()
    await s._open()  # the fixture shape: explicit open, then handshake
    feed = asyncio.create_task(s.feed_after_write(s._ready_marker + "\n"))
    await s._ensure_initialized()
    await feed
    opens_after_landing = s.opens

    def answer_target_handshake() -> None:
        # Ordered on the Zephyr handshake WRITE, so the marker read here is the
        # one enter_frame just minted (feed_after_write calls `then` after it).
        s.feed(f"{s.markers.ready}: command not found\n")

    feeder = asyncio.create_task(s.feed_after_write(then=answer_target_handshake))
    await s.enter_frame(ZephyrFrame())
    await feeder
    assert s.opens == opens_after_landing
    assert isinstance(s._frame, ZephyrFrame)
    assert s.alive


@pytest.mark.asyncio
async def test_enter_frame_mints_fresh_markers_and_ignores_a_stale_landing_ready(landed):
    old = landed.markers
    # A stale READY for the LANDING markers is still in the stream (the resend
    # loop can leave one unread). It must not confirm frame entry.
    landed.feed(old.ready + "\n")

    def answer_with_new_marker() -> None:
        landed.feed(f"{landed.markers.ready}: command not found\n")

    feeder = asyncio.create_task(landed.feed_after_write(then=answer_with_new_marker))
    await landed.enter_frame(ZephyrFrame())
    await feeder
    assert landed.markers.ready != old.ready
    assert landed.written[-1] == landed.markers.ready + "\n"


@pytest.mark.asyncio
async def test_enter_frame_twice_sends_two_handshakes(landed):
    seen: list[str] = []

    def answer_one() -> None:
        seen.append(landed.written[-1])
        landed.feed(f"{landed.markers.ready}: command not found\n")

    first = asyncio.create_task(landed.feed_after_write(then=answer_one))
    await landed.enter_frame(ZephyrFrame())
    await first
    second = asyncio.create_task(landed.feed_after_write(then=answer_one))
    await landed.enter_frame(ZephyrFrame())
    await second
    assert len(seen) == 2
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_enter_frame_failure_closes_and_raises_connection_error(landed, caplog):
    # The PER-CALL ceiling: _init_timeout is left at its class default, so a
    # `timeout=` that never reached confirm_live would run the handshake
    # against that default instead. The handshake-start line carries the
    # resolved deadline, which pins the wiring without a wall-clock assert.
    with (
        caplog.at_level(logging.DEBUG, logger="otto.host.session"),
        pytest.raises(ConnectionError, match="never became ready"),
    ):
        await landed.enter_frame(ZephyrFrame(), timeout=0.2)
    assert not landed.alive
    assert any(
        "handshake start" in r.getMessage() and "timeout=0.2s" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_enter_frame_refuses_inside_an_attached_app_shell(landed):
    old = landed.markers
    landed._app_shell = object()  # the lock an AppShell holds while attached
    with pytest.raises(AppShellActiveError):
        await landed.enter_frame(ZephyrFrame())
    # The refusal precedes every state change: same markers, nothing typed.
    assert landed.markers is old
    assert landed.written == []


@pytest.mark.asyncio
async def test_enter_frame_refuses_on_a_dead_session(landed):
    old = landed.markers
    landed._alive = False
    with pytest.raises(SessionSetupError, match="dead"):
        await landed.enter_frame(ZephyrFrame())
    # The refusal precedes every state change: same markers, nothing typed.
    assert landed.markers is old
    assert landed.written == []


@pytest.mark.asyncio
async def test_enter_frame_clears_pending_recovery(landed):
    landed._needs_recovery = True

    def answer() -> None:
        # The shape a real interactive shell answers a SECOND bash handshake
        # with: echo is off by now, so the prompt left over from the previous
        # command leads the line and only the payload's own bare `echo` puts
        # the marker at the start of one.
        landed.feed(_PS1 + "\r\n" + landed.markers.ready + "\r\n")

    feeder = asyncio.create_task(landed.feed_after_write(then=answer))
    await landed.enter_frame(BashFrame())
    await feeder
    assert landed._needs_recovery is False


@pytest.mark.asyncio
async def test_enter_frame_never_confirms_a_marker_the_prompt_runs_into(landed):
    """WHY bash's handshake prints a blank line before the marker.

    Drop the bare `echo` from `BashFrame.handshake` and this is what an
    interactive shell sends back once `stty -echo` is in effect: the pending
    prompt and the marker on ONE line. The readiness matcher is line-anchored
    on purpose, so it cannot confirm that — the shell is alive and answering,
    and frame entry fails anyway. A live bed found it; every double before
    this one landed on a shell with no prompt to print.
    """

    def answer_without_the_blank_line() -> None:
        landed.feed(_PS1 + landed.markers.ready + "\r\n")

    feeder = asyncio.create_task(landed.feed_after_write(then=answer_without_the_blank_line))
    with pytest.raises(ConnectionError, match="never became ready"):
        await landed.enter_frame(BashFrame(), timeout=0.4)
    await feeder
    assert not landed.alive


@pytest.mark.asyncio
async def test_raw_landing_writes_nothing_and_is_ready():
    s = MockSession(command_frame=RawFrame())
    await s._open()
    await s._ensure_initialized()
    assert s.written == []
    assert s.alive
    assert s._initialized


@pytest.mark.asyncio
async def test_run_cmd_in_a_raw_landing_refuses_and_keeps_the_session_alive():
    s = MockSession(command_frame=RawFrame())
    await s._open()
    await s._ensure_initialized()
    with pytest.raises(RawLandingError, match="enter_frame"):
        await s.run_cmd("ls")
    assert s.alive
    assert s.written == []


@pytest.mark.asyncio
async def test_run_cmd_in_a_raw_landing_refuses_before_ensure_ready():
    s = MockSession(command_frame=RawFrame())
    await s._open()  # transport up, NOT initialized
    s._needs_recovery = True
    with pytest.raises(RawLandingError, match="enter_frame"):
        await s.run_cmd("ls")
    # Both pin the guard's POSITION (before _ensure_ready, not after): an
    # _ensure_ready that ran would have marked the session initialized and
    # written the recovery Ctrl+C. A raw handshake writes nothing on its own,
    # so the pending recovery is what makes `written` discriminating here.
    assert not s._initialized
    assert s.written == []


@pytest.mark.asyncio
async def test_raw_landing_send_and_expect_are_raw():
    s = MockSession(command_frame=RawFrame())
    await s._open()
    await s._ensure_initialized()
    s.feed("Press 1 for shell\n")
    await s.send("1\n")
    got = await s.expect(re.compile(r"shell"), timeout=1.0)
    assert "Press 1 for shell" in got
    assert s.written == ["1\n"]


@pytest.mark.asyncio
async def test_recovery_in_a_raw_landing_writes_nothing():
    """Not even the interrupt: otto sends no byte it cannot mean into a raw console.

    A raw landing is a console otto has no dialect for, so ``0x03`` confirms
    nothing (there is no probe to render) and its meaning is the console's own
    — on a boot loader it interrupts autoboot and leaves the NEXT open at a
    prompt instead of the countdown the hook was written for. The guard
    therefore belongs at the ENTRY of recovery, not only at the confirm.
    """
    s = MockSession(command_frame=RawFrame())
    await s._open()
    await s._ensure_initialized()
    s._needs_recovery = True
    with pytest.raises(RuntimeError, match="not alive"):
        await s.send("x\n")
    assert s.written == []
    assert not s.alive


@pytest.mark.asyncio
async def test_confirm_recovered_in_a_raw_landing_marks_the_session_dead_and_writes_nothing():
    """The confirm-side guard, which an AppShell's graceful exit reaches directly.

    ``AppShell._exit`` calls ``_confirm_recovered`` without an interrupt, so
    the entry guard in ``_recover_session`` never sees that path; this one has
    to give the same verdict on its own — a raw frame cannot render a probe,
    so the session is marked dead, not crashed, and nothing is written.
    """
    s = MockSession(command_frame=RawFrame())
    await s._open()
    await s._ensure_initialized()
    assert await s._confirm_recovered() == ""
    assert not s.alive
    assert s.written == []
