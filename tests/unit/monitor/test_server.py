"""
Unit/integration tests for MonitorServer.

Covers:
  - Port 0 assigns an ephemeral port (not uvicorn's default 8000)
  - Two servers with port=0 bind to different ports simultaneously
  - Explicit port is respected
  - A busy port raises RuntimeError (not uvicorn's raw SystemExit)
  - Display host resolution for 0.0.0.0
"""

import asyncio
import contextlib
import fcntl
import functools
import json
import logging
import os
import socket
import sqlite3
import ssl
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio
from _archive import _make_archive
from _events import BLANKS
from _requests import asgi, settle

from otto.models import LabSnapshot, MonitorExport, SessionRecord
from otto.models.monitor import EventRecord
from otto.monitor import archive_edit, event_ops, review_source
from otto.monitor import server as server_module
from otto.monitor.collector import MetricCollector
from otto.monitor.events import MonitorEvent
from otto.monitor.export import build_db_export, document_json
from otto.monitor.review import parse_review_json
from otto.monitor.review_source import (
    MAX_SOURCE_UPLOAD_BYTES,
    ReviewSource,
    SourceSlot,
    UnknownSessionError,
    UploadTooLarge,
)
from otto.monitor.server import (
    OBSERVED_AT_HEADER,
    SOURCE_NAME_HEADER,
    SOURCE_TOKEN_HEADER,
    UNSAVED_HEADER,
    MonitorServer,
    _build_app,
)
from otto.monitor.session import new_frame
from tests._fixtures._fake_collector import FakeCollector
from tests._fixtures._source_token import FROM_MODE, MODE_PATH, FromMode, resolve_token
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.ports import reserved_port

_FIXED_NOW = datetime(2030, 6, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
"""A fixed server clock for patching :func:`otto.monitor.event_ops.utc_now`."""

_ARCHIVED_START = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
"""The start of the ``.json`` review's one session, where the ``_make_archive`` one starts."""


def _empty_collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


@dataclass
class _RunningServer:
    """Thin fixture handle: the booted server plus what a test pokes it with.

    ``collector``/``document`` are surfaced directly (not via ``.server.*``)
    so specs read ``live_server.collector.session_id`` /
    ``review_server.document.sessions[0].id`` without a level of indirection.
    """

    server: MonitorServer
    collector: MetricCollector
    document: MonitorExport | None = None
    _task: "asyncio.Task[None] | None" = None

    @property
    def key(self) -> str:
        return self.server.key

    @property
    def port(self) -> int:
        # Same private-attribute access other tests in this file already use.
        return self.server._port

    async def aclose(self) -> None:
        """Stop the server and wait for its ``serve()`` task to finish.

        Only meaningful for handles built via ``_boot``/``_review_server``
        (below): the ``live_server``/``review_server`` fixtures stop
        themselves at fixture teardown and never set ``_task``, so calling
        this on one of their handles would be a no-op either way.
        """
        self.server.stop()
        if self._task is not None:
            await self._task


async def _request_json(
    handle: _RunningServer, method: str, path: str, payload: dict[str, object] | None
) -> tuple[int, dict[str, object]]:
    """POST/PATCH/DELETE against *handle*'s server, returning ``(status, json-body)``."""
    status, _, body = await _request(handle, method, path, payload)
    return status, body


async def _request(
    handle: _RunningServer,
    method: str,
    path: str,
    payload: dict[str, object] | None,
    *,
    token: "str | FromMode | None" = FROM_MODE,
) -> tuple[int, dict[str, str], dict[str, object]]:
    """POST/PATCH/DELETE against *handle*'s server: ``(status, headers, json-body)``.

    Header names are lowercased: HTTP names are case-insensitive, and the
    server sends them lowercase.

    A write (any method but GET) carries ``SOURCE_TOKEN_HEADER``: by default
    the token ``/api/mode`` reports for the served source, as a tab sends it;
    ``token=None`` omits the header and a string sends that token.

    Every route sits behind the access-key middleware (``?key=``), attached
    here so call sites never repeat it. ``urllib`` raises ``HTTPError`` for
    any non-2xx response, so both outcomes are normalized to the same return
    shape instead of forcing every caller to catch it individually — these
    tests assert 2xx *and* 404/409/422/403 bodies alike.
    """

    async def get_mode() -> object:
        _, _, mode = await _request(handle, "GET", MODE_PATH, None)
        return mode

    token = await resolve_token(token, method, get_mode)
    url = f"http://127.0.0.1:{handle.port}{path}?key={handle.key}"
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    if token is not None:
        headers[SOURCE_TOKEN_HEADER] = token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    def _do() -> tuple[int, dict[str, str], dict[str, object]]:
        try:
            with contextlib.closing(urllib.request.urlopen(req)) as resp:
                body = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, headers, (json.loads(body) if body else {})
        except urllib.error.HTTPError as err:
            with contextlib.closing(err):
                body = err.read()
            headers = {k.lower(): v for k, v in err.headers.items()}
            return err.code, headers, (json.loads(body) if body else {})

    return await asyncio.to_thread(_do)


async def post_json(
    handle: _RunningServer, path: str, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    return await _request_json(handle, "POST", path, payload)


async def patch_json(
    handle: _RunningServer, path: str, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    return await _request_json(handle, "PATCH", path, payload)


async def delete_(handle: _RunningServer, path: str) -> tuple[int, dict[str, object]]:
    return await _request_json(handle, "DELETE", path, None)


async def get_text(handle: _RunningServer, path: str) -> str:
    """GET *path* against *handle*'s server, returning the raw response body as text.

    Unlike ``_request_json``, this doesn't parse the body — the review-mode
    tests need the raw ``/api/monitor_sessions`` document text to feed
    straight into ``json.loads``, mirroring how a real client consumes it.
    """
    url = f"http://127.0.0.1:{handle.port}{path}?key={handle.key}"

    def _do() -> str:
        with contextlib.closing(urllib.request.urlopen(url)) as resp:
            return resp.read().decode()

    return await asyncio.to_thread(_do)


async def _boot(
    server: MonitorServer, *, collector: MetricCollector, document: MonitorExport | None = None
) -> _RunningServer:
    """Start *server*, returning a handle the caller closes itself via ``.aclose()``.

    The ``live_server``/``review_server`` fixtures below inline this same
    boot loop with fixture-scoped teardown (one server per test); tests that
    need more than one server — or a review server whose ``archive_path``
    varies per case (``TestReviewDbEditing``) — build one directly here
    instead and are responsible for closing it.
    """
    task = asyncio.create_task(server.serve())
    await server.wait_started()
    return _RunningServer(server=server, collector=collector, document=document, _task=task)


async def _review_server(
    *, document: MonitorExport, source: str, archive_path: Path | None
) -> _RunningServer:
    """Build + boot a review-mode ``MonitorServer`` serving *document*."""
    collector = MetricCollector(targets=[])
    server = MonitorServer(
        collector,
        host="127.0.0.1",
        port=0,
        mode="review",
        document=document,
        source_name=source,
        archive_path=archive_path,
    )
    return await _boot(server, collector=collector, document=document)


@pytest_asyncio.fixture
async def live_server() -> AsyncGenerator[_RunningServer, None]:
    """A booted live ``MonitorServer`` with a stamped session (Plan 5c route specs)."""
    collector = FakeCollector()
    frame = new_frame(label=None, note=None)
    server = MonitorServer(
        collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
    )
    task = asyncio.create_task(server.serve())
    await server.wait_started()
    try:
        yield _RunningServer(server=server, collector=collector)
    finally:
        server.stop()
        await task


@pytest_asyncio.fixture
async def review_server() -> AsyncGenerator[_RunningServer, None]:
    """A booted review-mode ``MonitorServer`` serving a one-session ``.json`` document."""
    collector = _empty_collector()
    document = MonitorExport(
        format=1,
        sessions=[SessionRecord(id="archived", start=datetime.now(tz=timezone.utc))],
    )
    server = MonitorServer(
        collector,
        host="127.0.0.1",
        port=0,
        mode="review",
        document=document,
        source_name="archive.json",
        archive_path=None,  # a .json review: the document is the store
    )
    task = asyncio.create_task(server.serve())
    await server.wait_started()
    try:
        yield _RunningServer(server=server, collector=collector, document=document)
    finally:
        server.stop()
        await task


async def _start_and_stop(server: MonitorServer) -> int:
    """Start the server, capture the bound port, then stop immediately."""
    task = asyncio.create_task(server.serve())
    await server.wait_started()

    port = server._port
    server.stop()
    await task
    return port


class TestPortBinding:
    @pytest.mark.asyncio
    async def test_port_zero_avoids_default_8000(self):
        """port=0 should ask the OS for an ephemeral port, NOT fall back to 8000."""
        server = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
        port = await _start_and_stop(server)
        assert port != 0, "Port should have been assigned by the OS"
        assert port != 8000, (
            "port=0 should use an OS-assigned ephemeral port, "
            "not uvicorn default 8000 — is port= being passed to uvicorn.Config?"
        )

    @pytest.mark.asyncio
    async def test_two_servers_get_different_ports(self):
        """Two servers with port=0 must bind successfully to different ports."""
        server_a = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
        server_b = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)

        task_a = asyncio.create_task(server_a.serve())
        await server_a.wait_started()

        task_b = asyncio.create_task(server_b.serve())
        await server_b.wait_started()

        port_a = server_a._port
        port_b = server_b._port

        server_a.stop()
        server_b.stop()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

        assert port_a != port_b, f"Both servers bound to the same port {port_a}"

    @pytest.mark.asyncio
    async def test_explicit_port_is_used(self):
        """An explicit port should be passed through to uvicorn, not ignored."""
        # Reserved, not probed-and-released: a released port is anyone's
        # until uvicorn binds it (#968; see `tests/_fixtures/ports.py`).
        with reserved_port() as port:
            server = MonitorServer(_empty_collector(), host="127.0.0.1", port=port)
            actual = await _start_and_stop(server)
        assert actual == port, (
            f"Requested port {port} but server bound to {actual} — "
            "is port= being passed to uvicorn.Config?"
        )

    @pytest.mark.asyncio
    async def test_busy_port_raises_instead_of_system_exit(self):
        """A failed bind must surface as a catchable RuntimeError.

        uvicorn answers it with sys.exit(STARTUP_FAILURE), and a SystemExit
        raised inside serve()'s inner task is re-raised into the event loop
        itself — past every except clause and task.result() in the embedding
        application — killing the host process's asyncio.run() outright.
        serve() must translate it into a normal exception before it crosses
        the task boundary.
        """
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]

            server = MonitorServer(_empty_collector(), host="127.0.0.1", port=busy_port)
            with pytest.raises(RuntimeError, match="already in use"):
                await server.serve()


class TestDeleteEndpoint:
    """DELETE /api/session/{session_id}/event/{event_id} answers with the deleted record."""

    @pytest.mark.asyncio
    async def test_delete_returns_the_deleted_record(self):
        """Deleting an existing event answers 200 with the record it removed (#929).

        The dashboard's undo restores a deleted event from this body. Tests
        the ASGI app directly. Builds the app with a frame/lab (mirroring
        what ``MonitorServer.__init__`` does for a live server) and stamps
        ``collector.session_id`` itself since ``_build_app`` is called
        directly here, bypassing that constructor step — the path must
        match for ``_live_guard`` to let the request through.
        """
        collector = _empty_collector()
        frame = new_frame(label=None, note=None)
        collector.session_id = frame.id
        event = await collector.add_event(label="test-event")

        app = _build_app(collector, key="k", frame=frame, lab=LabSnapshot())

        path = f"/api/session/{frame.id}/event/{event.id}"
        status_code, record = await asgi(app, "DELETE", path)

        assert status_code == 200
        assert record is not None
        assert record["id"] == event.id
        assert record["label"] == "test-event"

    @pytest.mark.asyncio
    async def test_delete_nonexistent_event_returns_404(self):
        """Deleting a non-existent event returns 404 with an error body."""
        collector = _empty_collector()
        frame = new_frame(label=None, note=None)
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        handle = await _boot(server, collector=collector)
        try:
            status, body = await delete_(handle, f"/api/session/{frame.id}/event/9999")
            assert status == 404
            assert body["error"]
        finally:
            await handle.aclose()


class TestSessionEventRoutes:
    """Plan 5c: the session-aware event CRUD surface (live mode)."""

    @pytest.mark.asyncio
    async def test_create_returns_201_format1_record(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        status, body = await post_json(
            live_server, f"/api/session/{sid}/event", {"label": "deploy"}
        )
        assert status == 201
        assert body["label"] == "deploy"
        assert body["source"] == "manual"
        assert isinstance(body["id"], int)
        assert "end_timestamp" not in body  # exclude_none: a point event omits it

    @pytest.mark.asyncio
    async def test_create_with_wrong_session_404s(self, live_server: _RunningServer) -> None:
        status, _ = await post_json(live_server, "/api/session/nope/event", {"label": "x"})
        assert status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", BLANKS)
    async def test_create_blank_label_422s_and_stores_nothing(
        self, live_server: _RunningServer, blank: str
    ) -> None:
        """The HTTP create refuses the same blank labels the library writes do (#964)."""
        sid = live_server.collector.session_id
        status, _ = await post_json(live_server, f"/api/session/{sid}/event", {"label": blank})
        assert status == 422
        assert live_server.collector.get_events() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", BLANKS)
    async def test_patch_resending_a_legacy_blank_label_recolours(
        self, live_server: _RunningServer, blank: str
    ) -> None:
        """A PATCH obeys the library's label rule: an unchanged blank label is put back.

        Recolouring an older event whose label is already blank succeeds even
        when the PATCH resends that label, as a library update does, while a
        PATCH that blanks a labelled event is still refused (#964).
        """
        collector = live_server.collector
        sid = collector.session_id
        collector._store.note_imported_event(
            MonitorEvent(timestamp=datetime.now(timezone.utc), label=blank, id=41)
        )
        status, body = await patch_json(
            live_server, f"/api/session/{sid}/event/41", {"label": blank, "color": "#ff0000"}
        )
        assert status == 200
        assert body["color"] == "#ff0000"
        assert body["label"] == blank

        _, created = await post_json(live_server, f"/api/session/{sid}/event", {"label": "keep"})
        status, _ = await patch_json(
            live_server, f"/api/session/{sid}/event/{created['id']}", {"label": blank}
        )
        assert status == 422
        assert next(e for e in collector.get_events() if e.id == created["id"]).label == "keep"

    @pytest.mark.asyncio
    async def test_create_span_end_before_resolved_now_422s(
        self, live_server: _RunningServer
    ) -> None:
        sid = live_server.collector.session_id
        status, _ = await post_json(
            live_server,
            f"/api/session/{sid}/event",
            {"label": "x", "end_timestamp": "2020-01-01T00:00:00+00:00"},  # < server-now
        )
        assert status == 422

    @pytest.mark.asyncio
    async def test_end_stamps_now_and_second_end_409s(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(live_server, f"/api/session/{sid}/event", {"label": "soak"})
        status, ended = await post_json(
            live_server, f"/api/session/{sid}/event/{created['id']}/end", {}
        )
        assert status == 200
        assert ended["end_timestamp"] is not None
        status, _ = await post_json(
            live_server, f"/api/session/{sid}/event/{created['id']}/end", {}
        )
        assert status == 409

    @pytest.mark.asyncio
    async def test_patch_moves_timestamp_and_clears_end(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(
            live_server,
            f"/api/session/{sid}/event",
            {
                "label": "x",
                "timestamp": "2026-07-18T12:00:00+00:00",
                "end_timestamp": "2026-07-18T12:05:00+00:00",
            },
        )
        status, patched = await patch_json(
            live_server,
            f"/api/session/{sid}/event/{created['id']}",
            {"timestamp": "2026-07-18T11:55:00+00:00", "end_timestamp": None},
        )
        assert status == 200
        assert patched["timestamp"].startswith("2026-07-18T11:55")
        assert "end_timestamp" not in patched  # explicit null cleared it (span -> point)

    @pytest.mark.asyncio
    async def test_patch_that_inverts_merged_span_422s(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(
            live_server,
            f"/api/session/{sid}/event",
            {
                "label": "x",
                "timestamp": "2026-07-18T12:00:00+00:00",
                "end_timestamp": "2026-07-18T12:05:00+00:00",
            },
        )
        status, _ = await patch_json(
            live_server,
            f"/api/session/{sid}/event/{created['id']}",
            {"timestamp": "2026-07-18T12:10:00+00:00"},  # start past the kept end
        )
        assert status == 422

    @pytest.mark.asyncio
    async def test_delete_removes_event_and_second_delete_404s(
        self, live_server: _RunningServer
    ) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(live_server, f"/api/session/{sid}/event", {"label": "x"})
        status, deleted = await delete_(live_server, f"/api/session/{sid}/event/{created['id']}")
        assert status == 200
        assert deleted["id"] == created["id"]
        status, _ = await delete_(live_server, f"/api/session/{sid}/event/{created['id']}")
        assert status == 404

    @pytest.mark.asyncio
    async def test_event_routes_send_observed_at_header(
        self, live_server: _RunningServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Create, end, update and delete each tell the client the server's time (#866).

        The dashboard applies a mutation's response as a fragment, so the
        header raises the live window's horizon even when the SSE echo is
        withheld or late: a mark on a stalled collector shows at once. The
        header, a Mark-now timestamp and a Stop stamp all read the ONE server
        clock, so a fixed clock makes each of them exactly equal to it.
        """
        monkeypatch.setattr(event_ops, "utc_now", lambda: _FIXED_NOW)
        sid = live_server.collector.session_id
        base = f"/api/session/{sid}/event"

        def assert_stamped(headers: dict[str, str]) -> None:
            assert datetime.fromisoformat(headers[OBSERVED_AT_HEADER.lower()]) == _FIXED_NOW

        status, headers, created = await _request(live_server, "POST", base, {"label": "x"})
        assert status == 201
        assert_stamped(headers)
        assert EventRecord.model_validate(created).timestamp == _FIXED_NOW
        # The fixed clock would stamp end == start, which is refused: end a
        # span that started an hour earlier instead.
        started = (_FIXED_NOW - timedelta(hours=1)).isoformat()
        _, _, span = await _request(live_server, "POST", base, {"label": "s", "timestamp": started})
        status, headers, ended = await _request(live_server, "POST", f"{base}/{span['id']}/end", {})
        assert status == 200
        assert_stamped(headers)
        assert EventRecord.model_validate(ended).end_timestamp == _FIXED_NOW
        status, headers, _ = await _request(
            live_server, "PATCH", f"{base}/{created['id']}", {"label": "y"}
        )
        assert status == 200
        assert_stamped(headers)
        status, headers, _ = await _request(live_server, "DELETE", f"{base}/{created['id']}", None)
        assert status == 200
        assert_stamped(headers)

    @pytest.mark.asyncio
    async def test_review_responses_carry_the_stamp(self, review_server: _RunningServer) -> None:
        """A review write is stamped like a live one: the stamp orders it against a snapshot."""
        assert review_server.document is not None
        sid = review_server.document.sessions[0].id
        status, headers, _ = await _request(
            review_server, "POST", f"/api/session/{sid}/event", {"label": "x"}
        )
        assert status == 201
        assert _instant(headers[OBSERVED_AT_HEADER.lower()]).tzinfo is not None


class TestSourceState:
    """``/api/mode`` reports the served source; a live write must name it by its token."""

    @pytest.mark.asyncio
    async def test_mode_reports_the_source_state(
        self, live_server: _RunningServer, review_server: _RunningServer
    ) -> None:
        status, review = await _request_json(review_server, "GET", "/api/mode", None)
        assert status == 200
        assert (review["mode"], review["kind"], review["source"]) == (
            "review",
            "json",
            "archive.json",
        )
        assert review["editable"] is True
        assert (review["unsaved"], review["revision"]) == (0, 0)
        status, live = await _request_json(live_server, "GET", "/api/mode", None)
        assert status == 200
        assert (live["mode"], live["kind"], live["source"]) == ("live", "live", None)
        assert live["editable"] is True
        for body in (review, live):
            assert isinstance(body["token"], str)
            assert body["token"]
            assert _instant(body["observed_at"]).tzinfo is not None
        assert review["token"] != live["token"]

    @pytest.mark.asyncio
    async def test_live_token_is_stable_for_the_server(self, live_server: _RunningServer) -> None:
        _, first = await _request_json(live_server, "GET", "/api/mode", None)
        _, again = await _request_json(live_server, "GET", "/api/mode", None)
        assert first["token"] == again["token"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", [None, "wrong"])
    async def test_live_write_without_or_with_a_wrong_token_409s_source_changed(
        self, live_server: _RunningServer, token: str | None
    ) -> None:
        sid = live_server.collector.session_id
        _, mode = await _request_json(live_server, "GET", "/api/mode", None)
        status, _, body = await _request(
            live_server, "POST", f"/api/session/{sid}/event", {"label": "x"}, token=token
        )
        assert status == 409
        assert body["code"] == "source_changed"
        assert isinstance(body["error"], str)
        state = body["source_state"]
        assert isinstance(state, dict)
        assert state["token"] == mode["token"]
        assert state["kind"] == "live"
        assert live_server.collector.get_events() == []

    @pytest.mark.asyncio
    async def test_live_write_with_the_token_is_accepted(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        status, _ = await post_json(live_server, f"/api/session/{sid}/event", {"label": "x"})
        assert status == 201

    @pytest.mark.asyncio
    async def test_snapshot_answers_token_and_capture_headers(
        self, live_server: _RunningServer, review_server: _RunningServer
    ) -> None:
        for handle in (live_server, review_server):
            _, mode = await _request_json(handle, "GET", "/api/mode", None)
            status, headers, body = await _request(handle, "GET", "/api/monitor_sessions", None)
            assert status == 200
            assert body["format"] == 1
            assert headers[SOURCE_TOKEN_HEADER.lower()] == mode["token"]
            stamp = _instant(headers[OBSERVED_AT_HEADER.lower()])
            assert stamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_live_snapshot_stamp_is_the_sessions_observed_at(
        self, live_server: _RunningServer
    ) -> None:
        """The header is the stamp the snapshot itself was built with, not a second read."""
        _, headers, body = await _request(live_server, "GET", "/api/monitor_sessions", None)
        sessions = body["sessions"]
        assert isinstance(sessions, list)
        assert _instant(sessions[0]["observed_at"]) == _instant(headers[OBSERVED_AT_HEADER.lower()])

    @pytest.mark.asyncio
    async def test_server_exposes_its_source_slot(
        self, live_server: _RunningServer, review_server: _RunningServer
    ) -> None:
        assert live_server.server.source_slot.current is None
        current = review_server.server.source_slot.current
        assert current is not None
        _, mode = await _request_json(review_server, "GET", "/api/mode", None)
        assert current.token == mode["token"]


class TestReviewDbEditing:
    """A review-mode ``.db`` archive is event-editable.

    A ``.db`` source gets a real archive built via ``_make_archive`` and
    mutations both patch the served document in place (no restart needed)
    and persist to the archive file itself (verified by re-reading it with
    ``build_db_export``).
    """

    @pytest.mark.asyncio
    async def test_mode_advertises_editable(self, tmp_path: Path) -> None:
        """Every source is editable: live, a .db review and a .json review."""
        collector = _empty_collector()
        frame = new_frame(label=None, note=None)
        live = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        db_path, _sid = await asyncio.to_thread(_make_archive, tmp_path)
        json_document = MonitorExport(
            format=1,
            sessions=[SessionRecord(id="archived", start=datetime.now(tz=timezone.utc))],
        )

        live_handle = await _boot(live, collector=collector)
        db_handle = await _review_server(
            document=build_db_export(db_path), source=db_path, archive_path=Path(db_path)
        )
        json_handle = await _review_server(
            document=json_document, source="archive.json", archive_path=None
        )
        try:
            for handle, expected in (
                (live_handle, True),
                (db_handle, True),
                (json_handle, True),
            ):
                status, body = await _request_json(handle, "GET", "/api/mode", None)
                assert status == 200
                assert body["editable"] is expected
        finally:
            await live_handle.aclose()
            await db_handle.aclose()
            await json_handle.aclose()

    @pytest.mark.asyncio
    async def test_create_persists_and_updates_served_document(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        server = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        try:
            status, body = await post_json(
                server,
                f"/api/session/{sid}/event",
                {"label": "post-hoc note", "timestamp": "2026-07-18T12:01:00+00:00"},
            )
            assert status == 201
            assert body["label"] == "post-hoc note"
            # 1) the served document reflects it immediately (no restart)
            doc = json.loads(await get_text(server, "/api/monitor_sessions"))
            assert [e for e in doc["sessions"][0]["events"] if e["label"] == "post-hoc note"]
            # 2) it survives a fresh read of the archive (a restart)
            fresh = build_db_export(path)
            assert [e for e in fresh.sessions[0].events if e.label == "post-hoc note"]
        finally:
            await server.aclose()

    @pytest.mark.asyncio
    async def test_locked_archive_409s(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        server = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        try:
            fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                status, body = await post_json(server, f"/api/session/{sid}/event", {"label": "x"})
                assert status == 409
                assert body["code"] == "busy"
            finally:
                os.close(fd)
        finally:
            await server.aclose()

    @pytest.mark.asyncio
    async def test_delete_and_patch_round_trip(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        server = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        try:
            status, created = await post_json(
                server, f"/api/session/{sid}/event", {"label": "draft"}
            )
            assert status == 201

            status, patched = await patch_json(
                server, f"/api/session/{sid}/event/{created['id']}", {"label": "final"}
            )
            assert status == 200
            assert patched["label"] == "final"
            doc = json.loads(await get_text(server, "/api/monitor_sessions"))
            events = doc["sessions"][0]["events"]
            assert any(e["label"] == "final" for e in events)
            assert not any(e["label"] == "draft" for e in events)

            status, deleted = await delete_(server, f"/api/session/{sid}/event/{created['id']}")
            assert status == 200
            assert deleted["id"] == created["id"]
            assert deleted["label"] == "final"

            doc = json.loads(await get_text(server, "/api/monitor_sessions"))
            assert not any(e["id"] == created["id"] for e in doc["sessions"][0]["events"])
            fresh = build_db_export(path)
            assert not any(e.id == created["id"] for e in fresh.sessions[0].events)
        finally:
            await server.aclose()


@dataclass
class _Served:
    """A booted server over one of the three backends, with the ids a revert test needs."""

    handle: _RunningServer
    sid: str
    mode: str
    archive: str | None = None

    @property
    def base(self) -> str:
        return f"/api/session/{self.sid}/event"

    async def events(self) -> dict[int, dict[str, object]]:
        """The served document's events by id: what a fresh tab would see."""
        doc = json.loads(await get_text(self.handle, "/api/monitor_sessions"))
        return {e["id"]: e for e in doc["sessions"][0]["events"]}

    async def create(self, **fields: object) -> dict[str, object]:
        status, body = await post_json(
            self.handle,
            self.base,
            {"label": "x", "timestamp": "2026-07-18T12:01:00+00:00", **fields},
        )
        assert status == 201
        return body

    async def revert(
        self,
        event_id: int,
        expect: dict[str, object] | None,
        write: dict[str, object] | None,
    ) -> tuple[int, dict[str, str], object]:
        return await _request(
            self.handle,
            "POST",
            f"{self.base}/{event_id}/revert",
            {"expect": expect, "write": write},
        )


@pytest_asyncio.fixture(params=["live", "db", "json"])
async def served(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncGenerator[_Served, None]:
    """The event routes' three backends behind one handle: live, a ``.db``, a ``.json`` review."""
    if request.param == "live":
        collector = FakeCollector()
        frame = new_frame(label=None, note=None)
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        handle = await _boot(server, collector=collector)
        yield _Served(handle, collector.session_id, "live")
    elif request.param == "db":
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        handle = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        yield _Served(handle, sid, "db", archive=path)
    else:
        document = MonitorExport(
            format=1, sessions=[SessionRecord(id="archived", start=_ARCHIVED_START)]
        )
        handle = await _review_server(document=document, source="x.json", archive_path=None)
        yield _Served(handle, "archived", "json")
    await handle.aclose()


@contextlib.contextmanager
def _held_archive_lock(path: str):
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class TestEventRevertRoute:
    """``POST .../event/{id}/revert`` and the DELETE-returns-its-record contract (#929)."""

    @pytest.mark.asyncio
    async def test_delete_returns_the_deleted_record(self, served: _Served) -> None:
        created = await served.create(label="doomed")
        status, deleted = await delete_(served.handle, f"{served.base}/{created['id']}")
        assert status == 200
        assert deleted == created

    @pytest.mark.asyncio
    async def test_revert_changed_carries_current(self, served: _Served) -> None:
        created = await served.create()
        await patch_json(served.handle, f"{served.base}/{created['id']}", {"label": "other"})
        status, _, body = await served.revert(created["id"], created, {**created, "label": "z"})
        assert status == 409
        assert body["code"] == "changed"
        assert body["current"]["label"] == "other"
        assert (await served.events())[created["id"]]["label"] == "other"

    @pytest.mark.asyncio
    async def test_revert_missing_event_is_changed_with_null_current(self, served: _Served) -> None:
        created = await served.create()
        await delete_(served.handle, f"{served.base}/{created['id']}")
        status, _, body = await served.revert(created["id"], created, None)
        assert status == 409
        assert body["code"] == "changed"
        assert body["current"] is None

    @pytest.mark.asyncio
    async def test_revert_busy_code(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        handle = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        served = _Served(handle, sid, "db", archive=path)
        try:
            created = await served.create()
            with _held_archive_lock(path):
                status, _, body = await served.revert(created["id"], created, None)
            assert status == 409
            assert body["code"] == "busy"
            assert created["id"] in await served.events()
        finally:
            await handle.aclose()

    @pytest.mark.asyncio
    async def test_existing_routes_busy_code(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        handle = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        served = _Served(handle, sid, "db", archive=path)
        try:
            created = await served.create()
            eid = created["id"]
            with _held_archive_lock(path):
                results = [
                    await post_json(handle, served.base, {"label": "n"}),
                    await patch_json(handle, f"{served.base}/{eid}", {"label": "p"}),
                    await _request_json(handle, "POST", f"{served.base}/{eid}/end", {}),
                    await delete_(handle, f"{served.base}/{eid}"),
                ]
            for status, body in results:
                assert status == 409
                assert body["code"] == "busy"
        finally:
            await handle.aclose()

    @pytest.mark.asyncio
    async def test_revert_is_observed(self, served: _Served) -> None:
        created = await served.create()
        status, headers, _ = await served.revert(created["id"], created, None)
        assert status == 200
        assert OBSERVED_AT_HEADER.lower() in headers

    @pytest.mark.asyncio
    async def test_delete_then_create_then_undo_delete_keeps_both(self, served: _Served) -> None:
        """Undoing a delete after a later create puts the deleted event back beside it."""
        a = await served.create(label="a")
        status, deleted = await delete_(served.handle, f"{served.base}/{a['id']}")
        assert status == 200
        b = await served.create(label="b")
        assert isinstance(a["id"], int)
        assert isinstance(b["id"], int)
        assert b["id"] > a["id"]
        status, _, restored = await served.revert(a["id"], None, deleted)
        assert status == 200
        assert isinstance(restored, dict)
        assert restored["id"] == a["id"]
        events = await served.events()
        assert {events[a["id"]]["label"], events[b["id"]]["label"]} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_revert_update_publishes(self, live_server: _RunningServer) -> None:
        sid = live_server.collector.session_id
        _, created = await post_json(
            live_server,
            f"/api/session/{sid}/event",
            {"label": "a", "timestamp": "2026-07-18T12:01:00+00:00"},
        )
        queue = live_server.collector.subscribe()
        while not queue.empty():
            queue.get_nowait()
        edited = {**created, "label": "b"}
        status, _, body = await _request(
            live_server,
            "POST",
            f"/api/session/{sid}/event/{created['id']}/revert",
            {"expect": created, "write": edited},
        )
        assert status == 200
        assert body["label"] == "b"
        fragment = await asyncio.wait_for(queue.get(), timeout=5)
        assert [e["label"] for e in fragment["events"]] == ["b"]

    @pytest.mark.asyncio
    async def test_revert_each_leg_round_trips(self, served: _Served) -> None:
        h = served.handle

        async def undo_redo(eid: int, before: dict | None, after: dict | None) -> None:
            status, _, body = await served.revert(eid, after, before)
            assert status == 200
            assert body == before
            assert (await served.events()).get(eid) == before
            status, _, body = await served.revert(eid, before, after)
            assert status == 200
            assert body == after
            assert (await served.events()).get(eid) == after

        # create
        created = await served.create(label="made")
        await undo_redo(created["id"], None, created)
        # update
        _, edited = await patch_json(h, f"{served.base}/{created['id']}", {"label": "edited"})
        await undo_redo(created["id"], created, edited)
        # end
        span = await served.create(label="span")
        status, _, ended = await _request(h, "POST", f"{served.base}/{span['id']}/end", {})
        assert status == 200
        await undo_redo(span["id"], span, ended)
        # delete
        status, deleted = await delete_(h, f"{served.base}/{created['id']}")
        assert status == 200
        await undo_redo(created["id"], deleted, None)

    @pytest.mark.asyncio
    async def test_stale_tab_delete_restores_the_servers_record(self, served: _Served) -> None:
        created = await served.create(label="mine")
        # The server's event changes behind the client's back.
        _, current = await patch_json(
            served.handle,
            f"{served.base}/{created['id']}",
            {"label": "theirs", "color": "#ff0000"},
        )
        status, deleted = await delete_(served.handle, f"{served.base}/{created['id']}")
        assert status == 200
        assert deleted == current  # not the client's stale copy
        status, _, restored = await served.revert(created["id"], None, deleted)
        assert status == 200
        assert restored["label"] == "theirs"
        assert restored["color"] == "#ff0000"
        assert (await served.events())[created["id"]]["label"] == "theirs"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "write_patch",
        [{"end_timestamp": "2026-07-18T12:00:00+00:00"}],  # end before start
        ids=["span-order"],
    )
    async def test_invalid_write_is_422_and_changes_nothing(
        self, served: _Served, write_patch: dict[str, object]
    ) -> None:
        created = await served.create()
        status, _, _ = await served.revert(created["id"], created, {**created, **write_patch})
        assert status == 422
        assert (await served.events())[created["id"]] == created
        status, _, _ = await served.revert(created["id"], None, {**created, **write_patch})
        assert status == 422

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "write_patch",
        [{"color": "red"}, {"dash": "wavy"}],
        ids=["legacy-colour", "unknown-dash"],
    )
    async def test_live_write_is_strict_and_review_restore_lenient(
        self, served: _Served, write_patch: dict[str, object]
    ) -> None:
        """A live event is held strictly: a style no write route accepts is a
        422 on both legs, never a 500. A review archive restores a legacy
        style unchanged (EventRecord's lenient import rule)."""
        created = await served.create()
        status, _, _ = await served.revert(created["id"], created, {**created, **write_patch})
        assert status == (422 if served.mode == "live" else 200)
        if served.mode == "live":
            assert (await served.events())[created["id"]] == created
        other = await served.create(label="gone")
        await delete_(served.handle, f"{served.base}/{other['id']}")
        status, _, _ = await served.revert(other["id"], None, {**other, **write_patch})
        assert status == (422 if served.mode == "live" else 200)
        if served.mode == "live":
            assert other["id"] not in await served.events()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pick", "restorable"),
        [
            (lambda hw: 0, False),
            (lambda hw: -1, False),
            (lambda hw: 1, True),
            (lambda hw: hw, True),
            (lambda hw: hw + 1, False),
        ],
        ids=["zero", "negative", "first", "high-water", "above-high-water"],
    )
    async def test_restore_takes_only_a_handed_out_id(
        self, served: _Served, pick: Callable[[int], int], restorable: bool
    ) -> None:
        """Both backends share one id rule: a restore may only reuse an id
        that was handed out, 1 up to the high-water mark."""
        first = await served.create(label="a")
        last = await served.create(label="b")
        assert first["id"] == 1
        for made in (first, last):
            await delete_(served.handle, f"{served.base}/{made['id']}")
        event_id = pick(last["id"])
        status, _, body = await served.revert(event_id, None, {**first, "id": event_id})
        if restorable:
            assert status == 200
            assert body["id"] == event_id
        else:
            assert status == 409
            assert body == {"error": body["error"], "code": "changed", "current": None}
            assert event_id not in await served.events()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("end", "forward"),
        [
            (None, True),
            ("2026-07-18T12:01:00.000001+00:00", True),
            ("2026-07-18T12:01:00+00:00", False),
            ("2026-07-18T12:00:59+00:00", False),
        ],
        ids=["point", "one-microsecond", "equal", "backwards"],
    )
    async def test_create_and_revert_share_the_span_rule(
        self, served: _Served, end: str | None, forward: bool
    ) -> None:
        """POST and a revert's write accept and refuse the same spans."""
        start = "2026-07-18T12:01:00+00:00"
        status, _ = await post_json(
            served.handle, served.base, {"label": "s", "timestamp": start, "end_timestamp": end}
        )
        assert status == (201 if forward else 422)
        created = await served.create()
        write = {**created, "timestamp": start, "end_timestamp": end}
        status, _, _ = await served.revert(created["id"], created, write)
        assert status == (200 if forward else 422)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["", "   "], ids=["empty", "blank"])
    async def test_create_and_revert_share_the_label_rule(
        self, served: _Served, label: str
    ) -> None:
        """POST and a revert's write refuse a blank label with the same status,
        on the restore leg and on an update that changes the label."""
        status, _ = await post_json(served.handle, served.base, {"label": label})
        assert status == 422
        created = await served.create()
        status, _, _ = await served.revert(created["id"], created, {**created, "label": label})
        assert status == 422
        assert (await served.events())[created["id"]] == created
        await delete_(served.handle, f"{served.base}/{created['id']}")
        status, _, _ = await served.revert(created["id"], None, {**created, "label": label})
        assert status == 422
        assert created["id"] not in await served.events()

    @pytest.mark.asyncio
    async def test_revert_with_neither_state_is_422(self, served: _Served) -> None:
        status, _, _ = await served.revert(1, None, None)
        assert status == 422

    @pytest.mark.asyncio
    async def test_a_stale_token_is_refused_before_an_invalid_body(self, served: _Served) -> None:
        """Every backend refuses in one order: the token first, then the body. The same
        stale-token write with a body the write rule refuses answers 409 everywhere."""
        backward = {"label": "s", "timestamp": "2026-07-18T12:01:00+00:00"}
        backward["end_timestamp"] = "2026-07-18T12:00:00+00:00"
        # An end with no start passes the body model; the write rule refuses
        # it once the start resolves to now.
        ends_in_the_past = {"label": "s", "end_timestamp": "2000-01-01T00:00:00+00:00"}
        writes = [
            ("POST", served.base, ends_in_the_past),
            ("POST", f"{served.base}/1/revert", {"expect": None, "write": None}),
            ("POST", f"{served.base}/1/revert", {"expect": None, "write": {"id": 1, **backward}}),
        ]
        for method, path, body in writes:
            status, _, refused = await _request(served.handle, method, path, body, token="stale")
            assert (status, refused["code"]) == (409, "source_changed"), (path, body)

    @pytest.mark.asyncio
    async def test_revert_restore_into_unknown_session_404s(self, tmp_path: Path) -> None:
        path, _sid = await asyncio.to_thread(_make_archive, tmp_path)
        handle = await _review_server(
            document=build_db_export(path), source=path, archive_path=Path(path)
        )
        try:
            status, _, body = await _request(
                handle,
                "POST",
                "/api/session/nope/event/1/revert",
                {
                    "expect": None,
                    "write": {"id": 1, "timestamp": "2026-07-18T12:01:00+00:00", "label": "x"},
                },
            )
            assert status == 404
            assert body["error"] == str(UnknownSessionError("nope"))
        finally:
            await handle.aclose()


@dataclass
class _InLoop:
    """A review app served in the test's own loop, with the collector its frames go out on."""

    app: Any
    collector: MetricCollector
    slot: SourceSlot

    @property
    def source(self) -> ReviewSource:
        current = self.slot.current
        assert current is not None
        return current


def _in_loop(
    document: MonitorExport, *, name: str = "x.json", archive_path: Path | None = None
) -> _InLoop:
    collector = _empty_collector()
    source = ReviewSource(document, name=name, archive_path=archive_path, publish=collector.publish)
    slot = SourceSlot(source)
    return _InLoop(_build_app(collector, key="k", mode="review", slot=slot), collector, slot)


def _one_event_document(event_id: int = 1) -> MonitorExport:
    return MonitorExport(
        format=1,
        sessions=[
            SessionRecord(
                id="a",
                start=_ARCHIVED_START,
                events=[EventRecord(id=event_id, timestamp=_ARCHIVED_START, label="first")],
            )
        ],
    )


def _drain(queue: "asyncio.Queue[dict[str, Any]]") -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


SOURCE_PATH = "/api/source"
SAVED_PATH = "/api/source/saved"

_REPEATED_IDS = CHECKOUT_ROOT / "web" / "fixtures" / "repeated-ids.json"
_REPEATED_SID = "2026-07-01T08-00-00-repeated"


class TestReviewEvents:
    """Event writes to a review source: one sequence over the ``.db`` and ``.json`` backends."""

    @pytest.mark.asyncio
    async def test_json_revert_takes_the_lock_once(self) -> None:
        """Both revert legs run under ONE acquisition: a second would deadlock (asyncio's
        lock is not re-entrant), and the bounded wait fails the test instead of hanging."""
        rig = _in_loop(_one_event_document())
        base = "/api/session/a/event"
        first = rig.source.find_event("a", 1)
        assert first is not None
        stored = first.model_dump(mode="json")

        async def bounded(body: dict[str, object]) -> tuple[int | None, Any]:
            task = asyncio.ensure_future(asgi(rig.app, "POST", f"{base}/1/revert", body))
            # shield: a deadlocked section never ends, so the timeout must not wait on it.
            return await asyncio.wait_for(asyncio.shield(task), timeout=5)

        status, updated = await bounded({"expect": stored, "write": {**stored, "label": "y"}})
        assert status == 200
        assert updated["label"] == "y"
        status, deleted = await asgi(rig.app, "DELETE", f"{base}/1")
        assert status == 200
        status, restored = await bounded({"expect": None, "write": deleted})
        assert status == 200
        assert restored["id"] == 1
        assert not rig.collector.mutation_lock().locked()

    @pytest.mark.asyncio
    async def test_json_edit_counts_and_publishes_in_order(self) -> None:
        rig = _in_loop(_one_event_document())
        queue = rig.collector.subscribe()
        status, _ = await asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": "renamed"})
        assert status == 200
        fragment, frame = _drain(queue)
        assert fragment["session"] == "a"
        assert [e["label"] for e in fragment["events"]] == ["renamed"]
        assert "session" not in frame
        state = frame["source_state"]
        assert (state["unsaved"], state["revision"]) == (1, 1)
        assert state["token"] == rig.source.token
        assert frame["observed_at"] == state["observed_at"]

    @pytest.mark.asyncio
    async def test_db_edit_reaches_a_second_subscriber(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        rig = _in_loop(build_db_export(path), name="x.db", archive_path=Path(path))
        _, created = await asgi(rig.app, "POST", f"/api/session/{sid}/event", {"label": "a"})
        first, second = rig.collector.subscribe(), rig.collector.subscribe()
        status, _ = await asgi(
            rig.app, "PATCH", f"/api/session/{sid}/event/{created['id']}", {"label": "b"}
        )
        assert status == 200
        for queue in (first, second):
            [fragment] = _drain(queue)
            assert fragment["session"] == sid
            assert [e["label"] for e in fragment["events"]] == ["b"]
        assert rig.source.revision == 0

    @pytest.mark.asyncio
    async def test_db_read_repair_publishes_but_is_not_counted(self, tmp_path: Path) -> None:
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        rig = _in_loop(build_db_export(path), name="x.db", archive_path=Path(path))
        base = f"/api/session/{sid}/event"
        _, created = await asgi(rig.app, "POST", base, {"label": "mine"})
        conn = sqlite3.connect(path)
        try:
            conn.execute("UPDATE events SET label = 'theirs' WHERE id = ?", (created["id"],))
            conn.commit()
        finally:
            conn.close()
        queue = rig.collector.subscribe()
        status, body = await asgi(
            rig.app,
            "POST",
            f"{base}/{created['id']}/revert",
            {"expect": created, "write": {**created, "label": "undone"}},
        )
        assert status == 409
        assert body["code"] == "changed"
        [repair] = _drain(queue)
        assert [e["label"] for e in repair["events"]] == ["theirs"]
        assert (rig.source.revision, rig.source.unsaved) == (0, 0)

    @pytest.mark.asyncio
    async def test_db_write_refused_as_changed_repairs_and_is_not_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row changes between the read and the guarded write: the archive's
        refusal repairs the document to the row it carries, published, never counted."""
        path, sid = await asyncio.to_thread(_make_archive, tmp_path)
        rig = _in_loop(build_db_export(path), name="x.db", archive_path=Path(path))
        base = f"/api/session/{sid}/event"
        _, created = await asgi(rig.app, "POST", base, {"label": "mine"})
        real_update = archive_edit.update_event

        def changed_first(*args: Any, **kwargs: Any) -> bool:
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE events SET label = 'theirs' WHERE id = ?", (created["id"],))
                conn.commit()
            return real_update(*args, **kwargs)

        monkeypatch.setattr(archive_edit, "update_event", changed_first)
        queue = rig.collector.subscribe()
        status, body = await asgi(rig.app, "PATCH", f"{base}/{created['id']}", {"label": "edit"})
        assert status == 409
        assert body["code"] == "changed"
        assert body["current"]["label"] == "theirs"
        [repair] = _drain(queue)
        assert [e["label"] for e in repair["events"]] == ["theirs"]
        found = rig.source.find_event(sid, created["id"])
        assert found is not None
        assert found.label == "theirs"
        assert (rig.source.revision, rig.source.unsaved) == (0, 0)

    @pytest.mark.asyncio
    async def test_write_queued_behind_a_switch_is_refused(self) -> None:
        """The token is checked inside the lock, so a write that queued behind a source
        switch (the real upload route) is refused against the new source, never applied."""
        rig = _in_loop(_one_event_document())
        old_token = rig.source.token
        lock = rig.collector.mutation_lock()
        await lock.acquire()
        switching = asyncio.ensure_future(_upload(rig, _one_event_document()))
        await settle()
        patch = asyncio.ensure_future(
            asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": "late"}, token=old_token)
        )
        await settle()
        assert not switching.done()
        assert not patch.done()
        lock.release()
        status, state = await switching
        assert status == 200
        replacement = rig.source
        assert replacement.token == state["token"] != old_token
        before = replacement.body()
        status, body = await patch
        assert status == 409
        assert body["code"] == "source_changed"
        assert body["source_state"]["token"] == replacement.token
        assert replacement.body() == before
        assert replacement.revision == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", [None, "wrong"])
    async def test_review_write_without_or_with_a_wrong_token_409s(self, token: str | None) -> None:
        rig = _in_loop(_one_event_document())
        before = rig.source.body()
        status, body = await asgi(
            rig.app, "POST", "/api/session/a/event", {"label": "x"}, token=token
        )
        assert status == 409
        assert body["code"] == "source_changed"
        assert body["source_state"]["token"] == rig.source.token
        assert rig.source.body() == before

    @pytest.mark.asyncio
    async def test_json_write_to_an_unknown_session_404s(self) -> None:
        rig = _in_loop(_one_event_document())
        status, body = await asgi(rig.app, "POST", "/api/session/nope/event", {"label": "x"})
        assert status == 404
        assert body["error"] == str(UnknownSessionError("nope"))

    @pytest.mark.asyncio
    async def test_view_only_id_write_403s(self) -> None:
        document = parse_review_json(_REPEATED_IDS.read_bytes(), origin=str(_REPEATED_IDS))
        rig = _in_loop(document, name=_REPEATED_IDS.name)
        before = rig.source.body()
        one = f"/api/session/{_REPEATED_SID}/event/3"
        for method, body in (("PATCH", {"label": "x"}), ("DELETE", None)):
            status, refused = await asgi(rig.app, method, one, body)
            assert status == 403
            assert refused == {"error": refused["error"], "code": "view_only"}
        assert rig.source.body() == before
        assert rig.source.unsaved == 0

    @pytest.mark.asyncio
    async def test_create_beside_repeated_ids_gets_the_next_id(self) -> None:
        document = parse_review_json(_REPEATED_IDS.read_bytes(), origin=str(_REPEATED_IDS))
        rig = _in_loop(document, name=_REPEATED_IDS.name)
        status, created = await asgi(
            rig.app, "POST", f"/api/session/{_REPEATED_SID}/event", {"label": "x"}
        )
        assert status == 201
        assert created["id"] == 10

    @pytest.mark.asyncio
    async def test_create_past_the_id_ceiling_422s_and_counts_nothing(self) -> None:
        rig = _in_loop(_one_event_document(event_ops.MAX_SAFE_EVENT_ID))
        before = rig.source.body()
        status, body = await asgi(rig.app, "POST", "/api/session/a/event", {"label": "x"})
        assert status == 422
        assert set(body) == {"error"}
        assert rig.source.unsaved == 0
        assert rig.source.body() == before


async def _upload(
    rig: "_InLoop | Any",
    document: "MonitorExport | bytes | list[bytes]",
    *,
    token: "str | FromMode | None" = FROM_MODE,
    unsaved: "str | None" = "0",
    name: "str | None" = "y.json",
    declared: "int | None" = None,
) -> tuple[int | None, Any]:
    """POST *document* (a model, raw bytes, or chunks) to ``/api/source`` as a switch confirmation.

    ``unsaved`` and ``name`` are the confirmation's ``UNSAVED_HEADER`` and
    (percent-encoded) ``SOURCE_NAME_HEADER``; ``None`` omits the header.
    ``declared`` sends that ``Content-Length`` with chunks (none by default).
    """
    app = rig.app if isinstance(rig, _InLoop) else rig
    content = document_json(document).encode() if isinstance(document, MonitorExport) else document
    headers: dict[str, str] = {} if declared is None else {"content-length": str(declared)}
    if unsaved is not None:
        headers[UNSAVED_HEADER] = unsaved
    if name is not None:
        headers[SOURCE_NAME_HEADER] = quote(name)
    return await asgi(app, "POST", SOURCE_PATH, token=token, content=content, headers=headers)


def _two_session_ids_document() -> MonitorExport:
    """A valid model whose two sessions share an id: the upload validator refuses it."""
    one = _one_event_document().sessions[0]
    return MonitorExport(format=1, sessions=[one, one.model_copy()])


async def _ack(app: Any, token: str, revision: int) -> tuple[int | None, Any]:
    return await asgi(app, "POST", SAVED_PATH, {"token": token, "revision": revision}, token=None)


def _not_json_app(kind: str, tmp_path: Path) -> Any:
    """An app serving a source no upload replaces: a live one, or a ``.db`` archive."""
    if kind == "live":
        return _build_app(_empty_collector(), key="k", mode="live")
    path, _ = _make_archive(tmp_path)
    return _in_loop(build_db_export(path), name="x.db", archive_path=Path(path)).app


class TestSourceSwitch:
    """``POST /api/source`` (switch to an uploaded ``.json``) and ``/api/source/saved``."""

    @pytest.mark.asyncio
    async def test_upload_switches_every_tab(self) -> None:
        rig = _in_loop(_one_event_document())
        old_token = rig.source.token
        tabs = [rig.collector.subscribe(), rig.collector.subscribe()]
        status, state = await _upload(rig, _one_event_document(7))
        assert status == 200
        new_token = rig.source.token
        assert new_token != old_token
        assert (state["token"], state["source"], state["kind"]) == (new_token, "y.json", "json")
        assert (state["unsaved"], state["revision"]) == (0, 0)
        for tab in tabs:
            [frame] = _drain(tab)
            assert set(frame) == {"format", "source_state", "observed_at"}
            assert frame["source_state"]["token"] == new_token
            assert frame["observed_at"] <= state["observed_at"]
        assert rig.source.find_event("a", 7) is not None
        _, mode = await asgi(rig.app, "GET", MODE_PATH)
        assert mode["token"] == new_token

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["live", "db"])
    async def test_upload_refused_on_live_and_db_before_reading(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused before the gate and the body: an over-limit body still answers 403, not 413."""
        app = await asyncio.to_thread(_not_json_app, kind, tmp_path)
        monkeypatch.setattr(
            review_source, "read_upload", functools.partial(review_source.read_upload, limit=10)
        )
        status, body = await _upload(app, b"x" * 100)
        assert status == 403
        assert body == {"error": body["error"], "code": "not_switchable"}

    @pytest.mark.asyncio
    async def test_upload_with_stale_token_or_unsaved_409s(self) -> None:
        rig = _in_loop(_one_event_document())
        await asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": "edited"})
        served, before = rig.source, rig.source.body()
        queue = rig.collector.subscribe()
        refused: list[tuple[str | FromMode, str | None]] = [
            ("wrong", "1"),
            (FROM_MODE, "0"),
            (FROM_MODE, None),
        ]
        for token, unsaved in refused:
            status, body = await _upload(rig, _one_event_document(), token=token, unsaved=unsaved)
            assert status == 409
            assert body["code"] == "source_changed"
            assert body["source_state"]["token"] == served.token
            assert body["source_state"]["unsaved"] == 1
        assert rig.source is served
        assert (served.body(), served.unsaved) == (before, 1)
        assert _drain(queue) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [b"not json", document_json(_two_session_ids_document()).encode()],
        ids=["unparsable", "repeated-session-id"],
    )
    async def test_invalid_upload_leaves_source_and_unsaved_untouched(self, content: bytes) -> None:
        rig = _in_loop(_one_event_document())
        await asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": "edited"})
        served, token, before = rig.source, rig.source.token, rig.source.body()
        queue = rig.collector.subscribe()
        status, body = await _upload(rig, content, unsaved="1", name="bad.json")
        assert status == 422
        assert set(body) == {"error"}
        assert "bad.json" in body["error"]
        assert rig.source is served
        assert (served.token, served.body(), served.unsaved) == (token, before, 1)
        assert _drain(queue) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [b"[" * 11, [b"[" * 6, b"[" * 6]],
        ids=["content-length", "chunked"],
    )
    async def test_oversized_upload_413s(
        self, content: "bytes | list[bytes]", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _in_loop(_one_event_document())
        served = rig.source
        monkeypatch.setattr(
            review_source, "read_upload", functools.partial(review_source.read_upload, limit=10)
        )
        status, body = await _upload(rig, content)
        assert status == 413
        assert body == {"error": body["error"], "code": "too_large"}
        assert rig.source is served
        monkeypatch.undo()
        status, _ = await _upload(rig, _one_event_document())
        assert status == 200, "a refused upload releases the gate"

    @pytest.mark.asyncio
    async def test_the_route_reads_under_the_real_upload_limit(self) -> None:
        """A declared length past ``MAX_SOURCE_UPLOAD_BYTES`` is refused before the body is read.

        The route's own limit, unpatched: nothing that size is allocated, the
        declared length alone is refused, naming the real limit.
        """
        rig = _in_loop(_one_event_document())
        served = rig.source
        status, body = await _upload(rig, [b"{}"], declared=MAX_SOURCE_UPLOAD_BYTES + 1)
        assert status == 413
        assert body == {"error": str(UploadTooLarge(MAX_SOURCE_UPLOAD_BYTES)), "code": "too_large"}
        assert rig.source is served

    @pytest.mark.asyncio
    async def test_two_concurrent_uploads_are_serialised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _in_loop(_one_event_document())
        token = rig.source.token
        real_read = review_source.read_upload
        in_flight = 0
        most = 0

        async def counted_read(request: Any, **kwargs: Any) -> bytes:
            nonlocal in_flight, most
            in_flight += 1
            most = max(most, in_flight)
            try:
                await settle()
                return await real_read(request, **kwargs)
            finally:
                in_flight -= 1

        monkeypatch.setattr(review_source, "read_upload", counted_read)
        first, second = await asyncio.gather(
            _upload(rig, _one_event_document(), token=token),
            _upload(rig, _one_event_document(), token=token),
        )
        assert first[0] == 200
        assert second[0] == 409
        assert second[1]["code"] == "source_changed"
        assert second[1]["source_state"]["token"] == first[1]["token"]
        assert most == 1

    @pytest.mark.asyncio
    async def test_non_ascii_name_round_trips(self) -> None:
        rig = _in_loop(_one_event_document())
        status, state = await _upload(rig, _one_event_document(), name="messung-öl.json")
        assert status == 200
        assert state["source"] == "messung-öl.json"
        _, mode = await asgi(rig.app, "GET", MODE_PATH)
        assert mode["source"] == "messung-öl.json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [None, "", "\x1b\n"], ids=["absent", "empty", "unprintable"])
    async def test_upload_without_a_name_is_upload_json(self, name: "str | None") -> None:
        rig = _in_loop(_one_event_document())
        status, state = await _upload(rig, _one_event_document(), name=name)
        assert status == 200
        assert state["source"] == "upload.json"

    @pytest.mark.asyncio
    async def test_upload_name_drops_non_printable_characters(self) -> None:
        """No control sequence in the name can reach the terminal's unsaved-changes warning."""
        rig = _in_loop(_one_event_document())
        status, state = await _upload(rig, _one_event_document(), name="ev\x1b[2Jil\n.json")
        assert status == 200
        assert state["source"] == "ev[2Jil.json"
        assert rig.source.name == "ev[2Jil.json"

    @pytest.mark.asyncio
    async def test_ack_lowers_only_what_the_file_covered(self) -> None:
        rig = _in_loop(_one_event_document())
        for label in ("one", "two", "three"):
            await asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": label})
        token = rig.source.token
        queue = rig.collector.subscribe()
        status, state = await _ack(rig.app, token, 2)
        assert status == 200
        assert (state["unsaved"], state["revision"]) == (1, 3)
        [frame] = _drain(queue)
        assert frame["source_state"]["unsaved"] == 1
        status, state = await _ack(rig.app, token, 2)
        assert (status, state["unsaved"]) == (200, 1)
        assert _drain(queue) == [], "a repeated acknowledgement changes nothing"
        status, state = await _ack(rig.app, token, 99)
        assert (status, state["unsaved"]) == (200, 0)
        [frame] = _drain(queue)
        assert frame["source_state"]["unsaved"] == 0

    @pytest.mark.asyncio
    async def test_ack_with_a_stale_token_409s_and_changes_nothing(self) -> None:
        rig = _in_loop(_one_event_document())
        await asgi(rig.app, "PATCH", "/api/session/a/event/1", {"label": "edited"})
        queue = rig.collector.subscribe()
        status, body = await _ack(rig.app, "stale", 1)
        assert status == 409
        assert body["code"] == "source_changed"
        assert body["source_state"]["token"] == rig.source.token
        assert rig.source.unsaved == 1
        assert _drain(queue) == []

    @pytest.mark.asyncio
    async def test_ack_on_live_answers_the_unchanged_state(self, tmp_path: Path) -> None:
        app = await asyncio.to_thread(_not_json_app, "live", tmp_path)
        _, mode = await asgi(app, "GET", MODE_PATH)
        status, state = await _ack(app, "anything", 5)
        assert status == 200
        assert {**state, "observed_at": None} == {**mode, "observed_at": None}

    @pytest.mark.asyncio
    async def test_ack_on_db_is_a_no_op_its_counts_clamp_to(self, tmp_path: Path) -> None:
        """A ``.db`` write counts nothing an export saves: its revision stays 0, the ack clamps."""
        app = await asyncio.to_thread(_not_json_app, "db", tmp_path)
        _, mode = await asgi(app, "GET", MODE_PATH)
        status, state = await _ack(app, mode["token"], 5)
        assert status == 200
        assert {**state, "observed_at": None} == {**mode, "observed_at": None}
        assert (state["unsaved"], state["revision"]) == (0, 0)
        status, body = await _ack(app, "stale", 5)
        assert status == 409
        assert body["code"] == "source_changed"


def test_force_stop_before_serve_is_noop() -> None:
    server = MonitorServer(MetricCollector(hosts=[]))
    server.force_stop()  # must not raise: nothing started yet
    assert server.started is False


@pytest.mark.asyncio
async def test_force_stop_leaves_no_lifespan_coroutine_for_the_loop_owner_to_cancel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After ``force_stop``, nothing is left suspended in the ASGI lifespan.

    Regression for issue #383. ``uvicorn.Server.shutdown()`` ends with
    ``if not self.force_exit: await self.lifespan.shutdown()``, so uvicorn's
    force path deliberately skips the ``lifespan.shutdown`` message. Starlette's
    ``lifespan()`` is then still parked in ``receive()`` when ``serve()``
    returns. Whoever owns the loop must cancel it before closing (that is what
    ``DashboardHarness._drain_pending_tasks`` does, and what ``asyncio.run``
    does not), Starlette catches that cancellation and answers
    ``lifespan.shutdown.failed`` carrying the formatted traceback, and uvicorn
    logs that text at ERROR — a CancelledError traceback on stderr under a
    zero exit code, on every dashboard teardown and in ``make docs``.

    The hostile condition is injected, not waited for: ``force_stop`` IS the
    condition, and the drain below is the loop owner's cancel-everything pass
    verbatim. Pre-fix this logs the starlette/uvicorn ``CancelledError``
    traceback at ERROR; post-fix the lifespan has already reached its terminal
    state, so the drain finds nothing to cancel.
    """
    server = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
    serve_task = asyncio.create_task(server.serve())
    await server.wait_started()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        server.force_stop()
        await serve_task
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)

    # The gate's own contract first: nothing on this path may shout.
    shouted = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not shouted, f"force_stop teardown logged at ERROR: {shouted}"
    lifespan = server._server.lifespan  # type: ignore[union-attr]
    # ...and for the mechanism itself: the drain above would ALSO set
    # shutdown_event (LifespanOn.main's finally does), so the discriminator
    # between "shut down" and "cancelled" is which answer the app sent.
    assert not lifespan.shutdown_failed, (
        "the lifespan was cancelled mid-receive(), not shut down: it answered "
        "lifespan.shutdown.failed"
    )
    assert lifespan.shutdown_event.is_set()


@pytest.mark.asyncio
async def test_a_serve_task_that_raises_after_startup_still_settles_the_lifespan(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same settle must happen on the paths that raise, not just the clean one.

    Companion to the ``force_stop`` guard above (issue #383).
    ``await self.lifespan.shutdown()`` is the LAST statement of uvicorn's
    ``Server.shutdown()``, so a serve task that dies anywhere after
    ``lifespan.startup()`` completed leaves Starlette's ``lifespan()`` parked in
    ``receive()`` just as the force path does — and ``serve()``'s own unwind is
    then the only place left that can finish the handshake. (Not the bind
    failure: uvicorn's ``OSError`` branch awaits ``lifespan.shutdown()`` itself
    before ``sys.exit``. This injects the hostile condition instead — uvicorn's
    ``shutdown()`` raising part-way, past the startup, before the lifespan.)
    """

    async def _exploding_shutdown(self: object, sockets: object = None) -> None:
        raise RuntimeError("uvicorn shutdown blew up")

    monkeypatch.setattr(
        server_module._LifecycleOwnedServer, "shutdown", _exploding_shutdown, raising=True
    )
    server = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
    serve_task = asyncio.create_task(server.serve())
    await server.wait_started()
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        server.force_stop()
        with pytest.raises(RuntimeError, match="uvicorn shutdown blew up"):
            await serve_task
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in leftovers:
            task.cancel()
        await asyncio.gather(*leftovers, return_exceptions=True)

    shouted = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not shouted, f"a raising serve task left the lifespan to be cancelled: {shouted}"
    lifespan = server._server.lifespan  # type: ignore[union-attr]
    assert not lifespan.shutdown_failed
    assert lifespan.shutdown_event.is_set()


def test_force_stop_aborts_connection_registered_after_first_pass() -> None:
    """Regression for #109: a connection whose ``connection_made`` runs *after*
    the first abort pass must still be aborted.

    asyncio defers ``connection_made`` via ``call_soon``, so a socket accepted
    just as the listeners close registers its transport on a later loop turn —
    after a one-shot abort snapshot. An un-aborted mid-stream SSE then blocks
    uvicorn's ``Server.wait_closed()`` (3.12+), so ``serve()`` never returns and
    the harness thread-join wedges. ``force_stop`` must re-abort across turns.

    A fake loop/server drives the exact ordering: the (fake) listener's
    ``close()`` schedules the late registration, and we assert the late
    connection's transport is aborted (a single pass would miss it).
    """
    from collections import deque

    from otto.monitor.server import _StreamDrain

    class FakeLoop:
        def __init__(self):
            self._queue = deque()

        def call_soon_threadsafe(self, cb, *args):
            self._queue.append((cb, args))

        def call_soon(self, cb, *args):
            self._queue.append((cb, args))

        def is_closed(self):
            return False

        def run(self, max_turns=200):
            turns = 0
            while self._queue and turns < max_turns:
                cb, args = self._queue.popleft()
                cb(*args)
                turns += 1

    class FakeTransport:
        def __init__(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    class FakeConn:
        def __init__(self):
            self.transport = FakeTransport()

    class FakeState:
        def __init__(self, connections):
            self.connections = connections

    class FakeListener:
        def __init__(self, loop, on_close):
            self._loop = loop
            self._on_close = on_close
            self.closed = False
            self.sockets = []  # nothing to stop reading from; asyncio.Server's shape

        def close(self):
            self.closed = True
            # A socket accepted just before this close finishes its deferred
            # connection_made on a *later* loop turn.
            self._loop.call_soon(self._on_close)

    class FakeUvServer:
        def __init__(self, state, listeners):
            self.force_exit = False
            self.should_exit = False
            self.server_state = state
            self.servers = listeners

    loop = FakeLoop()
    early = FakeConn()
    connections = [early]
    late = FakeConn()
    listener = FakeListener(loop, lambda: connections.append(late))
    fake_server = FakeUvServer(FakeState(connections), [listener])

    server = MonitorServer.__new__(MonitorServer)
    server._server = fake_server
    server._loop = loop
    server._drain = _StreamDrain(_empty_collector())

    server.force_stop()
    loop.run()

    assert fake_server.force_exit is True
    assert fake_server.should_exit is True  # self.stop() ran
    assert listener.closed is True
    assert early.transport.aborted is True
    assert late.transport.aborted is True, "late-registered connection was never aborted"


def test_force_stop_completes_every_accepted_connection_before_closing_the_listener() -> None:
    """A connection accepted before ``force_stop`` must reach ``connection_made``.

    Regression for issue #319 (and the trigger of #320). asyncio accepts in a
    selector callback but builds the transport in a *task* that steps on the
    next turn, and ``_SelectorTransport.__init__`` calls ``Server._attach()``,
    which asserts the listener is still open. So if ``force_stop`` closes the
    listener between the accept and that step, the constructor raises part-way
    through: the protocol never gets ``connection_made`` (uvicorn registers a
    connection there, so nothing can ever abort it) and the half-built
    transport dies holding the accepted fd, firing the ``ResourceWarning``
    pair that fails the bookend lanes.

    The ordering is driven by hand with a real loop: the client is connected
    before the loop turns (so the accept is already pending in the selector),
    and ``force_stop`` runs from a callback on that same turn. Its stop
    callback then lands *ahead* of the build task on the following turn, which
    is the race exactly as it occurs under load. ``force_stop`` must stop
    accepting without invalidating an accept already in flight.
    """
    import types

    from otto.monitor.server import _StreamDrain

    loop = asyncio.new_event_loop()
    created: list[asyncio.Protocol] = []
    made: list[asyncio.Protocol] = []
    lost: list[asyncio.Protocol] = []
    connections: set[asyncio.Protocol] = set()

    class Registering(asyncio.Protocol):
        """Registers itself in connection_made, as uvicorn's protocols do."""

        def __init__(self) -> None:
            created.append(self)

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.transport = transport
            made.append(self)
            connections.add(self)

        def connection_lost(self, exc: Exception | None) -> None:
            lost.append(self)
            connections.discard(self)

    listener = loop.run_until_complete(loop.create_server(Registering, "127.0.0.1", 0))
    port = listener.sockets[0].getsockname()[1]
    fake_server = types.SimpleNamespace(
        force_exit=False,
        should_exit=False,
        server_state=types.SimpleNamespace(connections=connections),
        servers=[listener],
    )
    server = MonitorServer.__new__(MonitorServer)
    server._server = fake_server
    server._loop = loop
    server._drain = _StreamDrain(_empty_collector())

    # The handshake completes in the kernel: the listener is readable before the
    # loop's next select, so the accept callback joins the same batch as this.
    client = socket.create_connection(("127.0.0.1", port))
    try:
        loop.call_soon(server.force_stop)
        loop.run_until_complete(asyncio.sleep(0.2))

        assert fake_server.should_exit is True  # self.stop() ran
        assert not listener.is_serving()
        assert len(created) == 1, "the pending accept should have produced exactly one protocol"
        assert made == created, (
            "a connection accepted before force_stop never reached connection_made: "
            "its transport was built against an already-closed listener (#319/#320)"
        )
        assert lost == made, "the registered connection was never aborted"
    finally:
        client.close()
        listener.close()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


class _SchedulingFailsLoop:
    """Loop double whose ``call_soon_threadsafe`` raises *error*.

    *closes_on_raise* models the race ``_begin_drain`` tolerates: the loop is
    open at the check and closed (by its owner, on another thread) by the time
    the call lands.
    """

    def __init__(self, error: RuntimeError, *, closes_on_raise: bool) -> None:
        self._error = error
        self._closes_on_raise = closes_on_raise
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    def call_soon_threadsafe(self, cb, *args):
        if self._closes_on_raise:
            self._closed = True
        raise self._error


def _server_on(loop: _SchedulingFailsLoop) -> MonitorServer:
    import types

    from otto.monitor.server import _StreamDrain

    server = MonitorServer.__new__(MonitorServer)
    server._server = types.SimpleNamespace(should_exit=False)
    server._loop = loop
    server._drain = _StreamDrain(_empty_collector())
    return server


def test_begin_drain_tolerates_a_loop_that_closes_before_scheduling() -> None:
    loop = _SchedulingFailsLoop(RuntimeError("Event loop is closed"), closes_on_raise=True)
    server = _server_on(loop)
    server.stop()
    assert server._server.should_exit is True
    assert loop.is_closed(), "the double really raised from call_soon_threadsafe"


def test_begin_drain_re_raises_an_unrelated_scheduling_error() -> None:
    loop = _SchedulingFailsLoop(RuntimeError("unrelated scheduler failure"), closes_on_raise=False)
    server = _server_on(loop)
    with pytest.raises(RuntimeError, match="unrelated"):
        server.stop()


class TestDashboardRoute:
    """GET / — dist-required serving (Task 9 cutover: the React build at
    ``web/`` is the only frontend; there is no legacy fallback anymore).

    Builds a throwaway static directory under ``tmp_path`` (never the real
    ``src/otto/_webassets/monitor/``) and monkeypatches the module-level
    ``_STATIC_DIR`` to point at it, so these tests can't be satisfied by (or
    disturb) a real ``dist/`` build.
    """

    @staticmethod
    def _write_static_dir(tmp_path: Path, *, with_dist: bool) -> Path:
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        if with_dist:
            dist_dir = static_dir / "dist"
            dist_dir.mkdir()
            (dist_dir / "index.html").write_text("<html>DIST_MARKER</html>")
        return static_dir

    @pytest.mark.asyncio
    async def test_serves_dist_index_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        static_dir = self._write_static_dir(tmp_path, with_dist=True)
        monkeypatch.setattr(server_module, "_STATIC_DIR", static_dir)

        server = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        try:
            url = f"http://127.0.0.1:{server._port}/?key={server.key}"
            resp = await asyncio.to_thread(urllib.request.urlopen, url)
            with contextlib.closing(resp):
                assert b"DIST_MARKER" in resp.read()
        finally:
            server.stop()
            await task

    def test_raises_when_dist_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No React build means no server — fail fast at construction time.

        Before this route can be hit at all, ``MonitorServer.__init__``
        builds the app (``_build_app``), which must refuse to build when
        ``dist/index.html`` is missing rather than serving nothing (or the
        deleted legacy dashboard) at ``GET /``.
        """
        static_dir = self._write_static_dir(tmp_path, with_dist=False)
        monkeypatch.setattr(server_module, "_STATIC_DIR", static_dir)

        with pytest.raises(RuntimeError, match="make web"):
            MonitorServer(_empty_collector(), host="127.0.0.1", port=0)


class TestMonitorSessionsEndpoint:
    @pytest.mark.asyncio
    async def test_live_mode_serves_a_snapshot_of_the_running_session(self) -> None:
        """Live boot reuses review's hydration path: the snapshot IS a format:1 payload."""
        collector = FakeCollector()
        frame = new_frame(label="run", note=None)
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        try:
            await collector.push("r1", "cpu", 1.0)
            url = f"http://127.0.0.1:{server._port}/api/monitor_sessions?key={server.key}"
            resp = await asyncio.to_thread(urllib.request.urlopen, url)
            with contextlib.closing(resp):
                payload = json.loads(resp.read())
            export = MonitorExport.model_validate(payload)
            assert export.format == 1
            assert len(export.sessions) == 1
            assert export.sessions[0].id == frame.id
            assert export.sessions[0].end is None, "a live session is one whose end is still open"
            assert any(m.host == "r1" for m in export.sessions[0].metrics)
        finally:
            server.stop()
            await task

    @pytest.mark.asyncio
    async def test_snapshot_carries_observed_at(
        self, live_server: _RunningServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live snapshot stamps the session with the server's time (#866).

        A reload or a resync after a long collector stall must still place
        the live window at server-now, not at the last metric.
        """
        monkeypatch.setattr(event_ops, "utc_now", lambda: _FIXED_NOW)
        text = await get_text(live_server, "/api/monitor_sessions")
        export = MonitorExport.model_validate_json(text)
        assert export.sessions[0].observed_at == _FIXED_NOW

    @pytest.mark.asyncio
    async def test_live_mode_with_no_session_returns_404(self) -> None:
        """A live server built without frame/lab has no session to serve.

        A bare ``DashboardHarness``/``MonitorServer`` (``bare_live_dash`` in
        the dashboard e2e conftest) gets exactly this: the default
        ``mode="live"`` with ``frame=None``/``lab=None``. Before this fix, that
        combination raised ``RuntimeError`` from inside the route handler —
        an uncaught 500 + uvicorn traceback on every page load through either
        consumer, silently swallowed by the client's soft-fail boot contract
        so no test went red (issue found in Plan 5b Task 8 review). A "nothing
        is being recorded yet" live server is a legitimate empty state, not a
        programming error, so this must 404 with a clear detail instead.
        """
        collector = _empty_collector()
        # mode="live" default, no frame/lab.
        server = MonitorServer(collector, host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        try:
            url = f"http://127.0.0.1:{server._port}/api/monitor_sessions?key={server.key}"
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                await asyncio.to_thread(urllib.request.urlopen, url)
            with contextlib.closing(exc_info.value) as err:
                assert err.code == 404
                body = json.loads(err.read())
                assert "no monitor session is being recorded" in body["error"]
        finally:
            server.stop()
            await task

    @pytest.mark.asyncio
    async def test_retired_endpoints_are_gone(self) -> None:
        collector = FakeCollector()
        server = MonitorServer(
            collector,
            host="127.0.0.1",
            port=0,
            mode="live",
            frame=new_frame(label=None, note=None),
            lab=LabSnapshot(),
        )
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        try:
            for path in ("/api/document", "/api/meta", "/api/data"):
                url = f"http://127.0.0.1:{server._port}{path}?key={server.key}"
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    await asyncio.to_thread(urllib.request.urlopen, url)
                with contextlib.closing(exc_info.value) as err:
                    assert err.code == 404, f"{path} should be gone"
            # Plan 5c: the legacy (session-less) event routes are deleted
            # outright, replaced by the /api/session/{id}/event* family above.
            for method, path in (
                ("POST", "/api/event"),
                ("POST", "/api/event/1/end"),
                ("PATCH", "/api/event/1"),
                ("DELETE", "/api/event/1"),
            ):
                url = f"http://127.0.0.1:{server._port}{path}?key={server.key}"
                data = b"{}" if method in ("POST", "PATCH") else None
                headers = {"Content-Type": "application/json"} if data is not None else {}
                req = urllib.request.Request(url, data=data, method=method, headers=headers)
                with pytest.raises(urllib.error.HTTPError) as exc_info:
                    await asyncio.to_thread(urllib.request.urlopen, req)
                with contextlib.closing(exc_info.value) as err:
                    assert err.code in (404, 405), f"{method} {path} should be gone"
        finally:
            server.stop()
            await task


class TestWaitStarted:
    """The G7 readiness API: ``wait_started`` releases on failure, never hangs.

    The serve()-raises path for a bad PEM lives in test_server_tls.py; these
    pin the *waiter's* side — an external ``await server.wait_started()``
    inherits the startup failure instead of polling a flag nothing will flip.
    """

    _FAILURE_TIMEOUT = 5.0  # bounds a regression to a loud timeout, not a wedged run

    @pytest.mark.asyncio
    async def test_reraises_bad_tls_pair(self, tmp_path):
        cert = tmp_path / "cert.pem"
        cert.write_text("this is not a certificate")
        server = MonitorServer(
            MetricCollector(hosts=[]), host="127.0.0.1", port=0, tls_cert=cert, tls_key=cert
        )
        task = asyncio.create_task(server.serve())
        with pytest.raises(ssl.SSLError):
            await asyncio.wait_for(server.wait_started(), timeout=self._FAILURE_TIMEOUT)
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_clean_return_before_started_raises_not_hangs(self, monkeypatch):
        # A serve task that RETURNS without ever flipping `started` (no
        # exception to re-raise) must still release waiters loudly — the
        # guard the old test-side _wait_started helper carried.
        async def _returns_immediately(inner_self, sockets=None):
            return None

        monkeypatch.setattr("uvicorn.Server.serve", _returns_immediately)
        server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        with pytest.raises(RuntimeError, match="returned before signalling startup"):
            await asyncio.wait_for(server.wait_started(), timeout=self._FAILURE_TIMEOUT)
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_serve_twice_refused(self, monkeypatch):
        # The readiness latch and recorded startup outcome are single-shot:
        # a second serve() on the same instance would hand waiters the FIRST
        # lifecycle's outcome. Refused loudly instead.
        async def _returns_immediately(inner_self, sockets=None):
            return None

        monkeypatch.setattr("uvicorn.Server.serve", _returns_immediately)
        server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        with pytest.raises(RuntimeError, match="returned before signalling startup"):
            await asyncio.wait_for(server.wait_started(), timeout=self._FAILURE_TIMEOUT)
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(RuntimeError, match="serves once per lifetime"):
            await server.serve()

    @pytest.mark.asyncio
    async def test_cancel_during_startup_releases_waiter(self):
        # A serve task cancelled while the prologue is still waiting on
        # uvicorn must release waiters with a loud RuntimeError — not
        # re-raise CancelledError into a waiter that was never itself
        # cancelled (which would poison the waiter's own cancellation
        # state), and never strand it on an event nothing will set.
        server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        # One yield: serve() runs its prologue up to the first true await
        # (inside the startup wait), so the cancel lands INSIDE the guard.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(RuntimeError, match="startup was cancelled"):
            await asyncio.wait_for(server.wait_started(), timeout=self._FAILURE_TIMEOUT)
        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), timeout=self._FAILURE_TIMEOUT
        )

    @pytest.mark.asyncio
    async def test_surfaces_port_already_in_use(self):
        # Hold a bound, listening socket so uvicorn's bind fails with the
        # SystemExit that serve() translates to RuntimeError — the exact
        # failure the old polls turned into an infinite hang.
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            server = MonitorServer(MetricCollector(hosts=[]), host="127.0.0.1", port=port)
            task = asyncio.create_task(server.serve())
            with pytest.raises(RuntimeError, match="already in use"):
                await asyncio.wait_for(server.wait_started(), timeout=self._FAILURE_TIMEOUT)
            await asyncio.gather(task, return_exceptions=True)
        finally:
            blocker.close()


UTC_PLUS_2 = timedelta(hours=2)


def _instant(text: object) -> datetime:
    """Parse a wire timestamp (``Z`` or an offset; 3.10's fromisoformat takes only the latter)."""
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


class TestEventTimestampsAreAware:
    """#965: a naive event timestamp means UTC; a mixed write is never a 500.

    Every write route, live and review, answers 2xx with an aware stored
    instant, or 422 for a backward span; an existing offset is preserved.
    """

    @staticmethod
    async def _post(served: _Served, **fields: object) -> tuple[int, dict[str, object]]:
        return await post_json(served.handle, served.base, {"label": "x", **fields})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("start", "end", "ok"),
        [
            # aware start, naive end (naive = UTC)
            ("2026-07-18T12:00:00+02:00", "2026-07-18T11:00:00", True),
            ("2026-07-18T12:00:00+00:00", "2026-07-18T11:00:00", False),
            # naive start, aware end
            ("2026-07-18T12:00:00", "2026-07-18T15:00:00+02:00", True),
            ("2026-07-18T12:00:00", "2026-07-18T14:00:00+02:00", False),
            # both naive
            ("2026-07-18T12:00:00", "2026-07-18T12:30:00", True),
            ("2026-07-18T12:00:00", "2026-07-18T11:30:00", False),
        ],
        ids=[
            "aware-naive-ok",
            "aware-naive-back",
            "naive-aware-ok",
            "naive-aware-back",
            "naive-naive-ok",
            "naive-naive-back",
        ],
    )
    async def test_create_mixed_awareness(
        self, served: _Served, start: str, end: str, ok: bool
    ) -> None:
        status, body = await self._post(served, timestamp=start, end_timestamp=end)
        assert status == (201 if ok else 422)
        if ok:
            for key in ("timestamp", "end_timestamp"):
                assert _instant(body[key]).tzinfo is not None
            assert (await served.events())[body["id"]]["timestamp"] == body["timestamp"]

    @pytest.mark.asyncio
    async def test_create_preserves_an_existing_offset_and_pins_a_naive_one_to_utc(
        self, served: _Served
    ) -> None:
        _, aware = await self._post(served, timestamp="2026-07-18T12:00:00+02:00")
        assert _instant(aware["timestamp"]).utcoffset() == UTC_PLUS_2
        _, naive = await self._post(served, timestamp="2026-07-18T12:00:00")
        assert _instant(naive["timestamp"]) == datetime(2026, 7, 18, 12, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_patch_an_aware_event_with_a_naive_end(self, served: _Served) -> None:
        created = await served.create(timestamp="2026-07-18T12:00:00+00:00")
        url = f"{served.base}/{created['id']}"
        status, body = await patch_json(
            served.handle, url, {"end_timestamp": "2026-07-18T13:00:00"}
        )
        assert status == 200
        assert _instant(body["end_timestamp"]) == datetime(2026, 7, 18, 13, tzinfo=timezone.utc)
        status, _ = await patch_json(served.handle, url, {"end_timestamp": "2026-07-18T11:00:00"})
        assert status == 422

    @pytest.mark.asyncio
    async def test_revert_expect_naming_the_stored_instant_as_naive_matches(
        self, served: _Served
    ) -> None:
        created = await served.create(timestamp="2026-07-18T12:01:00+00:00")
        naive_expect = {**created, "timestamp": "2026-07-18T12:01:00"}
        status, _, body = await served.revert(
            created["id"], naive_expect, {**created, "label": "z"}
        )
        assert status == 200
        assert body["label"] == "z"

    @pytest.mark.asyncio
    async def test_a_library_event_with_a_naive_timestamp_takes_patch_and_end(
        self, live_server: _RunningServer
    ) -> None:
        sid = live_server.collector.session_id
        naive = datetime(2026, 7, 18, 12, 0)  # noqa: DTZ001 — the naive input under test
        event = await live_server.collector.add_event(label="lib", timestamp=naive)
        assert event.timestamp == naive.replace(tzinfo=timezone.utc)
        base = f"/api/session/{sid}/event"
        status, body = await patch_json(
            live_server, f"{base}/{event.id}", {"end_timestamp": "2026-07-18T13:00:00"}
        )
        assert status == 200
        assert _instant(body["end_timestamp"]) == datetime(2026, 7, 18, 13, tzinfo=timezone.utc)
        other = await live_server.collector.add_event(label="lib2", timestamp=naive)
        status, body = await _request_json(live_server, "POST", f"{base}/{other.id}/end", {})
        assert status == 200
        assert _instant(body["end_timestamp"]).tzinfo is not None
