"""ConnectionManager.close chain: per-step guards + tier-1 cancellation sweep.

Chain order is sftp -> ssh -> ftp -> telnet -> hop. One raising step (e.g.
``ftp.quit()`` on a dead socket) must not skip the steps behind it — in
particular the hop teardown (chaos spec: teardown chain robustness). A
CancelledError still aborts the chain loudly (force-abandon contract).
"""

import asyncio

import pytest

from otto.host.connections import ConnectionManager
from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, sweep_cancellation

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
        await self._points.point("ssh")


class _FakeSftp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    def exit(self) -> None:  # asyncssh SFTPClient.exit is synchronous
        self._points.sync_point("sftp")


class _FakeFtp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def quit(self) -> None:
        await self._points.point("ftp")


class _FakeTelnet:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("telnet")


class _FakeHop:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("hop")


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
    if exc_type is ConnectionDropped:
        # Guarded chain: the drop is logged, every later step still runs.
        assert outcome is None, f"drop at step {_STEPS[k - 1]!r} escaped ConnectionManager.close"
        assert points.executed == [s for i, s in enumerate(_STEPS) if i != k - 1], (
            f"steps after {_STEPS[k - 1]!r} were skipped"
        )
    else:
        # CancelledError: the chain stops loudly (force-abandon semantics).
        assert isinstance(outcome, asyncio.CancelledError)
        assert points.executed == _STEPS[: k - 1]


@pytest.mark.asyncio
async def test_close_chain_sweep():
    await sweep_cancellation(_scenario, _oracle)


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
