"""The monitor's live stream against its declared format (dump spec §13.5).

``GET /api/stream`` speaks ``monitor-live-stream``, a format of its own and
not the export document's. Its read version has a populated sample under
``tests/_fixtures/formats/monitor-live-stream/``: one fragment per server-sent
event, covering every payload field. The stream's one production reader is
the browser (``web/src/data/stream.ts``, tested against the same sample in
``web/src/__tests__/stream.formats.test.ts``). Here the sample goes through
``MonitorSessionFragment``, the model that declares the stream's shape and
generates the browser's type. Every write path of the collector gets an
emission test, and no emitter may spell the version as a literal.
"""

import ast
import inspect
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from otto.models import formats
from otto.models.monitor import MonitorSessionFragment, TunnelRecord
from otto.monitor import collector
from tests._fixtures import _fake_collector
from tests._fixtures._fake_collector import FakeCollector
from tests._fixtures.paths import TESTS_ROOT

SAMPLES = TESTS_ROOT / "_fixtures" / "formats" / "monitor-live-stream"
UTC = timezone.utc
SESSION = "2026-07-01T08-00-00-live"


def _sample(version: int) -> list[dict]:
    return json.loads((SAMPLES / f"{version}.json").read_text())


def _stream_v1(frags: "list[MonitorSessionFragment]") -> None:
    assert {f.session for f in frags} == {SESSION}
    first, second, added, edited, noise, deleted, logged, tunnels = frags
    assert [(m.host, m.label, m.value) for m in first.metrics + second.metrics] == [
        ("rack1-a", "cpu", 12.5),
        ("rack1-a", "cpu", 25.0),
    ]
    assert first.chart_map == {"cpu": "CPU"}
    assert first.meta is not None
    assert (first.meta.interval, [c.chart for c in first.meta.charts]) == (5.0, ["CPU"])
    assert (second.meta, second.chart_map) == (None, {})
    assert [(e.id, e.label, e.end_timestamp) for e in added.events + edited.events] == [
        (1, "deploy", None),
        (1, "deploy v2", datetime(2026, 7, 1, 8, 0, 9, tzinfo=UTC)),
    ]
    assert [e.id for e in noise.events] == deleted.deleted_event_ids == [2]
    assert logged.log_events[0].fields == {"level": "error", "message": "link flap"}
    assert logged.log_events[0].timestamp == datetime(2026, 7, 1, 8, 0, 8, tzinfo=UTC)
    assert tunnels.tunnels is not None
    assert [(t.id, t.hops) for t in tunnels.tunnels] == [("t1", ["rack1-a", "rack1-b"])]
    # tunnels is REPLACE-semantics: None means "no update", not "no tunnels"
    assert [f.tunnels for f in frags[:-1]] == [None] * 7


STREAMS = {1: _stream_v1}


@pytest.mark.parametrize("version", formats.MONITOR_STREAM_READ_VERSIONS)
def test_the_fragment_model_reads_each_declared_stream_sample(version):
    frags = [MonitorSessionFragment.model_validate_json(json.dumps(f)) for f in _sample(version)]
    assert {f.format for f in frags} == {version}
    STREAMS[version](frags)


def test_a_fragment_outside_the_declared_reads_is_refused():
    """The ``Literal`` is the declared read list, so the next version up is refused."""
    frag = {**_sample(1)[1], "format": max(formats.MONITOR_STREAM_READ_VERSIONS) + 1}
    with pytest.raises(ValidationError, match=r"literal_error"):
        MonitorSessionFragment.model_validate(frag)


def test_an_absent_format_reads_as_a_declared_version():
    """A fragment without ``format`` takes the field's default, which must be readable."""
    frag = {k: v for k, v in _sample(1)[1].items() if k != "format"}
    assert MonitorSessionFragment.model_validate(frag).format in (
        formats.MONITOR_STREAM_READ_VERSIONS
    )


def test_every_stream_write_version_is_also_read():
    """The browser reads back what it and the collector stamp."""
    assert set(formats.MONITOR_STREAM_WRITE_VERSIONS) <= set(formats.MONITOR_STREAM_READ_VERSIONS)


async def _publish_every_kind() -> "list[dict]":
    """Drive every collector path that publishes, and return what it published."""
    fake = FakeCollector(interval=5.0)
    fake.session_id = SESSION
    q = fake.subscribe()
    await fake.push("rack1-a", "cpu", 12.5, ts=datetime(2026, 7, 1, 8, 0, 5, tzinfo=UTC))
    ev = await fake.add_event("deploy", timestamp=datetime(2026, 7, 1, 8, 0, 6, tzinfo=UTC))
    await fake.update_event(
        ev.id, label="deploy v2", color="#111111", dash="solid", timestamp=ev.timestamp
    )
    await fake.delete_event(ev.id)
    await fake.push_log_events(
        "rack1-a", tab="syslog", rows=[(datetime(2026, 7, 1, 8, 0, 8, tzinfo=UTC), {"m": "up"})]
    )
    tunnel = TunnelRecord(id="t1", service_port=4789, hops=["rack1-a", "rack1-b"])

    async def scan() -> "list[TunnelRecord]":
        return [tunnel]

    fake._tunnel_source = scan
    await fake._tunnel_pass()
    await fake.push_tunnels([])
    published = []
    while not q.empty():
        published.append(q.get_nowait())
    fake.unsubscribe(q)
    return published


@pytest.mark.asyncio
@pytest.mark.parametrize("version", formats.MONITOR_STREAM_WRITE_VERSIONS)
async def test_every_collector_writer_stamps_the_declared_write_version(version):
    assert version == collector.STREAM_FORMAT
    published = await _publish_every_kind()
    # metric, event add/update/delete, log events, tunnel scan, scripted tunnels
    assert len(published) == 7
    assert {k for frag in published for k in frag} - {"format", "session"} == {
        "metrics",
        "chart_map",
        "meta",
        "events",
        "deleted_event_ids",
        "log_events",
        "tunnels",
    }
    assert [frag["format"] for frag in published] == [version] * len(published)
    for frag in published:
        assert MonitorSessionFragment.model_validate(frag).format == version


@pytest.mark.parametrize("module", [collector, _fake_collector])
def test_no_stream_writer_spells_the_version_as_a_literal(module):
    """Every ``"format"`` key a publisher builds is the declared ``STREAM_FORMAT``."""
    stamps = [
        value
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "format"
    ]
    assert stamps, f"{module.__name__} publishes no fragment"
    assert [ast.unparse(v) for v in stamps] == ["STREAM_FORMAT"] * len(stamps)
