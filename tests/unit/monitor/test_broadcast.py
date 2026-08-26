"""Broadcaster — SSE fan-out isolated from the collector."""

from otto.monitor.broadcast import Broadcaster


def test_publish_reaches_all_subscribers() -> None:
    b = Broadcaster()
    q1, q2 = b.subscribe(), b.subscribe()
    b.publish({"type": "metric", "value": 1.0})
    assert q1.get_nowait() == {"type": "metric", "value": 1.0}
    assert q2.get_nowait() == {"type": "metric", "value": 1.0}


def test_unsubscribed_queue_receives_nothing() -> None:
    b = Broadcaster()
    q1, q2 = b.subscribe(), b.subscribe()
    b.unsubscribe(q1)
    b.publish({"type": "event"})
    assert q1.empty()
    assert q2.get_nowait() == {"type": "event"}


def test_unsubscribe_unknown_queue_is_noop() -> None:
    import asyncio

    b = Broadcaster()
    b.unsubscribe(asyncio.Queue())  # never subscribed — must not raise


class TestBoundedQueues:
    """A stalled subscriber must not grow memory unboundedly — and must be told it lost frames.

    The first cut dropped the oldest frame on overflow, and this class pinned
    that as intended. It was the product bug: ``chart_map`` is a one-shot
    frame, an eviction triggers no reconnect, so the resync that was supposed
    to make dropping safe never ran and a tab showed thirteen ungrouped
    charts until reload. Overflow now LAPSES the subscriber: its queue is
    emptied, holds the ``LAPSED`` sentinel alone, and the SSE route ends
    that response so the client's ``onerror -> resync`` path runs.
    """

    def test_subscriber_queue_is_bounded(self):
        from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX, Broadcaster

        q = Broadcaster().subscribe()
        assert q.maxsize == SUBSCRIBER_QUEUE_MAX

    def test_an_overflowing_subscriber_lapses_instead_of_losing_frames_quietly(self):
        from otto.monitor.broadcast import LAPSED, SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        q = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX):
            b.publish({"seq": i})
        assert not b.has_lapsed(q), "a full queue that has not overflowed is still live"
        b.publish({"seq": SUBSCRIBER_QUEUE_MAX})
        assert b.has_lapsed(q)
        assert q.qsize() == 1, "every queued frame is gone; only the sentinel remains"
        assert q.get_nowait() is LAPSED, "identity, so no payload can impersonate it"

    def test_a_lapsed_subscriber_receives_nothing_more(self):
        from otto.monitor.broadcast import LAPSED, SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        q = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX + 1):
            b.publish({"seq": i})
        for i in range(5):
            b.publish({"after": i})
        assert q.qsize() == 1
        assert q.get_nowait() is LAPSED
        b.publish({"after": "sentinel read"})
        assert q.empty(), "a lapsed queue is not refilled even once drained"

    def test_unsubscribing_a_lapsed_queue_forgets_the_lapse(self):
        from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        q = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX + 1):
            b.publish({"seq": i})
        b.unsubscribe(q)
        assert not b.has_lapsed(q)
        assert b._lapsed == []
        assert b._subscribers == []

    def test_one_stalled_subscriber_does_not_affect_others(self):
        from otto.monitor.broadcast import LAPSED, SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        stalled = b.subscribe()
        healthy = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX + 5):
            b.publish({"seq": i})
            assert healthy.get_nowait() == {"seq": i}
        assert stalled.qsize() == 1
        assert stalled.get_nowait() is LAPSED
        assert healthy.qsize() == 0
        assert not b.has_lapsed(healthy)
