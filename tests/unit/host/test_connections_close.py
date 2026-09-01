"""ConnectionManager.close chain: per-step guards + tier-1 cancellation sweep.

Chain order is sftp -> ssh -> ftp -> telnet -> hop. One raising step (e.g.
``ftp.quit()`` on a dead socket) must not skip the steps behind it — in
particular the hop teardown (chaos spec: teardown chain robustness). A
CancelledError still aborts the chain loudly (force-abandon contract).
"""

import asyncio

import pytest

from otto.host.connections import ConnectionManager, teardown_step
from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, Surface, sweep_cancellation

_STEPS = ["sftp", "ssh", "ftp", "telnet", "hop"]


class _FakeTransport:
    def close(self) -> None:
        pass


class _FakeSsh:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points
        self._transport = _FakeTransport()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        await self._points.point("ssh", surface=Surface.NETWORK)


class _FakeSftp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    def exit(self) -> None:  # asyncssh SFTPClient.exit is synchronous
        self._points.sync_point("sftp", surface=Surface.NETWORK)


class _FakeFtp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def quit(self) -> None:
        await self._points.point("ftp", surface=Surface.NETWORK)


class _FakeTelnet:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("telnet", surface=Surface.NETWORK)


class _FakeHop:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("hop", surface=Surface.NETWORK)


def _manager(points: ChaosPoints) -> ConnectionManager:
    """A REAL ConnectionManager (the chain under test) over instrumented fakes."""
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], user="u", term="ssh", name="box")
    mgr._sftp_conn = _FakeSftp(points)
    mgr._ssh_conn = _FakeSsh(points)
    mgr._ftp_conn = _FakeFtp(points)
    mgr._telnet_conn = _FakeTelnet(points)
    mgr._hop = _FakeHop(points)
    return mgr


async def _scenario(points: ChaosPoints) -> None:
    await _manager(points).close()


def _oracle(points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int) -> None:
    """Mirror ``teardown_step``'s own catch: Exception is guarded, BaseException is not."""
    if issubclass(exc_type, Exception):
        # Guarded chain: the fault is logged, every later step still runs.
        assert outcome is None, (
            f"{exc_type.__name__} at step {_STEPS[k - 1]!r} escaped ConnectionManager.close"
        )
        assert points.executed == [s for i, s in enumerate(_STEPS) if i != k - 1], (
            f"steps after {_STEPS[k - 1]!r} were skipped"
        )
    else:
        # CancelledError: the chain stops loudly (force-abandon semantics).
        assert isinstance(outcome, exc_type), (
            f"cancellation at step {_STEPS[k - 1]!r} was swallowed"
        )
        assert points.executed == _STEPS[: k - 1]


@pytest.mark.asyncio
async def test_close_chain_sweep():
    report = await sweep_cancellation(_scenario, _oracle)
    assert report.points == len(_STEPS)
    # Every step here is a transport teardown, so a command-failure cannot
    # arise at any of them. Stated rather than silently skipped: this sweep
    # asserts four faults over five steps, not five.
    assert report.injected["command-failure"] == 0
    assert report.skipped["command-failure"] == len(_STEPS)
    for name in ("cancellation", "connection-dropped", "connection-reset", "timeout"):
        assert report.injected[name] == len(_STEPS), name


@pytest.mark.asyncio
async def test_close_clears_cached_slots_even_when_a_step_raises():
    """A failing close must not leave a half-dead connection cached for reuse."""
    points = ChaosPoints()
    points.arm(3, ConnectionDropped)  # ftp.quit() blows up
    mgr = _manager(points)
    await mgr.close()
    assert mgr._sftp_conn is None
    assert mgr._ssh_conn is None
    assert mgr._ftp_conn is None
    assert mgr._telnet_conn is None


@pytest.mark.asyncio
async def test_close_logs_the_failing_step(caplog):
    points = ChaosPoints()
    points.arm(3, ConnectionDropped)
    with caplog.at_level("WARNING", logger="otto.host.connections"):
        await _manager(points).close()
    assert any("ftp" in r.message and "box" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# teardown_step's own contract (public since G15 pointed every adopter at it)
# ---------------------------------------------------------------------------


def test_teardown_step_swallows_the_step_error_and_warns(caplog):
    with (
        caplog.at_level("WARNING", logger="otto.host.connections"),
        teardown_step("box", "probe-step"),
    ):
        raise ConnectionDropped("socket already gone")
    assert any("box: probe-step teardown failed" in r.message for r in caplog.records)


def test_teardown_step_lets_cancellation_through():
    """Force-abandon contract: a cancelled teardown stops loudly, not politely."""
    with pytest.raises(asyncio.CancelledError), teardown_step("box", "probe-step"):
        raise asyncio.CancelledError


# ---------------------------------------------------------------------------
# per-user connections (ssh_as/sftp_as, spec 2026-09-01 §3) drain in close()
# ---------------------------------------------------------------------------


class _FakeUserSsh:
    def __init__(self) -> None:
        self.closed = False
        self._transport = _FakeTransport()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _FakeUserSftp:
    def __init__(self) -> None:
        self.exited = False

    def exit(self) -> None:
        self.exited = True


@pytest.mark.asyncio
async def test_close_drains_per_user_connections():
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], user="u", term="ssh", name="box")
    user_ssh = _FakeUserSsh()
    user_sftp = _FakeUserSftp()
    mgr._user_ssh_conns["postgres"] = user_ssh
    mgr._user_sftp_conns["postgres"] = user_sftp

    await mgr.close()

    assert user_ssh.closed
    assert user_sftp.exited
    assert mgr._user_ssh_conns == {}
    assert mgr._user_sftp_conns == {}
