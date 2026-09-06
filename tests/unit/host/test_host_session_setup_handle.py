"""The HostSession handed to a session-setup hook."""

import asyncio
import logging

import pytest

from otto.host.command_frame import ZephyrFrame
from otto.host.session import HostSession
from otto.host.session_setup import SessionSetupError
from tests.unit.host.test_session import MockSession

# The `landed` fixture (a MockSession past its bash landing handshake) lives
# in tests/unit/host/conftest.py, shared with test_session_enter_frame.py.


def _handle(session: MockSession, **kw) -> HostSession:
    kw.setdefault("deregister", lambda _n: None)
    return HostSession(
        name="__setup_default__",
        session=session,
        log_command=lambda *_: None,
        log_output=lambda *_: None,
        creds=[],
        host_id="h",
        **kw,
    )


@pytest.mark.asyncio
async def test_enter_frame_delegates_to_the_target_frame(landed):
    h = _handle(landed, target_frame=ZephyrFrame(), establishing=True)

    def answer_target_handshake() -> None:
        # Ordered on the target handshake WRITE, so the marker read here is
        # the one enter_frame just minted (feed_after_write calls `then`
        # after it) — see test_session_enter_frame.py for the pattern.
        landed.feed(f"{landed.markers.ready}: command not found\n")

    feeder = asyncio.create_task(landed.feed_after_write(then=answer_target_handshake))
    await h.enter_frame()
    await feeder
    assert isinstance(landed._frame, ZephyrFrame)


@pytest.mark.asyncio
async def test_enter_frame_forwards_timeout_to_the_handshake(landed, caplog):
    h = _handle(landed, target_frame=ZephyrFrame(), establishing=True)
    # Never answered: the handshake must time out at the *forwarded* deadline,
    # not ShellSession's class default (_INIT_TIMEOUT, 3.0s). The
    # handshake-start debug line carries the resolved deadline, which pins the
    # forward without a wall-clock assert — same pattern as
    # test_session_enter_frame.py::test_enter_frame_failure_closes_and_raises_connection_error.
    with (
        caplog.at_level(logging.DEBUG, logger="otto.host.session"),
        pytest.raises(ConnectionError, match="never became ready"),
    ):
        await h.enter_frame(timeout=0.2)
    assert any(
        "handshake start" in r.getMessage() and "timeout=0.2s" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_enter_frame_outside_a_hook_is_refused(landed):
    h = _handle(landed)
    with pytest.raises(RuntimeError, match="session_setup hook"):
        await h.enter_frame()


@pytest.mark.asyncio
async def test_close_is_refused_while_establishing(landed):
    deregistered: list[str] = []
    h = _handle(
        landed, target_frame=ZephyrFrame(), establishing=True, deregister=deregistered.append
    )
    with pytest.raises(SessionSetupError, match="close"):
        await h.close()
    assert landed.alive
    assert deregistered == []


@pytest.mark.asyncio
async def test_close_works_on_an_ordinary_handle(landed):
    closed: list[str] = []
    h = HostSession(
        name="n",
        session=landed,
        log_command=lambda *_: None,
        log_output=lambda *_: None,
        deregister=closed.append,
        creds=[],
        host_id="h",
    )
    await h.close()
    assert closed == ["n"]
    assert not landed.alive
