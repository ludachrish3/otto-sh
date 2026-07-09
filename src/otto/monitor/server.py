"""
MonitorServer — FastAPI web server for the otto monitoring dashboard.

Endpoints
---------
GET  /              Serves the React dashboard's built index.html (``web/``, via ``make web``)
GET  /static/...    Serves the built dist/ assets (JS/CSS bundles, etc.)
GET  /api/meta      JSON metadata (host name, metric labels, units)
GET  /api/data      JSON snapshot of all metric series and events
GET  /api/stream    SSE stream — pushes metric updates and events in real time
POST /api/event     Record a manual event from the dashboard UI
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from logging import Filter, LogRecord, getLogger
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request
from typing_extensions import override

from ..logger import get_logger
from ..models.base import OttoModel
from .collector import MetricCollector
from .events import VALID_DASH_STYLES

_STATIC_DIR = Path(__file__).parent / "static"

logger = get_logger()


def _dist_index_path() -> Path:
    """Return the built React dashboard's ``index.html``, or raise a fix-it error.

    The Phase 2 React port (``web/``) is the only frontend as of the Task 9
    cutover — the legacy static dashboard was deleted. ``make web`` (vite
    build) writes the bundle to ``_STATIC_DIR / "dist"``, already covered by
    the ``/static`` mount (so ``/static/dist/*`` resolves without a second
    mount). A missing build here means a developer/CI environment skipped
    that step, not a runtime condition users should ever see — fail fast
    with a clear remedy rather than serving a 404 or an empty page.
    """
    dist_index = _STATIC_DIR / "dist" / "index.html"
    if not dist_index.exists():
        raise RuntimeError(
            f"React dashboard build not found at {dist_index} — run `make web` "
            "to build the web/ frontend before starting the monitor server."
        )
    return dist_index


# Suppress the ASGI log from uvicorn because it clutters up the output on exit.
class SuppressASGIWarning(Filter):
    """``logging.Filter`` that drops the uvicorn ASGI callable warning on shutdown."""

    @override
    def filter(self, record: LogRecord) -> bool:
        return "ASGI callable returned without completing" not in record.getMessage()


getLogger("uvicorn.error").addFilter(SuppressASGIWarning())


class _EventBody(OttoModel):
    label: str
    color: str = "#888888"
    dash: str = "dash"


class _EventUpdateBody(OttoModel):
    label: str | None = None
    color: str | None = None
    dash: str | None = None


def _build_app(collector: MetricCollector) -> FastAPI:  # noqa: C901 — FastAPI route-factory; complexity is route count, not branching
    dist_index = _dist_index_path()

    app = FastAPI(title="Otto Monitor")

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:  # type: ignore[reportUnusedFunction]
        """Serve the React dashboard's built ``index.html``."""
        return HTMLResponse(dist_index.read_text())

    @app.get("/api/meta")
    async def meta() -> JSONResponse:  # type: ignore[reportUnusedFunction]
        return JSONResponse(collector.get_meta())

    @app.get("/api/data")
    async def data() -> JSONResponse:  # type: ignore[reportUnusedFunction]
        payload: dict[str, Any] = {
            "series": {
                label: [pt.model_dump(mode="json", exclude_none=True) for pt in pts]
                for label, pts in collector.get_series().items()
            },
            "events": [e.to_dict() for e in collector.get_events()],
            "chart_map": collector.get_chart_map(),
            "log_events": collector.get_log_events(),
        }
        return JSONResponse(payload)

    @app.get("/api/stream")
    async def stream(request: Request) -> EventSourceResponse:  # type: ignore[reportUnusedFunction]
        q = collector.subscribe()

        async def generator() -> AsyncGenerator[dict[str, str], None]:
            # Prime the connection with an immediate comment. Firefox's
            # EventSource only fires `onopen` after the first *body* byte
            # arrives, whereas Chromium fires it on the response headers; with
            # no initial byte, an idle bed leaves Firefox stuck showing
            # "Connecting…" until the first metric or the 15s keepalive below.
            # A kickoff comment (the standard SSE priming trick, also friendly
            # to buffering proxies) makes every engine reach the live state at
            # once.
            yield {"comment": "connected"}
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield {"data": json.dumps(payload)}
                    except asyncio.TimeoutError:
                        # Send a keepalive comment so the browser doesn't close the connection
                        yield {"comment": "keepalive"}
            finally:
                collector.unsubscribe(q)

        return EventSourceResponse(generator())

    @app.post("/api/event")
    async def add_event(body: _EventBody) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        if body.dash not in VALID_DASH_STYLES:
            return JSONResponse(
                {"error": f"Invalid dash style. Choose from: {sorted(VALID_DASH_STYLES)}"},
                status_code=422,
            )
        event = await collector.add_event(
            label=body.label,
            color=body.color,
            dash=body.dash,
            source="manual",
        )
        return JSONResponse(event.to_dict(), status_code=201)

    @app.post("/api/event/{event_id}/end")
    async def end_event(event_id: int) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Record the end time of a span event (uses server clock)."""
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        updated = await collector.update_event(
            event_id=event_id,
            label=existing.label,
            color=existing.color,
            dash=existing.dash,
            end_timestamp=datetime.now(tz=timezone.utc),
        )
        if updated is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return JSONResponse(updated.to_dict())

    @app.get("/api/export/json")
    async def export_json() -> Response:  # type: ignore[reportUnusedFunction]
        """Download all metrics and events as a JSON file (compatible with --file)."""
        return Response(
            content=collector.to_json(),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="otto-metrics.json"'},
        )

    @app.delete("/api/event/{event_id}")
    async def delete_event(event_id: int) -> Response:  # type: ignore[reportUnusedFunction]
        if await collector.delete_event(event_id):
            return Response(status_code=204)
        return JSONResponse({"error": "Event not found"}, status_code=404)

    @app.patch("/api/event/{event_id}")
    async def update_event(event_id: int, body: _EventUpdateBody) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        if body.dash is not None and body.dash not in VALID_DASH_STYLES:
            return JSONResponse(
                {"error": f"Invalid dash style. Choose from: {sorted(VALID_DASH_STYLES)}"},
                status_code=422,
            )
        # Fetch existing event to fill in unchanged fields
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        updated = await collector.update_event(
            event_id=event_id,
            label=body.label if body.label is not None else existing.label,
            color=body.color if body.color is not None else existing.color,
            dash=body.dash if body.dash is not None else existing.dash,
        )
        if updated is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
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
            ["ip", "-4", "-o", "addr", "show"],  # noqa: S607 — resolved via PATH by design
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ips: list[str] = []
    for line in out.splitlines():
        # Format: "2: eth0    inet 10.0.2.15/24 ..."  # noqa: ERA001 — illustrative example
        parts = line.split()
        for i, part in enumerate(parts):
            if part == "inet" and i + 1 < len(parts):
                ip = parts[i + 1].split("/")[0]
                if not ip.startswith("127."):
                    ips.append(ip)
    return ips


# force_stop() re-aborts open transports across this many consecutive loop turns
# rather than once. asyncio defers connection_made (where uvicorn registers a
# connection) via call_soon, so a socket accepted just before the listeners
# close can register its transport *after* a single abort pass; uvicorn's
# shutdown then blocks on Server.wait_closed() forever (issue #109). The
# listeners are closed first, so all still-pending registrations run within a
# few turns — a small fixed number of unconditional passes drains them.
_FORCE_STOP_ABORT_PASSES = 10


class MonitorServer:
    """
    Wraps a FastAPI/uvicorn server for the monitoring dashboard.

    Call ``await serve()`` from an async context to run the server.
    Call ``stop()`` from any thread to trigger a graceful shutdown, or
    ``force_stop()`` to abort open SSE connections immediately instead of
    waiting for them to drain.
    """

    def __init__(
        self,
        collector: MetricCollector,
        host: str = "0.0.0.0",  # noqa: S104 — intentional all-interface bind
        port: int = 0,
    ) -> None:
        self._collector = collector
        self._bind_host = host
        self._port = port
        self._app = _build_app(collector)
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def url(self) -> str:
        """Primary URL using the first detected non-loopback IP (or the bind address)."""
        host = self._bind_host
        if host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            host = ips[0] if ips else self._bind_host
        return f"http://{host}:{self._port}"

    @property
    def urls(self) -> list[str]:
        """All URLs the server is reachable on (one per non-loopback interface)."""
        if self._bind_host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            if ips:
                return [f"http://{ip}:{self._port}" for ip in ips]
        return [f"http://{self._bind_host}:{self._port}"]

    @property
    def started(self) -> bool:
        """True once the server is ready to accept connections."""
        return self._server is not None

    # TODO: Catch keyboard interrupt or *something* to make monitoring end gracefully
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
        self._loop = asyncio.get_running_loop()
        task = asyncio.create_task(server.serve())

        # wait until uvicorn signals it's started
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
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

    def force_stop(self) -> None:
        """Shut down without waiting for open connections to drain (thread-safe).

        SSE dashboards hold /api/stream open indefinitely, so a graceful
        shutdown can wait forever. This sets uvicorn's ``force_exit`` (skip the
        drain) and aborts open connection transports on the server's own loop
        (h11 never closes a mid-stream transport, so clients would otherwise
        not see the connection die). Used by test harnesses and Ctrl+C paths;
        prefer ``stop()`` when clients should finish cleanly.

        The listening sockets are closed *first* so no new connection is
        accepted, then open transports are aborted. A single abort pass is not
        enough: uvicorn registers a connection in its protocol's
        ``connection_made``, which asyncio *defers* onto the loop via
        ``call_soon``. So a socket already accepted at the OS level when the
        listeners close can finish registering a live transport *after* the
        abort pass — and an un-aborted mid-stream h11 SSE keeps the server task
        alive (``asyncio.Server.wait_closed()`` waits on it on 3.12+), hanging
        shutdown indefinitely (issue #109). Because the listeners are closed,
        the set of still-pending registrations is bounded, so we re-abort across
        a few consecutive loop turns (:data:`_FORCE_STOP_ABORT_PASSES`) until
        they have all run and been aborted.
        """
        server, loop = self._server, self._loop
        if server is not None and loop is not None:
            server.force_exit = True
            state = server.server_state

            def _abort_open_transports(passes_left: int) -> None:
                # Abort every currently-open transport, then re-schedule: a
                # connection whose deferred connection_made runs on a later turn
                # is caught by a subsequent pass. Unconditional (not gated on
                # ``state.connections`` being non-empty) because that set can be
                # momentarily empty between an abort and a late registration.
                for conn in list(state.connections):
                    transport = getattr(conn, "transport", None)
                    if transport is not None:
                        transport.abort()
                if passes_left > 0:
                    loop.call_soon(_abort_open_transports, passes_left - 1)

            def _stop_accepting_and_abort() -> None:
                for listener in getattr(server, "servers", []):
                    listener.close()
                _abort_open_transports(_FORCE_STOP_ABORT_PASSES)

            loop.call_soon_threadsafe(_stop_accepting_and_abort)
        self.stop()
