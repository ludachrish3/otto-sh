"""Access-key gate for the monitor dashboard (spec 2026-07-16).

Everything is exercised at the raw-ASGI level (no httpx in this venv), the
same pattern TestDeleteEndpoint in test_server.py uses. The middleware is
pure ASGI (not BaseHTTPMiddleware) so the SSE stream is gated identically.
"""

import asyncio
import json
import logging
import urllib.request

import pytest

from otto.console import CONSOLE
from otto.logger import management
from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer, _build_app

TEST_KEY = "test-key-abc123"


def _collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


async def _asgi_get(app, path, query=b"", cookie=None, server_port=8123):
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
        "client": ("127.0.0.1", 55555),
    }
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


class TestAccessKeyNeverLogged:
    """Spec: 'the key appears exactly once in output (the printed URL) and is
    never logged per-request.'

    Uvicorn's own access logger logs the *full* request line, query string
    included (``get_path_with_query_string``), whenever it has a handler
    anywhere in its propagation chain -- which any real console/file logging
    setup gives it; ``log_config=None`` in ``MonitorServer.serve()`` only
    skips uvicorn's own ``dictConfig`` call, it does not detach this logger.
    So this must run through a REAL uvicorn server (not the raw-ASGI helper
    above, which never touches uvicorn's protocol/logging layer at all) and
    capture what uvicorn itself writes to ``uvicorn.access``.
    """

    @pytest.mark.asyncio
    async def test_key_does_not_appear_in_uvicorn_access_log(self, caplog):
        server = MonitorServer(_collector(), host="127.0.0.1", port=0)
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
        try:
            with caplog.at_level(logging.INFO, logger="uvicorn.access"):
                url = f"http://127.0.0.1:{server._port}/api/mode?key={server.key}"
                resp = await asyncio.to_thread(urllib.request.urlopen, url)
                resp.read()
        finally:
            server.stop()
            await task

        access_records = [r for r in caplog.records if r.name == "uvicorn.access"]
        assert access_records, "expected uvicorn to emit an access-log record for the request"
        for record in access_records:
            assert server.key not in record.getMessage()


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
