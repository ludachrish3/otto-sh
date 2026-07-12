"""Producer: live store / v2 archive → format:1 MonitorExport."""

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
from typing_extensions import override

from otto.models import HostSnapshot, LabSnapshot, MetricPoint, MonitorExport, SessionMeta
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB
from otto.monitor.export import build_db_export, build_live_export, document_json, session_meta
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext, TopCpuParser
from otto.monitor.session import new_frame
from otto.result import CommandResult, Status

UTC = timezone.utc
T0 = datetime(2026, 7, 12, 8, 0, 0, tzinfo=UTC)


class _MultiSeriesParser(MetricParser):
    """A parser whose SERIES labels differ from its CHART key — the real shape.

    ``TopCpuParser`` behaves exactly like this ("Overall CPU"/"proc/<pid>"
    both land in chart "CPU"), and it is why chart_map cannot be derived
    statically: the labels only exist once ``parse()`` has run.
    """

    y_title = "Usage %"
    unit = "%"
    command = "fake-cpu"
    tab = "cpu"
    tab_label = "CPU"
    chart = "CPU"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {"Overall CPU": MetricDataPoint(12.5), "proc/1234": MetricDataPoint(3.5)}


async def _write_session(
    path,
    label,
    *,
    minutes=0,
    lab_json="{}",
    meta_json="{}",
    finalize=True,
):
    """Inline equivalent of Task 1's ``write_session`` (test_db_v2.py), with a
    ``lab_json`` knob so the multi-session test can prove real round-tripping.
    """
    db = MetricDB(
        str(path),
        new_frame(label=label, note=f"note for {label}", now=T0 + timedelta(minutes=minutes)),
        lab_json=lab_json,
        meta_json=meta_json,
    )
    await db.open()
    await db.write_point(T0 + timedelta(minutes=minutes), "h1", "CPU %", 42.0)
    if finalize:
        await db.finalize(T0 + timedelta(minutes=minutes + 5))
    await db.close()


def test_live_export_wraps_one_open_session():
    collector = MetricCollector(hosts=[])
    frame = new_frame(label="live run", note="testing", now=T0)

    export = build_live_export(frame, collector, LabSnapshot())

    assert export.format == 1
    assert len(export.sessions) == 1
    session = export.sessions[0]
    assert session.id == frame.id
    assert session.label == "live run"
    assert session.end is None

    round_tripped = MonitorExport.model_validate_json(document_json(export))
    assert round_tripped == export


def test_live_export_splits_series_keys_on_first_slash_only():
    collector = MetricCollector(hosts=[])
    collector._store.series["h1/proc/io read"] = deque([MetricPoint(ts=T0, value=1.5)])
    collector._store.chart_map["proc/io read"] = "IO"
    # Bare-label edge case (no "/" at all): the historical --file compat shape
    # (history.load_json_into's empty-host case) — must not swallow the whole
    # key into `host`, matching store.hosts_from_series/history.to_json.
    collector._store.series["bare label"] = deque([MetricPoint(ts=T0, value=9.0)])
    frame = new_frame(label=None, note=None, now=T0)

    export = build_live_export(frame, collector, LabSnapshot())

    by_label = {m.label: m for m in export.sessions[0].metrics}
    assert by_label["proc/io read"].host == "h1"
    assert by_label["proc/io read"].label == "proc/io read"
    assert by_label["proc/io read"].value == 1.5
    assert by_label["bare label"].host == ""


@pytest.mark.asyncio
async def test_live_export_carries_meta_and_chart_map():
    collector = MetricCollector(hosts=[], parsers=[TopCpuParser()])
    collector._store.series["h1/Overall CPU"] = deque([MetricPoint(ts=T0, value=10.0)])
    collector._store.chart_map["Overall CPU"] = "CPU"
    await collector.add_event(label="marker", timestamp=T0)
    frame = new_frame(label=None, note=None, now=T0)

    export = build_live_export(frame, collector, LabSnapshot())

    session = export.sessions[0]
    assert session.chart_map == {"Overall CPU": "CPU"}
    assert [c.chart for c in session.meta.charts] == ["CPU"]
    assert len(session.events) == 1
    assert session.events[0].label == "marker"


def test_session_meta_is_the_only_safe_way_to_build_meta_json():
    """The MonitorMeta -> SessionMeta reshape RENAMES the chart list.

    ``MonitorMeta`` spells it ``metrics``; ``SessionMeta`` spells it
    ``charts``. ``SessionMeta`` is a lenient RowModel (``extra='ignore'``), so
    dumping a ``MonitorMeta`` straight into a session's ``meta_json`` column
    *validates silently* and yields ``charts=[]`` — while ``tabs`` survives
    intact (same field name), which makes the loss look partial and easy to
    miss. A session persisted that way replays with no chart specs and no
    units, the same degradation an empty ``chart_map`` causes.

    Both ``--db`` producers (otto.cli.monitor's live path and
    otto.suite.plugin's --monitor path) must persist
    ``session_meta(collector).model_dump_json()``. This pins the trap so a
    third call site can't quietly reintroduce the raw dump.
    """
    collector = MetricCollector(hosts=[], parsers=[TopCpuParser()])

    assert [c.chart for c in session_meta(collector).charts] == ["CPU"]

    naive = SessionMeta.model_validate_json(collector.get_meta_model().model_dump_json())
    assert naive.charts == [], "MonitorMeta dump must NOT be a usable SessionMeta"
    assert naive.tabs, "…and tabs survives, which is exactly why this is easy to miss"


@pytest.mark.asyncio
async def test_db_export_reads_multi_session_archive(tmp_path):
    path = tmp_path / "lab.db"
    lab_json = LabSnapshot(hosts=[HostSnapshot(id="h1", element="h1")]).model_dump_json()
    await _write_session(path, "run-1", minutes=0, lab_json=lab_json)
    await _write_session(path, "run-2", minutes=60)

    export = build_db_export(str(path))

    assert [s.label for s in export.sessions] == ["run-1", "run-2"]
    assert [s.note for s in export.sessions] == ["note for run-1", "note for run-2"]
    assert [h.id for h in export.sessions[0].lab.hosts] == ["h1"]
    assert export.sessions[1].lab == LabSnapshot()


@pytest.mark.asyncio
async def test_db_export_chart_map_survives_a_real_collector_run(tmp_path):
    """END-TO-END: a real collector tick must leave a real chart_map in the archive.

    This is the test the first fix wave lacked. The map is populated ONLY by
    ``MetricStore.append_point`` as points arrive — but the session row is
    INSERTed by ``MetricDB.open()`` (from ``init_db()``), which runs BEFORE
    the first tick. So any chart_map handed to the constructor is provably
    ``{}``, and a plumbing test that hand-writes a non-empty value proves
    nothing about ``otto monitor --db``. Drive the real path instead: open the
    DB (session INSERT, empty map), run one tick, then read it back.

    Deliberately NOT finalized — a crashed session must keep its map too,
    which is why the collector writes it as labels appear rather than at
    finalize.
    """
    path = tmp_path / "lab.db"
    parser = _MultiSeriesParser()
    db = MetricDB(
        str(path), new_frame(label="live", note=None, now=T0), lab_json="{}", meta_json="{}"
    )
    collector = MetricCollector(hosts=[], parsers=[parser], db=db)

    await collector.init_db()  # session row INSERTed here — chart_map is empty NOW
    await collector._process_host_results(
        "h1",
        T0,
        [CommandResult(Status.Success, value="raw", command=parser.command, retcode=0)],
        {parser.command: parser},
        ctx=ParseContext(ts=T0),
    )
    await collector.close_db()

    (session,) = build_db_export(str(path)).sessions

    assert session.chart_map == {"Overall CPU": "CPU", "proc/1234": "CPU"}
    # Never finalized, so the DB's `end` is NULL and the producer's crash
    # fallback stands in the last sample's ts — and the map survived anyway,
    # which is the whole point of writing it per-label instead of at finalize.
    assert session.end == T0


@pytest.mark.asyncio
async def test_db_export_null_end_falls_back_to_last_sample(tmp_path):
    path = tmp_path / "lab.db"

    crashed_frame = new_frame(label="crashed", note=None, now=T0)
    db1 = MetricDB(str(path), crashed_frame, lab_json="{}", meta_json="{}")
    await db1.open()
    await db1.write_point(T0, "h1", "CPU %", 1.0)
    await db1.write_point(T0 + timedelta(minutes=5), "h1", "CPU %", 2.0)
    await db1.close()  # never finalized -> DB-side end stays NULL

    empty_frame = new_frame(label="empty", note=None, now=T0 + timedelta(hours=1))
    db2 = MetricDB(str(path), empty_frame, lab_json="{}", meta_json="{}")
    await db2.open()
    await db2.close()  # never finalized, zero samples

    export = build_db_export(str(path))
    by_label = {s.label: s for s in export.sessions}

    assert by_label["crashed"].end == T0 + timedelta(minutes=5)
    assert by_label["empty"].end == by_label["empty"].start == T0 + timedelta(hours=1)
