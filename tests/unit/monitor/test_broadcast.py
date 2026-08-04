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
    """A stalled subscriber must not grow memory unboundedly (todo/TODO.md:80)."""

    def test_subscriber_queue_is_bounded(self):
        from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX, Broadcaster

        q = Broadcaster().subscribe()
        assert q.maxsize == SUBSCRIBER_QUEUE_MAX

    def test_full_queue_drops_oldest_keeps_newest(self):
        from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        q = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX + 3):
            b.publish({"seq": i})
        assert q.qsize() == SUBSCRIBER_QUEUE_MAX
        assert q.get_nowait() == {"seq": 3}, "oldest three dropped"
        drained = [q.get_nowait() for _ in range(q.qsize())]
        assert drained[-1] == {"seq": SUBSCRIBER_QUEUE_MAX + 2}, "newest always lands"

    def test_one_stalled_subscriber_does_not_affect_others(self):
        from otto.monitor.broadcast import SUBSCRIBER_QUEUE_MAX, Broadcaster

        b = Broadcaster()
        stalled = b.subscribe()
        healthy = b.subscribe()
        for i in range(SUBSCRIBER_QUEUE_MAX + 5):
            b.publish({"seq": i})
            healthy.get_nowait()
        assert stalled.qsize() == SUBSCRIBER_QUEUE_MAX
        assert healthy.qsize() == 0
