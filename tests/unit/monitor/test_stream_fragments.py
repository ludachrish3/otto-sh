"""Every published payload validates as a MonitorSessionFragment — the anti-drift pin."""

from datetime import datetime, timezone

import pytest

from otto.models.monitor import MonitorSessionFragment
from otto.monitor.server import MonitorServer
from otto.monitor.session import new_frame
from tests._fixtures._fake_collector import FakeCollector


class TestPublishedPayloadsAreFragments:
    @pytest.mark.asyncio
    async def test_metric_publishes_a_format1_fragment(self) -> None:
        collector = FakeCollector()
        collector.session_id = "2026-07-12T10-00-00Z"
        q = collector.subscribe()

        await collector.push(
            "r1", "cpu", 12.5, ts=datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc)
        )

        frag = MonitorSessionFragment.model_validate(q.get_nowait())
        assert frag.session == "2026-07-12T10-00-00Z"
        assert len(frag.metrics) == 1
        assert frag.metrics[0].host == "r1"
        assert frag.metrics[0].label == "cpu"
        assert frag.metrics[0].value == 12.5
        # The first sighting of a label carries the chart specs with it, so the
        # client can render a brand-new chart without re-fetching.
        assert frag.chart_map, "a newly seen label must ship its chart_map"
        assert frag.meta is not None, "meta must carry the chart specs"
        assert frag.meta.charts, "meta must carry the chart specs"
        # THE 5a TRAP: SessionMeta spells the chart list `charts`, MonitorMeta
        # spells it `metrics`. A raw get_meta_model() dump would silently give []
        # The catalog's ChartSpec.label is the chart's canonical (properly-cased)
        # name — chart_map["cpu"] == "CPU" confirms the newly-seen label was
        # grouped into that chart, so that's the label to look for here.
        assert any(c.label == "CPU" for c in frag.meta.charts)
        collector.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_event_add_update_delete_publish_fragments(self) -> None:
        collector = FakeCollector()
        collector.session_id = "s1"
        q = collector.subscribe()

        ev = await collector.add_event("boot")
        added = MonitorSessionFragment.model_validate(q.get_nowait())
        assert [e.id for e in added.events] == [ev.id]

        await collector.update_event(ev.id, label="boot2", color="#111111", dash="solid")
        updated = MonitorSessionFragment.model_validate(q.get_nowait())
        # No separate "updated" kind — the client upserts by id, so an edited
        # event is just an event.
        assert updated.events[0].label == "boot2"

        await collector.delete_event(ev.id)
        deleted = MonitorSessionFragment.model_validate(q.get_nowait())
        assert deleted.deleted_event_ids == [ev.id]
        collector.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_log_events_publish_a_fragment(self) -> None:
        collector = FakeCollector()
        collector.session_id = "s1"
        q = collector.subscribe()

        await collector.push_log_events(
            "r1",
            tab="syslog",
            rows=[(datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc), {"msg": "up"})],
        )

        frag = MonitorSessionFragment.model_validate(q.get_nowait())
        assert frag.log_events[0].host == "r1"
        assert frag.log_events[0].tab == "syslog"
        assert frag.log_events[0].fields == {"msg": "up"}
        collector.unsubscribe(q)


class TestServerStampsTheSessionId:
    def test_server_sets_collector_session_id_from_the_frame(self) -> None:
        """The collector knows nothing about sessions; the server, which holds the
        frame, stamps it. One place, so no call site can forget."""
        collector = FakeCollector()
        frame = new_frame(label=None, note=None)
        MonitorServer(collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=None)
        assert collector.session_id == frame.id
