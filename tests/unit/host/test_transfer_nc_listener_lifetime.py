"""A remote netcat listener is spelled in one place and lives through one helper.

``_listener_command`` is the one spelling of a listener; ``_remote_listener``
is the one lifetime: the spawn, one teardown attempt whatever the exit, then
the unforward and (when the caller reserved it) the port's release. Each test
drives the helper through fakes of the host's exec and spawn and records the
order of what it did.
"""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import otto.host.transfer.nc as transfer_mod
from otto.result import CommandResult
from tests.unit.host._nc_listener_helpers import ReapRecorder, until, userland
from tests.unit.host.test_transfer_nc_early_exit import _make_ft, _no_status, _status

_PORT = 4000
_LISTENER = "nc -l -p 4000 < /dev/null > /dev/null 2> /dev/null"
_SMALL_BOUND = 0.05


class _Backend(ReapRecorder):
    """An nc backend whose listener, reap, unforward and release are recorded in order."""

    def __init__(self, listener: Callable[..., Awaitable[CommandResult]]) -> None:
        super().__init__()
        self.ft = _make_ft(AsyncMock(side_effect=listener))
        self.ft._connections.unforward_port = MagicMock(
            side_effect=lambda **kw: self.events.append(f"unforward {kw['dest_port']}")
        )
        real_release = self.ft._release_port

        def release(port: int) -> None:
            self.events.append(f"release {port}")
            real_release(port)

        self.ft._release_port = release  # type: ignore[method-assign]
        self.ft._reserved_ports.add(_PORT)
        self.ft._reap_nc_listener = self.reap  # type: ignore[method-assign]


def _running_listener(events: list[str]) -> Callable[..., Awaitable[CommandResult]]:
    async def listener(cmd: str, **_kw: Any) -> CommandResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        raise AssertionError("unreachable")

    return listener


def _ended_listener(ending: "CommandResult | Exception") -> Callable[..., Awaitable[CommandResult]]:
    async def listener(cmd: str, **_kw: Any) -> CommandResult:
        if isinstance(ending, Exception):
            raise ending
        return ending

    return listener


def _running_backend() -> _Backend:
    events: list[str] = []
    backend = _Backend(_running_listener(events))
    backend.events = events
    return backend


_TORN_DOWN = [f"unforward {_PORT}", f"release {_PORT}"]


def test_the_listener_command_is_spelled_in_one_place() -> None:
    redirect = "< /dev/null > /dev/null 2> /dev/null"
    coreutils = _make_ft(AsyncMock())
    coreutils._userland = userland("coreutils")
    assert coreutils._listener_command(_PORT, redirect) == (
        "timeout 3600 nc -l -p 4000 < /dev/null > /dev/null 2> /dev/null"
    )
    dash_t = _make_ft(AsyncMock())
    dash_t._userland = userland("dash-t")
    assert dash_t._listener_command(_PORT, redirect, cap_s=90).startswith(
        "timeout -t 90 nc -l -p 4000 "
    )
    absent = _make_ft(AsyncMock())
    absent._userland = userland("absent")
    assert absent._listener_command(_PORT, redirect).startswith("nc -l -p 4000 ")


@pytest.mark.asyncio
async def test_a_listener_that_gave_its_status_is_not_reaped() -> None:
    backend = _Backend(_ended_listener(_status(0)))
    async with backend.ft._remote_listener(_PORT, _LISTENER) as task:
        assert (await task).retcode == 0
    assert backend.events == _TORN_DOWN
    assert _PORT not in backend.ft._reserved_ports


@pytest.mark.asyncio
async def test_a_listener_still_running_is_cancelled_and_reaped_once_on_exit() -> None:
    backend = _running_backend()
    async with backend.ft._remote_listener(_PORT, _LISTENER):
        await asyncio.sleep(0)
    assert backend.events == ["cancelled", f"reap {_PORT}", "reaped", *_TORN_DOWN]


@pytest.mark.asyncio
async def test_a_listener_that_ended_with_no_exit_status_is_still_reaped() -> None:
    ending = _no_status()
    backend = _Backend(_ended_listener(ending))
    async with backend.ft._remote_listener(_PORT, _LISTENER) as task:
        await task
    assert backend.events == [f"reap {_PORT}", "reaped", *_TORN_DOWN]


@pytest.mark.asyncio
async def test_a_listener_whose_command_raised_is_still_reaped() -> None:
    backend = _Backend(_ended_listener(OSError("channel closed")))
    async with backend.ft._remote_listener(_PORT, _LISTENER) as task:
        await asyncio.wait({task})
    assert backend.events == [f"reap {_PORT}", "reaped", *_TORN_DOWN]


async def _enter_and_wait(backend: _Backend, entered: asyncio.Event, **kw: Any) -> None:
    async with backend.ft._remote_listener(_PORT, _LISTENER, **kw):
        entered.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_cancelled_body_reaps_once_under_the_bound_and_reraises_the_cancellation() -> None:
    backend = _running_backend()
    entered = asyncio.Event()
    running = asyncio.create_task(_enter_and_wait(backend, entered))
    await until(entered, running)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert backend.events == ["cancelled", f"reap {_PORT}", "reaped", *_TORN_DOWN]

    # The same, with a reap that never finishes: the bound ends it.
    hung = _running_backend()
    hung.reap_gate = asyncio.Event()
    entered = asyncio.Event()
    with patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", _SMALL_BOUND):
        running = asyncio.create_task(_enter_and_wait(hung, entered))
        await until(entered, running)
        running.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 5.0)
        finally:
            running.cancel()  # a regressed bound must not leak the task into later tests
    assert hung.reaps() == 1
    assert "reaped" not in hung.events
    assert hung.events[-2:] == _TORN_DOWN


async def _leave_with_an_error(backend: _Backend) -> None:
    async with backend.ft._remote_listener(_PORT, _LISTENER):
        raise RuntimeError("the data phase failed")


@pytest.mark.asyncio
async def test_a_cancellation_during_the_ordinary_teardown_does_not_tear_the_reap() -> None:
    backend = _running_backend()
    backend.reap_gate = asyncio.Event()
    running = asyncio.create_task(_leave_with_an_error(backend))
    await until(backend.reaping, running)
    running.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not running.done(), "the held cancellation waits for the reap"
    backend.reap_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert backend.events == ["cancelled", f"reap {_PORT}", "reaped", *_TORN_DOWN]


@pytest.mark.asyncio
async def test_a_cancellation_during_an_ordinary_teardown_that_hangs_is_bounded() -> None:
    backend = _running_backend()
    backend.reap_gate = asyncio.Event()  # never set: the reap never finishes
    loop = asyncio.get_running_loop()
    with patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", _SMALL_BOUND):
        running = asyncio.create_task(_leave_with_an_error(backend))
        await until(backend.reaping, running)
        cancelled_at = loop.time()
        running.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 5.0)
        finally:
            running.cancel()  # a regressed bound must not leak the task into later tests
    assert loop.time() - cancelled_at >= _SMALL_BOUND, "the bound counts from the cancellation"
    assert backend.reaps() == 1
    assert "reaped" not in backend.events
    assert backend.events[-2:] == _TORN_DOWN


def _spawn_that_is_cancelled(*, typed: bool) -> Callable[..., Awaitable[Any]]:
    async def spawn(cmd: str, **kw: Any) -> "asyncio.Task[CommandResult]":
        if typed:
            kw["on_typed"]()
        # The host's spawn cancelled its call and its stop failed to say more.
        raise asyncio.CancelledError

    return spawn


@pytest.mark.asyncio
async def test_a_spawn_cancelled_before_the_command_was_typed_reaps_nothing() -> None:
    backend = _running_backend()
    backend.ft._spawn_cmd = _spawn_that_is_cancelled(typed=False)
    with pytest.raises(asyncio.CancelledError):
        async with backend.ft._remote_listener(_PORT, _LISTENER):
            raise AssertionError("no task was handed back")
    assert backend.events == _TORN_DOWN


@pytest.mark.asyncio
async def test_a_spawn_cancelled_after_the_command_was_typed_reaps_the_port_once() -> None:
    backend = _running_backend()
    backend.ft._spawn_cmd = _spawn_that_is_cancelled(typed=True)
    with pytest.raises(asyncio.CancelledError):
        async with backend.ft._remote_listener(_PORT, _LISTENER):
            raise AssertionError("no task was handed back")
    assert backend.events == [f"reap {_PORT}", "reaped", *_TORN_DOWN]

    # Under the bound: a reap that never finishes is abandoned at it.
    hung = _running_backend()
    hung.ft._spawn_cmd = _spawn_that_is_cancelled(typed=True)
    hung.reap_gate = asyncio.Event()

    async def enter() -> None:
        async with hung.ft._remote_listener(_PORT, _LISTENER):
            raise AssertionError("no task was handed back")

    with patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", _SMALL_BOUND):
        running = asyncio.create_task(enter())
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 5.0)
        finally:
            running.cancel()  # a regressed bound must not leak the task into later tests
    assert hung.reaps() == 1
    assert hung.events[-2:] == _TORN_DOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["reap", "teardown"])
async def test_the_port_is_unforwarded_and_released_when_the_reap_raises(where: str) -> None:
    backend = _running_backend()
    if where == "reap":
        backend.reap_error = OSError("forward refused")
        async with backend.ft._remote_listener(_PORT, _LISTENER):
            pass
    else:
        backend.ft._cancel_and_reap = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("teardown failed")
        )
        with pytest.raises(RuntimeError, match="teardown failed"):
            async with backend.ft._remote_listener(_PORT, _LISTENER):
                pass
    assert backend.events[-2:] == _TORN_DOWN


@pytest.mark.asyncio
async def test_a_caller_that_reserved_nothing_releases_nothing() -> None:
    """The reservation a transfer holds stays, and so does that transfer's forward."""
    backend = _Backend(_ended_listener(_status(0)))
    async with backend.ft._remote_listener(_PORT, _LISTENER, owns_reservation=False) as task:
        await task
    assert _PORT in backend.ft._reserved_ports
    assert backend.events == []


@pytest.mark.asyncio
async def test_a_caller_that_reserved_nothing_still_hands_back_a_forward_nobody_else_holds() -> (
    None
):
    """With no reservation on the port, the forward can only be the caller's own reap's."""
    backend = _running_backend()
    backend.ft._reserved_ports.discard(_PORT)
    async with backend.ft._remote_listener(_PORT, _LISTENER, owns_reservation=False):
        await asyncio.sleep(0)
    assert backend.events == ["cancelled", f"reap {_PORT}", "reaped", f"unforward {_PORT}"]


@pytest.mark.asyncio
async def test_the_caller_hears_when_the_command_was_typed() -> None:
    """The caller's hook runs when the host's spawn says the command was typed, and only then."""
    for typed in (True, False):
        backend = _running_backend()
        backend.ft._spawn_cmd = _spawn_that_is_cancelled(typed=typed)
        heard: list[str] = []
        with pytest.raises(asyncio.CancelledError):
            async with backend.ft._remote_listener(
                _PORT, _LISTENER, on_typed=partial(heard.append, "typed")
            ):
                raise AssertionError("no task was handed back")
        assert heard == (["typed"] if typed else [])


@pytest.mark.asyncio
async def test_the_reap_says_whether_it_connected() -> None:
    ft = _make_ft(AsyncMock())
    ft._connections.ip = "127.0.0.1"

    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    try:
        open_port = server.sockets[0].getsockname()[1]
        assert await ft._reap_nc_listener(open_port) is True
    finally:
        server.close()
        await server.wait_closed()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    real_connect = transfer_mod._connect_with_retry

    async def short_connect(host: str, port: int, timeout: float = 5.0) -> Any:
        return await real_connect(host, port, timeout=_SMALL_BOUND)

    with patch.object(transfer_mod, "_connect_with_retry", new=short_connect):
        assert await ft._reap_nc_listener(closed_port) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["nc put listener reap", "nc get listener reap"])
async def test_an_abandoned_reap_is_named_by_the_label_it_was_given(
    label: str, caplog: pytest.LogCaptureFixture
) -> None:
    backend = _running_backend()
    backend.reap_gate = asyncio.Event()  # never set: the reap is abandoned at the bound
    entered = asyncio.Event()

    async def enter() -> None:
        async with backend.ft._remote_listener(_PORT, _LISTENER, label=label):
            entered.set()
            await asyncio.Event().wait()

    with patch.object(transfer_mod, "_INTERRUPTED_REAP_TIMEOUT", _SMALL_BOUND):
        running = asyncio.create_task(enter())
        await until(entered, running)
        running.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(running), 5.0)
        finally:
            running.cancel()  # a regressed bound must not leak the task into later tests
    assert any(f"{label} (port {_PORT})" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]
