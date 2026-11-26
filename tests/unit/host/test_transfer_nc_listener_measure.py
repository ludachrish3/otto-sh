"""The netcat backend measures its listener checks against a listener it opens.

``_measure_listener_checks`` opens a listener through ``_remote_listener``,
counts it with the checks it is given, closes it with the reap's connect and
close, and fills an ``NcListenerMeasurement`` as it goes. Each test drives it
through a fake listener command (which ends when the close connects), a fake
reap and a fake runner that answers each check from a script.
"""

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.host.errors import CommandValueError
from otto.host.transfer.nc import NcListenerMeasurement, NcRungToolError
from otto.host.transfer.nc_scripts import TOOL_FAILED, ListenerRung
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host._nc_listener_helpers import ReapRecorder, until, userland
from tests.unit.host.test_transfer_nc_early_exit import _make_ft, _no_status, _status

_PORT = 4000
_SMALL_BOUND = 0.05
_HANG = object()
"""A runner answer: the check never returns."""

_SS = ListenerRung(name="ss", present="true", script=lambda port: f"ss {port}")
_NETSTAT = ListenerRung(name="netstat", present="true", script=lambda port: f"netstat {port}")
_CHECKS = (_SS, _NETSTAT)


@pytest.fixture(autouse=True)
def _small_waits() -> Iterator[None]:
    """Every wait of the measurement small, so a timeout costs no real time."""
    with (
        patch.object(transfer_mod, "_NC_LISTENER_READY_TIMEOUT", _SMALL_BOUND),
        patch.object(transfer_mod, "_NC_LISTENER_FAST_POLL_S", 0.001),
        patch.object(transfer_mod, "_LISTENER_POLL_INTERVAL", 0.001),
        patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", _SMALL_BOUND),
    ):
        yield


def _answer(value: str) -> CommandResult:
    return CommandResult(command="", value=value, status=Status.Success, retcode=0)


class _Runner:
    """Answers each check from its own list, one answer per ask; the last repeats."""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.hanging = asyncio.Event()

    async def __call__(self, script: str) -> CommandResult:
        name = script.split(" ", 1)[0]
        self.asked.append(name)
        queue = self.answers[name]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if answer is _HANG:
            self.hanging.set()
            await asyncio.Event().wait()
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = await answer()
        await asyncio.sleep(0)
        return _answer(answer)


class _Backend(ReapRecorder):
    """An nc backend whose listener ends when the close connects, unless told otherwise."""

    def __init__(
        self,
        *,
        ending: "CommandResult | Exception | None" = None,
        at_once: bool = False,
        ends_on_close: bool = True,
        connects: bool = True,
        has_tunnel: bool = False,
    ) -> None:
        super().__init__()
        self.closed = asyncio.Event()
        self.ending = _status(0) if ending is None else ending
        self.at_once = at_once
        self.ends_on_close = ends_on_close
        self.connects = connects
        self.exec = AsyncMock(side_effect=self._listener)
        self.ft = _make_ft(self.exec, has_tunnel=has_tunnel)
        self.ft._reap_nc_listener = self.reap  # type: ignore[method-assign]

    async def _listener(self, cmd: str, **_kw: Any) -> CommandResult:
        self.events.append("spawned")
        if not self.at_once:
            try:
                await self.closed.wait()
            except asyncio.CancelledError:
                self.events.append("cancelled")
                raise
        self.events.append("ended")
        if isinstance(self.ending, Exception):
            raise self.ending
        return self.ending

    def ends_then(self, value: str) -> Any:
        """A runner answer: end the listener, then answer *value*."""

        async def answer() -> str:
            self.closed.set()
            # Enough loop turns for the listener's task to finish; the
            # assertions then show whether a check was asked after it.
            for _ in range(20):
                await asyncio.sleep(0)
            return value

        return answer

    def after_reap(self) -> None:
        if self.ends_on_close:
            self.closed.set()


async def _measure(backend: _Backend, run: _Runner, *, cap_s: int = 90) -> NcListenerMeasurement:
    into = NcListenerMeasurement(port=_PORT)
    await backend.ft._measure_listener_checks(run, _CHECKS, into, cap_s=cap_s)
    return into


@pytest.mark.asyncio
async def test_a_check_that_counts_one_is_recorded_from_the_complete_round() -> None:
    backend = _Backend()
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert into.answers == {"ss": 1, "netstat": 1}
    assert into.counted_early == {"ss": 1, "netstat": 1}
    assert not into.timed_out
    assert not into.ended_early
    assert into.was_up
    assert into.end_confirmed
    assert into.exit_status == 0
    assert backend.reaps() == 1


@pytest.mark.asyncio
async def test_a_check_blind_a_moment_before_the_bind_is_asked_again() -> None:
    into = await _measure(_Backend(), _Runner(ss=["0", "1"], netstat=["1"]))
    assert into.answers["ss"] == 1
    assert into.counted_early == {"netstat": 1}


@pytest.mark.asyncio
async def test_a_check_that_counts_two_is_recorded_as_two() -> None:
    into = await _measure(_Backend(), _Runner(ss=["2"], netstat=["1"]))
    assert into.answers == {"ss": 2, "netstat": 1}


@pytest.mark.asyncio
async def test_a_tool_failure_is_recorded_as_that_checks_error_and_the_others_are_still_asked() -> (
    None
):
    run = _Runner(ss=[TOOL_FAILED], netstat=["1"])
    into = await _measure(_Backend(), run)
    assert isinstance(into.answers["ss"], NcRungToolError)
    assert into.answers["netstat"] == 1
    garbled = await _measure(_Backend(), _Runner(ss=["1"], netstat=["not a count"]))
    assert isinstance(garbled.answers["netstat"], CommandValueError)
    assert garbled.answers["ss"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("has_tunnel", "was_up"), [(False, True), (True, False)])
async def test_no_check_counting_before_the_timeout_records_what_the_close_showed(
    has_tunnel: bool, was_up: bool
) -> None:
    # The listener ignores the close's stream: it does not end with status 0
    # after it, so only the connect can be evidence.
    backend = _Backend(has_tunnel=has_tunnel, ending=_status(1))
    into = await _measure(backend, _Runner(ss=["0"], netstat=[""]))
    assert into.timed_out
    assert into.waited_s == _SMALL_BOUND
    assert into.counted_early == {}
    assert into.was_up is was_up
    assert backend.reaps() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "words"),
    [
        (127, "'nc' exited with status 127, so it was not found on the host"),
        (1, "the listener command exited with status 1"),
    ],
)
async def test_a_listener_that_ends_before_the_close_skips_the_complete_round(
    status: int, words: str
) -> None:
    backend = _Backend(ending=_status(status), at_once=True)
    into = await _measure(backend, _Runner(ss=["0"], netstat=["0"]))
    assert into.ended_early
    assert into.exit_status == status
    assert into.ending.startswith(words)
    assert into.answers == {}
    assert backend.reaps() == 0
    assert into.end_confirmed
    assert not into.was_up


@pytest.mark.asyncio
async def test_a_listener_command_that_raised_is_recorded_by_its_cause() -> None:
    backend = _Backend(ending=OSError("channel closed"))
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert into.exit_status is None
    assert into.ending == "the 'nc' command failed: channel closed"
    assert not into.end_confirmed
    # The measurement's close, then the helper's one teardown: an end with no
    # exit status does not confirm the remote listener ended.
    assert backend.events == [
        "spawned",
        f"reap {_PORT}",
        "reaped",
        "ended",
        f"reap {_PORT}",
        "reaped",
    ]
    # Raised before the close: no close, so the helper's reap is the only connect.
    early = _Backend(ending=OSError("channel closed"), at_once=True)
    before = await _measure(early, _Runner(ss=["0"], netstat=["0"]))
    assert before.ended_early
    assert before.exit_status is None
    assert early.events == ["spawned", "ended", f"reap {_PORT}", "reaped"]


@pytest.mark.asyncio
async def test_one_that_gave_no_exit_status_is_given_no_status() -> None:
    ending = _no_status()
    backend = _Backend(ending=ending)
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert into.exit_status is None
    assert into.ending.startswith("the 'nc' command gave no exit status")
    assert not into.end_confirmed
    # The measurement's close, then the helper's one teardown: an end with no
    # exit status does not confirm the remote listener ended.
    assert backend.events == [
        "spawned",
        f"reap {_PORT}",
        "reaped",
        "ended",
        f"reap {_PORT}",
        "reaped",
    ]
    # Ended so before the close: the helper's reap is the only connect.
    early = _Backend(ending=ending, at_once=True)
    before = await _measure(early, _Runner(ss=["0"], netstat=["0"]))
    assert before.ended_early
    assert early.events == ["spawned", "ended", f"reap {_PORT}", "reaped"]


@pytest.mark.asyncio
async def test_status_zero_only_after_the_close_is_evidence_the_listener_was_up() -> None:
    # Through a hop, with no check counting: only the status 0 after the close says so.
    backend = _Backend(has_tunnel=True)
    into = await _measure(backend, _Runner(ss=["0"], netstat=["0"]))
    assert into.timed_out
    assert into.exit_status == 0
    assert not into.ended_early
    assert into.was_up
    # The same status before the close is not.
    early = await _measure(
        _Backend(at_once=True, has_tunnel=True), _Runner(ss=["0"], netstat=["0"])
    )
    assert early.ended_early
    assert early.exit_status == 0
    assert not early.was_up


@pytest.mark.asyncio
async def test_a_command_that_does_not_end_within_the_bound_leaves_the_end_unconfirmed() -> None:
    backend = _Backend(ends_on_close=False)
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert not into.end_confirmed
    assert into.exit_status is None
    assert into.ending == ""
    assert not into.ended_early
    assert into.answers == {"ss": 1, "netstat": 1}
    # The close, then the helper's one teardown of a listener still running.
    assert backend.events.count("cancelled") == 1
    assert backend.reaps() == 2


@pytest.mark.asyncio
async def test_a_cancelled_measurement_leaves_its_port_cap_and_counts_and_reaps_once() -> None:
    backend = _Backend()
    backend.ft._userland = userland("coreutils")
    run = _Runner(ss=["1"], netstat=[_HANG])
    into = NcListenerMeasurement(port=_PORT)
    running = asyncio.create_task(backend.ft._measure_listener_checks(run, _CHECKS, into, cap_s=90))
    try:
        await until(run.hanging, running)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(running), 5.0)
    finally:
        running.cancel()  # a regressed bound must not leak the task into later tests
    assert into.port == _PORT
    assert into.spawned
    assert into.capped
    assert into.counted_early == {"ss": 1}
    assert into.answers == {}
    assert backend.reaps() == 1


async def _measure_under(backend: _Backend, run: _Runner, grant: float) -> None:
    """Run the measurement under *grant*; ``wait_for`` must raise its timeout."""
    into = NcListenerMeasurement(port=_PORT)
    running = asyncio.ensure_future(
        asyncio.wait_for(backend.ft._measure_listener_checks(run, _CHECKS, into, cap_s=90), grant)
    )
    try:
        # The outer bound only catches a hang; the reap bound is shown by the
        # reap being abandoned below.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(running), 5.0)
    finally:
        running.cancel()  # a regressed bound must not leak the task into later tests


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["during the count", "during an ordinary teardown"])
async def test_a_budget_that_ends_the_measurement_is_exceeded_by_no_more_than_the_reap_bound(
    when: str, caplog: pytest.LogCaptureFixture
) -> None:
    grant = 0.05
    backend = _Backend()
    backend.reap_gate = asyncio.Event()  # never set: every reap hangs
    if when == "during the count":
        run = _Runner(ss=[_HANG], netstat=["0"])
    else:
        # A runner error leaves the body at once; the teardown's reap is in flight
        # when the grant ends.
        run = _Runner(ss=[RuntimeError("the runner broke")], netstat=["0"])
    await _measure_under(backend, run, grant)
    assert backend.reaps() == 1
    # The hanging reap was cancelled at compensate's bound, not waited out.
    assert backend.events[-1] == "abandoned"
    assert any(
        f"nc listener measurement reap (port {_PORT})" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_a_runner_error_that_is_not_the_checks_own_leaves_through_the_teardown() -> None:
    backend = _Backend()
    run = _Runner(ss=[RuntimeError("the runner broke")], netstat=["0"])
    into = NcListenerMeasurement(port=_PORT)
    with pytest.raises(RuntimeError, match="the runner broke"):
        await backend.ft._measure_listener_checks(run, _CHECKS, into, cap_s=90)
    assert backend.events[-3:] == ["cancelled", f"reap {_PORT}", "reaped"]
    assert backend.reaps() == 1


@pytest.mark.asyncio
async def test_the_listener_is_spawned_unlogged_with_the_given_cap() -> None:
    backend = _Backend()
    backend.ft._userland = userland("coreutils")
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]), cap_s=90)
    assert into.spawned
    assert into.capped
    (cmd,), kwargs = backend.exec.call_args
    assert cmd == "timeout 90 nc -l -p 4000 < /dev/null > /dev/null 2> /dev/null"
    assert kwargs["log"] is LogMode.NEVER
    # No timeout prefix where the userland has no timeout: not capped.
    bare = _Backend()
    uncapped = await _measure(bare, _Runner(ss=["1"], netstat=["1"]))
    assert uncapped.spawned
    assert not uncapped.capped
    assert bare.exec.call_args.args[0].startswith("nc -l -p 4000 ")


@pytest.mark.asyncio
async def test_the_userland_is_resolved_before_the_command_is_built() -> None:
    backend = _Backend()
    resolver = userland("absent")

    async def resolve() -> None:
        resolver.timeout_style = "coreutils"

    resolver.resolve = AsyncMock(side_effect=resolve)
    backend.ft._userland = resolver
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    resolver.resolve.assert_awaited_once()
    assert into.capped
    assert backend.exec.call_args.args[0].startswith("timeout 90 nc -l -p 4000 ")


@pytest.mark.asyncio
async def test_a_reservation_held_on_the_measured_port_is_still_held_afterwards() -> None:
    backend = _Backend()
    backend.ft._reserved_ports.add(_PORT)
    await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert _PORT in backend.ft._reserved_ports


@pytest.mark.asyncio
async def test_no_check_is_asked_after_the_listener_ends_in_an_early_round() -> None:
    backend = _Backend()
    run = _Runner(ss=[backend.ends_then("0")], netstat=["0"])
    into = await _measure(backend, run)
    assert run.asked == ["ss"]
    assert into.ended_early
    assert into.answers == {}
    assert into.counted_early == {}


@pytest.mark.asyncio
async def test_no_check_is_asked_after_the_listener_ends_in_the_complete_round() -> None:
    backend = _Backend()
    run = _Runner(ss=["1", backend.ends_then("1")], netstat=["1"])
    into = await _measure(backend, run)
    # The early round asked both; the complete round stopped after ss.
    assert run.asked == ["ss", "netstat", "ss"]
    assert into.ended_early
    # The answer given before the end stays; the round is partial.
    assert into.answers == {"ss": 1}
    assert backend.reaps() == 0


def _spawn(*, typed: bool, then: "BaseException | None") -> Any:
    """A host spawn that says the command was typed (or not), then raises *then* or fails."""

    async def spawn(cmd: str, **kw: Any) -> "asyncio.Task[CommandResult]":
        if typed:
            kw["on_typed"]()
        if then is not None:
            raise then

        async def failed_open() -> CommandResult:
            raise OSError("the session did not open")

        task = asyncio.ensure_future(failed_open())
        await asyncio.wait({task})
        return task

    return spawn


@pytest.mark.asyncio
async def test_a_spawn_cancelled_once_the_command_was_typed_says_a_listener_may_be_up() -> None:
    """The cancelled measurement still records the typed listener, and its end as unconfirmed."""
    backend = _Backend(connects=False)
    backend.ft._spawn_cmd = _spawn(typed=True, then=asyncio.CancelledError())
    into = NcListenerMeasurement(port=_PORT)
    run = _Runner(ss=["1"], netstat=["1"])
    with pytest.raises(asyncio.CancelledError):
        await backend.ft._measure_listener_checks(run, _CHECKS, into, cap_s=90)
    assert into.spawned
    assert not into.end_confirmed
    assert backend.reaps() == 1


@pytest.mark.asyncio
async def test_a_listener_whose_session_never_opened_was_not_spawned() -> None:
    """A command never typed is not a listener otto opened, so none may be left."""
    backend = _Backend()
    backend.ft._spawn_cmd = _spawn(typed=False, then=None)
    into = await _measure(backend, _Runner(ss=["0"], netstat=["0"]))
    assert not into.spawned
    assert into.ended_early
    assert into.lost
    assert into.ending == "the 'nc' command failed: the session did not open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_tunnel", "connects", "waits"),
    [(False, False, 0), (False, True, 1), (True, False, 1)],
    ids=["direct-unreached", "direct-reached", "hop-unreached"],
)
async def test_the_close_waits_for_the_listener_only_when_a_connection_may_have_reached_it(
    has_tunnel: bool, connects: bool, waits: int
) -> None:
    """On a direct route a close that could not connect reached nothing, so nothing is awaited."""
    backend = _Backend(has_tunnel=has_tunnel, connects=connects, ends_on_close=False)
    real_wait = asyncio.wait
    closes: list[float] = []

    async def counting_wait(fs: Any, *args: Any, **kw: Any) -> Any:
        if kw.get("timeout") == transfer_mod._INTERRUPTED_REAP_TIMEOUT and len(fs) == 1:
            closes.append(kw["timeout"])
        return await real_wait(fs, *args, **kw)

    with patch.object(transfer_mod.asyncio, "wait", counting_wait):
        into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert len(closes) == waits
    assert not into.end_confirmed
    assert into.answers == {"ss": 1, "netstat": 1}


@pytest.mark.asyncio
async def test_the_measurement_carries_the_bare_listener_command_from_the_builder() -> None:
    backend = _Backend()
    backend.ft._userland = userland("coreutils")
    into = await _measure(backend, _Runner(ss=["1"], netstat=["1"]))
    assert into.bare_command == backend.ft._bare_listener_command(_PORT) == "nc -l -p 4000"
    assert backend.exec.call_args.args[0] == backend.ft._listener_command(
        _PORT, transfer_mod._ALL_STREAMS_DROPPED, cap_s=90
    )
