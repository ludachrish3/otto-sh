"""
MonitorServer — FastAPI web server for the otto monitoring dashboard.

Endpoints
---------
GET  /              Serves the React dashboard's built index.html (``web/``, via ``make web``)
GET  /static/...    Serves the built dist/ assets (JS/CSS bundles, etc.)
GET  /api/mode      JSON ``{"mode": "live"|"review", "source": str|None, "editable": bool}``
GET  /api/monitor_sessions  The ``format:1`` payload: the loaded archive in
review mode, or a snapshot of the running session in live mode (a live
session is just one whose ``end`` is still open). Both modes hydrate
through this one endpoint.
GET  /api/stream    SSE stream — pushes metric updates and events in real time
POST /api/session/{session_id}/event          Record a manual event
POST /api/session/{session_id}/event/{id}/end Stamp a span event's end (server clock)
PATCH /api/session/{session_id}/event/{id}    Edit an event's fields
DELETE /api/session/{session_id}/event/{id}   Remove an event
GET  /api/export/json  Download the current data as a ``format:1`` document
"""

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from logging import Filter, LogRecord, getLogger
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typing_extensions import override

from ..console import CONSOLE
from ..models import LabSnapshot, MonitorExport
from ..models.monitor import EventCreateBody, EventRecord, EventUpdateBody, SessionRecord
from . import archive_edit
from .collector import MetricCollector
from .event_ops import EventValidationError, merge_update, resolve_create
from .events import MonitorEvent
from .export import build_live_export, document_json
from .session import SessionFrame

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


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


_ACCESS_LOG_PATH_ARG_INDEX = 2  # position of the path+query string in uvicorn's access-log args


class _RedactAccessLogQueryString(Filter):
    """``logging.Filter`` that drops the query string from uvicorn's access log.

    Every route now carries ``?key=<token>`` (see ``_AccessKeyMiddleware``
    below), and uvicorn's access logger logs the *full* request line
    including the query string (``get_path_with_query_string``) whenever
    ``uvicorn.access`` has any handler in its propagation chain — which is
    exactly what otto's own CLI logging setup gives it, console or file. The
    ``log_config=None`` passed to ``uvicorn.Config`` in ``serve()`` only
    skips uvicorn's *own* ``logging.config.dictConfig()`` call; it does not
    disable this logger or stop it from inheriting the root logger's
    handlers. Without this filter, the per-run access key — meant to appear
    exactly once, in the printed URL — would be written to the log on every
    single request. The access-log call site passes the path+query string as
    the third positional arg (``'%s - "%s %s HTTP/%s" %d'``), so mutating
    ``record.args`` here strips it before formatting while keeping method,
    client address, and status intact.
    """

    @override
    def filter(self, record: LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > _ACCESS_LOG_PATH_ARG_INDEX:
            path = args[_ACCESS_LOG_PATH_ARG_INDEX]
            if isinstance(path, str) and "?" in path:
                new_args = list(args)
                new_args[_ACCESS_LOG_PATH_ARG_INDEX] = path.split("?", 1)[0]
                record.args = tuple(new_args)
        return True


getLogger("uvicorn.access").addFilter(_RedactAccessLogQueryString())


def _cookie_name(port: int | None) -> str:
    """Auth-cookie name, scoped by port.

    Cookies are host- but NOT port-scoped, so two monitor servers on one
    machine would clobber a fixed-name cookie; suffixing the request's own
    port keeps them independent (the same trick Jupyter uses).
    """
    return f"otto_monitor_{port if port is not None else 80}"


_FORBIDDEN_HTML = """<!doctype html>
<html><head><title>otto monitor — access key required</title></head>
<body style="font-family: system-ui; max-width: 40rem; margin: 4rem auto;">
<h1>Access key required</h1>
<p>This dashboard is protected by a per-run access key. Open the full URL
printed in the console by <code>otto monitor</code> — it ends in
<code>?key=…</code>. The bare address will not work.</p>
</body></html>"""


class _AccessKeyMiddleware:
    """Pure-ASGI gate for every route: valid ``?key=`` or auth cookie, else 403.

    Deliberately NOT ``BaseHTTPMiddleware``: the wrapped-response machinery
    there interferes with streaming responses/disconnect detection, and
    ``/api/stream`` (SSE) must be gated by the same code path as everything
    else. A correctly-keyed request of ANY path gets the cookie minted on its
    response, so keyed deep links work, and the browser then authenticates
    every follow-up (assets, fetches, EventSource) via the cookie alone.
    """

    def __init__(self, app: ASGIApp, *, key: str, secure_cookie: bool) -> None:
        self._app = app
        self._key = key
        self._secure = secure_cookie

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No websocket routes exist anywhere in this app today. Fail
            # CLOSED rather than passing an ungated scope through to the
            # wrapped app: a bare `!= "http"` passthrough here would let a
            # future websocket route silently bypass the access-key gate
            # entirely (it would never hit the checks below, which only run
            # for scope["type"] == "http"). Drain the connect event first —
            # ASGI servers expect it to be consumed — then close with 1008
            # (policy violation), the standard code for "you weren't allowed
            # to be here."
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope)
        cookie_name = _cookie_name(request.url.port)
        key_bytes = self._key.encode("ascii")
        supplied = request.query_params.get("key")
        # secrets.compare_digest(str, str) raises TypeError on non-ASCII input
        # (it refuses to guess an encoding) — an adversarial or fat-fingered
        # ?key=/cookie containing e.g. a UTF-8-only char must still fall
        # through to an ordinary 403 below, never an unhandled 500. Comparing
        # as bytes sidesteps the restriction entirely: supplied/from_cookie
        # are always str here (decoded by Starlette), so .encode("utf-8")
        # never raises.
        if supplied is not None and secrets.compare_digest(supplied.encode("utf-8"), key_bytes):
            cookie = f"{cookie_name}={self._key}; Path=/; HttpOnly; SameSite=Lax"
            if self._secure:
                cookie += "; Secure"

            async def send_with_cookie(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.append("set-cookie", cookie)
                await send(message)

            await self._app(scope, receive, send_with_cookie)
            return
        from_cookie = request.cookies.get(cookie_name)
        if from_cookie is not None and secrets.compare_digest(
            from_cookie.encode("utf-8"), key_bytes
        ):
            await self._app(scope, receive, send)
            return
        if request.url.path.startswith("/api/"):
            response: Response = JSONResponse(
                {"error": "missing or invalid access key"}, status_code=403
            )
        else:
            response = HTMLResponse(_FORBIDDEN_HTML, status_code=403)
        await response(scope, receive, send)


def _build_app(  # noqa: C901 — FastAPI route-factory; complexity is route count, not branching
    collector: MetricCollector,
    *,
    key: str,
    secure_cookie: bool = False,
    mode: Literal["live", "review"] = "live",
    document: MonitorExport | None = None,
    source_name: str | None = None,
    frame: SessionFrame | None = None,
    lab: LabSnapshot | None = None,
    archive_path: Path | None = None,
) -> FastAPI:
    dist_index = _dist_index_path()

    app = FastAPI(title="Otto Monitor")
    app.add_middleware(_AccessKeyMiddleware, key=key, secure_cookie=secure_cookie)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # The served review body is cached (a --db archive's document can hold many
    # sessions; re-serializing per request is waste) but no longer immutable:
    # a review-mode event mutation (Plan 5c) patches `document` in place and
    # refreshes this cache. A dict holder rather than a bare nonlocal keeps the
    # closure reads/writes obvious.
    _document_state: dict[str, str | None] = {
        "body": document_json(document) if document is not None else None
    }

    def _require_document_body() -> str:
        body = _document_state["body"]
        if body is None:
            raise RuntimeError(
                "MonitorServer built with mode='review' but no document — this "
                "is a programming error: the CLI always supplies one for "
                "review mode."
            )
        return body

    def _document_session(session_id: str) -> "SessionRecord | None":
        if document is None:
            return None
        return next((s for s in document.sessions if s.id == session_id), None)

    def _find_document_event(session_id: str, event_id: int) -> EventRecord | None:
        session = _document_session(session_id)
        if session is None:
            return None
        return next((e for e in session.events if e.id == event_id), None)

    def _patch_document_event(session_id: str, record: EventRecord) -> None:
        """Upsert *record* into the served document and refresh the cached body."""
        session = _document_session(session_id)
        assert session is not None  # noqa: S101 — guard ran before any write
        assert document is not None  # noqa: S101 — a session was found on it above
        for i, existing in enumerate(session.events):
            if existing.id == record.id:
                session.events[i] = record
                break
        else:
            session.events.append(record)
        _document_state["body"] = document_json(document)

    def _drop_document_event(session_id: str, event_id: int) -> None:
        session = _document_session(session_id)
        assert session is not None  # noqa: S101 — guard ran before any write
        assert document is not None  # noqa: S101 — a session was found on it above
        session.events = [e for e in session.events if e.id != event_id]
        _document_state["body"] = document_json(document)

    def _require_live_snapshot_body() -> str:
        """Build the current live-session document body, or fail loud.

        Mirrors ``_require_document_body()``'s "the caller broke the contract"
        framing: a ``mode="live"`` server with no ``frame``/``lab`` has no
        session to build a snapshot from, which is a programming error here —
        the CLI always supplies both for live mode. Only ``/api/export/json``
        calls this directly; ``/api/monitor_sessions`` hits the identical
        missing-frame/lab state during ordinary page loads (see its
        docstring) and so must check ``frame``/``lab`` itself first to return
        its softer 404 instead of raising — it then calls this for the
        build once it knows both are present.
        """
        if frame is None or lab is None:
            raise RuntimeError(
                "MonitorServer built with mode='live' but no frame/lab — this "
                "is a programming error: the CLI always supplies both for "
                "live mode."
            )
        return document_json(build_live_export(frame, collector, lab))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:  # type: ignore[reportUnusedFunction]
        """Serve the React dashboard's built ``index.html``."""
        return HTMLResponse(dist_index.read_text())

    @app.get("/api/mode")
    async def get_mode() -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Report whether this server serves live data or a loaded review document.

        ``editable`` tells the UI whether marking is possible at all: live
        mode always is, and a review document is only if it was loaded from a
        ``--db`` archive (Task 5) — an ``archive_path`` is set so a mutation
        has somewhere to persist. A ``.json`` review has no such target and
        stays permanently read-only.
        """
        return JSONResponse(
            {
                "mode": mode,
                "source": source_name,
                "editable": mode == "live" or archive_path is not None,
            }
        )

    @app.get("/api/monitor_sessions")
    async def monitor_sessions() -> Response:  # type: ignore[reportUnusedFunction]
        """Serve the format:1 payload: the loaded archive, or a snapshot of the live run.

        Live and review hydrate through the SAME endpoint and the SAME shape —
        that is what lets every view work live with no per-view work. A live
        monitor session is just one whose ``end`` is still open, exactly like a
        crashed session on disk.

        A ``mode="live"`` server that isn't recording a session (``frame``/
        ``lab`` unset — e.g. a bare ``DashboardHarness`` used only to serve the
        Import shell, or a dashboard opened before ``otto monitor --live`` has
        attached one) has no session to serve. That's a legitimate "nothing
        here", not a programming error: it 404s rather than raising, so the
        client's soft-fail boot contract (bootstrap.ts) sees an ordinary failed
        hydrate instead of a 500 + server traceback on every such page load.
        """
        if mode == "review":
            return Response(content=_require_document_body(), media_type="application/json")
        if frame is None or lab is None:
            return JSONResponse(
                {"error": "no monitor session is being recorded"},
                status_code=404,
            )
        return Response(content=_require_live_snapshot_body(), media_type="application/json")

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

    def _event_response(event_dict: dict[str, object], status_code: int = 200) -> JSONResponse:
        """Serialize a MonitorEvent.to_dict() as a format:1 EventRecord.

        One reshape, exclude_none like every other format:1 surface — a point
        event omits end_timestamp instead of carrying null (document_json's
        contract, so the SSE echo and the HTTP response are field-identical).
        """
        record = EventRecord.model_validate(event_dict)
        return JSONResponse(
            record.model_dump(mode="json", exclude_none=True), status_code=status_code
        )

    def _mutation_guard(session_id: str) -> JSONResponse | None:
        """404 for a session this server doesn't hold; 403 where editing is impossible."""
        if mode == "live":
            if frame is None or session_id != collector.session_id:
                return JSONResponse({"error": "unknown session"}, status_code=404)
            return None
        if archive_path is None:
            # A .json source has no persistence target (spec: .json review is
            # read-only); the UI hides marking via /api/mode's editable flag.
            return JSONResponse({"error": "this monitor source is read-only"}, status_code=403)
        if _document_session(session_id) is None:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        return None

    @app.post("/api/session/{session_id}/event")
    async def create_event(session_id: str, body: EventCreateBody) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        try:
            timestamp, end_timestamp = resolve_create(body)
        except EventValidationError as err:
            return JSONResponse({"error": str(err)}, status_code=422)
        if mode == "review":
            assert archive_path is not None  # noqa: S101 — guard enforced this
            try:
                rowid = await asyncio.to_thread(
                    archive_edit.insert_event,
                    str(archive_path),
                    session_id,
                    timestamp=timestamp,
                    end_timestamp=end_timestamp,
                    label=body.label,
                    source="manual",
                    color=body.color,
                    dash=body.dash,
                )
            except archive_edit.ArchiveLockedError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            record = EventRecord(
                id=rowid,
                timestamp=timestamp,
                end_timestamp=end_timestamp,
                label=body.label,
                source="manual",
                color=body.color,
                dash=body.dash,
            )
            _patch_document_event(session_id, record)
            return JSONResponse(record.model_dump(mode="json", exclude_none=True), status_code=201)
        event = await collector.add_event(
            label=body.label,
            timestamp=timestamp,
            color=body.color,
            dash=body.dash,
            source="manual",
            end_timestamp=end_timestamp,
        )
        return _event_response(event.to_dict(), status_code=201)

    async def _apply_live_update(
        event_id: int, existing: "MonitorEvent", body: EventUpdateBody
    ) -> JSONResponse:
        """One merge rule for PATCH and /end — event_ops resolves, collector writes."""
        try:
            fields = merge_update(
                body,
                existing_label=existing.label,
                existing_color=existing.color,
                existing_dash=existing.dash,
                existing_timestamp=existing.timestamp,
                existing_end=existing.end_timestamp,
            )
        except EventValidationError as err:
            return JSONResponse({"error": str(err)}, status_code=422)
        updated = await collector.update_event(
            event_id,
            label=fields.label,
            color=fields.color,
            dash=fields.dash,
            timestamp=fields.timestamp,
            end_timestamp=fields.end_timestamp,
        )
        if updated is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return _event_response(updated.to_dict())

    async def _apply_review_update(
        session_id: str, event_id: int, existing: EventRecord, body: EventUpdateBody
    ) -> JSONResponse:
        """Review-mode mirror of ``_apply_live_update`` — same merge rule, archive write.

        The SAME ``event_ops.merge_update`` Task 3 wired into the live path
        (Chris's dedup directive): only the write target (archive_edit vs. the
        collector) and the "existing event" source (the document's
        ``EventRecord`` vs. a live ``MonitorEvent``) differ.
        """
        assert archive_path is not None  # noqa: S101 — guard enforced this
        try:
            fields = merge_update(
                body,
                existing_label=existing.label,
                existing_color=existing.color,
                existing_dash=existing.dash,
                existing_timestamp=existing.timestamp,
                existing_end=existing.end_timestamp,
            )
        except EventValidationError as err:
            return JSONResponse({"error": str(err)}, status_code=422)
        try:
            updated = await asyncio.to_thread(
                archive_edit.update_event,
                str(archive_path),
                session_id,
                event_id,
                timestamp=fields.timestamp,
                end_timestamp=fields.end_timestamp,
                label=fields.label,
                color=fields.color,
                dash=fields.dash,
            )
        except archive_edit.ArchiveLockedError as err:
            return JSONResponse({"error": str(err)}, status_code=409)
        if not updated:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        record = EventRecord(
            id=event_id,
            timestamp=fields.timestamp,
            end_timestamp=fields.end_timestamp,
            label=fields.label,
            source=existing.source,
            color=fields.color,
            dash=fields.dash,
        )
        _patch_document_event(session_id, record)
        return JSONResponse(record.model_dump(mode="json", exclude_none=True))

    @app.post("/api/session/{session_id}/event/{event_id}/end")
    async def end_event(session_id: str, event_id: int) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Stamp a span's end with the server clock (the live Stop button)."""
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        # Ending IS a partial update (end_timestamp only) — same seam, not a
        # second resolution path, in either mode.
        stamp_end = EventUpdateBody(end_timestamp=datetime.now(tz=timezone.utc))
        if mode == "review":
            doc_existing = _find_document_event(session_id, event_id)
            if doc_existing is None:
                return JSONResponse({"error": "Event not found"}, status_code=404)
            if doc_existing.end_timestamp is not None:
                return JSONResponse({"error": "Event already ended"}, status_code=409)
            return await _apply_review_update(session_id, event_id, doc_existing, stamp_end)
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        if existing.end_timestamp is not None:
            return JSONResponse({"error": "Event already ended"}, status_code=409)
        return await _apply_live_update(event_id, existing, stamp_end)

    @app.patch("/api/session/{session_id}/event/{event_id}")
    async def update_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, body: EventUpdateBody
    ) -> JSONResponse:
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        if mode == "review":
            doc_existing = _find_document_event(session_id, event_id)
            if doc_existing is None:
                return JSONResponse({"error": "Event not found"}, status_code=404)
            return await _apply_review_update(session_id, event_id, doc_existing, body)
        existing = next((e for e in collector.get_events() if e.id == event_id), None)
        if existing is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return await _apply_live_update(event_id, existing, body)

    @app.delete("/api/session/{session_id}/event/{event_id}")
    async def delete_event(session_id: str, event_id: int) -> Response:  # type: ignore[reportUnusedFunction]
        refused = _mutation_guard(session_id)
        if refused is not None:
            return refused
        if mode == "review":
            assert archive_path is not None  # noqa: S101 — guard enforced this
            try:
                deleted = await asyncio.to_thread(
                    archive_edit.delete_event, str(archive_path), session_id, event_id
                )
            except archive_edit.ArchiveLockedError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            if not deleted:
                return JSONResponse({"error": "Event not found"}, status_code=404)
            _drop_document_event(session_id, event_id)
            return Response(status_code=204)
        if await collector.delete_event(event_id):
            return Response(status_code=204)
        return JSONResponse({"error": "Event not found"}, status_code=404)

    @app.get("/api/export/json")
    async def export_json() -> Response:  # type: ignore[reportUnusedFunction]
        """Download the current data as a ``format:1`` document.

        Review mode re-serves the loaded document verbatim; live mode builds
        a fresh single-session document from *frame*/*collector*/*lab*.
        """
        body = _require_document_body() if mode == "review" else _require_live_snapshot_body()
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="monitor-export.json"'},
        )

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

    Two modes, chosen by the CLI at construction time:

    * ``mode="live"`` (default) — serves a running
      :class:`~otto.monitor.collector.MetricCollector`. ``frame`` and ``lab``
      must both be supplied so ``/api/monitor_sessions``/``/api/export/json``
      can build a snapshot document on demand.
    * ``mode="review"`` — serves a pre-built ``document`` (a
      ``format:1`` :class:`~otto.models.monitor.MonitorExport`, e.g. read back
      from a ``--db`` session archive). ``source_name`` is the human-facing
      origin (a file path) reported by ``/api/mode``. ``archive_path`` (Task
      5), when given, is a ``.db`` archive event mutations persist to and
      patch ``document`` in place for; omitted (the ``.json`` case) leaves
      review mode permanently read-only.

    Call ``await serve()`` from an async context to run the server.
    Call ``stop()`` from any thread to trigger a graceful shutdown, or
    ``force_stop()`` to abort open SSE connections immediately instead of
    waiting for them to drain.
    """

    def __init__(  # noqa: PLR0913 — two construction modes (live/review) share one wide API
        self,
        collector: MetricCollector,
        host: str = "0.0.0.0",  # noqa: S104 — intentional all-interface bind
        port: int = 0,
        *,
        mode: Literal["live", "review"] = "live",
        document: MonitorExport | None = None,
        source_name: str | None = None,
        frame: SessionFrame | None = None,
        lab: LabSnapshot | None = None,
        tls_cert: Path | None = None,
        tls_key: Path | None = None,
        archive_path: Path | None = None,
    ) -> None:
        self._collector = collector
        self._bind_host = host
        self._port = port
        self._mode = mode
        self._document = document
        self._source_name = source_name
        self._frame = frame
        self._lab = lab
        self._tls_cert = tls_cert
        self._tls_key = tls_key
        self._archive_path = archive_path
        self._key = secrets.token_urlsafe(16)
        # The collector has no notion of sessions; the server holds the frame, so
        # it stamps the id here — one place, so no construction site can forget.
        if frame is not None:
            collector.session_id = frame.id
        self._app = _build_app(
            collector,
            key=self._key,
            secure_cookie=tls_cert is not None,
            mode=mode,
            document=document,
            source_name=source_name,
            frame=frame,
            lab=lab,
            archive_path=archive_path,
        )
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def key(self) -> str:
        """The per-run access key (printed once in the URLs, required by every request)."""
        return self._key

    @property
    def origin(self) -> str:
        """Scheme+host+port with NO key — for callers composing their own paths."""
        scheme = "https" if self._tls_cert is not None else "http"
        host = self._bind_host
        if host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            host = ips[0] if ips else self._bind_host
        return f"{scheme}://{host}:{self._port}"

    @property
    def url(self) -> str:
        """Primary self-authenticating URL (first non-loopback IP, ``?key=`` appended)."""
        return f"{self.origin}/?key={self._key}"

    @property
    def urls(self) -> list[str]:
        """All reachable URLs (one per non-loopback interface), each ``?key=``-keyed."""
        scheme = "https" if self._tls_cert is not None else "http"
        if self._bind_host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            if ips:
                return [f"{scheme}://{ip}:{self._port}/?key={self._key}" for ip in ips]
        return [f"{scheme}://{self._bind_host}:{self._port}/?key={self._key}"]

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
            ssl_certfile=str(self._tls_cert) if self._tls_cert else None,
            ssl_keyfile=str(self._tls_key) if self._tls_key else None,
        )
        server = uvicorn.Server(config)

        async def run_uvicorn() -> None:
            # uvicorn answers a failed startup (e.g. the requested port is
            # already bound) with sys.exit(STARTUP_FAILURE). A SystemExit
            # raised inside a Task is NOT stored on the task like a normal
            # exception — Task.__step re-raises it into the event loop
            # itself, so it detonates out of the embedding application's
            # asyncio.run() where no except around serve() (and no
            # task.result() below) can ever catch it. Translate it here,
            # inside the coroutine, while it still unwinds normally.
            try:
                await server.serve()
            except SystemExit as exc:
                raise RuntimeError(
                    f"monitor server failed to start on "
                    f"{self._bind_host}:{self._port} — is the port already in use?"
                ) from exc

        # start the server in a background task
        self._loop = asyncio.get_running_loop()
        task = asyncio.create_task(run_uvicorn())

        # wait until uvicorn signals it's started
        while not server.started:
            if task.done():
                # The serve task died before signalling startup (e.g. a bad
                # TLS cert/key raises ssl.SSLError out of Config.load()) — left
                # unchecked, this loop would poll `server.started` forever
                # since nothing will ever flip it. task.result() re-raises
                # whatever killed it, surfacing the real cause instead of a
                # silent hang.
                task.result()
            await asyncio.sleep(0.05)

        # extract the port from the socket
        self._port = server.servers[0].sockets[0].getsockname()[1]
        all_urls = self.urls  # each carries the per-run ?key=<token> credential
        # SECURITY: the access key must never be written to the on-disk log
        # sinks. Print the keyed URL straight to the terminal via CONSOLE, which
        # bypasses the file-backed 'otto' logger entirely (the same way
        # management._print_output_dir emits the output dir) — logging it would
        # persist a live credential to console.log / verbose.log on every run.
        # Only a keyless origin goes to the logger for the on-disk audit trail.
        if len(all_urls) == 1:
            CONSOLE.print(f"Server running at {all_urls[0]}", highlight=False)
        else:
            CONSOLE.print("Server running at:", highlight=False)
            for u in all_urls:
                CONSOLE.print(f"  {u}", highlight=False)
        CONSOLE.print("Press Ctrl+C to stop", highlight=False)
        logger.info(f"Monitor dashboard started on {self.origin} (access key omitted from logs)")

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
        a few consecutive loop turns (``_FORCE_STOP_ABORT_PASSES``) until
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
