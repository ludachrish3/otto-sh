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
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otto.models import LabSnapshot, MonitorExport
from otto.monitor import server as server_module
from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer, _build_app
from otto.monitor.session import new_frame
from tests._fixtures._fake_collector import FakeCollector


def _empty_collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


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
    """Tests for DELETE /api/event/{event_id} — the 204 No Content response."""

    @pytest.mark.asyncio
    async def test_delete_event_returns_204_empty_body(self):
        """Deleting an existing event must return 204 with an empty body.

        Before the fix, JSONResponse(None, status_code=204) sent a 4-byte
        "null" body, causing h11 to raise LocalProtocolError because HTTP 204
        responses must not have a body.

        Tests the ASGI app directly to inspect the actual response body (the
        h11 error is server-side — HTTP clients still receive 204).
        """
        collector = _empty_collector()
        event = await collector.add_event(label="test-event")

        app = _build_app(collector, key="k")

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
            "path": f"/api/event/{event.id}",
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
        server = MonitorServer(collector, host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)

        try:
            url = f"http://127.0.0.1:{server._port}/api/event/9999?key={server.key}"
            req = urllib.request.Request(url, method="DELETE")
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                await asyncio.to_thread(urllib.request.urlopen, req)
            assert exc_info.value.code == 404
            exc_info.value.close()
        finally:
            server.stop()
            await task


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
        finally:
            server.stop()
            await task
