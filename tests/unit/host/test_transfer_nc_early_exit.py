"""A netcat that ends before the transfer starts is reported at once, by its exit status.

The listener (put, tunnelled get) and the sender (direct get) are commands
otto already waits on, so how they ended is the evidence: no further
readiness poll is sent once the listener has ended, a status a fresh port
cannot change (126, 127) is not retried, and the message carries the status.
Only 126 and 127 name ``nc_options.exec_name``: any other status is the whole
command line's, so the message blames the command, never the netcat. Every test drives the public
``put_files`` / ``get_files``.
"""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.exit_status import gave_no_exit_status
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.result import CommandResult, Result
from otto.utils import Status
from tests.unit.host.nc_spawn_support import spawn_once_typed


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _status(code: int) -> CommandResult:
    return CommandResult(
        command="", value="", status=Status.Success if code == 0 else Status.Failed, retcode=code
    )


def _no_status() -> CommandResult:
    """A listener that gave no exit status: the result of a session that died under it."""
    return CommandResult(
        command="nc", value="Session died unexpectedly (EOF)", status=Status.Error, retcode=-1
    )


def _make_ft(exec_cmd: AsyncMock, *, has_tunnel: bool = False, exec_name: str = "nc"):
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = has_tunnel
    mock_connections.ip = "10.0.0.1"
    mock_connections.term = "ssh"
    mock_connections.forward_port = AsyncMock(return_value=15000)
    return NcFileTransfer(
        connections=mock_connections,
        name="test2",
        transfer="nc",
        nc_options=NcOptions(
            exec_name=exec_name,
            port=9000,
            port_strategy="ss",
            port_cmd=None,
            listener_check="ss",
            listener_cmd=None,
            listener_timeout=5.0,
        ),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=exec_cmd,
        userland=None,
    )


_CANCELLED = "cancelled"
"""A `_Host` ending: the listener's task is cancelled from outside the attempt."""


class _Host:
    """A host whose netcat command ends as *ending* says.

    *ending* is a ``CommandResult`` to return, an exception to raise, or
    ``_CANCELLED``: the readiness check cancels the listener's task from
    outside, as a session teardown would. ``during_check`` ends the listener
    from inside a readiness check, which answers only once the listener's
    task is done, so the wait's outcome follows from that order alone and
    never from the readiness budget expiring. ``None`` keeps the listener up
    until a connect arrives.
    """

    def __init__(
        self,
        ending: "CommandResult | Exception | str | None",
        *,
        during_check: bool = False,
        exec_name: str = "nc",
    ) -> None:
        self.ending = ending
        self.during_check = during_check
        self.exec_name = exec_name
        self.listen_cmds: list[str] = []
        self.send_cmds: list[str] = []
        self.events: list[str] = []
        self.release = asyncio.Event()
        self.served = asyncio.Event()
        self.listener: asyncio.Task | None = None

    async def _end(self) -> CommandResult:
        self.events.append("ended")
        if isinstance(self.ending, Exception):
            raise self.ending
        assert self.ending is not None
        return self.ending

    async def exec_side(self, cmd: str, *a, **kw) -> CommandResult:
        if " -l -p " in cmd:
            self.listen_cmds.append(cmd)
            self.listener = asyncio.current_task()
            if self.ending == _CANCELLED:
                await asyncio.Event().wait()
            if self.ending is None:
                await self.served.wait()
                self.events.append("served")
                return _ok()
            if self.during_check:
                await self.release.wait()
            return await self._end()
        if cmd.startswith(f"{self.exec_name} 127.0.0.1 "):
            self.send_cmds.append(cmd)
            return await self._end()
        if "sport = :" in cmd:
            self.events.append("check")
        await asyncio.sleep(0)
        if "sport = :" in cmd:
            if self.ending is None:
                return _ok("1\n")
            if self.ending == _CANCELLED:
                assert self.listener is not None
                self.listener.cancel()
            elif self.during_check:
                self.release.set()
            if self.ending == _CANCELLED or self.during_check:
                assert self.listener is not None
                await asyncio.wait({self.listener})
            return CommandResult(command="", value="0\n", status=Status.Failed, retcode=1)
        if cmd.startswith("stat"):
            return _ok("5 regular file\n")
        if 'reserved="' in cmd:
            self.events.append("scanned")
            return _ok("9000\n")
        return _ok()

    def checks_after_the_end(self) -> int:
        """Return how many readiness checks were SENT after a listener had ended.

        Counted from each ending to the next attempt's port scan.
        """
        sent, ended = 0, False
        for event in self.events:
            if event == "ended":
                ended = True
            elif event == "scanned":
                ended = False
            elif event == "check" and ended:
                sent += 1
        return sent


_ROUTES = pytest.mark.parametrize(
    ("direction", "has_tunnel"),
    [("put", False), ("put", True), ("get", True)],
    ids=["direct-put", "tunnelled-put", "tunnelled-get"],
)


async def _transfer(
    host: _Host,
    direction: str,
    has_tunnel: bool,
    tmp_path: Path,
    *,
    exec_name: str = "nc",
    spawn_cmd=None,
) -> tuple[Result, Result]:
    """Run one file through the public put or get; return the aggregate and the file's result."""
    ft = _make_ft(AsyncMock(side_effect=host.exec_side), has_tunnel=has_tunnel, exec_name=exec_name)
    if spawn_cmd is not None:
        ft._spawn_cmd = spawn_cmd

    async def connect(*_a, **_kw):
        host.served.set()
        writer = MagicMock()
        writer.transport.get_write_buffer_size.return_value = 0
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        return MagicMock(), writer

    with (
        patch.object(NcFileTransfer, "_scan_start", new=lambda self: 9000),
        # Far beyond any delay: no outcome here may hang on the budget expiring.
        patch.object(transfer_mod, "_NC_LISTENER_READY_TIMEOUT", 60.0),
        patch.object(transfer_mod, "_connect_with_retry", new=connect),
        patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
        patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
    ):
        if direction == "put":
            src = tmp_path / "payload.bin"
            src.write_bytes(b"x" * 5)
            outcome = await asyncio.wait_for(
                ft.put_files(src_files=[src], dest_dir=Path("/remote/dst"), show_progress=False),
                5.0,
            )
        else:
            src = Path("/remote/data.bin")
            dst = tmp_path / "dst"
            dst.mkdir(exist_ok=True)
            outcome = await asyncio.wait_for(
                ft.get_files(src_files=[src], dest_dir=dst, show_progress=False), 5.0
            )
    return outcome, outcome.value[src]


def _assert_failed(outcome: Result, own: Result) -> str:
    assert not outcome.is_ok
    assert own.status is Status.Error
    return own.msg or ""


def _assert_not_found(message: str) -> None:
    assert "nc_options.exec_name" in message
    assert "'nc'" in message
    assert "127" in message
    assert "not found" in message


@pytest.mark.asyncio
async def test_put_with_a_listener_that_exits_127_fails_once_and_names_the_binary(
    tmp_path: Path,
) -> None:
    host = _Host(_status(127))
    message = _assert_failed(*await _transfer(host, "put", False, tmp_path))
    _assert_not_found(message)
    assert len(host.listen_cmds) == 1, "a missing binary is not retried"
    assert "two attempts" not in message
    assert host.checks_after_the_end() == 0


@pytest.mark.asyncio
async def test_tunnelled_get_with_a_listener_that_exits_127_fails_once_and_names_the_binary(
    tmp_path: Path,
) -> None:
    host = _Host(_status(127))
    message = _assert_failed(*await _transfer(host, "get", True, tmp_path))
    _assert_not_found(message)
    assert len(host.listen_cmds) == 1
    assert host.checks_after_the_end() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [127, 1])
async def test_direct_get_whose_sender_exits_without_connecting_fails_with_its_status(
    status: int, tmp_path: Path
) -> None:
    host = _Host(_status(status))
    message = _assert_failed(*await _transfer(host, "get", False, tmp_path))
    assert len(host.send_cmds) == 1
    assert f"status {status}" in message
    assert ("nc_options.exec_name" in message) is (status == 127)
    assert ("not found" in message) is (status == 127)


@pytest.mark.asyncio
async def test_a_listener_that_exits_126_fails_once(tmp_path: Path) -> None:
    host = _Host(_status(126))
    message = _assert_failed(*await _transfer(host, "put", False, tmp_path))
    assert "status 126" in message
    assert "nc_options.exec_name" in message
    assert "not found" not in message
    assert len(host.listen_cmds) == 1
    assert host.checks_after_the_end() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["put", "get"])
async def test_a_listener_command_that_raises_is_retried_with_its_cause(
    direction: str, tmp_path: Path
) -> None:
    host = _Host(ConnectionResetError("the channel's own words"))
    message = _assert_failed(*await _transfer(host, direction, True, tmp_path))
    assert len(host.listen_cmds) == 2
    assert "the channel's own words" in message
    assert "exited with status" not in message
    assert message.endswith("(two attempts were made)")


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["put", "get"])
async def test_a_listener_with_no_exit_status_is_retried_and_never_worded_as_a_status(
    direction: str, tmp_path: Path
) -> None:
    host = _Host(_no_status())
    message = _assert_failed(*await _transfer(host, direction, True, tmp_path))
    assert len(host.listen_cmds) == 2
    assert "no exit status" in message
    assert "exited with status" not in message
    assert "-1" not in message


@pytest.mark.asyncio
async def test_a_listener_that_ends_during_the_last_readiness_check_is_classified_the_same(
    tmp_path: Path,
) -> None:
    host = _Host(_status(127), during_check=True)
    message = _assert_failed(*await _transfer(host, "put", False, tmp_path))
    _assert_not_found(message)
    assert len(host.listen_cmds) == 1
    assert "not ready within" not in message


@pytest.mark.asyncio
async def test_a_listener_that_exits_1_is_retried_on_a_fresh_port(tmp_path: Path) -> None:
    host = _Host(_status(1))
    message = _assert_failed(*await _transfer(host, "put", False, tmp_path))
    assert len(host.listen_cmds) == 2
    assert "the listener command exited with status 1" in message
    assert "nc_options.exec_name" not in message
    assert message.endswith("(two attempts were made)")


@pytest.mark.asyncio
async def test_a_listener_that_exits_0_while_waiting_is_still_fed_by_another(
    tmp_path: Path,
) -> None:
    host = _Host(_status(0))
    message = _assert_failed(*await _transfer(host, "put", False, tmp_path))
    assert len(host.listen_cmds) == 2
    assert "another process's stream reached it (port shared)" in message
    assert message.endswith("(two attempts were made)")


_ENDINGS = [
    pytest.param(_status(0), 2, "(port shared)", id="status-0"),
    pytest.param(_status(1), 2, "exited with status 1", id="status-1"),
    pytest.param(_status(126), 1, "exited with status 126", id="status-126"),
    pytest.param(_status(127), 1, "not found", id="status-127"),
    pytest.param(OSError("the cause"), 2, "the cause", id="raised"),
    pytest.param(
        _no_status(),
        2,
        "no exit status",
        id="no-status",
    ),
]


@pytest.mark.asyncio
@_ROUTES
@pytest.mark.parametrize("during_check", [False, True], ids=["before-poll", "during-last-poll"])
@pytest.mark.parametrize(("ending", "spawns", "needle"), _ENDINGS)
async def test_every_ending_on_every_route_reads_and_retries_by_its_status(
    direction: str,
    has_tunnel: bool,
    during_check: bool,
    ending: CommandResult | Exception,
    spawns: int,
    needle: str,
    tmp_path: Path,
) -> None:
    host = _Host(ending, during_check=during_check)
    message = _assert_failed(*await _transfer(host, direction, has_tunnel, tmp_path))
    assert needle in message
    assert len(host.listen_cmds) == spawns
    assert message.endswith("(two attempts were made)") is (spawns == 2)
    assert "not ready within" not in message
    if not during_check:
        assert host.checks_after_the_end() == 0
    if not isinstance(ending, CommandResult) or gave_no_exit_status(ending):
        assert "exited with status" not in message


async def _failed_open() -> None:
    raise OSError("session open failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "has_tunnel"), [("put", False), ("get", True)], ids=["put", "tunnelled-get"]
)
@pytest.mark.parametrize(
    ("before_typed", "spawns", "needle"),
    [
        pytest.param(None, 1, "not found", id="typed-then-127"),
        pytest.param(_failed_open, 2, "session open failed", id="already-done-raising"),
    ],
)
async def test_a_listener_through_the_host_spawn_is_read_by_how_it_ended(
    direction: str, has_tunnel: bool, before_typed, spawns: int, needle: str, tmp_path: Path
) -> None:
    """The host's spawn may hand back a listener task that is already done."""
    host = _Host(_status(127))
    spawned: list[str] = []

    async def spawn_cmd(cmd: str, **kw) -> "asyncio.Task[CommandResult]":
        spawned.append(cmd)
        return await spawn_once_typed(lambda: host.exec_side(cmd, **kw), before_typed)

    message = _assert_failed(
        *await _transfer(host, direction, has_tunnel, tmp_path, spawn_cmd=spawn_cmd)
    )
    assert needle in message
    assert len(spawned) == spawns
    assert all(" -l -p " in cmd for cmd in spawned)
    assert host.listen_cmds == ([] if before_typed else spawned), "no fallback exec"
    assert message.endswith("(two attempts were made)") is (spawns == 2)
    if spawns == 1:
        _assert_not_found(message)
    assert "not ready within" not in message


@pytest.mark.asyncio
async def test_a_multi_word_exec_name_is_sent_as_written(tmp_path: Path) -> None:
    host = _Host(None, exec_name="busybox nc")
    outcome, own = await _transfer(host, "put", False, tmp_path, exec_name="busybox nc")
    assert own.status is Status.Success, own.msg
    assert outcome.is_ok
    assert len(host.listen_cmds) == 1
    assert re.search(r"busybox nc -l -p \d+", host.listen_cmds[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["put", "get"])
async def test_a_listener_cancelled_from_outside_is_retried_and_never_worded_as_a_status(
    direction: str, tmp_path: Path
) -> None:
    host = _Host(_CANCELLED)
    message = _assert_failed(*await _transfer(host, direction, True, tmp_path))
    assert len(host.listen_cmds) == 2
    assert "cancelled before the transfer started" in message
    assert "exited with status" not in message
    assert message.endswith("(two attempts were made)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "has_tunnel"), [("put", False), ("get", True)], ids=["put", "tunnelled-get"]
)
async def test_a_cancelled_transfer_still_propagates(
    direction: str, has_tunnel: bool, tmp_path: Path
) -> None:
    host = _Host(_CANCELLED)
    started = asyncio.Event()
    ft = _make_ft(AsyncMock(side_effect=host.exec_side), has_tunnel=has_tunnel)

    async def parked(*_a, **_kw) -> bool:
        started.set()
        await asyncio.Event().wait()
        return False

    with (
        patch.object(NcFileTransfer, "_scan_start", new=lambda self: 9000),
        patch.object(NcFileTransfer, "_wait_for_remote_listener", new=parked),
        patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
    ):
        if direction == "put":
            src = tmp_path / "payload.bin"
            src.write_bytes(b"x" * 5)
            call = ft.put_files(src_files=[src], dest_dir=Path("/remote/dst"), show_progress=False)
        else:
            dst = tmp_path / "dst"
            dst.mkdir()
            call = ft.get_files(
                src_files=[Path("/remote/data.bin")], dest_dir=dst, show_progress=False
            )
        transfer = asyncio.create_task(call)
        await asyncio.wait_for(started.wait(), 5.0)
        transfer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(transfer, 5.0)


@pytest.mark.asyncio
@_ROUTES
async def test_a_readiness_timeout_after_the_listener_ended_reports_the_ending_not_the_timeout(
    direction: str, has_tunnel: bool, tmp_path: Path
) -> None:
    """The wait fails only once the listener has ended, so no clock decides the order."""

    async def times_out_after_the_end(self, port, timeout=0.0, interval=0.0, *, listener=None):
        assert listener is not None
        await asyncio.wait({listener})
        raise ConnectionError("timeout")

    host = _Host(_status(2))
    with patch.object(NcFileTransfer, "_wait_for_remote_listener", new=times_out_after_the_end):
        message = _assert_failed(*await _transfer(host, direction, has_tunnel, tmp_path))
    assert "exited with status 2" in message
    assert "timeout" not in message
    assert len(host.listen_cmds) == 2
    assert message.endswith("(two attempts were made)")


class _Sender:
    """A direct get's host whose sender ends as *ending* says.

    *ending* is a ``CommandResult`` to return, ``_CANCELLED`` (the sender's
    own task is cancelled from inside, as a session teardown would), or
    ``None`` (the sender never ends on its own). With ``connect_after``, a
    stream carrying the file reaches otto's server only once the sender's
    task is done, so the order of the two events is fixed by the events
    themselves and never by a clock.
    """

    def __init__(self, ending: "CommandResult | str | None", *, connect_after: bool = False):
        self.ending = ending
        self.connect_after = connect_after
        self.sender: asyncio.Task | None = None
        self.started = asyncio.Event()
        self.background: set[asyncio.Task] = set()

    async def _deliver(self, port: int) -> None:
        assert self.sender is not None
        await asyncio.wait({self.sender})
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x" * 5)
        await writer.drain()
        await reader.read()  # otto's close at `total` bytes
        writer.close()

    async def exec_side(self, cmd: str, *a, **kw) -> CommandResult:
        if cmd.startswith("stat"):
            return _ok("5 regular file\n")
        if not cmd.startswith("nc 127.0.0.1 "):
            return _ok()
        self.sender = asyncio.current_task()
        self.started.set()
        if self.connect_after:
            port = int(cmd.split()[2])
            self.background.add(asyncio.create_task(self._deliver(port)))
        if self.ending is None:
            await asyncio.Event().wait()
        if self.ending == _CANCELLED:
            self.sender.cancel()
            await asyncio.sleep(0)
        assert isinstance(self.ending, CommandResult)
        return self.ending


def _direct_get(host: _Sender, tmp_path: Path):
    ft = _make_ft(AsyncMock(side_effect=host.exec_side))
    dst = tmp_path / "dst"
    dst.mkdir(exist_ok=True)
    return ft.get_files(src_files=[Path("/remote/data.bin")], dest_dir=dst, show_progress=False)


@pytest.mark.asyncio
async def test_direct_get_whose_sender_task_is_cancelled_fails_promptly_with_a_named_failure(
    tmp_path: Path,
) -> None:
    host = _Sender(_CANCELLED)
    outcome = await asyncio.wait_for(_direct_get(host, tmp_path), 5.0)
    message = _assert_failed(outcome, outcome.value[Path("/remote/data.bin")])
    assert "sender" in message
    assert "cancelled before the transfer started" in message


@pytest.mark.asyncio
async def test_a_cancelled_direct_get_still_propagates_and_leaves_no_sender_behind(
    tmp_path: Path,
) -> None:
    host = _Sender(None)
    transfer = asyncio.create_task(_direct_get(host, tmp_path))
    await asyncio.wait_for(host.started.wait(), 5.0)
    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(transfer, 5.0)
    assert host.sender is not None
    assert host.sender.done(), "the sender's task outlived the call"


@pytest.mark.asyncio
async def test_direct_get_whose_sender_exits_0_before_its_stream_is_delivered_succeeds(
    tmp_path: Path,
) -> None:
    host = _Sender(_status(0), connect_after=True)
    # Far beyond any delay: a success here cannot come from the grace expiring.
    with patch.object(transfer_mod, "_NC_SENDER_CONNECT_GRACE", 60.0):
        outcome = await asyncio.wait_for(_direct_get(host, tmp_path), 5.0)
    own = outcome.value[Path("/remote/data.bin")]
    assert own.status is Status.Success, own.msg
    assert (tmp_path / "dst" / "data.bin").read_bytes() == b"x" * 5
    await asyncio.gather(*host.background)


@pytest.mark.asyncio
async def test_direct_get_whose_sender_exits_0_without_connecting_fails_after_the_grace(
    tmp_path: Path,
) -> None:
    host = _Sender(_status(0))
    with patch.object(transfer_mod, "_NC_SENDER_CONNECT_GRACE", 0.0):
        outcome = await asyncio.wait_for(_direct_get(host, tmp_path), 5.0)
    message = _assert_failed(outcome, outcome.value[Path("/remote/data.bin")])
    assert "ended without connecting" in message
    assert "status 0" in message


@pytest.mark.asyncio
async def test_direct_get_whose_sender_exits_127_fails_without_waiting_for_the_grace(
    tmp_path: Path,
) -> None:
    host = _Sender(_status(127))
    with patch.object(transfer_mod, "_NC_SENDER_CONNECT_GRACE", 60.0):
        outcome = await asyncio.wait_for(_direct_get(host, tmp_path), 5.0)
    message = _assert_failed(outcome, outcome.value[Path("/remote/data.bin")])
    _assert_not_found(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["put", "get"])
async def test_a_status_other_than_126_or_127_blames_the_command_not_the_netcat(
    direction: str, tmp_path: Path
) -> None:
    host = _Host(_status(2))
    message = _assert_failed(*await _transfer(host, direction, True, tmp_path))
    assert "listener command" in message
    assert "exited with status 2" in message
    assert "/remote/" in message
    assert "nc_options.exec_name" not in message
    assert "not found" not in message
