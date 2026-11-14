"""compensate(held=): a cancellation the caller already caught is the one that leaves.

A cleanup run from ``except BaseException as exc:`` passes the caught
cancellation in; a cleanup failure, a later cancellation or an expiry of
either bound never replaces it (D3, M10). Without *held* the call is
unchanged (``test_lifecycle_compensate.py`` pins that path).
Deterministic: expiry is driven by ``deadline=0`` / ``timeout=0``.
"""

import asyncio

import pytest

from otto.lifecycle import compensate, held_cancellation

pytestmark = [pytest.mark.asyncio]


async def test_compensate_with_a_held_cancellation_logs_a_failure_and_reraises_the_cancellation(
    caplog,
):
    async def failing() -> None:
        await asyncio.sleep(0)
        raise OSError("x")

    with pytest.raises(asyncio.CancelledError):
        await compensate(failing(), timeout=1, what="w", held=asyncio.CancelledError())
    assert "w failed during shielded unwind" in caplog.text


async def test_compensate_with_a_held_cancellation_runs_the_work_to_completion():
    done: "list[int]" = []

    async def work() -> None:
        await asyncio.sleep(0)
        done.append(1)

    with pytest.raises(asyncio.CancelledError):
        await compensate(work(), held=asyncio.CancelledError())
    assert done == [1]


async def test_compensate_with_a_held_cancellation_arms_its_deadline_at_once():
    async def forever() -> None:
        await asyncio.Event().wait()

    with pytest.raises(asyncio.CancelledError):
        # Returns on the next loop turn.
        await compensate(forever(), deadline=0, held=asyncio.CancelledError())


@pytest.mark.parametrize("bound", [{"deadline": 0}, {"timeout": 0}])
async def test_an_expiry_reraises_the_same_held_object_and_leaves_no_task(bound):
    held = asyncio.CancelledError("first")

    async def forever() -> None:
        await asyncio.Event().wait()

    # Awaited directly: identity is observable here.
    with pytest.raises(asyncio.CancelledError) as caught:
        await compensate(forever(), held=held, **bound)
    assert caught.value is held
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_a_later_cancellation_does_not_replace_the_held_one():
    held, gate = asyncio.CancelledError("first"), asyncio.Event()
    seen: "list[BaseException]" = []
    cleaned: "list[int]" = []

    async def work() -> None:
        await gate.wait()
        cleaned.append(1)

    async def owner() -> None:  # identity asserted INSIDE the awaiting coroutine
        try:
            await compensate(work(), held=held)
        except asyncio.CancelledError as e:
            seen.append(e)
            raise

    task = asyncio.ensure_future(owner())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):  # outside: a task re-wraps it (Python 3.10)
        await task
    assert seen == [held]
    assert seen[0] is held
    assert cleaned == [1]


async def test_without_held_result_and_failure_pass_through_unchanged():
    async def ok() -> int:
        return 7

    async def bad() -> None:
        raise OSError("x")

    assert await compensate(ok()) == 7
    with pytest.raises(OSError, match="x"):
        await compensate(bad())


async def test_held_cancellation_keeps_only_a_cancellation():
    cancel = asyncio.CancelledError()
    assert held_cancellation(cancel) is cancel
    assert held_cancellation(OSError("x")) is None
