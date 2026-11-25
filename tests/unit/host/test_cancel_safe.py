"""Bounded awaits never drop the caller's cancellation (``otto.host.cancel_safe``).

On Python 3.10 and 3.11 ``asyncio.wait_for`` returns the inner result when the
inner await finishes in the loop turn its caller is cancelled. Every case here
puts the cancel in that turn, or behind an inner await that drops one itself.
"""

import asyncio
import contextlib
from typing import Any

import pytest

from otto.host.cancel_safe import await_within, result_within


def _finishing_as_its_waiter_is_cancelled(
    waiter: "dict[str, asyncio.Task[Any]]",
) -> "asyncio.Future[str]":
    """A future that gets its result in the same loop turn it cancels the task waiting on it.

    The turn Python 3.10's ``wait_for`` answers with the result: the inner
    await is done, and the waiter's cancellation is already requested.
    """
    loop = asyncio.get_running_loop()
    inner: "asyncio.Future[str]" = loop.create_future()

    def finish() -> None:
        inner.set_result("read")
        waiter["task"].cancel()

    loop.call_soon(finish)
    return inner


@pytest.mark.asyncio
async def test_result_within_raises_a_cancel_that_lands_as_the_inner_await_finishes() -> None:
    """The cancellation wins, and the finished result is dropped."""
    waiter: "dict[str, asyncio.Task[Any]]" = {}

    async def bounded() -> str:
        return await result_within(_finishing_as_its_waiter_is_cancelled(waiter), 30.0)

    waiter["task"] = asyncio.ensure_future(bounded())
    done, _ = await asyncio.wait({waiter["task"]}, timeout=5.0)
    assert done, "the bounded wait neither returned nor raised"
    assert waiter["task"].cancelled()


@pytest.mark.asyncio
async def test_control_the_cancel_does_land_in_the_turn_the_inner_await_finishes() -> None:
    """The arrangement above is the one the standard ``wait_for`` used to lose a cancel in.

    With the same future, a plain ``await`` sees the cancellation too: the
    cancel really is requested before the waiter runs again. Without this,
    the test above would pass on a cancel that simply arrived late.
    """
    waiter: "dict[str, asyncio.Task[Any]]" = {}
    seen: list[str] = []

    async def plain() -> None:
        inner = _finishing_as_its_waiter_is_cancelled(waiter)
        try:
            await asyncio.shield(inner)
        except asyncio.CancelledError:
            seen.append("done" if inner.done() else "pending")
            raise

    waiter["task"] = asyncio.ensure_future(plain())
    await asyncio.wait({waiter["task"]}, timeout=5.0)
    assert seen == ["done"]


@pytest.mark.asyncio
async def test_result_within_returns_the_result_and_raises_the_inner_error() -> None:
    async def answers() -> str:
        return "answer"

    async def fails() -> str:
        raise ConnectionResetError("peer went away")

    assert await result_within(answers(), 30.0) == "answer"
    assert await result_within(answers(), None) == "answer"
    with pytest.raises(ConnectionResetError):
        await result_within(fails(), 30.0)


@pytest.mark.asyncio
async def test_result_within_times_out_with_the_inner_await_cancelled() -> None:
    inner: "dict[str, asyncio.Task[Any]]" = {}

    async def never() -> None:
        task = asyncio.current_task()
        assert task is not None
        inner["task"] = task
        await asyncio.get_running_loop().create_future()

    with pytest.raises(asyncio.TimeoutError):
        await result_within(never(), 0.01)
    assert inner["task"].cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, 30.0], ids=["no_timeout", "timeout"])
async def test_await_within_re_cancels_an_inner_await_that_swallows_the_first_cancel(
    timeout: float | None,
) -> None:
    """The caller's cancel propagates at once even when the inner await drops one.

    telnetlib3's own ``asyncio.wait_for`` on Python 3.10 and 3.11 can swallow
    the first cancellation and carry on into a long wait (its negotiation, up
    to ``connect_maxwait``). One cancel of the inner await would leave the
    caller's cancellation waiting that out.
    """
    loop = asyncio.get_running_loop()
    inner: dict[str, asyncio.Task[Any]] = {}
    started = asyncio.Event()

    async def swallows_one_cancel() -> None:
        task = asyncio.current_task()
        assert task is not None
        inner["task"] = task
        started.set()
        # Dropped, as 3.10/3.11 ``wait_for`` does in the turn its inner await finishes.
        with contextlib.suppress(asyncio.CancelledError):
            await loop.create_future()
        await asyncio.sleep(30)

    outer = asyncio.create_task(await_within(swallows_one_cancel(), timeout))
    await started.wait()
    await asyncio.sleep(0)
    outer.cancel()
    cancelled_at = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(outer, 5.0)

    assert loop.time() - cancelled_at < 1.0
    assert inner["task"].done()


@pytest.mark.asyncio
async def test_await_within_timeout_re_cancels_an_inner_await_that_swallows_the_first_cancel() -> (
    None
):
    """A timeout ends the wait promptly too, with the swallowing inner await finished."""
    loop = asyncio.get_running_loop()
    inner: dict[str, asyncio.Task[Any]] = {}

    async def swallows_one_cancel() -> None:
        task = asyncio.current_task()
        assert task is not None
        inner["task"] = task
        with contextlib.suppress(asyncio.CancelledError):
            await loop.create_future()
        await asyncio.sleep(30)

    started = loop.time()
    outcome = await asyncio.wait_for(await_within(swallows_one_cancel(), 0.05), 5.0)

    assert outcome is None
    assert loop.time() - started < 1.0
    assert inner["task"].done()


@pytest.mark.parametrize("timeout", [0, -1.0], ids=["zero", "negative"])
@pytest.mark.asyncio
async def test_result_within_with_no_time_left_never_starts_its_await(timeout: float) -> None:
    """As ``asyncio.wait_for`` does: the coroutine is cancelled before its first step."""
    ran: list[bool] = []

    async def body() -> str:
        ran.append(True)
        return "ran"

    with pytest.raises(asyncio.TimeoutError):
        await result_within(body(), timeout)
    assert ran == []


@pytest.mark.asyncio
async def test_await_within_with_no_time_left_is_none() -> None:
    ran: list[bool] = []

    async def body() -> None:
        ran.append(True)

    assert await await_within(body(), 0) is None
    assert ran == []


@pytest.mark.asyncio
async def test_a_finished_future_with_no_time_left_keeps_its_result() -> None:
    done: "asyncio.Future[str]" = asyncio.get_running_loop().create_future()
    done.set_result("ready")
    assert await result_within(done, 0) == "ready"


@pytest.mark.asyncio
async def test_a_caller_cancelled_with_no_time_left_is_still_cancelled() -> None:
    """The caller's cancel lands while the never-started await is joined."""

    async def bounded() -> None:
        await result_within(asyncio.Event().wait(), 0)

    task = asyncio.ensure_future(bounded())
    await asyncio.sleep(0)  # the task's first step: result_within is joining the cancelled await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
