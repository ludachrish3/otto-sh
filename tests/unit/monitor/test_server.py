"""
Unit/integration tests for MonitorServer.

Covers:
  - Port 0 assigns an ephemeral port (not uvicorn's default 8000)
  - Two servers with port=0 bind to different ports simultaneously
  - Explicit port is respected
  - Display host resolution for 0.0.0.0
"""

import asyncio
import contextlib
import fcntl
import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from _archive import _make_archive

from otto.models import LabSnapshot, MonitorExport, SessionRecord
from otto.monitor import server as server_module
from otto.monitor.collector import MetricCollector
from otto.monitor.export import build_db_export
from otto.monitor.server import MonitorServer, _build_app
from otto.monitor.session import new_frame
from tests._fixtures._fake_collector import FakeCollector


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
    """POST/PATCH/DELETE against *handle*'s server, returning ``(status, json-body)``.

    Every route sits behind the access-key middleware (``?key=``), attached
    here so call sites never repeat it. ``urllib`` raises ``HTTPError`` for
    any non-2xx response, so both outcomes are normalized to the same return
    shape instead of forcing every caller to catch it individually — these
    tests assert 2xx *and* 404/409/422/403 bodies alike.
    """
    url = f"http://127.0.0.1:{handle.port}{path}?key={handle.key}"
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    def _do() -> tuple[int, dict[str, object]]:
        try:
            with contextlib.closing(urllib.request.urlopen(req)) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as err:
            with contextlib.closing(err):
                body = err.read()
            return err.code, (json.loads(body) if body else {})

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
    while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
        await asyncio.sleep(0.05)
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
    while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
        await asyncio.sleep(0.05)
    try:
        yield _RunningServer(server=server, collector=collector)
    finally:
        server.stop()
        await task


@pytest_asyncio.fixture
async def review_server() -> AsyncGenerator[_RunningServer, None]:
    """A booted review-mode ``MonitorServer`` serving a one-session document (read-only)."""
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
        archive_path=None,  # a .json review: Task 5 keeps this permanently read-only
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
        await asyncio.sleep(0.05)
    try:
        yield _RunningServer(server=server, collector=collector, document=document)
    finally:
        server.stop()
        await task


async def _start_and_stop(server: MonitorServer) -> int:
    """Start the server, capture the bound port, then stop immediately."""
    task = asyncio.create_task(server.serve())

    while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
        await asyncio.sleep(0.05)

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
        while not server_a.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)

        task_b = asyncio.create_task(server_b.serve())
        while not server_b.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)

        port_a = server_a._port
        port_b = server_b._port

        server_a.stop()
        server_b.stop()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

        assert port_a != port_b, f"Both servers bound to the same port {port_a}"

    @pytest.mark.asyncio
    async def test_explicit_port_is_used(self):
        """An explicit port should be passed through to uvicorn, not ignored."""
        # Bind to port 0 first to get a known-free port, then re-bind to it explicitly.
        probe = MonitorServer(_empty_collector(), host="127.0.0.1", port=0)
        free_port = await _start_and_stop(probe)

        server = MonitorServer(_empty_collector(), host="127.0.0.1", port=free_port)
        actual = await _start_and_stop(server)
        assert actual == free_port, (
            f"Requested port {free_port} but server bound to {actual} — "
            "is port= being passed to uvicorn.Config?"
        )


class TestDeleteEndpoint:
    """Tests for DELETE /api/session/{session_id}/event/{event_id} — the 204 No Content response."""

    @pytest.mark.asyncio
    async def test_delete_event_returns_204_empty_body(self):
        """Deleting an existing event must return 204 with an empty body.

        Before the fix, JSONResponse(None, status_code=204) sent a 4-byte
        "null" body, causing h11 to raise LocalProtocolError because HTTP 204
        responses must not have a body.

        Tests the ASGI app directly to inspect the actual response body (the
        h11 error is server-side — HTTP clients still receive 204). Builds
        the app with a frame/lab (mirroring what ``MonitorServer.__init__``
        does for a live server) and stamps ``collector.session_id`` itself
        since ``_build_app`` is called directly here, bypassing that
        constructor step — the path must match for ``_mutation_guard`` to
        let the request through to the handler under test.
        """
        collector = _empty_collector()
        frame = new_frame(label=None, note=None)
        collector.session_id = frame.id
        event = await collector.add_event(label="test-event")

        app = _build_app(collector, key="k", frame=frame, lab=LabSnapshot())

        status_code = None
        body_chunks: list[bytes] = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "DELETE",
            "path": f"/api/session/{frame.id}/event/{event.id}",
            "query_string": b"key=k",
            "headers": [],
            "root_path": "",
            "server": ("127.0.0.1", 8000),
        }
        await app(scope, receive, send)

        assert status_code == 204, f"Expected 204, got {status_code}"
        response_body = b"".join(body_chunks)
        assert response_body == b"", (
            f"HTTP 204 response must have an empty body — "
            f"got {response_body!r} ({len(response_body)} bytes). "
            f'h11 will raise "Too much data for declared Content-Length" '
            f"when attempting to send a body with 204."
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_event_returns_404(self):
        """Deleting a non-existent event returns 404 with an error body."""
        collector = _empty_collector()
        frame = new_frame(label=None, note=None)
        server = MonitorServer(
            collector, host="127.0.0.1", port=0, mode="live", frame=frame, lab=LabSnapshot()
        )
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)

        try:
            url = f"http://127.0.0.1:{server._port}/api/session/{frame.id}/event/9999?key={server.key}"
            req = urllib.request.Request(url, method="DELETE")
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                await asyncio.to_thread(urllib.request.urlopen, req)
            assert exc_info.value.code == 404
            exc_info.value.close()
        finally:
            server.stop()
            await task


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
        status, _ = await delete_(live_server, f"/api/session/{sid}/event/{created['id']}")
        assert status == 204
        status, _ = await delete_(live_server, f"/api/session/{sid}/event/{created['id']}")
        assert status == 404

    @pytest.mark.asyncio
    async def test_review_mode_mutations_403(self, review_server: _RunningServer) -> None:
        assert review_server.document is not None
        sid = review_server.document.sessions[0].id
        status, _ = await post_json(review_server, f"/api/session/{sid}/event", {"label": "x"})
        assert status == 403


class TestReviewDbEditing:
    """Plan 5c Task 5: a review-mode ``.db`` archive is event-editable.

    A ``.json`` review source has no persistence target, so it stays
    read-only (``TestSessionEventRoutes.test_review_mode_mutations_403``
    above pins that with ``archive_path=None``); a ``.db`` source gets a
    real archive built via ``_make_archive`` and mutations both patch the
    served document in place (no restart needed) and persist to the archive
    file itself (verified by re-reading it with ``build_db_export``).
    """

    @pytest.mark.asyncio
    async def test_mode_advertises_editable(self, tmp_path: Path) -> None:
        """live -> editable; .db review -> editable; .json review -> read-only."""
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
                (json_handle, False),
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
                status, _ = await post_json(server, f"/api/session/{sid}/event", {"label": "x"})
                assert status == 409
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

            status, _ = await delete_(server, f"/api/session/{sid}/event/{created['id']}")
            assert status == 204

            doc = json.loads(await get_text(server, "/api/monitor_sessions"))
            assert not any(e["id"] == created["id"] for e in doc["sessions"][0]["events"])
            fresh = build_db_export(path)
            assert not any(e.id == created["id"] for e in fresh.sessions[0].events)
        finally:
            await server.aclose()


def test_force_stop_before_serve_is_noop() -> None:
    server = MonitorServer(MetricCollector(hosts=[]))
    server.force_stop()  # must not raise: nothing started yet
    assert server.started is False


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

    class FakeLoop:
        def __init__(self):
            self._queue = deque()

        def call_soon_threadsafe(self, cb, *args):
            self._queue.append((cb, args))

        def call_soon(self, cb, *args):
            self._queue.append((cb, args))

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

    server.force_stop()
    loop.run()

    assert fake_server.force_exit is True
    assert fake_server.should_exit is True  # self.stop() ran
    assert listener.closed is True
    assert early.transport.aborted is True
    assert late.transport.aborted is True, "late-registered connection was never aborted"


class TestDashboardRoute:
    """GET / — dist-required serving (Task 9 cutover: the React build at
    ``web/`` is the only frontend; there is no legacy fallback anymore).

    Builds a throwaway static directory under ``tmp_path`` (never the real
    ``src/otto/monitor/static/``) and monkeypatches the module-level
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
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
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
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
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
    async def test_live_mode_with_no_session_returns_404(self) -> None:
        """A live server built without frame/lab has no session to serve.

        Both in-repo consumers that build a bare ``DashboardHarness``/
        ``MonitorServer`` (``shell_dash`` in the dashboard e2e conftest, and
        ``scripts/capture_docs_media.py``) get exactly this: the default
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
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
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
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
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
