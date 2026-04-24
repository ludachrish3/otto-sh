"""
Unit/integration tests for MonitorServer.

Covers:
  - Port 0 assigns an ephemeral port (not uvicorn's default 8000)
  - Two servers with port=0 bind to different ports simultaneously
  - Explicit port is respected
  - Display host resolution for 0.0.0.0
"""

import asyncio
import urllib.error
import urllib.request

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer, _build_app


def _empty_collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


async def _start_and_stop(server: MonitorServer) -> int:
    """Start the server, capture the bound port, then stop immediately."""
    task = asyncio.create_task(server.serve())

    while not server.started:
        await asyncio.sleep(0.05)

    port = server._port
    server.stop()
    await task
    return port


class TestPortBinding:
    @pytest.mark.asyncio
    async def test_port_zero_avoids_default_8000(self):
        """port=0 should ask the OS for an ephemeral port, NOT fall back to 8000."""
        server = MonitorServer(_empty_collector(), host='127.0.0.1', port=0)
        port = await _start_and_stop(server)
        assert port != 0, 'Port should have been assigned by the OS'
        assert port != 8000, (
            'port=0 should use an OS-assigned ephemeral port, '
            'not uvicorn default 8000 — is port= being passed to uvicorn.Config?'
        )

    @pytest.mark.asyncio
    async def test_two_servers_get_different_ports(self):
        """Two servers with port=0 must bind successfully to different ports."""
        server_a = MonitorServer(_empty_collector(), host='127.0.0.1', port=0)
        server_b = MonitorServer(_empty_collector(), host='127.0.0.1', port=0)

        task_a = asyncio.create_task(server_a.serve())
        while not server_a.started:
            await asyncio.sleep(0.05)

        task_b = asyncio.create_task(server_b.serve())
        while not server_b.started:
            await asyncio.sleep(0.05)

        port_a = server_a._port
        port_b = server_b._port

        server_a.stop()
        server_b.stop()
        await asyncio.gather(task_a, task_b, return_exceptions=True)

        assert port_a != port_b, f'Both servers bound to the same port {port_a}'

    @pytest.mark.asyncio
    async def test_explicit_port_is_used(self):
        """An explicit port should be passed through to uvicorn, not ignored."""
        # Bind to port 0 first to get a known-free port, then re-bind to it explicitly.
        probe = MonitorServer(_empty_collector(), host='127.0.0.1', port=0)
        free_port = await _start_and_stop(probe)

        server = MonitorServer(_empty_collector(), host='127.0.0.1', port=free_port)
        actual = await _start_and_stop(server)
        assert actual == free_port, (
            f'Requested port {free_port} but server bound to {actual} — '
            'is port= being passed to uvicorn.Config?'
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
        event = await collector.add_event(label='test-event')

        app = _build_app(collector)

        status_code = None
        body_chunks: list[bytes] = []

        async def receive():
            return {'type': 'http.request', 'body': b''}

        async def send(message):
            nonlocal status_code
            if message['type'] == 'http.response.start':
                status_code = message['status']
            elif message['type'] == 'http.response.body':
                body_chunks.append(message.get('body', b''))

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0'},
            'http_version': '1.1',
            'method': 'DELETE',
            'path': f'/api/event/{event.id}',
            'query_string': b'',
            'headers': [],
            'root_path': '',
            'server': ('127.0.0.1', 8000),
        }
        await app(scope, receive, send)

        assert status_code == 204, f'Expected 204, got {status_code}'
        response_body = b''.join(body_chunks)
        assert response_body == b'', (
            f'HTTP 204 response must have an empty body — '
            f'got {response_body!r} ({len(response_body)} bytes). '
            f'h11 will raise "Too much data for declared Content-Length" '
            f'when attempting to send a body with 204.'
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_event_returns_404(self):
        """Deleting a non-existent event returns 404 with an error body."""
        collector = _empty_collector()
        server = MonitorServer(collector, host='127.0.0.1', port=0)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.05)

        try:
            url = f'http://127.0.0.1:{server._port}/api/event/9999'
            req = urllib.request.Request(url, method='DELETE')
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                await asyncio.to_thread(urllib.request.urlopen, req)
            assert exc_info.value.code == 404
        finally:
            server.stop()
            await task
