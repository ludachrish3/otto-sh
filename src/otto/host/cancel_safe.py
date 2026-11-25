"""Bounded awaits that never drop the caller's cancellation.

On Python 3.10 and 3.11, ``asyncio.wait_for`` returns the inner result when
the inner await completes in the same loop turn the caller is cancelled, and
the cancellation is lost: the caller carries on as if nobody had cancelled it.
That turn is not rare on a session: one packet from a host can complete a read
in one task and, in another, the read whose answer makes that task cancel the
first.

:func:`result_within` is the replacement where ``wait_for``'s result or
:exc:`asyncio.TimeoutError` is wanted, :func:`await_within` where the caller
reads the finished future itself.
"""

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

_R = TypeVar("_R")


async def await_within(
    awaitable: Awaitable[Any], timeout: float | None
) -> "asyncio.Future[Any] | None":
    """Await *awaitable* for up to *timeout*: its finished future, or None on a timeout.

    A *timeout* of None waits for as long as *awaitable* runs, so only its
    end or the caller's cancellation ends the wait. A *timeout* of zero or
    less never starts *awaitable*: unless it is already done, it is cancelled
    before its first step, as ``asyncio.wait_for`` does.

    The caller's cancellation always propagates. On a timeout or a
    cancellation the inner await is cancelled and awaited, so nothing stays
    pending behind the call. It is cancelled again for as long as it keeps
    running (see :func:`cancel_until_done`). The caller reads the outcome
    with ``.result()``, which re-raises the inner await's own error.
    """
    inner = asyncio.ensure_future(awaitable)
    if timeout is not None and timeout <= 0 and not inner.done():
        await cancel_until_done(inner)
        return None if inner.cancelled() else inner
    try:
        done, _ = await asyncio.wait({inner}, timeout=timeout)
    except asyncio.CancelledError:
        await cancel_until_done(inner)
        raise
    if not done:
        await cancel_until_done(inner)
        if inner.cancelled():
            return None
    return inner


async def result_within(awaitable: Awaitable[_R], timeout: float | None) -> _R:
    """Return *awaitable*'s result, or raise :exc:`asyncio.TimeoutError` after *timeout*.

    What ``asyncio.wait_for`` does, through :func:`await_within`: a
    cancellation of the caller is raised even when the inner await finished
    in the same loop turn, and its result is then dropped. An inner await
    that finishes while the timeout cancels it keeps its result. With no
    time left (a *timeout* of zero or less) it is never started.
    """
    finished = await await_within(awaitable, timeout)
    if finished is None:
        raise asyncio.TimeoutError
    return finished.result()


RECANCEL_INTERVAL: float = 0.05
"""How long :func:`cancel_until_done` waits on a cancelled await before cancelling it again."""


async def cancel_until_done(inner: "asyncio.Future[Any]") -> None:
    """Cancel *inner* until it has finished, then retrieve its outcome.

    One cancel is not always enough: a library's own ``asyncio.wait_for``
    (telnetlib3's opener runs its TCP setup under one) can drop a cancel on
    Python 3.10 and 3.11 and carry on into a long wait. So *inner* is
    cancelled again every :data:`RECANCEL_INTERVAL` while it keeps running. A
    cancellation of the caller that arrives meanwhile is held until *inner*
    has finished, then raised, so nothing is left pending behind the call. An
    *inner* whose own cancellation cleanup awaits longer than
    :data:`RECANCEL_INTERVAL` is cancelled again mid-cleanup, so a new caller
    whose await cleans up that way must check it can bear that.
    """
    held: asyncio.CancelledError | None = None
    while not inner.done():
        inner.cancel()
        try:
            await asyncio.wait({inner}, timeout=RECANCEL_INTERVAL)
        except asyncio.CancelledError as exc:
            held = exc
    if not inner.cancelled():
        inner.exception()  # retrieved, so the loop never reports it as unhandled
    if held is not None:
        raise held
