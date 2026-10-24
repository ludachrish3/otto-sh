"""Expire a deadline on cue: the running loop's clock, stepped by the test.

A test that gives otto a short deadline (``deadline=0.1``) and asserts what
happened before it ran out is betting that the machine reaches that state in
time. A CI runner whose loop stalls for half a second loses the bet: the
deadline expires first and the test fails with nothing wrong (#705).

otto measures every deadline on the event loop's own clock (``loop.time()``)
and the timers the loop schedules from it. :func:`stepped_loop_clock`
shadows that clock with real time plus an offset the test controls, so a
test can pass a deadline no machine reaches on its own, wait for the state
it is about, then :meth:`SteppedClock.advance` past the deadline: the
expiry lands after that state, every time. The
``no-short-deadline-in-tests`` ast-grep rule points here.
"""

import asyncio
import contextlib
from collections.abc import Iterator

LONG_DEADLINE = 60.0
"""A deadline no test reaches on its own: pass it, then expire it with ``advance``."""


class SteppedClock:
    """The loop's clock plus an offset that only :meth:`advance` moves."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._real = loop.time
        self.offset = 0.0

    def time(self) -> float:
        return self._real() + self.offset

    def advance(self, seconds: float = LONG_DEADLINE) -> None:
        """Move the loop's clock forward: every timer now due fires on the loop's next pass."""
        self.offset += seconds


@contextlib.contextmanager
def stepped_loop_clock(loop: "asyncio.AbstractEventLoop | None" = None) -> Iterator[SteppedClock]:
    """Shadow *loop*'s clock (default: the running loop) with a :class:`SteppedClock`.

    Two rules follow from moving the clock every timer is measured on:

    - An advance makes EVERY pending timer that much nearer, not only the
      deadline under test. An await that is still pending when the clock
      advances -- a ``wait_for(..., 5)`` around a sweep that a callback
      expires mid-wait -- needs a bound longer than the advance, or it comes
      due at the advance too and cancels what it was waiting for. Prefer
      advancing first and starting the bounded wait after.
    - Keep every await that can schedule a timer inside the block: a timer set
      while the clock is advanced is due at an advanced time, and once the
      block restores the real clock it would fire late by the offset.
    """
    loop = loop if loop is not None else asyncio.get_running_loop()
    clock = SteppedClock(loop)
    loop.time = clock.time  # type: ignore[method-assign]  # the instance shadows the method
    try:
        yield clock
    finally:
        del loop.time
