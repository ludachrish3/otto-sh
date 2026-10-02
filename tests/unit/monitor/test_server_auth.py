"""Access-key gate for the monitor dashboard (spec 2026-07-16).

Everything is exercised at the raw-ASGI level (no httpx in this venv), the
same pattern TestDeleteEndpoint in test_server.py uses. The middleware is
pure ASGI (not BaseHTTPMiddleware) so the SSE stream is gated identically.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request

import pytest

from otto.console import CONSOLE
from otto.logger import management
from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer, _build_app

TEST_KEY = "test-key-abc123"


def _collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


async def _asgi_get(
    app, path, query=b"", cookie=None, server_port=8123, client=("127.0.0.1", 55555)
):
    """Drive one GET through the ASGI app; return (status, headers-list, body)."""
    headers = [(b"host", f"127.0.0.1:{server_port}".encode())]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers,
        "server": ("127.0.0.1", server_port),
    }
    if client is not None:
        scope["client"] = client
    status, resp_headers, chunks = None, [], []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal status, resp_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            resp_headers = message["headers"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, resp_headers, b"".join(chunks)


def _header_values(headers, name: bytes) -> list[str]:
    return [v.decode() for k, v in headers if k.lower() == name]


class TestAccessKeyMiddleware:
    @pytest.mark.asyncio
    async def test_no_key_on_dashboard_is_403_html(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, headers, body = await _asgi_get(app, "/")
        assert status == 403
        assert "text/html" in _header_values(headers, b"content-type")[0]
        assert b"otto monitor" in body  # the fix-it hint names the command

    @pytest.mark.asyncio
    async def test_no_key_on_api_is_403_json(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _headers, body = await _asgi_get(app, "/api/mode")
        assert status == 403
        assert json.loads(body) == {"error": "missing or invalid access key"}

    @pytest.mark.asyncio
    async def test_wrong_key_is_403(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(app, "/api/mode", query=b"key=wrong")
        assert status == 403

    @pytest.mark.asyncio
    async def test_non_ascii_query_key_is_403_not_500(self):
        """``secrets.compare_digest(str, str)`` raises TypeError on non-ASCII

        input — an adversarial or fat-fingered ``?key=`` must still be an
        ordinary 403, never an unhandled-exception 500.
        """
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(app, "/api/mode", query=b"key=%C3%BC")
        assert status == 403

    @pytest.mark.asyncio
    async def test_non_ascii_cookie_is_403_not_500(self):
        """Same TypeError trap, reached via the cookie fallback path."""
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(
            app, "/api/mode", cookie="otto_monitor_8123=ü", server_port=8123
        )
        assert status == 403

    @pytest.mark.asyncio
    async def test_good_query_key_allows_and_sets_port_scoped_cookie(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, headers, _ = await _asgi_get(
            app, "/api/mode", query=f"key={TEST_KEY}".encode(), server_port=8123
        )
        assert status == 200
        (set_cookie,) = _header_values(headers, b"set-cookie")
        assert set_cookie.startswith(f"otto_monitor_8123={TEST_KEY}")
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie.replace("samesite=lax", "SameSite=Lax")
        assert "Secure" not in set_cookie  # no TLS in this task

    @pytest.mark.asyncio
    async def test_keyed_deep_link_sets_cookie_too(self):
        """Cookie is minted on ANY keyed request, not just '/' (spec: deep links)."""
        app = _build_app(_collector(), key=TEST_KEY)
        _, headers, _ = await _asgi_get(app, "/", query=f"key={TEST_KEY}".encode())
        assert _header_values(headers, b"set-cookie")

    @pytest.mark.asyncio
    async def test_cookie_only_followup_allows(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(
            app, "/api/mode", cookie=f"otto_monitor_8123={TEST_KEY}", server_port=8123
        )
        assert status == 200

    @pytest.mark.asyncio
    async def test_wrong_port_cookie_is_rejected(self):
        """Port-scoped names: another server's cookie must not unlock this one."""
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(
            app, "/api/mode", cookie=f"otto_monitor_9999={TEST_KEY}", server_port=8123
        )
        assert status == 403

    @pytest.mark.asyncio
    async def test_sse_stream_is_gated(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(app, "/api/stream")
        assert status == 403

    @pytest.mark.asyncio
    async def test_websocket_scope_is_denied_fail_closed(self):
        """No websocket routes exist today, but the gate must fail CLOSED for

        one anyway: a bare ``scope["type"] != "http"`` passthrough (the
        original shape, meant only for ``lifespan``) would let a future
        websocket route bypass the access-key check entirely. The middleware
        must consume the connect event then close with code 1008 (policy
        violation) without ever reaching the wrapped app.
        """
        app = _build_app(_collector(), key=TEST_KEY)
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8123")],
            "server": ("127.0.0.1", 8123),
            "client": ("127.0.0.1", 55555),
        }
        receive_called = False

        async def receive():
            nonlocal receive_called
            receive_called = True
            return {"type": "websocket.connect"}

        sent = []

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)

        assert receive_called, "middleware must drain the connect event before closing"
        assert sent == [{"type": "websocket.close", "code": 1008}]

    @pytest.mark.asyncio
    async def test_static_is_gated(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(app, "/static/dist/index.html")
        assert status == 403


class TestServerKeyProperties:
    def test_key_is_generated_and_stable_per_server(self):
        server = MonitorServer(_collector(), host="127.0.0.1", port=0)
        assert len(server.key) >= 20  # token_urlsafe(16) ≈ 22 chars
        assert server.key == server.key

    def test_two_servers_get_different_keys(self):
        a = MonitorServer(_collector(), host="127.0.0.1", port=0)
        b = MonitorServer(_collector(), host="127.0.0.1", port=0)
        assert a.key != b.key

    def test_url_and_urls_carry_the_key(self):
        server = MonitorServer(_collector(), host="127.0.0.1", port=7777)
        assert server.origin == "http://127.0.0.1:7777"
        assert server.url == f"http://127.0.0.1:7777/?key={server.key}"
        assert all(u.endswith(f"/?key={server.key}") for u in server.urls)


class TestRequestLogging:
    """otto logs dashboard connections itself; uvicorn's per-request access log is off.

    Runs a REAL uvicorn server: the access log and the client address only
    exist on uvicorn's protocol layer, never on the raw-ASGI helper above.
    """

    @staticmethod
    async def _get(url: str) -> None:
        def fetch() -> None:
            # a 403 is an answer, not a failure, here; close it, or 3.14's
            # tempfile finalizer warns about the unread error body
            try:
                with urllib.request.urlopen(url) as response:
                    response.read()
            except urllib.error.HTTPError as refused:
                refused.close()

        await asyncio.to_thread(fetch)

    @pytest.mark.asyncio
    async def test_one_info_per_admitted_ip_one_warning_per_refused_ip(self, caplog):
        server = MonitorServer(_collector(), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        await server.wait_started()
        base = f"http://127.0.0.1:{server._port}/api/mode"
        try:
            with caplog.at_level(logging.DEBUG):
                for _ in range(2):
                    await self._get(f"{base}?key={server.key}")
                    await self._get(base)
        finally:
            server.stop()
            await task

        ours = [r for r in caplog.records if r.name == "otto.monitor.server"]
        infos = [
            r.getMessage()
            for r in ours
            if r.levelno == logging.INFO and r.getMessage().startswith("Dashboard client")
        ]
        warnings = [r.getMessage() for r in ours if r.levelno == logging.WARNING]
        debugs = [
            r.getMessage()
            for r in ours
            if r.levelno == logging.DEBUG and r.getMessage().startswith("GET /api/mode")
        ]
        assert infos == ["Dashboard client connected from 127.0.0.1"]
        assert warnings == ["Dashboard request without a valid access key from 127.0.0.1"]
        assert len(debugs) == 4
        assert not [r for r in caplog.records if r.name == "uvicorn.access"]
        assert all(server.key not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_scope_without_a_client_logs_as_unknown(caplog):
    """No ``client`` in the scope (a unix socket, some proxies) must not crash the gate."""
    app = _build_app(_collector(), key="k")
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.server"):
        status, _headers, _body = await _asgi_get(app, "/api/mode", client=None)
    assert status == 403
    assert "Dashboard request without a valid access key from unknown" in caplog.messages


@pytest.mark.asyncio
async def test_a_client_admitted_by_its_cookie_alone_logs_one_info(caplog):
    """A client that never sends ``?key=`` (a bookmarked tab) still logs its connection."""
    app = _build_app(_collector(), key=TEST_KEY)
    cookie = f"otto_monitor_8123={TEST_KEY}"
    with caplog.at_level(logging.DEBUG, logger="otto.monitor.server"):
        for _ in range(2):
            status, _headers, _body = await _asgi_get(
                app, "/api/mode", cookie=cookie, client=("10.0.0.7", 40000)
            )
            assert status == 200
    infos = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.getMessage().startswith("Dashboard client")
    ]
    assert infos == ["Dashboard client connected from 10.0.0.7"]


class TestAccessKeyNeverWrittenToLogFiles:
    """The per-run access key is a live credential: it must be shown to the
    user (the printed URL) but never persisted to otto's on-disk log sinks.

    ``MonitorServer.serve()`` announces the dashboard URL, which carries
    ``?key=<token>``. Routing that announcement through ``logger.info`` would
    fan it into ``console.log`` / ``verbose.log`` -- the file sinks wired by
    ``otto.logger.management`` -- writing the credential to disk on every run.
    The keyed URL must instead go straight to the terminal via ``CONSOLE``
    (which bypasses the file-backed logger, exactly like management's
    output-dir print), leaving only a keyless origin in the log files.
    """

    def test_serve_keeps_the_key_off_disk_but_on_the_console(self, tmp_path, monkeypatch):
        # Wire the real three-sink logging pipeline against a temp output dir,
        # so console.log / verbose.log are the actual files serve() would write.
        management.reset()
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        out = management.create_output_dir("monitor")

        server = MonitorServer(_collector(), host="127.0.0.1", port=0)

        # Stub uvicorn's socket loop: flip ``started`` and fabricate a bound
        # socket so serve() reaches (and returns from) its URL announcement
        # without ever opening a real listener.
        class _FakeSocket:
            def getsockname(self):
                return ("127.0.0.1", 54321)

        class _FakeBound:
            def __init__(self):
                self.sockets = [_FakeSocket()]

        async def _fake_uvicorn_serve(inner_self, sockets=None):
            inner_self.started = True
            inner_self.servers = [_FakeBound()]

        monkeypatch.setattr("uvicorn.Server.serve", _fake_uvicorn_serve)

        with CONSOLE.capture() as cap:
            asyncio.run(server.serve())
        console_text = cap.get()

        management._state.listener.stop()  # drain the async queue into the files

        # The console (terminal) is the ONE place the key may appear -- the user
        # has no other way to read it.
        assert "Server running at" in console_text, console_text
        assert f"?key={server.key}" in console_text, console_text

        # ...it must never reach the log files on disk.
        for name in ("console.log", "verbose.log"):
            text = (out / name).read_text()
            assert server.key not in text, f"access key leaked into {name}:\n{text}"
            assert "?key=" not in text, f"keyed URL leaked into {name}:\n{text}"

        # The keyless server-start line is still recorded for the audit trail.
        assert "Monitor dashboard started on" in (out / "verbose.log").read_text()

    def test_a_failing_route_writes_its_traceback_without_the_key(self, tmp_path):
        """A route's exception reaches both files, and the key in the middleware's locals does not.

        Every request runs through ``_AccessKeyMiddleware.__call__``, whose
        locals hold the key, and uvicorn logs a route's exception with its
        traceback. The files write tracebacks with locals, so without
        ``_OmitTracebackLocals`` on ``uvicorn.error`` this one would carry the
        key in full.
        """
        management.reset()
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        out = management.create_output_dir("monitor")
        server = MonitorServer(_collector(), host="127.0.0.1", port=0)

        async def explode() -> None:
            raise RuntimeError("the route exploded")

        server._app.add_api_route("/api/explode", explode)

        async def scenario() -> None:
            task = asyncio.create_task(server.serve())
            await server.wait_started()
            try:
                url = f"http://127.0.0.1:{server._port}/api/explode?key={server.key}"

                def fetch() -> int:
                    # the 500 is the point; close it, or 3.14's tempfile
                    # finalizer warns about the unread error body
                    try:
                        with urllib.request.urlopen(url) as response:
                            response.read()
                    except urllib.error.HTTPError as exploded:
                        exploded.close()
                        return exploded.code
                    return 200

                assert await asyncio.to_thread(fetch) == 500
            finally:
                server.stop()
                await task

        try:
            with CONSOLE.capture():
                asyncio.run(scenario())
            management._state.listener.stop()  # drain the async queue into the files
            for name in ("console.log", "verbose.log"):
                text = (out / name).read_text()
                assert "RuntimeError: the route exploded" in text, f"{name}:\n{text}"
                assert server.key not in text, f"access key leaked into {name}:\n{text}"
        finally:
            management.reset()
