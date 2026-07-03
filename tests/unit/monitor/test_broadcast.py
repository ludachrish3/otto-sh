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
