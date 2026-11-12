"""Review mode in the library: load a saved export, serve it."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config.scope import MonitorSettings
from otto.monitor.errors import MonitorTlsError, ReviewSourceError
from otto.monitor.event_ops import MAX_SAFE_EVENT_ID
from otto.monitor.review import (
    check_editable_document,
    load_review_document,
    parse_review_json,
    serve_review,
    view_only_ids,
)
from tests._fixtures.fake_project import fake_project
from tests._fixtures.paths import CHECKOUT_ROOT

_REPEATED_IDS = CHECKOUT_ROOT / "web" / "fixtures" / "repeated-ids.json"


def _doc(*event_ids, session_ids=("s",)):
    events = [
        {"timestamp": "2026-07-01T08:00:00Z", **({} if i is None else {"id": i})} for i in event_ids
    ]
    return json.dumps(
        {
            "format": 1,
            "sessions": [
                {"id": sid, "start": "2026-07-01T08:00:00Z", "events": events}
                for sid in session_ids
            ],
        }
    ).encode()


def test_duplicate_session_ids_are_refused():
    with pytest.raises(ReviewSourceError, match="duplicate session id 'dup'"):
        parse_review_json(_doc(session_ids=("dup", "dup")), origin="x.json")


def test_event_id_beyond_browser_precision_is_refused():
    with pytest.raises(ReviewSourceError, match=rf"session 's'.*{2**53}"):
        parse_review_json(_doc(2**53), origin="x.json")
    loaded = parse_review_json(_doc(MAX_SAFE_EVENT_ID), origin="x.json")
    assert loaded.sessions[0].events[0].id == 2**53 - 1


@pytest.mark.parametrize("bad", [0, -4])
def test_non_positive_event_id_is_refused(bad):
    with pytest.raises(ReviewSourceError, match=rf"session 's'.*{bad}"):
        parse_review_json(_doc(1, bad), origin="x.json")


def test_repeated_ids_fixture_loads_with_view_only_ids():
    document = load_review_document(path=_REPEATED_IDS)
    assert view_only_ids(document.sessions[0]) == frozenset({3})


def test_parse_review_json_names_its_origin():
    with pytest.raises(ReviewSourceError, match=r"upload\.json"):
        parse_review_json(b'{"format": 99}', origin="upload.json")
    with pytest.raises(ReviewSourceError, match=r"other\.json"):
        parse_review_json(_doc(0), origin="other.json")


def test_db_result_goes_through_the_same_validator(tmp_path):
    src = tmp_path / "x.db"
    src.write_bytes(b"")
    document = parse_review_json(_doc(1), origin="x")
    document.sessions[0].events[0].id = 0
    with (
        patch("otto.monitor.export.build_db_export", return_value=document),
        pytest.raises(ReviewSourceError, match="session 's'"),
    ):
        load_review_document(path=src)
    check_editable_document(parse_review_json(_doc(1), origin="x"), origin="x")


def test_unsupported_suffix_is_refused_naming_the_suffix(tmp_path):
    src = tmp_path / "x.csv"
    src.write_text("a,b")
    with pytest.raises(ReviewSourceError, match=r"suffix '\.csv'") as e:
        load_review_document(path=src)
    assert e.value.field == "source"


def test_invalid_json_export_is_refused(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 99}')
    with pytest.raises(ReviewSourceError, match="not a valid format:1 monitor export"):
        load_review_document(path=src)


def test_valid_json_export_loads(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    assert load_review_document(path=src).sessions == []


def test_unsupported_db_is_refused(tmp_path):
    src = tmp_path / "x.db"
    src.write_bytes(b"garbage")
    with pytest.raises(ReviewSourceError, match="not a monitor database"):
        load_review_document(path=src)


@pytest.mark.asyncio
async def test_serve_review_resolves_tls_from_the_given_projects_before_serving(tmp_path):
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    bad = fake_project("r", monitor_settings=MonitorSettings(tls_cert=tmp_path / "missing.pem"))
    with (
        patch("otto.monitor.server.MonitorServer") as server_cls,
        pytest.raises(MonitorTlsError),
    ):
        await serve_review(path=src, projects=[bad])
    server_cls.assert_not_called()


@pytest.mark.asyncio
async def test_serve_review_serves_a_db_with_its_archive_path(tmp_path):
    src = tmp_path / "x.db"
    server = MagicMock(serve=AsyncMock())
    with (
        patch("otto.monitor.review.load_review_document", return_value=MagicMock()),
        patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls,
    ):
        await serve_review(path=src, projects=[])
    kwargs = server_cls.call_args.kwargs
    assert (kwargs["mode"], kwargs["source_name"], kwargs["archive_path"]) == (
        "review",
        "x.db",
        src,
    )
    assert kwargs["tls_cert"] is None
    server.serve.assert_awaited_once()


@pytest.mark.asyncio
async def test_serve_review_serves_a_json_source_editable(tmp_path):
    from otto.monitor.server import MonitorServer

    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    served: list[MonitorServer] = []

    async def fake_serve(self):
        served.append(self)

    with patch.object(MonitorServer, "serve", fake_serve):
        await serve_review(path=src, projects=[])
    server = served[0]
    assert server.source_name == "x.json"
    assert server.unsaved_changes == 0
    current = server.source_slot.current
    assert current is not None
    assert current.kind == "json"


async def _stop_with_unsaved(tmp_path, unsaved, capsys, *, raises=None):
    from otto.monitor.server import MonitorServer

    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')

    async def fake_serve(self):
        current = self.source_slot.current
        assert current is not None
        current.revision += unsaved  # stand-in for `unsaved` acknowledged-less writes
        if raises is not None:
            raise raises

    with patch.object(MonitorServer, "serve", fake_serve):
        if raises is None:
            await serve_review(path=src, projects=[])
        else:
            with pytest.raises(type(raises)):
                await serve_review(path=src, projects=[])
    return capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unsaved", "text"),
    [
        (1, "Warning: 1 unsaved change to x.json since the last export is lost."),
        (3, "Warning: 3 unsaved changes to x.json since the last export are lost."),
    ],
)
async def test_stop_with_unsaved_edits_warns_with_the_count(tmp_path, capsys, unsaved, text):
    out = " ".join((await _stop_with_unsaved(tmp_path, unsaved, capsys)).split())
    assert text in out


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [KeyboardInterrupt(), asyncio.CancelledError()])
async def test_an_interrupted_serve_still_warns_and_propagates(tmp_path, capsys, exc):
    out = " ".join((await _stop_with_unsaved(tmp_path, 2, capsys, raises=exc)).split())
    assert "Warning: 2 unsaved changes to x.json" in out


@pytest.mark.asyncio
async def test_serve_review_of_a_db_reports_no_unsaved_changes(tmp_path):
    from otto.monitor.server import MonitorServer

    src = tmp_path / "x.db"
    src.write_bytes(b"")
    served: list[MonitorServer] = []

    async def fake_serve(self):
        served.append(self)

    document = parse_review_json(_doc(1), origin="x")
    with (
        patch("otto.monitor.review.load_review_document", return_value=document),
        patch.object(MonitorServer, "serve", fake_serve),
    ):
        await serve_review(path=src, projects=[])
    current = served[0].source_slot.current
    assert current is not None
    assert current.kind == "db"
    assert served[0].unsaved_changes == 0


@pytest.mark.asyncio
async def test_stop_with_nothing_unsaved_is_silent(tmp_path, capsys):
    assert "unsaved" not in await _stop_with_unsaved(tmp_path, 0, capsys)


@pytest.mark.asyncio
async def test_serve_review_hands_the_server_the_declared_cert_and_key(tmp_path, tls_pair):
    cert, key = tls_pair
    src = tmp_path / "x.json"
    src.write_text('{"format": 1, "sessions": []}')
    project = fake_project("r", monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key))
    server = MagicMock(serve=AsyncMock())
    with patch("otto.monitor.server.MonitorServer", return_value=server) as server_cls:
        await serve_review(path=src, projects=[project])
    kwargs = server_cls.call_args.kwargs
    assert (kwargs["tls_cert"], kwargs["tls_key"]) == (cert, key)
