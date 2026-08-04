"""The collector's loop supervisor: no sibling loop may outlive a failure.

Guards the Tier 0.3 fix — a bare ``asyncio.gather`` re-raises the first
exception while orphaning its remaining tasks; ``_gather_cancelling_siblings``
must cancel and await every sibling on any exit path.
"""

import asyncio

import pytest

from otto.monitor.collector import _gather_cancelling_siblings


@pytest.mark.asyncio
async def test_failing_loop_cancels_siblings():
    cancelled = asyncio.Event()

    async def failing() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("bucket died")

    async def forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(RuntimeError, match="bucket died"):
        await _gather_cancelling_siblings([failing(), forever()])
    assert cancelled.is_set(), "sibling loop was orphaned, not cancelled"


@pytest.mark.asyncio
async def test_outer_cancellation_reaches_every_loop():
    reached: list[str] = []

    async def loop(name: str) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            reached.append(name)
            raise

    task = asyncio.create_task(_gather_cancelling_siblings([loop("a"), loop("b")]))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(reached) == ["a", "b"]


@pytest.mark.asyncio
async def test_normal_completion_passes_through():
    done: list[int] = []

    async def quick(i: int) -> None:
        done.append(i)

    await _gather_cancelling_siblings([quick(1), quick(2)])
    assert sorted(done) == [1, 2]
