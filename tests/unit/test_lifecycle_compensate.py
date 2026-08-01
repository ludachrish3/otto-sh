"""compensate(): rollback/undo runs to completion even when the caller is cancelled.

Chaos spec (shielded compensating actions): an interrupt mid-compensation
must not tear the rollback; a hung rollback is bounded by a deadline; both
paths re-raise the held cancellation once the compensation resolves.
Deterministic: expiry is driven by ``deadline=0`` (the call_later fires on
the next loop turn), never by wall-clock waits.
"""

import asyncio

import pytest

from otto.lifecycle import compensate


@pytest.mark.asyncio
async def test_result_passthrough_without_cancellation():
    async def rollback() -> str:
        return "undone"

    assert await compensate(rollback(), deadline=60.0, what="test rollback") == "undone"


@pytest.mark.asyncio
async def test_exception_passthrough_without_cancellation():
    async def rollback() -> None:
        raise ValueError("undo failed")

    with pytest.raises(ValueError, match="undo failed"):
        await compensate(rollback(), deadline=60.0, what="test rollback")


@pytest.mark.asyncio
async def test_cancellation_is_held_until_the_rollback_completes():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the cancel land in compensate's shield
    assert not done  # rollback still parked on its event — held, not torn, not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]  # the rollback finished BEFORE the cancellation re-raised


@pytest.mark.asyncio
async def test_second_cancellation_does_not_tear_the_rollback():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # a second cancel is the shield's whole point
    await asyncio.sleep(0)
    assert not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]


@pytest.mark.asyncio
async def test_deadline_abandons_a_hung_rollback(caplog):
    hung = asyncio.Event()  # never set

    async def rollback() -> None:
        await hung.wait()

    task = asyncio.ensure_future(compensate(rollback(), deadline=0.0, what="hung rollback"))
    await asyncio.sleep(0)  # rollback parked
    task.cancel()  # holds the cancel and arms the deadline; 0.0 fires next loop turn
    with (
        caplog.at_level("WARNING", logger="otto.lifecycle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert any("hung rollback" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_wins_over_a_late_rollback_failure(caplog):
    started = asyncio.Event()
    release = asyncio.Event()

    async def rollback() -> None:
        started.set()
        await release.wait()
        raise ValueError("undo failed late")

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="late failure"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with (
        caplog.at_level("WARNING", logger="otto.lifecycle"),
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert any("late failure" in r.message for r in caplog.records)
