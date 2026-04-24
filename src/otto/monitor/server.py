"""
MonitorServer — FastAPI web server for the otto monitoring dashboard.

Endpoints
---------
GET  /              Serves dashboard.html
GET  /static/...    Serves static assets (plotly.min.js, etc.)
GET  /api/meta      JSON metadata (host name, metric labels, units)
GET  /api/data      JSON snapshot of all metric series and events
GET  /api/stream    SSE stream — pushes metric updates and events in real time
POST /api/event     Record a manual event from the dashboard UI
"""

import asyncio
import json
import socket
from datetime import datetime
from logging import Filter, LogRecord, getLogger
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from ..logger import getOttoLogger
from .collector import MetricCollector
from .events import VALID_DASH_STYLES

_STATIC_DIR = Path(__file__).parent / 'static'

logger = getOttoLogger()

# Suppress the ASGI log from uvicorn because it clutters up the output on exit.
class SuppressASGIWarning(Filter):
    def filter(self, record: LogRecord):
        return "ASGI callable returned without completing" not in record.getMessage()

getLogger("uvicorn.error").addFilter(SuppressASGIWarning())

class _EventBody(BaseModel):
    label: str
    color: str = '#888888'
    dash:  str = 'dash'


class _EventUpdateBody(BaseModel):
    label: str | None = None
    color: str | None = None
    dash:  str | None = None


def _build_app(collector: MetricCollector) -> FastAPI:
    app = FastAPI(title='Otto Monitor')

    app.mount('/static', StaticFiles(directory=str(_STATIC_DIR)), name='static')

    @app.get('/', response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse: # type: ignore[reportUnusedFunction]
        html = (_STATIC_DIR / 'dashboard.html').read_text()
        return HTMLResponse(html)

    @app.get('/api/meta')
    async def meta() -> JSONResponse: # type: ignore[reportUnusedFunction]
        return JSONResponse(collector.get_meta())

    @app.get('/api/data')
    async def data() -> JSONResponse: # type: ignore[reportUnusedFunction]
        payload: dict[str, Any] = {
            'series': {
                label: [
                    {'ts': ts.isoformat(), 'value': value, **({'meta': meta} if meta is not None else {})}
                    for ts, value, meta in pts
                ]
                for label, pts in collector.get_series().items()
            },
            'events':    [e.to_dict() for e in collector.get_events()],
            'chart_map': collector.get_chart_map(),
        }
        return JSONResponse(payload)

    @app.get('/api/stream')
    async def stream(request: Request) -> EventSourceResponse: # type: ignore[reportUnusedFunction]
        q = collector.subscribe()

        async def generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield {'data': json.dumps(payload)}
                    except asyncio.TimeoutError:
                        # Send a keepalive comment so the browser doesn't close the connection
                        yield {'comment': 'keepalive'}
            finally:
                collector.unsubscribe(q)

        return EventSourceResponse(generator())

    @app.post('/api/event')
    async def add_event(body: _EventBody) -> JSONResponse: # type: ignore[reportUnusedFunction]
        if body.dash not in VALID_DASH_STYLES:
            return JSONResponse(
                {'error': f'Invalid dash style. Choose from: {sorted(VALID_DASH_STYLES)}'},
                status_code=422,
            )
        event = await collector.add_event(
            label=body.label,
            color=body.color,
            dash=body.dash,
            source='manual',
        )
        return JSONResponse(event.to_dict(), status_code=201)

    @app.post('/api/event/{event_id}/end')
    async def end_event(event_id: int) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Record the end time of a span event (uses server clock)."""
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({'error': 'Event not found'}, status_code=404)
        updated = await collector.update_event(
            event_id=event_id,
            label=existing.label,
            color=existing.color,
            dash=existing.dash,
            end_timestamp=datetime.now(),
        )
        if updated is None:
            return JSONResponse({'error': 'Event not found'}, status_code=404)
        return JSONResponse(updated.to_dict())

    @app.get('/api/export/json')
    async def export_json() -> Response:  # type: ignore[reportUnusedFunction]
        """Download all metrics and events as a JSON file (compatible with --file)."""
        return Response(
            content=collector.to_json(),
            media_type='application/json',
            headers={'Content-Disposition': 'attachment; filename="otto-metrics.json"'},
        )

    @app.delete('/api/event/{event_id}')
    async def delete_event(event_id: int) -> Response: # type: ignore[reportUnusedFunction]
        if await collector.delete_event(event_id):
            return Response(status_code=204)
        return JSONResponse({'error': 'Event not found'}, status_code=404)

    @app.patch('/api/event/{event_id}')
    async def update_event(event_id: int, body: _EventUpdateBody) -> JSONResponse: # type: ignore[reportUnusedFunction]
        if body.dash is not None and body.dash not in VALID_DASH_STYLES:
            return JSONResponse(
                {'error': f'Invalid dash style. Choose from: {sorted(VALID_DASH_STYLES)}'},
                status_code=422,
            )
        # Fetch existing event to fill in unchanged fields
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({'error': 'Event not found'}, status_code=404)
        updated = await collector.update_event(
            event_id=event_id,
            label=body.label if body.label is not None else existing.label,
            color=body.color if body.color is not None else existing.color,
            dash=body.dash  if body.dash  is not None else existing.dash,
        )
        if updated is None:
            return JSONResponse({'error': 'Event not found'}, status_code=404)
        return JSONResponse(updated.to_dict())

    return app


def _get_all_ips() -> list[str]:
    """Return all non-loopback IPv4 addresses on this machine.

    Uses the ``ip -4 -o addr show`` command which is universally available on
    Linux and reports addresses from all interfaces regardless of how the
    hostname resolves in DNS/hosts.
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ['ip', '-4', '-o', 'addr', 'show'],
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ips: list[str] = []
    for line in out.splitlines():
        # Format: "2: eth0    inet 10.0.2.15/24 ..."
        parts = line.split()
        for i, part in enumerate(parts):
            if part == 'inet' and i + 1 < len(parts):
                ip = parts[i + 1].split('/')[0]
                if not ip.startswith('127.'):
                    ips.append(ip)
    return ips


class MonitorServer:
    """
    Wraps a FastAPI/uvicorn server for the monitoring dashboard.

    Call ``await serve()`` from an async context to run the server.
    Call ``stop()`` from any thread to trigger a graceful shutdown.
    """

    def __init__(
        self,
        collector: MetricCollector,
        host: str = '0.0.0.0',
        port: int = 0,
    ) -> None:
        self._collector = collector
        self._bind_host = host
        self._port = port
        self._app = _build_app(collector)
        self._server: uvicorn.Server | None = None

    @property
    def url(self) -> str:
        """Primary URL using the first detected non-loopback IP (or the bind address)."""
        host = self._bind_host
        if host in ('0.0.0.0', '::'):
            ips = _get_all_ips()
            host = ips[0] if ips else self._bind_host
        return f'http://{host}:{self._port}'

    @property
    def urls(self) -> list[str]:
        """All URLs the server is reachable on (one per non-loopback interface)."""
        if self._bind_host in ('0.0.0.0', '::'):
            ips = _get_all_ips()
            if ips:
                return [f'http://{ip}:{self._port}' for ip in ips]
        return [f'http://{self._bind_host}:{self._port}']

    @property
    def started(self) -> bool:
        """True once the server is ready to accept connections."""
        return self._server is not None

    # TODO; Catch keyboard interrupt or *something* to make monitoring end gracefully
    async def serve(self) -> None:
        """Run the web server until stop() is called or the process exits."""
        config = uvicorn.Config(
            self._app,
            host=self._bind_host,
            port=self._port,
            log_config=None,
        )
        server = uvicorn.Server(config)

        # start the server in a background task
        task = asyncio.create_task(server.serve())

        # wait until uvicorn signals it's started
        while not server.started:
            await asyncio.sleep(0.05)

        # extract the port from the socket
        self._port = server.servers[0].sockets[0].getsockname()[1]
        all_urls = self.urls
        if len(all_urls) == 1:
            logger.info(f"Server running at {all_urls[0]}")
        else:
            logger.info("Server running at:")
            for u in all_urls:
                logger.info(f"  {u}")
        logger.info("Press Ctrl+C to stop")

        self._server = server

        await task

    def stop(self) -> None:
        """Signal the server to shut down (thread-safe)."""
        if self._server:
            self._server.should_exit = True
