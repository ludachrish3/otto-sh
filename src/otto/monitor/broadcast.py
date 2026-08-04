"""Broadcaster — fan-out of monitor payloads to SSE subscriber queues.

One ``asyncio.Queue`` per connected dashboard tab. ``publish()`` uses
``put_nowait()`` — safe because collection and the SSE route handlers all run
on the same event loop.

Queues are BOUNDED (``SUBSCRIBER_QUEUE_MAX``) with drop-oldest overflow: a
tab that stops draining (frozen renderer, half-dead connection) can no longer
grow memory without bound at ~90 fragments/tick. Dropping is safe because the
dashboard client re-hydrates from ``/api/monitor_sessions`` on every SSE
reconnect — a gap in the stream is recovered by resync, not replay.
"""

import asyncio
from typing import Any

# ~11 ticks of headroom at the live bed's ~90 fragments/tick before a stalled
# subscriber starts losing its OLDEST payloads (newest always lands).
SUBSCRIBER_QUEUE_MAX = 1024


class Broadcaster:
    """Holds SSE subscriber queues and pushes JSON-safe payloads to all of them."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its (bounded) queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove ``q`` so it receives no further pushes (unknown queues are a no-op)."""
        self._subscribers = [sq for sq in self._subscribers if sq is not q]

    def publish(self, payload: dict[str, Any]) -> None:
        """Push a JSON-safe dict to every subscriber; a full queue drops its oldest."""
        for q in list(self._subscribers):
            if q.full():
                # Drop the oldest to make room. Same loop, no await between
                # the check and the put — the freed slot cannot be raced.
                q.get_nowait()
            q.put_nowait(payload)
