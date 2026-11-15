"""The collector's loop supervisor: no sibling loop may outlive a failure.

Guards the Tier 0.3 fix — a bare ``asyncio.gather`` re-raises the first
exception while orphaning its remaining tasks; ``_gather_cancelling_siblings``
must cancel and await every sibling on any exit path.
"""

import asyncio

import pytest

from otto.lifecycle import compensate, held_cancellation
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


class _StopsOnCancelHost:
    """A host whose second ``run`` is in flight until cancelled, then stops its command.

    The stop takes a timer's worth of time, as a shell session's does (the
    interrupt, the settle, the probe): a supervisor that lets the cancel
    return before it ends has left the stop running unowned.
    """

    id = "fake"
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.in_flight = asyncio.Event()
        self.stopped = False

    async def run(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        if self.calls == 1:
            return  # the initial collection returns; the next tick hangs
        self.in_flight.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            # Held, as a shell session's stop is: a second cancel waits for it.
            await compensate(self._stop(), held=held_cancellation(exc))
            raise

    async def _stop(self) -> None:
        await asyncio.sleep(0.05)
        self.stopped = True


@pytest.mark.asyncio
async def test_cancelled_collection_waits_for_the_command_stop():
    """Cancelling collection mid-tick returns only once the in-flight command's stop ended.

    The tick sleeps and collects side by side; cancelling it cancels the
    sleep at once, and a bare ``gather`` then returned with the collect still
    stopping its command, which the loop's close later cut short.
    """
    from datetime import timedelta

    from otto.monitor.collector import MetricCollector, MonitorTarget
    from otto.monitor.parsers import MetricParser

    class _Parser(MetricParser):
        chart = "T"
        y_title = "V"
        unit = ""
        command = "true"

        def parse(self, output, *, ctx):
            return None

    host = _StopsOnCancelHost()
    collector = MetricCollector(targets=[MonitorTarget(host=host, parsers={"true": _Parser()})])  # ty: ignore[invalid-argument-type] — duck-typed host
    task = asyncio.create_task(collector.run(interval=timedelta(seconds=1)))
    await host.in_flight.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert host.stopped, "collection's cancel returned while the command was still being stopped"
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


@pytest.mark.asyncio
async def test_normal_completion_passes_through():
    done: list[int] = []

    async def quick(i: int) -> None:
        done.append(i)

    await _gather_cancelling_siblings([quick(1), quick(2)])
    assert sorted(done) == [1, 2]
