"""ConnectionManager.close chain: per-step guards + tier-1 cancellation sweep.

Chain order is telnet -> sftp -> ssh -> ftp -> hop: telnet (and console) go
first, so a hop never closes under a drain it carries. One raising step (e.g.
``ftp.quit()`` on a dead socket) must not skip the steps behind it — in
particular the hop teardown (chaos spec: teardown chain robustness). A
CancelledError still aborts the chain loudly (force-abandon contract).
"""

import asyncio

import pytest

from otto.host import console as console_mod
from otto.host import telnet as telnet_mod
from otto.host.connections import ConnectionManager, ConsoleEndpoint, teardown_step
from otto.host.console import ConsoleClient
from otto.host.options import ConsoleOptions, TelnetOptions
from otto.host.telnet import TelnetClient
from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, Surface, sweep_cancellation

_STEPS = ["telnet", "sftp", "ssh", "ftp", "hop"]


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

    def abort(self) -> None:
        pass


class _FakeHop:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("hop", surface=Surface.NETWORK)

    def abort(self) -> None:
        pass


def _manager(points: ChaosPoints) -> ConnectionManager:
    """A REAL ConnectionManager (the chain under test) over instrumented fakes."""
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="ssh", name="box", host_id="box")
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
    points.arm(4, ConnectionDropped)  # ftp.quit() blows up
    mgr = _manager(points)
    await mgr.close()
    assert mgr._sftp_conn is None
    assert mgr._ssh_conn is None
    assert mgr._ftp_conn is None
    assert mgr._telnet_conn is None


@pytest.mark.asyncio
async def test_close_logs_the_failing_step(caplog):
    points = ChaosPoints()
    points.arm(4, ConnectionDropped)  # ftp.quit() blows up
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
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="ssh", name="box", host_id="box")
    user_ssh = _FakeUserSsh()
    user_sftp = _FakeUserSftp()
    mgr._user_ssh_conns["postgres"] = user_ssh
    mgr._user_sftp_conns["postgres"] = user_sftp

    await mgr.close()

    assert user_ssh.closed
    assert user_sftp.exited
    assert mgr._user_ssh_conns == {}
    assert mgr._user_sftp_conns == {}


# ---------------------------------------------------------------------------
# telnet and console are released first, and aborted if the teardown is cancelled
#
# A hop (the SSH tunnel a forwarded telnet or console stream rides) must
# outlive any drain on a stream it carries, so the manager releases its telnet
# and console clients before any other await. A cancellation anywhere after it
# detaches them aborts every one it detached, so none leaks its stream.
# ---------------------------------------------------------------------------


class _RecordingStep:
    """A teardown double that records its step name; ``abort()`` is sync, as on the clients."""

    def __init__(self, order: list[str], name: str) -> None:
        self._order = order
        self._name = name

    async def close(self) -> None:
        self._order.append(self._name)

    async def quit(self) -> None:
        self._order.append(self._name)

    def exit(self) -> None:
        self._order.append(self._name)

    def abort(self) -> None:
        self._order.append(f"abort:{self._name}")


class _RecordingSsh:
    def __init__(self, order: list[str], name: str) -> None:
        self._order = order
        self._name = name
        self._transport = _FakeTransport()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        self._order.append(self._name)


@pytest.mark.asyncio
async def test_telnet_and_console_close_before_any_other_teardown():
    order: list[str] = []
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="ssh", name="box", host_id="box")
    mgr._user_sftp_conns["pg"] = _RecordingStep(order, "sftp-as-pg")
    mgr._user_ssh_conns["pg"] = _RecordingSsh(order, "ssh-as-pg")
    mgr._sftp_conn = _RecordingStep(order, "sftp")
    mgr._ssh_conn = _RecordingSsh(order, "ssh")
    mgr._ftp_conn = _RecordingStep(order, "ftp")
    mgr._telnet_conn = _RecordingStep(order, "telnet")
    mgr._console_conn = _RecordingStep(order, "console")
    mgr._console_endpoint = ConsoleEndpoint(
        server_ip="10.0.0.2", hop=_RecordingStep(order, "console hop")
    )
    mgr._hop = _RecordingStep(order, "hop")

    await mgr.close()

    assert order == [
        "telnet",
        "console",
        "sftp-as-pg",
        "ssh-as-pg",
        "sftp",
        "ssh",
        "ftp",
        "console hop",
        "hop",
    ]


def _manager_with_hops(order: list[str]) -> ConnectionManager:
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="console", name="box", host_id="box")
    mgr._console_conn = _RecordingStep(order, "console")
    mgr._console_endpoint = ConsoleEndpoint(
        server_ip="10.0.0.2", hop=_RecordingStep(order, "console hop")
    )
    mgr._hop = _RecordingStep(order, "hop")
    return mgr


def test_abandon_releases_the_line_then_every_hop():
    """A manager dropped unclosed lets go of what reaches past its target (#451).

    The console server's hop and the host's own outlive a reboot of the
    target, and nothing closes a dropped manager: unreleased, each leaks an
    open ssh connection and a forward listener.
    """
    order: list[str] = []
    mgr = _manager_with_hops(order)

    mgr.abandon()

    assert order == ["abort:console", "abort:console hop", "abort:hop"]
    assert mgr._console_conn is None


def test_abandon_with_nothing_dialed_is_a_no_op():
    ConnectionManager(ip="10.0.0.1", creds=[], term="ssh", name="box", host_id="box").abandon()


@pytest.mark.asyncio
async def test_a_cancelled_close_aborts_the_hops_it_never_reached():
    """Cancelled mid-teardown of the target, the hops are aborted, not left open (#999).

    The close chain tears the target down before the hops, so a cancel there
    never reaches the hop steps; only the cancel arm can release them.
    """
    order: list[str] = []
    reached, never = asyncio.Event(), asyncio.Event()

    class _StalledSsh:
        _transport = None

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            reached.set()
            await never.wait()

    mgr = _manager_with_hops(order)
    mgr._console_conn = None
    mgr._ssh_conn = _StalledSsh()
    task = asyncio.ensure_future(mgr.close())
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["abort:console hop", "abort:hop"]


@pytest.fixture
def _empty_registry():
    telnet_mod._live_console_transports.clear()
    yield
    telnet_mod._live_console_transports.clear()


class _Writer:
    """A writer over *transport*: closing it closes the transport, as telnetlib3's does."""

    def __init__(self, transport) -> None:
        self.transport = transport

    def write(self, data: bytes) -> None:
        self.transport.write(data)

    def is_closing(self) -> bool:
        return self.transport.is_closing()

    def close(self) -> None:
        self.transport.close()


class _BlockingReader:
    """A reader whose every read signals *reading*, then never returns."""

    def __init__(self) -> None:
        self.reading = asyncio.Event()

    async def read(self, n: int) -> bytes:
        self.reading.set()
        await asyncio.get_running_loop().create_future()
        raise AssertionError("unreachable")


def _install(client, reader, transport) -> None:
    """Install a registered stream on *client*, as its connect() or open() would."""
    client._install(reader, _Writer(transport))
    telnet_mod._register_console_transport(transport)


def _telnet_client() -> TelnetClient:
    return TelnetClient(
        host="h",
        user="u",
        password="p",
        options=TelnetOptions(login=False, single_client_console=True),
    )


def _console_client() -> ConsoleClient:
    return ConsoleClient(
        host="localhost",
        port=54321,
        user="u",
        password=None,
        options=ConsoleOptions(server="test1", port=4001, login=False),
    )


def _blocking_release(monkeypatch, release: asyncio.Event, calls: list) -> None:
    """Make the one release record each call, then wait for *release*."""

    async def blocked(reader, writer, transport, **kwargs):
        calls.append(transport)
        await release.wait()

    monkeypatch.setattr(telnet_mod, "release_telnet_stream", blocked)


async def _turns(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registry")
@pytest.mark.parametrize("first", ["session", "manager"])
async def test_session_and_manager_close_share_one_release(monkeypatch, stalled_transport, first):
    """A default session and its manager close the one cached client: one release, hop last.

    The session's closer is the client's own ``close()``. Whichever closer
    takes the stream releases it; the other waits for that release to end,
    so the manager cannot close the hop under it.
    """
    release, calls, order = asyncio.Event(), [], []
    _blocking_release(monkeypatch, release, calls)
    client = _telnet_client()
    _install(client, _BlockingReader(), stalled_transport())
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="telnet", name="box", host_id="box")
    mgr._telnet_conn = client
    mgr._hop = _RecordingStep(order, "hop")

    closers = [client.close(), mgr.close()]
    if first == "manager":
        closers.reverse()
    tasks = [asyncio.ensure_future(c) for c in closers]
    await _turns()
    assert len(calls) == 1
    assert order == [], "the hop closed under a release still running on a stream it carries"

    release.set()
    await asyncio.gather(*tasks)
    assert order == ["hop"]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registry")
async def test_cancel_after_detach_aborts_every_detached_client(monkeypatch, stalled_transport):
    """Cancelled while telnet drains: both streams aborted, the console's included.

    The console's ``close()`` never started, so only the manager's cancel arm
    can release its stream. Each transport stays registered exactly when its
    release was not observed.
    """
    telnet, console = _telnet_client(), _console_client()
    telnet_reader = _BlockingReader()
    telnet_transport, console_transport = stalled_transport(), stalled_transport()
    _install(telnet, telnet_reader, telnet_transport)
    _install(console, _BlockingReader(), console_transport)
    console_aborts: list[None] = []
    real_abort = console.abort
    monkeypatch.setattr(console, "abort", lambda: (console_aborts.append(None), real_abort()))
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="telnet", name="box", host_id="box")
    mgr._telnet_conn, mgr._console_conn = telnet, console

    task = asyncio.ensure_future(mgr.close())
    await telnet_reader.reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _turns()

    for transport in (telnet_transport, console_transport):
        assert transport.aborts >= 1
        released = transport.sock.fileno() == -1
        assert released
        assert (transport in telnet_mod._live_console_transports) is not released
    for client in (telnet, console):
        assert client.writer is None
        assert client.reader is None
    assert console_aborts, "the cancel arm did not abort the console it detached"


class _AbortSpy:
    """Count a client's ``abort()`` calls, still running the real one."""

    def __init__(self, monkeypatch, client) -> None:
        self.calls = 0
        real = client.abort

        def spy() -> None:
            self.calls += 1
            real()

        monkeypatch.setattr(client, "abort", spy)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registry")
async def test_cancel_while_waiting_on_another_release_leaves_it_running(
    monkeypatch, stalled_transport
):
    """A manager cancelled while it waits on a session's release re-raises; that release goes on.

    The session's ``client.close()`` holds the telnet release. The manager's
    ``telnet.close()`` is a second closer waiting on ``_released``; its
    cancellation propagates, and the arm's ``telnet.abort()`` finds nothing to
    take, so the first release is neither aborted nor cut short. The console,
    which no release has reached, is still aborted.
    """
    release, calls = asyncio.Event(), []
    _blocking_release(monkeypatch, release, calls)
    telnet, console = _telnet_client(), _console_client()
    telnet_transport, console_transport = stalled_transport(), stalled_transport()
    _install(telnet, _BlockingReader(), telnet_transport)
    _install(console, _BlockingReader(), console_transport)
    telnet_aborts, console_aborts = _AbortSpy(monkeypatch, telnet), _AbortSpy(monkeypatch, console)
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="telnet", name="box", host_id="box")
    mgr._telnet_conn, mgr._console_conn = telnet, console

    session_close = asyncio.ensure_future(telnet.close())
    await _turns()
    manager_close = asyncio.ensure_future(mgr.close())
    await _turns()
    assert len(calls) == 1
    manager_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await manager_close
    await _turns()

    assert telnet_aborts.calls == 1, "the cancel arm did not reach the telnet client"
    assert telnet_transport.aborts == 0, "the cancel arm aborted a release still under way"
    assert not session_close.done()
    assert console_aborts.calls == 1
    assert console_transport.aborts == 1
    assert console.writer is None

    release.set()
    await session_close
    assert telnet_transport.aborts == 0
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registry")
async def test_cancel_after_both_clients_closed_aborts_nothing(monkeypatch, stalled_transport):
    """Cancelled in the ssh step, after telnet and console are released: the aborts are inert."""
    telnet, console = _telnet_client(), _console_client()
    telnet_transport, console_transport = stalled_transport(), stalled_transport()
    _install(telnet, _BlockingReader(), telnet_transport)
    _install(console, _BlockingReader(), console_transport)

    async def released(reader, writer, transport, **kwargs):
        writer.close()
        telnet_mod._unregister_console_transport(transport)

    monkeypatch.setattr(telnet_mod, "release_telnet_stream", released)
    monkeypatch.setattr(console_mod, "release_telnet_stream", released)
    telnet_aborts, console_aborts = _AbortSpy(monkeypatch, telnet), _AbortSpy(monkeypatch, console)

    ssh_reached = asyncio.Event()

    class _HangingSsh(_RecordingSsh):
        async def wait_closed(self) -> None:
            ssh_reached.set()
            await asyncio.get_running_loop().create_future()

    mgr = ConnectionManager(ip="10.0.0.1", creds=[], term="telnet", name="box", host_id="box")
    mgr._telnet_conn, mgr._console_conn = telnet, console
    mgr._ssh_conn = _HangingSsh([], "ssh")

    task = asyncio.ensure_future(mgr.close())
    await ssh_reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _turns()

    # Deliberate: the cancellation arm calls abort() on both clients
    # unconditionally, and that call is the contract pinned here. That those
    # calls do nothing (no transport aborted) is the next line's separate claim.
    assert (telnet_aborts.calls, console_aborts.calls) == (1, 1)
    assert (telnet_transport.aborts, console_transport.aborts) == (0, 0)
    assert telnet._released.is_set()
    assert console._released.is_set()
    assert not telnet_mod._live_console_transports


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registry")
async def test_reconnect_during_blocked_release_waits_then_dials(monkeypatch, stalled_transport):
    """A reconnect while another task releases the cached client dials only once it is gone."""
    release, calls, dials = asyncio.Event(), [], []
    _blocking_release(monkeypatch, release, calls)

    async def fake_open(host, **kwargs):
        dials.append(host)
        transport = stalled_transport()
        telnet_mod._register_console_transport(transport)
        return _BlockingReader(), _Writer(transport)

    async def no_negotiation(writer, timeout):
        pass

    monkeypatch.setattr(telnet_mod, "open_telnet_connection", fake_open)
    monkeypatch.setattr(telnet_mod, "negotiate_no_echo", no_negotiation)
    client = _telnet_client()
    _install(client, _BlockingReader(), stalled_transport())
    mgr = ConnectionManager(
        ip="10.0.0.1",
        creds=[],
        term="telnet",
        name="box",
        host_id="box",
        telnet_options=TelnetOptions(login=False, single_client_console=True),
    )
    mgr._telnet_conn = client

    closing = asyncio.ensure_future(client.close())
    await _turns()
    reconnect = asyncio.ensure_future(mgr.telnet())
    await _turns()
    assert len(calls) == 1
    assert dials == [], "the reconnect dialled while the old stream was still being released"

    release.set()
    await closing
    fresh = await reconnect
    assert dials == ["10.0.0.1"]
    assert fresh is not client
    assert fresh.alive
