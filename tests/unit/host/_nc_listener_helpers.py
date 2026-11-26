"""Test helpers shared by the netcat listener's lifetime and measurement tests."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

from otto.host.userland import Userland


def userland(style: str) -> Userland:
    """A resolved-looking ``Userland`` whose ``timeout_style`` is *style*."""
    resolved = MagicMock(spec=Userland)
    resolved.timeout_style = style
    return resolved


async def until(event: asyncio.Event, running: "asyncio.Future[Any]") -> None:
    """Wait for *event*; fail at once if *running* ends first, rather than hang."""
    waiter = asyncio.ensure_future(event.wait())
    await asyncio.wait({waiter, running}, return_when=asyncio.FIRST_COMPLETED)
    if not event.is_set():
        waiter.cancel()
        running.result()  # raises what ended it
        raise AssertionError("the code under test ended before the awaited point")


class ReapRecorder:
    """A stand-in for the backend's reap that records each one in ``events``.

    Each reap appends ``reap <port>``, sets ``reaping``, raises ``reap_error``
    when one is set, waits for ``reap_gate`` when one is set (appending
    ``abandoned`` if it is cancelled there), calls :meth:`after_reap`, appends
    ``reaped`` and returns ``connects``.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.reap_gate: asyncio.Event | None = None
        self.reaping = asyncio.Event()
        self.reap_error: Exception | None = None
        self.connects = True

    def after_reap(self) -> None:
        """What a reap that went through does to the fake listener; nothing by default."""

    async def reap(self, port: int) -> bool:
        self.events.append(f"reap {port}")
        self.reaping.set()
        if self.reap_error is not None:
            raise self.reap_error
        if self.reap_gate is not None:
            try:
                await self.reap_gate.wait()
            except asyncio.CancelledError:
                self.events.append("abandoned")
                raise
        self.after_reap()
        self.events.append("reaped")
        return self.connects

    def reaps(self) -> int:
        return sum(1 for e in self.events if e.startswith("reap "))
