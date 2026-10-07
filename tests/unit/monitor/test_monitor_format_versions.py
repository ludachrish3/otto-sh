"""The monitor's archive and export formats against frozen samples (dump spec §13.5).

Each declared read version has a populated sample under
``tests/_fixtures/formats/``: a v2 archive as frozen SQL (historical schema,
rows and ``user_version``) and a format-1 export document. Every Python reader
takes each through its real entry point: ``read_sessions``, ``build_db_export``
with its nested-JSON decoding, and review mode's ``load_review_document``.
Every write version gets an emission test. Both paths that write INTO an
existing archive run on a copy of each read sample: a live run appending a
session keeps the rows and the stamp or refuses, and a review edit keeps them
(it checks no version yet).
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import otto.models.formats as model_formats
from otto.models.monitor import LabSnapshot
from otto.monitor import db, export, formats
from otto.monitor.archive_edit import delete_event, insert_event
from otto.monitor.collector import MetricCollector
from otto.monitor.db import MetricDB, UnsupportedDBError, read_sessions
from otto.monitor.errors import ReviewSourceError
from otto.monitor.export import build_db_export, build_live_export, document_json
from otto.monitor.review import load_review_document
from otto.monitor.session import new_frame
from tests._fixtures.paths import TESTS_ROOT

SAMPLES = TESTS_ROOT / "_fixtures" / "formats"
UTC = timezone.utc
FIRST = "2026-07-01T08-00-00Z"
OPEN = "2026-07-01T09-00-00Z"
APPENDED = "2026-07-02T00-00-00Z"


def _archive(version: int, dest: Path) -> Path:
    """Build a database in *dest* from *version*'s frozen SQL; the sample itself stays text."""
    path = dest / f"sample-v{version}.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript((SAMPLES / "monitor-database" / f"{version}.sql").read_text())
    finally:
        conn.close()
    return path


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _note(path: Path) -> int:
    return insert_event(
        str(path),
        FIRST,
        timestamp=datetime(2026, 7, 1, 8, 4, tzinfo=UTC),
        end_timestamp=None,
        label="note",
        source="manual",
        color="#888888",
        dash="dash",
    )


async def _append_session(path: Path) -> None:
    frame = new_frame("appended", None, now=datetime(2026, 7, 2, tzinfo=UTC))
    metric_db = MetricDB(str(path), frame, lab_json="{}", meta_json="{}")
    await metric_db.open()
    await metric_db.write_point(datetime(2026, 7, 2, 0, 0, 30, tzinfo=UTC), "h1", "CPU", 1.0)
    await metric_db.finalize(datetime(2026, 7, 2, 0, 1, tzinfo=UTC))
    await metric_db.close()


def _rows_v2(sessions) -> None:
    first, open_ = sessions[0], sessions[1]
    assert (first.id, open_.id) == (FIRST, OPEN)
    assert (first.label, first.note, first.end) == (
        "first run",
        "finalized session",
        "2026-07-01T08:05:00+00:00",
    )
    assert [(m[2], m[3]) for m in first.metrics] == [("Overall CPU", 12.5), ("core 0", 25.0)]
    assert [(e[3], e[2]) for e in first.events] == [
        ("deploy", None),
        ("outage", "2026-07-01T08:03:00+00:00"),
    ]
    assert [json.loads(f[3]) for f in first.log_events] == [
        {"level": "error", "message": "link flap"}
    ]
    assert (open_.end, len(open_.metrics), open_.events, open_.log_events) == (None, 1, [], [])


def _decoded_v2(doc) -> None:
    first, open_ = doc.sessions
    assert first.chart_map == {"Overall CPU": "CPU", "core 0": "CPU"}
    assert [t.hops for t in first.tunnels] == [["h1", "h2"]]
    assert first.lab.hosts[0].interfaces == {"eth0": "10.0.0.1"}
    assert [c.chart for c in first.meta.charts] == ["CPU"]
    assert first.log_events[0].fields == {"level": "error", "message": "link flap"}
    assert first.events[1].end_timestamp == datetime(2026, 7, 1, 8, 3, tzinfo=UTC)
    # a crashed session's end falls back to its last sample
    assert open_.end == datetime(2026, 7, 1, 9, 0, 30, tzinfo=UTC)


def _document_v1(doc) -> None:
    (s,) = doc.sessions
    assert (s.id, s.note) == ("2026-07-01T08-00-00-sample", "frozen monitor-export v1 sample")
    assert s.end - s.start == timedelta(minutes=1)
    assert [h.id for h in s.lab.hosts] == ["rack1-a", "rack1-b"]
    assert [e.host for e in s.lab.links[0].endpoints] == ["rack1-a", "rack1-b"]
    assert s.lab.elements[0].type == "physical"
    assert [m.value for m in s.metrics] == [12.5, 25.0, 50.0]
    assert [(e.label, e.end_timestamp) for e in s.events] == [
        ("deploy", None),
        ("outage", datetime(2026, 7, 1, 8, 0, 40, tzinfo=UTC)),
    ]
    assert s.log_events[0].fields == {"level": "error", "message": "link flap"}
    assert s.chart_map == {"CPU %": "cpu"}
    assert s.tunnels[0].hops == ["rack1-a", "rack1-b"]


ROWS = {2: _rows_v2}
DECODED = {2: _decoded_v2}
DOCUMENTS = {1: _document_v1}


@pytest.mark.parametrize("version", formats.MONITOR_DB_READ_VERSIONS)
def test_read_sessions_reads_each_declared_archive(version, tmp_path):
    ROWS[version](read_sessions(str(_archive(version, tmp_path))))


@pytest.mark.parametrize("version", formats.MONITOR_DB_READ_VERSIONS)
def test_the_db_export_decodes_each_declared_archive(version, tmp_path):
    DECODED[version](build_db_export(str(_archive(version, tmp_path))))


@pytest.mark.parametrize("version", formats.MONITOR_DB_READ_VERSIONS)
def test_review_mode_opens_each_declared_archive(version, tmp_path):
    DECODED[version](load_review_document(_archive(version, tmp_path)))


@pytest.mark.parametrize("version", model_formats.MONITOR_EXPORT_READ_VERSIONS)
def test_review_mode_reads_each_declared_export(version):
    DOCUMENTS[version](load_review_document(SAMPLES / "monitor-export" / f"{version}.json"))


def test_the_archive_paths_hold_the_declared_lists():
    assert db.MONITOR_DB_READ_VERSIONS is formats.MONITOR_DB_READ_VERSIONS
    assert db.MONITOR_DB_WRITE_VERSIONS is formats.MONITOR_DB_WRITE_VERSIONS


def test_reading_follows_the_declared_read_versions(monkeypatch, tmp_path):
    path = _archive(2, tmp_path)
    monkeypatch.setattr(db, "MONITOR_DB_READ_VERSIONS", [3])
    with pytest.raises(UnsupportedDBError, match=r"schema version 2\)"):
        read_sessions(str(path))


def test_review_mode_refuses_an_export_version_outside_the_declared_reads(tmp_path):
    """The format's ``Literal`` is the declared read list, so the next version up is refused."""
    document = json.loads((SAMPLES / "monitor-export" / "1.json").read_text())
    document["format"] = max(model_formats.MONITOR_EXPORT_READ_VERSIONS) + 1
    path = tmp_path / "undeclared.json"
    path.write_text(json.dumps(document))
    with pytest.raises(
        ReviewSourceError, match=r"format\n\s+Input should be .*\[type=literal_error"
    ):
        load_review_document(path)


@pytest.mark.asyncio
async def test_appending_to_an_archive_follows_the_declared_write_versions(monkeypatch, tmp_path):
    """A version otto reads but does not write may be reviewed, never appended to."""
    path = _archive(2, tmp_path)
    monkeypatch.setattr(db, "MONITOR_DB_WRITE_VERSIONS", [3])
    with pytest.raises(UnsupportedDBError):
        await _append_session(path)
    ROWS[2](read_sessions(str(path)))
    assert _user_version(path) == 2


def test_a_review_edit_checks_no_version_yet(tmp_path):
    """A review edit writes into an archive of any ``user_version``, as it did before.

    Refusing an archive outside ``MONITOR_DB_WRITE_VERSIONS`` is a contract
    change (dump spec §13.1), left to its own marked commit. This pins today's
    behaviour until that commit changes it on purpose.
    """
    path = _archive(2, tmp_path)
    undeclared = max(formats.MONITOR_DB_WRITE_VERSIONS) + 1
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {undeclared}")
        conn.commit()
    finally:
        conn.close()
    assert _note(path) > 0
    assert _user_version(path) == undeclared


@pytest.mark.asyncio
@pytest.mark.parametrize("version", formats.MONITOR_DB_READ_VERSIONS)
async def test_a_live_run_appends_to_a_copy_of_each_archive_or_refuses(version, tmp_path):
    path = _archive(version, tmp_path)
    if version not in formats.MONITOR_DB_WRITE_VERSIONS:
        with pytest.raises(UnsupportedDBError):
            await _append_session(path)
        assert _user_version(path) == version
        return
    await _append_session(path)
    sessions = read_sessions(str(path))
    ROWS[version](sessions)
    assert [s.id for s in sessions] == [FIRST, OPEN, APPENDED]
    assert len(sessions[2].metrics) == 1
    assert _user_version(path) == version


@pytest.mark.parametrize("version", formats.MONITOR_DB_READ_VERSIONS)
def test_a_review_edit_on_a_copy_of_each_archive_keeps_it(version, tmp_path):
    """A review edit keeps each read version's rows and stamp; it checks no version yet."""
    path = _archive(version, tmp_path)
    rowid = _note(path)
    assert delete_event(str(path), FIRST, 1) is True
    first, open_ = read_sessions(str(path))
    assert [(e[0], e[3]) for e in first.events] == [(2, "outage"), (rowid, "note")]
    assert (len(first.metrics), len(open_.metrics)) == (2, 1)
    assert _user_version(path) == version


@pytest.mark.asyncio
@pytest.mark.parametrize("version", formats.MONITOR_DB_WRITE_VERSIONS)
async def test_a_new_archive_is_stamped_with_the_declared_write_version(version, tmp_path):
    await _append_session(tmp_path / "new.db")
    assert _user_version(tmp_path / "new.db") == version


@pytest.mark.parametrize("version", model_formats.MONITOR_EXPORT_WRITE_VERSIONS)
def test_the_export_writers_stamp_the_declared_write_version(version, tmp_path):
    document = build_db_export(str(_archive(2, tmp_path)))
    assert version == export.EXPORT_FORMAT
    assert document.format == version
    assert json.loads(document_json(document))["format"] == version
    live = build_live_export(
        new_frame("live", None, now=datetime(2026, 7, 2, tzinfo=UTC)),
        MetricCollector(hosts=[]),
        LabSnapshot(),
    )
    assert live.format == version


def test_every_export_write_version_is_also_read():
    """Writers build a ``MonitorExport``, whose ``Literal`` is held equal to the read list."""
    assert set(model_formats.MONITOR_EXPORT_WRITE_VERSIONS) <= set(
        model_formats.MONITOR_EXPORT_READ_VERSIONS
    )
