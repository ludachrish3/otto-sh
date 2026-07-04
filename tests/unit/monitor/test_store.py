"""MetricStore — in-memory series/chart-map/event bookkeeping."""

from datetime import datetime, timedelta, timezone

from otto.models import MetricPoint
from otto.monitor.events import MonitorEvent
from otto.monitor.parsers import LogEvent
from otto.monitor.store import MetricStore

TS = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _point(value: float) -> MetricPoint:
    return MetricPoint.model_construct(ts=TS, value=value, meta=None)


def test_append_point_creates_series_and_chart_map() -> None:
    store = MetricStore()
    store.append_point("host1/Overall CPU", _point(1.0), label="Overall CPU", chart="CPU")
    store.append_point("host1/Overall CPU", _point(2.0), label="Overall CPU", chart="CPU")
    assert [p.value for p in store.snapshot_series()["host1/Overall CPU"]] == [1.0, 2.0]
    assert store.snapshot_chart_map() == {"Overall CPU": "CPU"}


def test_snapshot_is_a_copy() -> None:
    store = MetricStore()
    store.append_point("h/x", _point(1.0), label="x", chart="X")
    snap = store.snapshot_series()
    snap["h/x"].append(_point(9.9))
    assert len(store.snapshot_series()["h/x"]) == 1


def test_event_id_quirk_preserved() -> None:
    """rowid wins when nonzero, but next_id advances regardless (legacy behavior)."""
    store = MetricStore()
    e1 = store.add_event(MonitorEvent(timestamp=TS, label="a"), rowid=0)
    assert e1.id == 1
    e2 = store.add_event(MonitorEvent(timestamp=TS, label="b"), rowid=7)
    assert e2.id == 7
    e3 = store.add_event(MonitorEvent(timestamp=TS, label="c"), rowid=0)
    assert e3.id == 3  # next_id advanced past e2 despite the rowid


def test_hosts_from_series_ignores_bare_labels() -> None:
    store = MetricStore()
    store.append_point("host2/CPU", _point(1.0), label="CPU", chart="CPU")
    store.append_point("host1/CPU", _point(1.0), label="CPU", chart="CPU")
    store.append_point("bare-label", _point(1.0), label="bare-label", chart="X")
    assert store.hosts_from_series() == ["host1", "host2"]


def test_remove_and_find_event() -> None:
    store = MetricStore()
    ev = store.add_event(MonitorEvent(timestamp=TS, label="a"), rowid=0)
    assert store.find_event(ev.id) is ev
    assert store.remove_event(ev.id) is True
    assert store.remove_event(ev.id) is False
    assert store.find_event(ev.id) is None


def _ev(second: int) -> LogEvent:
    return LogEvent(
        ts=datetime(2026, 7, 4, 12, 0, second, tzinfo=timezone.utc),
        fields={"message": f"row {second}"},
    )


class TestLogEventRing:
    def test_append_and_snapshot_roundtrip(self) -> None:
        store = MetricStore()
        store.append_log_event("host1", "syslog", _ev(1))
        store.append_log_event("host1", "syslog", _ev(2))
        store.append_log_event("host2", "syslog", _ev(3))
        assert store.snapshot_log_events() == [
            ("host1", "syslog", _ev(1)),
            ("host1", "syslog", _ev(2)),
            ("host2", "syslog", _ev(3)),
        ]

    def test_ring_caps_at_1000_dropping_oldest(self) -> None:
        store = MetricStore()
        for i in range(1001):
            store.append_log_event(
                "h",
                "t",
                LogEvent(
                    ts=datetime(2026, 7, 4, tzinfo=timezone.utc) + timedelta(seconds=i),
                    fields={"i": str(i)},
                ),
            )
        rows = [ev for _, _, ev in store.snapshot_log_events()]
        assert len(rows) == 1000
        assert rows[0].fields["i"] == "1"  # row 0 dropped

    def test_hosts_from_series_includes_log_events_only_host(self) -> None:
        """A host that produced only log events (no chart series) is still visible."""
        store = MetricStore()
        store.append_log_event("host9", "syslog", _ev(1))
        assert store.hosts_from_series() == ["host9"]

    def test_hosts_from_series_unions_series_and_log_event_hosts(self) -> None:
        store = MetricStore()
        store.append_point("host1/CPU", _point(1.0), label="CPU", chart="CPU")
        store.append_log_event("host2", "syslog", _ev(1))
        store.append_log_event("host1", "syslog", _ev(2))  # already in series hosts
        assert store.hosts_from_series() == ["host1", "host2"]
