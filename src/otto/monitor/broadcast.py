"""Broadcaster — fan-out of monitor payloads to SSE subscriber queues.

One ``asyncio.Queue`` per connected dashboard tab. ``publish()`` uses
``put_nowait()`` — safe because collection and the SSE route handlers all run
on the same event loop.
"""

import asyncio
from typing import Any


class Broadcaster:
    """Holds SSE subscriber queues and pushes JSON-safe payloads to all of them."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove ``q`` so it receives no further pushes (unknown queues are a no-op)."""
        self._subscribers = [sq for sq in self._subscribers if sq is not q]

    def publish(self, payload: dict[str, Any]) -> None:
        """Push a JSON-safe dict to every subscriber queue."""
        for q in list(self._subscribers):
            q.put_nowait(payload)
