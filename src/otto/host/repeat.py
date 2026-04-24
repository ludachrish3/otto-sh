"""
Periodic background task runner for RemoteHost.

RepeatRunner owns the repeat-task lifecycle: starting named periodic coroutines,
storing their results in a bounded deque, and cancelling them cleanly.

It is decoupled from SSH/telnet — it receives a ``run_cmds`` coroutine factory
so it can be tested with an AsyncMock without any real connection.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..utils import CommandStatus, Status
from .host import RunResult

# deque of (timestamp, results) pairs for one named repeat task
MetricResult = deque[tuple[datetime, list[CommandStatus]]]


class RepeatRunner:
    """Runs named periodic command tasks and stores their results.

    Parameters
    ----------
    run_cmds:
        Async callable that accepts a ``list[str] | str`` and returns a
        :class:`RunResult`.  Typically bound to ``Host.run``.
    """

    def __init__(
        self,
        run_cmds: Callable[..., Any],
    ) -> None:
        self._run_cmds = run_cmds
        self._repeat_tasks: dict[str, asyncio.Task[Any]] = {}
        self._repeat_results: dict[str, MetricResult] = {}

    # ------------------------------------------------------------------
    # Internal coroutine
    # ------------------------------------------------------------------

    async def _run_loop(
        self,
        name: str,
        cmds: list[str] | str,
        interval: timedelta,
        times: int,
        duration: timedelta,
        until: datetime,
        on_result: Callable[[str, datetime, list[CommandStatus]], None] | None,
    ) -> None:
        if interval <= timedelta():
            raise ValueError('Command interval must be a positive time interval')

        try:
            duration_end_time = datetime.now() + duration
        except OverflowError:
            duration_end_time = datetime.max

        end_time = min(duration_end_time, until)
        times_remaining = times

        while datetime.now() < end_time and times_remaining != 0:
            try:
                # Run command and interval sleep concurrently.  If the command
                # finishes before the interval we still wait; if it takes longer
                # we proceed immediately — no overlapping tasks on the same
                # session.  Mirrors the pattern in MetricCollector.run().
                results = await asyncio.gather(
                    self._run_cmds(cmds),
                    asyncio.sleep(interval.total_seconds()),
                    return_exceptions=True,
                )
                cmd_result = results[0]
                if not isinstance(cmd_result, BaseException):
                    ts = datetime.now()
                    cmd_statuses = cmd_result.statuses
                    if name in self._repeat_results:
                        self._repeat_results[name].append((ts, cmd_statuses))
                    if on_result is not None:
                        on_result(name, ts, cmd_statuses)
                times_remaining -= 1

            except asyncio.CancelledError:
                break

        # pop() with default avoids race conditions with stop()
        self._repeat_tasks.pop(name, None)

    # ------------------------------------------------------------------
    # Done-callback
    # ------------------------------------------------------------------

    def _store_result(
        self,
        name: str,
        task: 'asyncio.Task[RunResult]',
        on_result: Callable[[str, datetime, list[CommandStatus]], None] | None,
    ) -> None:
        """Store result and fire optional user callback."""
        if task.cancelled() or task.exception() is not None:
            return
        results = task.result().statuses
        ts = datetime.now()
        if name in self._repeat_results:
            self._repeat_results[name].append((ts, results))
        if on_result is not None:
            on_result(name, ts, results)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        name: str,
        cmds: list[str] | str,
        interval: timedelta,
        times: int = -1,
        duration: timedelta = timedelta.max,
        until: datetime = datetime.max,
        on_result: Callable[[str, datetime, list[CommandStatus]], None] | None = None,
        max_history: int = 1000,
    ) -> None:
        """Start a named periodic task. Raises if that name is already running."""
        if name in self._repeat_tasks and not self._repeat_tasks[name].done():
            raise RuntimeError(f"Periodic task {name!r} is already running")

        self._repeat_results[name] = deque(maxlen=max_history)

        self._repeat_tasks[name] = asyncio.create_task(
            self._run_loop(
                cmds=cmds, interval=interval, times=times, duration=duration,
                until=until, name=name, on_result=on_result,
            ),
            name=name,
        )

    async def stop(self, name: str) -> None:
        """Cancel a specific named periodic task."""
        task = self._repeat_tasks.pop(name, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_all(self) -> None:
        """Cancel all running periodic tasks."""
        await asyncio.gather(*[
            self.stop(name)
            for name in list(self._repeat_tasks)
        ])

    def get_results(
        self, name: str
    ) -> list[tuple[datetime, list[CommandStatus]]]:
        """Return a snapshot of stored results for a named repeat task."""
        return list(self._repeat_results.get(name, []))
