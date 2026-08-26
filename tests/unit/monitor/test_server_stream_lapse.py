"""The SSE route ends a lapsed subscriber's response, so the browser resyncs.

Driven at the raw-ASGI level like ``test_server_auth.py``: the app is run as
a task, the test reads the response body messages as the route emits them,
overflows the subscriber through the collector, and expects the response to
END — ``more_body: False`` — rather than keep streaming. Ending the response
is the whole signal: the client's ``EventSource`` reports a server-side close
as ``onerror``, and ``web/src/data/stream.ts`` resyncs from the snapshot
before reopening. Nothing else in the stack turns a lost frame into a
resync, which is why drop-oldest was a silent-loss bug (``chart_map`` is
emitted once).
"""

import asyncio
import contextlib

import pytest

from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX
from otto.monitor.collector import MetricCollector
from otto.monitor.server import _build_app

_KEY = "test-key-lapse"


def _app(collector: MetricCollector):
    return _build_app(collector, key=_KEY)


async def _run_stream(app, collector: MetricCollector, *, overflow: bool) -> list[bytes]:
    """Open /api/stream, optionally overflow the subscriber, collect chunks until it ends."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": f"key={_KEY}".encode(),
        "headers": [(b"host", b"127.0.0.1:8123")],
        "server": ("127.0.0.1", 8123),
        "client": ("127.0.0.1", 55555),
    }
    chunks: list[bytes] = []
    first_chunk = asyncio.Event()
    second_chunk = asyncio.Event()
    ended = asyncio.Event()
    never = asyncio.Event()  # the client never disconnects on its own

    async def receive():
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            first_chunk.set()
            if len(chunks) >= 2:
                second_chunk.set()
            if not message.get("more_body", False):
                ended.set()

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_chunk.wait(), timeout=5.0)
        if overflow:
            for i in range(SUBSCRIBER_QUEUE_MAX + 1):
                collector._publish({"seq": i})
            await asyncio.wait_for(ended.wait(), timeout=5.0)
        else:
            collector._publish({"seq": 0})
            # Wait for the FRAME, not for a slice of wall clock: a loaded box
            # must not turn "the route had no turn yet" into a red control.
            await asyncio.wait_for(second_chunk.wait(), timeout=5.0)
            assert not ended.is_set(), "a live subscriber's response ended without a lapse"
    finally:
        never.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return chunks


@pytest.mark.asyncio
async def test_an_overflowed_subscribers_response_ends_with_a_lapse_comment():
    collector = MetricCollector(hosts=[], parsers=[])
    chunks = await _run_stream(_app(collector), collector, overflow=True)
    body = b"".join(chunks)
    assert b": connected" in body, "the priming comment still opens the stream"
    assert b": lapsed: resync" in body, "the close is announced, not silent"
    assert b'"seq"' not in body, "every frame the queue held was discarded, none replayed"
    assert collector._broadcast._subscribers == [], "the route unsubscribed on the way out"


@pytest.mark.asyncio
async def test_a_live_subscriber_keeps_streaming():
    """Control: without an overflow the route streams the frame and stays open."""
    collector = MetricCollector(hosts=[], parsers=[])
    chunks = await _run_stream(_app(collector), collector, overflow=False)
    assert b'"seq": 0' in b"".join(chunks)
