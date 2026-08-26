"""Broadcaster — fan-out of monitor payloads to SSE subscriber queues.

One ``asyncio.Queue`` per connected dashboard tab. ``publish()`` uses
``put_nowait()`` — safe because collection and the SSE route handlers all run
on the same event loop.

Queues are BOUNDED (``SUBSCRIBER_QUEUE_MAX``). A subscriber that stops
draining (frozen renderer, half-dead connection) can no longer grow memory
without bound at ~90 fragments/tick — but what happens at the bound is a
LAPSE, not a drop. The first cut dropped the oldest frame to make room, on
the argument that the dashboard resyncs from ``/api/monitor_sessions`` on
every SSE reconnect. It does — and an eviction never causes a reconnect, so
nothing ever resynced. The frames are not interchangeable either:
``chart_map`` is emitted ONCE, the first time a bare label appears, and a
tab that lost it drew thirteen ungrouped ``chart-m*`` charts instead of one
``chart-CPU``, silently, until reload.

So an overflowing queue is emptied and handed a single :data:`LAPSED`
sentinel, and receives nothing more. The SSE route ends that subscriber's
response when it reads the sentinel; the browser's ``EventSource`` sees the
close as an error, and the client's existing ``onerror -> resync -> reopen``
path — the one the drop argument was counting on — finally runs. A gap in
the stream is recovered by resync, and now the gap is announced.

The trade is deliberate: a subscriber that is alive but sustainedly slower
than the stream now lapses, resyncs from the snapshot and lapses again
(roughly every dozen seconds at live-bed rate), where drop-oldest cost it
nothing and showed it stale data. Loud and correct over quiet and wrong.
"""

import asyncio
from typing import Any, Final

# ~11 ticks of headroom at the live bed's ~90 fragments/tick before a stalled
# subscriber lapses and is told to resync.
SUBSCRIBER_QUEUE_MAX = 1024

#: The identity sentinel — the ONLY item a lapsed subscriber's queue holds.
#: Compared with ``is``, never by value, so a real payload can never
#: impersonate it. (No ``name: text`` shape on the first line, deliberately:
#: napoleon reads that as an attribute's ``type:`` field.)
LAPSED: Final[dict[str, Any]] = {"type": "lapse"}


class Broadcaster:
    """Holds SSE subscriber queues and pushes JSON-safe payloads to all of them."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lapsed: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber and return its (bounded) queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove ``q`` so it receives no further pushes (unknown queues are a no-op)."""
        self._subscribers = [sq for sq in self._subscribers if sq is not q]
        self._lapsed = [sq for sq in self._lapsed if sq is not q]

    def has_lapsed(self, q: asyncio.Queue[dict[str, Any]]) -> bool:
        """Report whether ``q`` overflowed: it then holds :data:`LAPSED` and nothing else, ever."""
        return any(sq is q for sq in self._lapsed)

    def publish(self, payload: dict[str, Any]) -> None:
        """Push a JSON-safe dict to every live subscriber; a full queue LAPSES.

        Same loop, no await between the check and the put — the queue cannot
        change under this method.
        """
        for q in list(self._subscribers):
            if self.has_lapsed(q):
                # Told to resync already; nothing further is worth queueing.
                continue
            if q.full():
                # Not "drop the oldest": every frame this queue held is gone
                # and the subscriber must resync from the snapshot. Empty it
                # so the sentinel is the next — and last — thing it reads.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(LAPSED)
                self._lapsed.append(q)
                continue
            q.put_nowait(payload)
