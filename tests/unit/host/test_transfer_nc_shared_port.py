"""A remote port two processes chose at once is REFUSED, not shared.

Measured 2026-09-11 on the bed (nc.openbsd 1.226 on test1 and test4): the
listener binds with ``SO_REUSEPORT``, so a second ``nc -l -p PORT`` from
another process does not fail with EADDRINUSE -- it coexists on the same
``0.0.0.0:PORT`` and the kernel hands each incoming connection to whichever
listener it hashes to. Two otto processes (two xdist workers, or two otto
invocations) scanning the same host from the same base port in the same
window pick the same lowest free port, and "a listener is up on PORT" -- the
readiness check as it was -- is satisfied by the OTHER process's listener.
The streams then cross: sender A's bytes land in B's file and B's in A's,
both with rc 0, and when the two files happen to be the same length the
byte-count verify passes too. A transfer that reports success with someone
else's bytes is the worst outcome this backend has, so the fix is two-fold:

* the readiness wait COUNTS listeners and refuses more than one
  (:class:`~otto.host.transfer.nc.NcPortSharedError`), which the per-file
  retry turns into a fresh port; and
* the scan starts at a RANDOM offset above the base port, so two scanners
  overlapping in time rarely agree on a port in the first place -- without
  that, both refuse, both retry, and both land on the same next port again.
"""

import asyncio
import dataclasses
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.connections import ConnectionManager
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.host.transfer.nc import NcListenerAnswerError, NcPortSharedError
from otto.result import CommandResult
from otto.utils import Status
from tests._fixtures.nc_listings import tool_failed as _tool_failed


def _ok(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Success, retcode=0)


def _fail(output: str = "") -> CommandResult:
    return CommandResult(command="", value=output, status=Status.Failed, retcode=1)


def _never_ran(cmd: str) -> CommandResult:
    """A command that gave no exit status: the result of a session that died under it."""
    return CommandResult(
        command=cmd, value="Session died unexpectedly (EOF)", status=Status.Error, retcode=-1
    )


def _make_ft(
    exec_cmd: AsyncMock,
    *,
    has_tunnel: bool = False,
    listener_check: str = "ss",
    port_strategy: str = "ss",
) -> NcFileTransfer:
    mock_connections = MagicMock(spec=ConnectionManager)
    mock_connections.has_tunnel = has_tunnel
    mock_connections.ip = "10.0.0.1"
    mock_connections.term = "ssh"
    return NcFileTransfer(
        connections=mock_connections,
        name="test2",
        transfer="nc",
        nc_options=NcOptions(
            exec_name="nc",
            port=9000,
            port_strategy=port_strategy,  # type: ignore[arg-type]
            port_cmd=None,
            listener_check=listener_check,  # type: ignore[arg-type]
            listener_cmd=None,
            listener_timeout=5.0,
        ),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=exec_cmd,
        userland=None,
    )


class _Bed:
    """A remote host's port space, driven from the commands the backend sends.

    ``racing`` names the ports another process binds in the window between
    our scan and our readiness check -- invisible to the scan, counted by the
    check, and bound (so visible to every later scan) from then on. That is
    the collision as it happens on the bed. ``fed_by_another`` names the ports
    whose listener another process's stream reaches the moment it is up.
    """

    def __init__(
        self, racing: set[int] | None = None, fed_by_another: set[int] | None = None
    ) -> None:
        self.racing = set(racing or ())
        self.fed_by_another = set(fed_by_another or ())
        self.bound: set[int] = set()  # what a scan can see
        self.ours: set[int] = set()
        self.served: dict[int, asyncio.Event] = {}
        self.listen_cmds: list[str] = []

    def serve(self, port: int) -> None:
        """The connection that lets our listener on *port* exit."""
        self.served.setdefault(port, asyncio.Event()).set()

    async def exec_side(self, cmd: str, *a, **kw) -> CommandResult:
        if " -l -p " in cmd:
            port = int(re.search(r" -l -p (\d+) ", cmd).group(1))  # type: ignore[union-attr]
            self.listen_cmds.append(cmd)
            # Bound on the task's first step, before any control op that was
            # issued after `create_task` gets to run.
            self.ours.add(port)
            if port in self.fed_by_another:
                self.fed_by_another.discard(port)
                return _ok()
            await self.served.setdefault(port, asyncio.Event()).wait()
            return _ok()
        # A control op reaches the host after the listener task has started.
        await asyncio.sleep(0)
        if "sport = :" in cmd:
            port = int(re.search(r"sport = :(\d+)", cmd).group(1))  # type: ignore[union-attr]
            if port in self.racing:
                self.bound.add(port)
            count = (1 if port in self.bound else 0) + (1 if port in self.ours else 0)
            return _ok(f"{count}\n") if count else _fail("0\n")
        if cmd.startswith("stat"):
            return _ok("5 regular file\n")
        m = re.search(r'reserved=" ([^"]*)"', cmd)
        if m is not None:
            start = int(re.search(r"p=(\d+);", cmd).group(1))  # type: ignore[union-attr]
            taken = {int(p) for p in m.group(1).split()} | self.bound
            p = start
            while p in taken:
                p += 1
            return _ok(f"{p}\n")
        return _ok()


class _FakeWriter:
    def __init__(self) -> None:
        self.transport = MagicMock()
        self.transport.get_write_buffer_size.return_value = 0
        self.transport.is_closing.return_value = False
        self.closed = False

    def write(self, _data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed


def _fixed_scan_start(port: int):
    return patch.object(NcFileTransfer, "_scan_start", new=lambda self: port)


class TestTheWaitCountsListeners:
    """``_wait_for_remote_listener`` is an ownership check, not a presence check."""

    @pytest.mark.asyncio
    async def test_two_listeners_is_refused_at_once(self) -> None:
        exec_cmd = AsyncMock(return_value=_ok("2\n"))
        ft = _make_ft(exec_cmd)
        with pytest.raises(NcPortSharedError, match=r"port 8080.*2 listeners"):
            await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 1, "a shared port is not something to wait out"

    @pytest.mark.asyncio
    async def test_a_shared_port_is_a_connection_error_to_existing_handlers(self) -> None:
        assert issubclass(NcPortSharedError, ConnectionError)

    @pytest.mark.asyncio
    async def test_zero_then_one_is_ready(self) -> None:
        exec_cmd = AsyncMock(side_effect=[_fail("0\n"), _fail("0\n"), _ok("1\n")])
        ft = _make_ft(exec_cmd)
        await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 3

    @pytest.mark.asyncio
    async def test_unparseable_output_is_refused_at_once(self) -> None:
        """Text that is not a count is never read as "not ready yet": it is quoted at once."""
        exec_cmd = AsyncMock(return_value=_fail("sh: ss: not found"))
        ft = _make_ft(exec_cmd)
        with pytest.raises(NcListenerAnswerError, match="sh: ss: not found"):
            await ft._wait_for_remote_listener(8080, timeout=5.0, interval=0.01)
        assert exec_cmd.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("strategy", "tool_needle"),
        [
            ("ss", "sport = :"),
            ("netstat", "netstat -tln"),
            ("proc", "/tcp"),
        ],
    )
    async def test_every_builtin_check_reports_a_count(
        self, strategy: str, tool_needle: str
    ) -> None:
        exec_cmd = AsyncMock(return_value=_ok("1\n"))
        ft = _make_ft(exec_cmd, listener_check=strategy)
        await ft._wait_for_remote_listener(8080)
        script = exec_cmd.call_args[0][0]
        assert "echo $n" in script
        assert tool_needle in script

    @pytest.mark.asyncio
    async def test_a_custom_check_is_a_presence_check_and_says_so(self) -> None:
        """``custom`` exits 0/1 by contract; it cannot count, and is not asked to."""
        exec_cmd = AsyncMock(return_value=_ok(""))
        ft = _make_ft(exec_cmd, listener_check="custom")
        ft._nc_options = dataclasses.replace(ft._nc_options, listener_cmd="mycheck {port}")
        await ft._wait_for_remote_listener(8080)
        assert exec_cmd.call_args[0][0] == "mycheck 8080"


class TestPutTakesAFreshPortWhenSharedByAnother:
    @pytest.mark.asyncio
    async def test_first_port_shared_second_port_carries_the_file(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 5)
        bed = _Bed(racing={9000})
        connects: list[int] = []

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            connects.append(port)
            bed.serve(port)
            return None, _FakeWriter()

        ft = _make_ft(AsyncMock(side_effect=bed.exec_side))
        with (
            _fixed_scan_start(9000),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            per_file = await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), 5.0)

        assert per_file[src].status is Status.Success, per_file[src].msg
        assert connects == [9001], "nothing may be sent to a port another process shares"
        assert [int(re.search(r"-p (\d+)", c).group(1)) for c in bed.listen_cmds] == [  # type: ignore[union-attr]
            9000,
            9001,
        ]
        assert not ft._reserved_ports, "both ports are released after the batch"

    @pytest.mark.asyncio
    async def test_both_ports_shared_is_a_named_error(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 5)
        bed = _Bed(racing={9000, 9001})
        fake_connect = AsyncMock(side_effect=AssertionError("must not connect"))

        ft = _make_ft(AsyncMock(side_effect=bed.exec_side))
        with (
            _fixed_scan_start(9000),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            per_file = await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), 5.0)

        assert per_file[src].status is Status.Error
        assert "shared" in per_file[src].msg
        assert "9001" in per_file[src].msg
        fake_connect.assert_not_awaited()


class TestAListenerFedBeforeWeConnectIsRefused:
    """The other half of the swap: our listener took someone else's stream.

    The count check cannot see it -- by the time it runs the other listener
    may be gone, leaving exactly one -- but a ``nc -l`` exits after its one
    connection, so ours being gone before we connected is the proof.
    """

    @pytest.mark.asyncio
    async def test_put_refuses_and_retries_on_a_fresh_port(self, tmp_path: Path) -> None:
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 5)
        bed = _Bed(fed_by_another={9000})
        connects: list[int] = []

        async def fake_connect(host: str, port: int, timeout: float = 2.0):
            connects.append(port)
            bed.serve(port)
            return None, _FakeWriter()

        ft = _make_ft(AsyncMock(side_effect=bed.exec_side))
        with (
            _fixed_scan_start(9000),
            patch.object(transfer_mod, "_connect_with_retry", new=fake_connect),
            patch.object(NcFileTransfer, "_verify_nc_dest_size", new=AsyncMock(return_value=None)),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            per_file = await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), 5.0)

        assert per_file[src].status is Status.Success, per_file[src].msg
        # With the scan start pinned, the retry's port is the same number --
        # on a live host the random offset moves it -- so what is asserted
        # is the shape: two listeners spawned, ONE connection, to the second.
        assert len(bed.listen_cmds) == 2, bed.listen_cmds
        assert len(connects) == 1, "a listener that already exited is not ours to send to"

    @pytest.mark.asyncio
    async def test_tunneled_get_refuses_and_retries_on_a_fresh_port(self, tmp_path: Path) -> None:
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        bed = _Bed(fed_by_another={9000})
        forwarded: list[int] = []

        class _Reader:
            async def read(self, _n: int) -> bytes:
                return b"hello"

        async def forward_port(*, dest_port: int) -> int:
            port = dest_port
            forwarded.append(port)
            bed.serve(port)
            return 15000

        ft = _make_ft(AsyncMock(side_effect=bed.exec_side), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(side_effect=forward_port)
        with (
            _fixed_scan_start(9000),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(return_value=(_Reader(), _FakeWriter())),
            ),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            per_file = await asyncio.wait_for(ft._get_files_nc_tunneled([src_remote], dst_dir), 5.0)

        assert per_file[src_remote].status is Status.Success, per_file[src_remote].msg
        assert len(bed.listen_cmds) == 2, bed.listen_cmds
        assert len(forwarded) == 1, "a listener that already served someone is not ours to read"


class TestTunneledGetTakesAFreshPortWhenSharedByAnother:
    @pytest.mark.asyncio
    async def test_first_port_shared_second_port_carries_the_file(self, tmp_path: Path) -> None:
        src_remote = Path("/remote/data.bin")
        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        bed = _Bed(racing={9000})
        forwarded: list[int] = []

        class _Reader:
            async def read(self, _n: int) -> bytes:
                return b"hello"

        async def forward_port(*, dest_port: int) -> int:
            port = dest_port
            forwarded.append(port)
            bed.serve(port)
            return 15000

        ft = _make_ft(AsyncMock(side_effect=bed.exec_side), has_tunnel=True)
        ft._connections.forward_port = AsyncMock(side_effect=forward_port)
        with (
            _fixed_scan_start(9000),
            patch.object(
                transfer_mod,
                "_connect_with_retry",
                AsyncMock(return_value=(_Reader(), _FakeWriter())),
            ),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=AsyncMock(return_value=None)),
        ):
            per_file = await asyncio.wait_for(ft._get_files_nc_tunneled([src_remote], dst_dir), 5.0)

        assert per_file[src_remote].status is Status.Success, per_file[src_remote].msg
        assert (dst_dir / "data.bin").read_bytes() == b"hello"
        assert forwarded == [9001], "no forward may be opened onto a port another process shares"


class TestTheScanStartsAtARandomOffset:
    def test_start_is_within_the_spread_above_the_base_port(self) -> None:
        ft = _make_ft(AsyncMock())
        starts = {ft._scan_start() for _ in range(50)}
        assert all(9000 <= s < 9000 + transfer_mod._NC_PORT_SCAN_SPREAD for s in starts)
        assert len(starts) > 1, "fifty scans from one base all starting at the same port"

    def test_the_spread_never_runs_past_the_port_range(self) -> None:
        ft = _make_ft(AsyncMock())
        ft._nc_options = dataclasses.replace(ft._nc_options, port=65535)
        assert all(ft._scan_start() == 65535 for _ in range(20))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", ["ss", "netstat", "proc"])
    async def test_every_scanning_strategy_starts_where_the_offset_says(
        self, strategy: str
    ) -> None:
        exec_cmd = AsyncMock(return_value=_ok("9123\n"))
        ft = _make_ft(exec_cmd, port_strategy=strategy)
        with _fixed_scan_start(9123):
            assert await ft._find_free_port() == 9123
        assert "p=9123;" in exec_cmd.call_args[0][0]


class _CheckBed:
    """A host whose listener check answers from *answers*, one per poll (the last repeats).

    The listener spawn parks until it is cancelled, so every attempt reaches
    its own way out; ``spawns`` counts the attempts.
    """

    def __init__(
        self,
        answers: list[CommandResult],
        *,
        scan: CommandResult | None = None,
        stat: CommandResult | None = None,
    ) -> None:
        self.answers = answers
        # `is None`, not `or`: a failed result is falsy.
        self.scan = _ok("9000\n") if scan is None else scan
        self.stat = _ok("5 regular file\n") if stat is None else stat
        self.spawns = 0
        self.checks = 0
        self.scans = 0
        self.stats = 0

    async def exec_side(self, cmd: str, *a, **kw) -> CommandResult:
        if " -l -p " in cmd:
            self.spawns += 1
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        if "sport = :" in cmd:
            answer = self.answers[min(self.checks, len(self.answers) - 1)]
            self.checks += 1
            return answer
        if cmd.startswith("stat"):
            self.stats += 1
            return self.stat
        if 'reserved="' in cmd:
            self.scans += 1
            return self.scan
        return _ok()


async def _transfer(
    direction: str,
    tmp_path: Path,
    answers: list[CommandResult],
    connect: AsyncMock,
    *,
    bed: _CheckBed | None = None,
    has_tunnel: bool = True,
) -> tuple[str, _CheckBed, AsyncMock]:
    """Run one put or tunnelled get against a `_CheckBed`; return the message, bed and reaps."""
    tmp_path.mkdir(exist_ok=True)
    bed = bed or _CheckBed(answers)
    reaps = AsyncMock(return_value=None)
    ft = _make_ft(AsyncMock(side_effect=bed.exec_side), has_tunnel=has_tunnel)
    ft._connections.forward_port = AsyncMock(return_value=15000)
    with (
        _fixed_scan_start(9000),
        patch.object(transfer_mod, "_NC_LISTENER_READY_TIMEOUT", 0.05),
        patch.object(transfer_mod, "_connect_with_retry", new=connect),
        patch.object(NcFileTransfer, "_reap_nc_listener", new=reaps),
    ):
        if direction == "put":
            src = tmp_path / "payload.bin"
            src.write_bytes(b"x" * 5)
            per_file = await asyncio.wait_for(ft._put_files_nc([src], tmp_path / "dst"), 5.0)
        else:
            src = Path("/remote/data.bin")
            (tmp_path / "dst").mkdir()
            per_file = await asyncio.wait_for(ft._get_files_nc([src], tmp_path / "dst"), 5.0)
    assert per_file[src].status is Status.Error
    assert not ft._reserved_ports
    return per_file[src].msg or "", bed, reaps


_DIRECTIONS = pytest.mark.parametrize("direction", ["put", "get"])
_NO_CONNECT = AsyncMock(side_effect=AssertionError("must not connect"))


class TestBothDirectionsShareTheWaitAndTheRetry:
    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_a_failed_tool_stops_the_transfer_after_one_attempt(
        self, direction: str, tmp_path: Path
    ) -> None:
        message, bed, reaps = await _transfer(direction, tmp_path, [_tool_failed()], _NO_CONNECT)
        assert bed.spawns == 1, "a failed tool is not retried"
        assert "listener check's 'ss' rung failed on test2" in message
        assert "nc_options.listener_check" in message
        assert "two attempts" not in message
        assert reaps.await_count == 1, "the spawned listener is reaped"

    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_text_that_is_not_a_count_is_retried_once(
        self, direction: str, tmp_path: Path
    ) -> None:
        message, bed, reaps = await _transfer(
            direction, tmp_path, [_ok("ss: weird\n1")], _NO_CONNECT
        )
        assert bed.spawns == 2
        assert "not a count" in message
        assert message.endswith("(two attempts were made)")
        assert reaps.await_count == 2

    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_a_readiness_timeout_is_retried_once_and_names_its_stage(
        self, direction: str, tmp_path: Path
    ) -> None:
        message, bed, _ = await _transfer(direction, tmp_path, [_fail("0\n")], _NO_CONNECT)
        assert bed.spawns == 2
        assert "remote readiness" in message
        assert message.endswith("(two attempts were made)")

    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_a_forwarded_connect_failure_names_its_stage_and_cause(
        self, direction: str, tmp_path: Path
    ) -> None:
        connect = AsyncMock(side_effect=ConnectionError("the connect's own words"))
        message, bed, _ = await _transfer(direction, tmp_path, [_ok("1\n")], connect)
        assert bed.spawns == 2
        assert "forwarded connect" in message
        assert "nc listener on test2 port 9000 through localhost:15000" in message
        assert "'ss'" in message
        assert "nc_options.listener_check" in message
        assert "the listener process was still running" in message
        assert "the connect's own words" in message
        assert message.endswith("(two attempts were made)")

    @pytest.mark.asyncio
    async def test_a_direct_connect_failure_names_its_stage_and_the_host(
        self, tmp_path: Path
    ) -> None:
        connect = AsyncMock(side_effect=ConnectionError("the connect's own words"))
        message, bed, _ = await _transfer("put", tmp_path, [_ok("1\n")], connect, has_tunnel=False)
        assert bed.spawns == 2
        assert "forwarded" not in message
        assert ": connect: " in message
        assert "nc listener on test2 port 9000;" in message
        assert "the connect's own words" in message
        assert message.endswith("(two attempts were made)")

    @pytest.mark.asyncio
    async def test_a_wait_failure_with_an_ended_listener_says_it_had_exited_and_how(self) -> None:
        """The wording a listener ending during the last check, or before a connect, reaches."""

        async def ended() -> CommandResult:
            return CommandResult(command="", value="", status=Status.Failed, retcode=1)

        listener = asyncio.ensure_future(ended())
        await listener
        message = transfer_mod._nc_wait_failure(
            "connect", "test2 port 9000", "ss", listener, "refused", "nc"
        )
        assert "; the listener process had already exited: " in message
        assert "the listener command exited with status 1" in message
        assert "was still running" not in message

    @pytest.mark.asyncio
    async def test_both_directions_end_on_the_same_words(self, tmp_path: Path) -> None:
        answers = [_ok("0\n"), _ok("not a count")]
        put, _, _ = await _transfer("put", tmp_path / "p", answers, _NO_CONNECT)
        get, _, _ = await _transfer("get", tmp_path / "g", answers, _NO_CONNECT)
        assert put.split(": ", 1)[1] == get.split(": ", 1)[1]

    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_a_check_that_never_ran_is_polled_again_not_quoted(
        self, direction: str, tmp_path: Path
    ) -> None:
        """The first attempt polls past it and reaches its connect, as the second does."""
        never_ran = _never_ran("check")
        connect = AsyncMock(side_effect=ConnectionError("refused"))
        message, bed, _ = await _transfer(direction, tmp_path, [never_ran, _ok("1\n")], connect)
        assert bed.spawns == 2
        assert connect.await_count == 2, "the first attempt spent itself on a poll that never ran"
        assert never_ran.value not in message


class TestAPinnedPortRungIsNotRetried:
    @pytest.mark.asyncio
    @_DIRECTIONS
    async def test_the_port_finder_runs_once_and_the_failure_names_the_rung(
        self, direction: str, tmp_path: Path
    ) -> None:
        bed = _CheckBed([_ok("1\n")], scan=_tool_failed())
        message, _, _ = await _transfer(direction, tmp_path, [], _NO_CONNECT, bed=bed)
        assert bed.scans == 1
        assert bed.spawns == 0
        assert "pinned 'ss' rung failed on test2" in message
        assert "two attempts" not in message


class TestARefusalBeforeAnyAttemptReadsTheSameOnBothGetArms:
    @pytest.mark.asyncio
    async def test_a_directory_is_refused_once_and_unsuffixed(self, tmp_path: Path) -> None:
        messages = []
        for has_tunnel in (True, False):
            bed = _CheckBed([_ok("1\n")], stat=_ok("4096 directory\n"))
            message, _, _ = await _transfer(
                "get", tmp_path / str(has_tunnel), [], _NO_CONNECT, bed=bed, has_tunnel=has_tunnel
            )
            assert bed.stats == 1
            assert bed.scans == 0
            assert bed.spawns == 0
            assert "not a regular file" in message
            assert "two attempts" not in message
            messages.append(message)
        assert messages[0] == messages[1]
