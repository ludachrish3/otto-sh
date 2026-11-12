"""
MonitorServer — FastAPI web server for the otto monitoring dashboard.

Endpoints
---------
GET  /              Serves the React dashboard's built index.html (``web/``, via ``make web``)
GET  /static/...    Serves the built dist/ assets (JS/CSS bundles, etc.)
GET  /api/mode      The served source's state, a ``MonitorSourceState``: its mode,
kind (``live``, ``db`` or ``json``), name, token, whether it is editable, and
its write counts, stamped with the server's time
GET  /api/monitor_sessions  The ``format:1`` payload: the loaded archive in
review mode, or a snapshot of the running session in live mode (a live
session is just one whose ``end`` is still open). Both modes hydrate
through this one endpoint. The response names the source it was read from
(``X-Otto-Source``) and the time it was read (``X-Otto-Observed-At``).
GET  /api/stream    SSE stream, in both modes: live pushes metric updates and
events in real time; review pushes every event change and the source's state
(a switch, the unsaved count), so every tab on the URL shows the same data
POST /api/session/{session_id}/event          Record a manual event
POST /api/session/{session_id}/event/{id}/end Stamp a span event's end (server clock)
PATCH /api/session/{session_id}/event/{id}    Edit an event's fields
POST /api/session/{session_id}/event/{id}/revert  Undo or redo one event change
(body ``{expect, write}``, each an event record or ``null``). Writes ``write``
only if the event is still ``expect``: 200 with the stored record (``null``
after a delete); 409 ``{code: "changed", current}`` when it is not (or is gone).
DELETE /api/session/{session_id}/event/{id}   Remove an event: 200 with the
deleted record (not 204), so a client can restore it
GET  /api/export/json  Download the current data as a ``format:1`` document
POST /api/source    Switch a ``.json`` review to an uploaded ``.json`` (the raw
body), confirmed with the token and unsaved count the tab was shown
(``X-Otto-Source``, ``X-Otto-Unsaved``) and named by ``X-Otto-Source-Name``:
200 with the new source's state. 403 ``not_switchable`` for a live or ``.db``
source, 413 ``too_large``, 409 ``source_changed`` for a stale confirmation,
422 for a document that is not an editable export
POST /api/source/saved  Acknowledge an export (body ``{token, revision}``): the
unsaved count drops by the writes the file covered (a source whose writes no
export saves keeps its count); 200 with the state

Every event write carries the token of the source it was made against, in
``X-Otto-Source``. A write with a missing or different token is refused
with 409 ``{code: "source_changed", source_state}``, the source as it is now.
A review write is checked under the mutation lock, so a write that queued
behind a source switch is refused too. A review edit streams its session
fragment, and a ``.json`` one then a source frame with the new counts.
"""

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from logging import Filter, LogRecord, getLogger
from pathlib import Path
from typing import Any, Literal, ParamSpec, TypeVar
from urllib.parse import unquote

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typing_extensions import final, override

from .._webassets.artifacts import MONITOR
from ..console import CONSOLE, print_literal
from ..logger.formatters import NO_LOCALS_ATTR
from ..models.monitor import (
    EventCreateBody,
    EventRecord,
    EventRevertBody,
    EventUpdateBody,
    LabSnapshot,
    MonitorExport,
    MonitorSourceState,
    SourceSavedBody,
)
from ..utils import wait_for_async
from . import event_ops, review_source
from .archive_edit import ArchiveLockedError
from .broadcast import ENDED, LAPSED, end_queue
from .collector import MetricCollector, observed_now
from .errors import ReviewSourceError
from .event_ops import EventValidationError, merge_update, resolve_create
from .events import MonitorEvent
from .export import build_live_export, document_json
from .review_source import (
    NotSwitchable,
    ReviewEvents,
    ReviewSource,
    SourceChanged,
    SourceSlot,
    UnknownSessionError,
    UploadGate,
    UploadTooLarge,
    ViewOnly,
    new_source_token,
    switch_source,
)
from .session import SessionFrame

_STATIC_DIR = MONITOR

logger = logging.getLogger(__name__)


class _AlreadyEndedError(Exception):
    """An /end resolver found the span already ended (answered 409)."""


def _resolve_update(
    body: EventUpdateBody, current: "MonitorEvent | EventRecord", *, refuse_ended: bool
) -> event_ops.ResolvedEventFields:
    """Resolve a PATCH or /end onto the event as it stands: the ONE update resolver for both modes.

    ``refuse_ended`` is /end's "already ended" check (:class:`_AlreadyEndedError`);
    the merge is ``event_ops.merge_update``. Each mode's adapter builds its
    own record from the result (a live ``MonitorEvent`` via
    ``dataclasses.replace``, a review ``EventRecord``) and runs it inside its
    mutation sequence, so it reads the current fields under the lock.
    """
    if refuse_ended and current.end_timestamp is not None:
        raise _AlreadyEndedError
    return merge_update(
        body,
        existing_label=current.label,
        existing_color=current.color,
        existing_dash=current.dash,
        existing_timestamp=current.timestamp,
        existing_end=current.end_timestamp,
    )


OBSERVED_AT_HEADER = "X-Otto-Observed-At"
"""The response header that carries the server's time on every event mutation, in both modes.

The dashboard (``web/src/data/sourceApi.ts`` spells the name again;
tests/unit/models/test_browser_format_mirrors.py holds the two equal)
puts it on the fragment it applies from the response, so a mark raises the
live window's horizon at once even when the stream's echo is withheld (#866).
``/api/monitor_sessions`` answers it too, in both modes: the time its body
was read, which the dashboard replays later pushes against.
"""

SOURCE_TOKEN_HEADER = "X-Otto-Source"  # noqa: S105 — a header name, not a secret
"""The header that names a source by its token.

``/api/monitor_sessions`` answers it with the token of the source its body was
read from, and every event write sends it with the token of the source it was
made against. ``web/src/data/sourceApi.ts`` spells the name again;
tests/unit/models/test_browser_format_mirrors.py holds the two equal.
"""

UNSAVED_HEADER = "X-Otto-Unsaved"
"""The unsaved count a source switch confirmation was shown, in decimal, as a header.

The compare is exact: the value must be the exact decimal string of the
served source's count (``str(count)``), so a leading zero, a sign or any
padding (``03``, ``+3``) is refused as ``source_changed``, like a stale count.

Mirrored by ``web/src/data/sourceApi.ts`` (see :data:`SOURCE_TOKEN_HEADER`).
"""

SOURCE_NAME_HEADER = "X-Otto-Source-Name"
"""The header that carries an uploaded source's file name (percent-encoded UTF-8).

Mirrored by ``web/src/data/sourceApi.ts`` (see :data:`SOURCE_TOKEN_HEADER`).
"""

_UPLOAD_NAME = "upload.json"
"""The name an upload whose :data:`SOURCE_NAME_HEADER` is absent or empty is served under."""


def _upload_name(request: Request) -> str:
    """Return the name an upload is served under: its decoded :data:`SOURCE_NAME_HEADER`.

    Every non-printable character is dropped, so the name the dashboard and
    the terminal's unsaved-changes warning show carries no control sequence.
    A header that is absent, empty, or empty once dropped gives
    :data:`_UPLOAD_NAME`.
    """
    decoded = unquote(request.headers.get(SOURCE_NAME_HEADER, ""))
    return "".join(ch for ch in decoded if ch.isprintable()) or _UPLOAD_NAME


_P = ParamSpec("_P")
_R = TypeVar("_R", bound=Response)


def _dist_index_path() -> Path:
    """Return the built React dashboard's ``index.html``, or raise a fix-it error.

    The React port (``web/``) is the only frontend — the legacy static
    dashboard was deleted at the cutover. ``make web`` (vite
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


class _OmitTracebackLocals(Filter):
    """``logging.Filter`` that keeps the locals out of a uvicorn error's traceback.

    A route that raises is logged by uvicorn on ``uvicorn.error`` with the
    exception, and otto's log files write a traceback with every frame's local
    variables. Every request passes through ``_AccessKeyMiddleware.__call__``,
    whose locals hold the access key (the decoded ``key_bytes``, the
    ``?key=`` value, the cookie it mints), so the key would reach both files
    on any route failure. Marking the record here works because a logger's
    filters run on the logging thread before any handler sees the record, so
    the mark is in place before the record's exception is captured as data.
    The traceback itself, with its exception type and message, is still
    written.
    """

    @override
    def filter(self, record: LogRecord) -> bool:
        if record.exc_info:
            setattr(record, NO_LOCALS_ATTR, True)
        return True


getLogger("uvicorn.error").addFilter(_OmitTracebackLocals())


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

    Also the dashboard's request log: one INFO per admitted IP and one WARNING
    per refused IP for the life of this app (one server run), and a DEBUG line
    per request. uvicorn's own access log is off.
    """

    def __init__(self, app: ASGIApp, *, key: str, secure_cookie: bool) -> None:
        self._app = app
        self._key = key
        self._secure = secure_cookie
        self._admitted: set[str] = set()
        self._refused: set[str] = set()

    def _note_admitted(self, ip: str) -> None:
        if ip not in self._admitted:
            self._admitted.add(ip)
            logger.info(f"Dashboard client connected from {ip}")

    def _note_refused(self, ip: str) -> None:
        if ip not in self._refused:
            self._refused.add(ip)
            logger.warning(f"Dashboard request without a valid access key from {ip}")

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
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        method, path = scope.get("method", ""), scope.get("path", "")

        async def logged_send(message: Message) -> None:
            # scope["path"] only, never the query string: the access key rides
            # in ?key= and must never reach a log record.
            if message["type"] == "http.response.start":
                logger.debug(f"{method} {path} {message['status']} from {ip}")
            await send(message)

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
                await logged_send(message)

            self._note_admitted(ip)
            await self._app(scope, receive, send_with_cookie)
            return
        from_cookie = request.cookies.get(cookie_name)
        if from_cookie is not None and secrets.compare_digest(
            from_cookie.encode("utf-8"), key_bytes
        ):
            self._note_admitted(ip)
            await self._app(scope, receive, logged_send)
            return
        self._note_refused(ip)
        if request.url.path.startswith("/api/"):
            response: Response = JSONResponse(
                {"error": "missing or invalid access key"}, status_code=403
            )
        else:
            response = HTMLResponse(_FORBIDDEN_HTML, status_code=403)
        await response(scope, receive, logged_send)


def _build_app(  # noqa: C901 — FastAPI route-factory; complexity is route count, not branching
    collector: MetricCollector,
    *,
    key: str,
    secure_cookie: bool = False,
    mode: Literal["live", "review"] = "live",
    slot: SourceSlot | None = None,
    frame: SessionFrame | None = None,
    lab: LabSnapshot | None = None,
    drain: "_StreamDrain | None" = None,
) -> FastAPI:
    dist_index = _dist_index_path()
    # The /api/stream admission this app serves; MonitorServer passes its own
    # so it can close it when the drain begins. A bare app gets a private,
    # never-closed one.
    streams = drain if drain is not None else _StreamDrain(collector)

    app = FastAPI(title="Otto Monitor")
    app.add_middleware(_AccessKeyMiddleware, key=key, secure_cookie=secure_cookie)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # The review source this app serves (`slot.current`; None for a live
    # server). A bare app gets an empty slot of its own.
    source_slot = slot if slot is not None else SourceSlot(None)
    # A live server's one source never changes, so its token is made once.
    live_token = new_source_token()

    def _review_source() -> ReviewSource:
        current = source_slot.current
        if current is None:
            raise RuntimeError(
                "MonitorServer built with mode='review' but no document — this "
                "is a programming error: the CLI always supplies one for "
                "review mode."
            )
        return current

    def _source_state() -> MonitorSourceState:
        """Return the served source's state, stamped now: the ONE answer of ``/api/mode``."""
        stamp = observed_now()
        if mode == "live":
            return MonitorSourceState(
                mode="live",
                kind="live",
                source=None,
                token=live_token,
                editable=True,
                observed_at=stamp,
            )
        return _review_source().state(stamp)

    def _source_changed(err: SourceChanged) -> JSONResponse:
        """409 for a write aimed at a source this server no longer serves: the ONE shape.

        ``err.state`` is the source the server serves now.
        """
        return JSONResponse(
            {
                "error": str(err),
                "code": "source_changed",
                "source_state": err.state.model_dump(mode="json"),
            },
            status_code=409,
        )

    review_events = ReviewEvents(source_slot, collector.mutation_lock)
    upload_gate = UploadGate()

    def _live_snapshot_body(observed_at: str) -> str:
        """Build the current live-session document body, stamped *observed_at*, or fail loud.

        A ``mode="live"`` server with no ``frame``/``lab`` has no session to
        build a snapshot from, which is a programming error here — the CLI
        always supplies both for live mode. Only ``/api/export/json`` reaches
        this without checking; ``/api/monitor_sessions`` hits the identical
        missing-frame/lab state during ordinary page loads (see its
        docstring) and so checks ``frame``/``lab`` itself first to return its
        softer 404 instead of raising.
        """
        if frame is None or lab is None:
            raise RuntimeError(
                "MonitorServer built with mode='live' but no frame/lab — this "
                "is a programming error: the CLI always supplies both for "
                "live mode."
            )
        return document_json(build_live_export(frame, collector, lab, observed_at=observed_at))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:  # type: ignore[reportUnusedFunction]
        """Serve the React dashboard's built ``index.html``."""
        return HTMLResponse(dist_index.read_text())

    @app.get("/api/mode")
    async def get_mode() -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Report the served source's state (:class:`~otto.models.monitor.MonitorSourceState`).

        ``editable`` tells the UI whether marking is possible at all: live
        mode always is, and so is every review source
        (:meth:`~otto.monitor.review_source.ReviewSource.state`). ``token`` is
        what every write sends back.
        Every field is sent, defaults included: the dashboard reads the whole
        state.
        """
        return JSONResponse(_source_state().model_dump(mode="json"))

    @app.get("/api/monitor_sessions")
    async def monitor_sessions() -> Response:  # type: ignore[reportUnusedFunction]
        """Serve the format:1 payload: the loaded archive, or a snapshot of the live run.

        Live and review hydrate through the SAME endpoint and the SAME shape —
        that is what lets every view work live with no per-view work. A live
        monitor session is just one whose ``end`` is still open, exactly like a
        crashed session on disk.

        A ``mode="live"`` server that isn't recording a session (``frame``/
        ``lab`` unset — e.g. a bare ``DashboardHarness``, or a dashboard opened
        before ``otto monitor --live`` has attached one) has no session to
        serve. That's a legitimate "nothing here", not a programming error: it
        404s rather than raising, so the client (bootstrap.ts) sees an ordinary
        failed hydrate, keeps the live source pending with Import withheld, and
        retries through its stream's reconnect, instead of a 500 + server
        traceback on every such attempt.
        """
        if mode == "review":
            source = _review_source()
            # No await between the body read and the stamp: the stamp is when
            # this body was the served one.
            body = source.body()
            stamp = observed_now()
            token = source.token
        else:
            if frame is None or lab is None:
                return JSONResponse(
                    {"error": "no monitor session is being recorded"},
                    status_code=404,
                )
            stamp = observed_now()
            body = _live_snapshot_body(stamp)
            token = live_token
        return Response(
            content=body,
            media_type="application/json",
            headers={SOURCE_TOKEN_HEADER: token, OBSERVED_AT_HEADER: stamp},
        )

    @app.get("/api/stream")
    async def stream(request: Request) -> Response:  # type: ignore[reportUnusedFunction]
        if streams.closed:
            # The server is draining: refuse at once, so a request that
            # arrives after the drain began (keep-alive, or accepted before
            # the listeners closed) completes instead of holding the drain.
            return Response(status_code=503)
        # Subscribe and register with no await between: _StreamDrain.close()
        # runs on this loop, so it sees either neither or both.
        q = collector.subscribe()
        streams.queues.add(q)

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
                    except asyncio.TimeoutError:
                        # Send a keepalive comment so the browser doesn't close the connection
                        yield {"comment": "keepalive"}
                        continue
                    if payload is LAPSED:
                        # This subscriber overflowed and its queued frames are
                        # gone (see otto.monitor.broadcast). Ending the
                        # response is the signal: EventSource reports the
                        # close as an error and the client resyncs from the
                        # snapshot before reopening — the only path that
                        # restores a lost one-shot frame such as chart_map.
                        yield {"comment": "lapsed: resync"}
                        break
                    if payload is ENDED:
                        # The server's graceful drain began (_StreamDrain):
                        # the clean end of the response is the signal.
                        break
                    yield {"data": json.dumps(payload)}
            finally:
                streams.queues.discard(q)
                collector.unsubscribe(q=q)

        return EventSourceResponse(generator())

    def _event_response(event_dict: dict[str, object], status_code: int = 200) -> JSONResponse:
        """Serialize a MonitorEvent.to_dict() as a format:1 EventRecord.

        One reshape, exclude_none like every other format:1 surface — a point
        event omits end_timestamp instead of carrying null (document_json's
        contract, so the SSE echo and the HTTP response are field-identical).
        """
        return _record_response(EventRecord.model_validate(event_dict), status_code)

    def _record_response(record: EventRecord, status_code: int = 200) -> JSONResponse:
        """Serialize a format:1 EventRecord, exclude_none (see ``_event_response``)."""
        return JSONResponse(
            record.model_dump(mode="json", exclude_none=True), status_code=status_code
        )

    def _event_changed(err: event_ops.EventChanged) -> JSONResponse:
        """409 ``changed`` carrying the event as it stands: the ONE shape a stale write answers."""
        current = (
            None if err.current is None else err.current.model_dump(mode="json", exclude_none=True)
        )
        return JSONResponse(
            {"error": str(err), "code": "changed", "current": current}, status_code=409
        )

    # Every refusal an event or source route answers, and its response: the
    # ONE list, for both modes. Checked in order, so a subclass (IdsExhausted, an
    # EventValidationError) takes its base's answer unless listed first.
    refusals: "dict[type[Exception], Callable[[Any], JSONResponse]]" = {
        _AlreadyEndedError: lambda _err: JSONResponse(
            {"error": "Event already ended"}, status_code=409
        ),
        # The backend's own write rule (event_ops.check_event_write), or a
        # .json source out of ids: the same 422 in both modes.
        EventValidationError: lambda err: JSONResponse({"error": str(err)}, status_code=422),
        event_ops.EventChanged: _event_changed,
        # An archive another otto holds: the ONE busy shape.
        ArchiveLockedError: lambda err: JSONResponse(
            {"error": str(err), "code": "busy"}, status_code=409
        ),
        SourceChanged: _source_changed,
        ViewOnly: lambda err: JSONResponse(
            {"error": str(err), "code": "view_only"}, status_code=403
        ),
        UnknownSessionError: lambda err: JSONResponse({"error": str(err)}, status_code=404),
        NotSwitchable: lambda err: JSONResponse(
            {"error": str(err), "code": "not_switchable"}, status_code=403
        ),
        UploadTooLarge: lambda err: JSONResponse(
            {"error": str(err), "code": "too_large"}, status_code=413
        ),
        # An uploaded document that is not an editable export.
        ReviewSourceError: lambda err: JSONResponse({"error": str(err)}, status_code=422),
    }
    refused_types = tuple(refusals)

    def _refused(err: Exception) -> JSONResponse:
        """Answer a refused event write with the first ``refusals`` entry it is an instance of."""
        return next(answer(err) for kind, answer in refusals.items() if isinstance(err, kind))

    def _live_guard(session_id: str, request: Request) -> JSONResponse | None:
        """Refuse a live write this server cannot take: 409 or 404.

        A live write whose ``SOURCE_TOKEN_HEADER`` is missing or names another
        source is refused as ``source_changed``. The live source never
        changes, so this check, outside the mutation lock, is exact. Then 404
        for a session this server doesn't hold. A review write is checked by
        :class:`~otto.monitor.review_source.ReviewEvents`, under the lock.
        """
        if _token(request) != live_token:
            return _refused(SourceChanged(_source_state()))
        if frame is None or session_id != collector.session_id:
            return _refused(UnknownSessionError(session_id))
        return None

    def _token(request: Request) -> str | None:
        """Return the token of the source a write was made against."""
        return request.headers.get(SOURCE_TOKEN_HEADER)

    def _observed(
        route: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        """Stamp an event route's response, whatever its status, with server time.

        One wrapper for every event route in both modes, so no route can
        forget the header. The stamp follows the publish of the response's
        own change, so the dashboard can order the two.
        """

        @functools.wraps(route)
        async def stamped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            response = await route(*args, **kwargs)
            response.headers[OBSERVED_AT_HEADER] = observed_now()
            return response

        return stamped

    @app.post("/api/session/{session_id}/event")
    @_observed
    async def create_event(  # type: ignore[reportUnusedFunction]
        session_id: str, body: EventCreateBody, request: Request
    ) -> JSONResponse:
        try:
            if mode == "review":
                timestamp, end_timestamp = resolve_create(body)
                new = EventRecord(
                    timestamp=timestamp,
                    end_timestamp=end_timestamp,
                    label=body.label,
                    source="manual",
                    color=body.color,
                    dash=body.dash,
                )
                record = await review_events.create(_token(request), session_id, new)
                return _record_response(record, status_code=201)
            refused = _live_guard(session_id, request)
            if refused is not None:
                return refused
            timestamp, end_timestamp = resolve_create(body)
            event = await collector.add_event(
                label=body.label,
                timestamp=timestamp,
                color=body.color,
                dash=body.dash,
                source="manual",
                end_timestamp=end_timestamp,
            )
        except refused_types as err:
            return _refused(err)
        return _event_response(event.to_dict(), status_code=201)

    async def _apply_update(
        request: Request,
        session_id: str,
        event_id: int,
        body: EventUpdateBody,
        *,
        refuse_ended: bool = False,
    ) -> JSONResponse:
        """One merge rule for PATCH and /end in both modes: ``_resolve_update`` inside the sequence.

        The merge runs INSIDE each mode's one mutation entry point
        (``collector.mutate_event`` live, ``ReviewEvents.mutate`` in
        review), onto the event as it stands under the lock: a PATCH racing
        an /end (or a delete) can never merge onto fields read before the
        other landed. Only the record each backend builds differs.
        """
        try:
            if mode == "review":
                record = await review_events.mutate(
                    _token(request),
                    session_id,
                    event_id,
                    lambda current: EventRecord(
                        id=current.id,
                        source=current.source,
                        **event_ops.editable_fields(
                            _resolve_update(body, current, refuse_ended=refuse_ended)
                        ),
                    ),
                )
            else:
                refused = _live_guard(session_id, request)
                if refused is not None:
                    return refused
                event = await collector.mutate_event(
                    event_id=event_id,
                    resolve=lambda current: dataclasses.replace(
                        current,
                        **event_ops.editable_fields(
                            _resolve_update(body, current, refuse_ended=refuse_ended)
                        ),
                    ),
                )
                record = None if event is None else EventRecord.model_validate(event.to_dict())
        except refused_types as err:
            return _refused(err)
        if record is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return _record_response(record)

    @app.post("/api/session/{session_id}/event/{event_id}/end")
    @_observed
    async def end_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, request: Request
    ) -> JSONResponse:
        """Stamp a span's end with the server clock (the live Stop button)."""
        # Ending IS a partial update (end_timestamp only) — same seam, not a
        # second resolution path, in either mode.
        stamp_end = EventUpdateBody(end_timestamp=event_ops.utc_now())
        return await _apply_update(request, session_id, event_id, stamp_end, refuse_ended=True)

    @app.patch("/api/session/{session_id}/event/{event_id}")
    @_observed
    async def update_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, body: EventUpdateBody, request: Request
    ) -> JSONResponse:
        return await _apply_update(request, session_id, event_id, body)

    @app.delete("/api/session/{session_id}/event/{event_id}")
    @_observed
    async def delete_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, request: Request
    ) -> JSONResponse:
        """Remove an event, answering with the record removed (the undo of it restores that)."""
        try:
            if mode == "review":
                deleted = await review_events.mutate(
                    _token(request), session_id, event_id, lambda _current: None
                )
            else:
                refused = _live_guard(session_id, request)
                if refused is not None:
                    return refused
                removed = await collector.delete_event(event_id=event_id)
                deleted = None if removed is None else EventRecord.model_validate(removed.to_dict())
        except refused_types as err:
            return _refused(err)
        if deleted is None:
            return JSONResponse({"error": "Event not found"}, status_code=404)
        return _record_response(deleted)

    @app.post("/api/session/{session_id}/event/{event_id}/revert")
    @_observed
    async def revert_event(  # type: ignore[reportUnusedFunction]
        session_id: str, event_id: int, body: EventRevertBody, request: Request
    ) -> JSONResponse:
        """Move an event from ``expect`` to ``write`` only if it is still ``expect`` (#929)."""
        # Both modes refuse in one order: the token, the session, then the
        # body (event_ops.check_revert_write). A review revert checks all
        # three under one acquisition of the mutation lock, with the leg's
        # compare against the store, the write and the document change
        # (ReviewEvents.revert).
        try:
            if mode == "review":
                record = await review_events.revert(
                    _token(request), session_id, event_id, body.expect, body.write
                )
            else:
                refused = _live_guard(session_id, request)
                if refused is not None:
                    return refused
                event_ops.check_revert_write(body.expect, body.write)
                event = await collector.revert_event(
                    event_id=event_id, expect=body.expect, write=body.write
                )
                record = EventRecord.model_validate(event.to_dict())
        except refused_types as err:
            return _refused(err)
        if event_ops.revert_leg(body.expect, body.write) == "delete":
            return JSONResponse(None)
        return _record_response(record)

    def _switchable() -> bool:
        """Report whether an upload may replace the served source: only a ``.json`` one may.

        Exact outside the lock: a switch serves a ``.json`` in place of a
        ``.json``, so a source's kind never changes under a request.
        """
        return mode == "review" and _review_source().kind == "json"

    @app.post("/api/source")
    async def upload_source(request: Request) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Switch to the uploaded ``.json`` (the raw body), as a tab's confirmation names it.

        A live or ``.db`` source is refused at once, the body unread. Then
        the :class:`~otto.monitor.review_source.UploadGate` is taken before
        the body is read (one upload body at a time), and the switch
        (:func:`~otto.monitor.review_source.switch_source`) runs under the
        mutation lock every write takes. The answer's state is stamped after
        the new source's frame, so it is the later of the two.
        """
        name = _upload_name(request)
        try:
            if not _switchable():
                raise NotSwitchable("only a .json review source can be switched to an upload")
            async with upload_gate:
                # Read through the module, so a test can wrap the reader.
                data = await review_source.read_upload(request)

                async def commit() -> MonitorSourceState:
                    new = switch_source(
                        source_slot,
                        token=_token(request),
                        unsaved=request.headers.get(UNSAVED_HEADER),
                        data=data,
                        name=name,
                        publish=collector.publish,
                    )
                    return new.state(observed_now())

                state = await event_ops.run_locked(collector.mutation_lock(), commit)
        except refused_types as err:
            return _refused(err)
        return JSONResponse(state.model_dump(mode="json"))

    @app.post("/api/source/saved")
    async def source_saved(body: SourceSavedBody) -> JSONResponse:  # type: ignore[reportUnusedFunction]
        """Acknowledge an export that holds every write up to ``body.revision``.

        Whether a source holds writes an export saves is its backend's
        ``counts_writes``: a ``.db`` source counts none, so its revision stays
        0 and the acknowledgement clamps to a no-op. A live server has no
        review source, and answers its unchanged state.
        """
        if mode == "live":
            return JSONResponse(_source_state().model_dump(mode="json"))

        async def commit() -> MonitorSourceState:
            source = source_slot.require(body.token)
            source.acknowledge(body.revision)
            return source.state(observed_now())

        try:
            state = await event_ops.run_locked(collector.mutation_lock(), commit)
        except refused_types as err:
            return _refused(err)
        return JSONResponse(state.model_dump(mode="json"))

    @app.get("/api/export/json")
    async def export_json() -> Response:  # type: ignore[reportUnusedFunction]
        """Download the current data as a ``format:1`` document.

        Review mode re-serves the served source's document; live mode builds
        a fresh single-session document from *frame*/*collector*/*lab*.
        """
        body = _review_source().body() if mode == "review" else _live_snapshot_body(observed_now())
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


class _LifecycleOwnedServer(uvicorn.Server):
    """uvicorn Server that leaves signal handling to otto's lifecycle.

    ``uvicorn.Server.serve`` wraps its whole run in ``capture_signals()``,
    which rebinds SIGINT/SIGTERM via raw ``signal.signal`` for the entire
    serve window — displacing the per-loop handlers ``run_command``
    installs and bypassing otto's two-stage interrupt policy (status line,
    teardown deadline, force hooks). otto owns interrupt policy; shutdown
    is driven by cancellation from the lifecycle (see ``MonitorServer.serve``),
    not by uvicorn-observed signals.
    """

    @override
    @contextlib.contextmanager
    def capture_signals(self) -> "Generator[None, None, None]":
        yield


class _StreamDrain:
    """One server's /api/stream admission: open until close(), then every admitted stream ends.

    sse_starlette learns about a shutdown only through uvicorn's signal
    handling (its patched ``Server.handle_exit``, or a ``Server`` found behind
    the SIGTERM handler), and :class:`_LifecycleOwnedServer` removes both on
    purpose. Left alone, an open stream parks in its queue and holds uvicorn's
    graceful drain until otto's teardown deadline forces past it (#784). So
    the server ends its own streams when the drain begins.

    Owned by the server, not the collector: a fresh server on the same
    collector starts with open admission.
    """

    def __init__(self, collector: MetricCollector) -> None:
        self._collector = collector
        self.closed = False
        self.queues: "set[asyncio.Queue[dict[str, Any]]]" = set()

    def close(self) -> None:
        """Refuse new streams and end every admitted one (server loop only; idempotent).

        No await, so no request is admitted between setting the flag and
        ending the queues. Each queue is detached from the broadcaster BEFORE
        it is handed :data:`~otto.monitor.broadcast.ENDED`, so a later publish
        can neither append behind the end marker nor replace it with a lapse.
        """
        if self.closed:
            return
        self.closed = True
        for q in self.queues:
            self._collector.unsubscribe(q=q)
            end_queue(q, ENDED)
        self.queues.clear()


async def _run_uvicorn_translated(server: uvicorn.Server, bind_host: str, port: int) -> None:
    """Run ``server.serve()`` with the startup ``SystemExit`` translated.

    uvicorn answers a failed startup (e.g. the requested port is already
    bound) with ``sys.exit(STARTUP_FAILURE)``. A SystemExit raised inside a
    Task is NOT stored on the task like a normal exception — ``Task.__step``
    re-raises it into the event loop itself, so it detonates out of the
    embedding application's ``asyncio.run()`` where no except around
    ``serve()`` (and no ``task.result()`` in the startup wait) can ever catch
    it. Translate it here, inside the coroutine, while it still unwinds
    normally.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        raise RuntimeError(
            f"monitor server failed to start on {bind_host}:{port} — is the port already in use?"
        ) from exc


async def _settle_lifespan(server: uvicorn.Server) -> None:
    """Send the ASGI ``lifespan.shutdown`` uvicorn's force path skips.

    ``uvicorn.Server.shutdown()`` ends with ``if not self.force_exit:
    await self.lifespan.shutdown()`` — so every ``force_stop()`` leaves
    uvicorn's ``LifespanOn.main()`` task suspended in ``receive()``, with
    Starlette's ``lifespan()`` still inside its ``async with`` body. It is a
    live coroutine nothing will ever resume, and whoever closes the loop must
    cancel it; Starlette catches that cancellation, formats it and answers
    ``lifespan.shutdown.failed`` with the traceback text, which uvicorn logs
    at ERROR. Exit code 0, a traceback on stderr — exactly what the no-stderr
    gates exist to catch (issue #383).

    otto owns this server's lifecycle (see :class:`_LifecycleOwnedServer`), so
    otto finishes the handshake uvicorn declined to: once the serve task is
    done, drive the lifespan to its terminal state so no coroutine is left
    suspended. Idempotent and safe on every path — a graceful shutdown has
    already set ``shutdown_event`` (and a lifespan that never started, or one
    whose app raised, has nothing to say), so this is a no-op there.

    Deliberately unbounded, like uvicorn's own call: ``force_stop`` promises
    not to wait for *connections*, and this app's lifespan has no shutdown
    work at all. A hypothetical wedge here is a real bug in otto's own ASGI
    app, and surfaces loudly as the caller's join/teardown deadline rather
    than being papered over.
    """
    lifespan = getattr(server, "lifespan", None)
    if lifespan is None:
        return  # serve() never reached its prologue
    if not lifespan.startup_event.is_set() or lifespan.shutdown_event.is_set():
        return
    await lifespan.shutdown()


async def _uvicorn_signalled_started(task: "asyncio.Task[None]", server: uvicorn.Server) -> bool:
    """Startup-wait predicate: readiness, or a loud re-raise if serve() died.

    ``started`` wins over task-death, matching the old poll's order: a serve
    task that completed AFTER flipping the flag (the auth suite's stubbed
    uvicorn does exactly that) is a started server, not a startup failure.
    """
    if server.started:
        return True
    if task.done():
        # The serve task died before signalling startup (e.g. a bad TLS
        # cert/key raises ssl.SSLError out of Config.load()) — left
        # unchecked, the wait would poll `server.started` forever since
        # nothing will ever flip it. task.result() re-raises whatever killed
        # it, surfacing the real cause instead of a silent hang. A CLEAN
        # return before startup (task.result() is None) must raise too, or
        # this predicate would poll a done task forever — the guard the old
        # test-side helper carried, now owed to every waiter.
        task.result()
        raise RuntimeError("monitor serve task returned before signalling startup")
    return False


@final
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
      origin (a file path) reported by ``/api/mode``. ``archive_path``, when
      given, is a ``.db`` archive event mutations persist to; omitted (the
      ``.json`` case), the served document is itself the store, and its
      edits are held until an export saves them; ``POST /api/source``
      can switch it to an uploaded ``.json``. The server serves its own
      copy of ``document``
      (``otto.monitor.review_source.ReviewSource``), held in
      :attr:`source_slot`; the caller's document is never changed.

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
        self._frame = frame
        self._lab = lab
        self._tls_cert = tls_cert
        self._tls_key = tls_key
        self._key = secrets.token_urlsafe(16)
        review = None
        if mode == "review" and document is not None:
            if source_name is None:
                raise ValueError("a review-mode MonitorServer needs the source_name it serves")
            review = ReviewSource(
                document,
                name=source_name,
                archive_path=archive_path,
                publish=collector.publish,
            )
        self.source_slot = SourceSlot(review)
        """The review source this server serves (``current`` is ``None`` for a live server)."""
        # The collector has no notion of sessions; the server holds the frame, so
        # it stamps the id here — one place, so no construction site can forget.
        if frame is not None:
            collector.session_id = frame.id
        self._drain = _StreamDrain(collector)
        self._app = _build_app(
            collector,
            key=self._key,
            secure_cookie=tls_cert is not None,
            mode=mode,
            slot=self.source_slot,
            frame=frame,
            lab=lab,
            drain=self._drain,
        )
        self._server: uvicorn.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._serve_entered = False

    @property
    def unsaved_changes(self) -> int:
        """The writes the served source's last export does not hold (0 for live and ``.db``)."""
        current = self.source_slot.current
        return current.unsaved if current is not None else 0

    @property
    def source_name(self) -> str | None:
        """The human-facing name of the served review source (``None`` for a live server)."""
        current = self.source_slot.current
        return current.name if current is not None else None

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

    async def wait_started(self) -> None:
        """Block until the server accepts connections, or re-raise its startup failure.

        The one sanctioned way to await readiness (gate G7): a raw
        ``while not server.started`` poll converts a startup failure into an
        infinite hang, because nothing ever flips the flag. ``serve()``
        records whatever killed its startup and releases waiters either way;
        on failure this re-raises that exact exception (``ssl.SSLError`` for
        a bad PEM, the translated ``RuntimeError`` for an already-bound
        port). Call with ``serve()`` already scheduled on the running loop —
        a ``MonitorServer`` serves once per lifetime; construct a fresh one
        to retry.
        """
        await self._ready.wait()
        if self._startup_error is None:
            return
        error: BaseException = self._startup_error
        if isinstance(error, asyncio.CancelledError):
            # Never re-raise CancelledError into a task that was not itself
            # cancelled: it poisons the waiter's cancellation state (and, on
            # 3.11+, timeout/TaskGroup uncancel accounting), and otto reads
            # CancelledError as the lifecycle teardown signal. The waiter
            # gets a plain loud error; serve()'s own unwind kept the
            # original, chained here as the cause.
            error = RuntimeError("monitor server startup was cancelled")
            error.__cause__ = self._startup_error
        raise error

    # TODO: Catch keyboard interrupt or *something* to make monitoring end gracefully
    async def serve(self) -> None:
        """Run the web server until stop() is called or the process exits.

        One serve per ``MonitorServer`` lifetime — the readiness latch and
        the recorded startup outcome are single-shot; construct a fresh
        instance to retry.
        """
        # A plain bool set synchronously at entry, NOT `_ready.is_set()`:
        # the event only trips when the first prologue FINISHES, so it would
        # wave a second serve() through while the first is still starting up
        # (two uvicorn tasks, clobbered _server/_startup_error).
        if self._serve_entered:
            raise RuntimeError(
                "MonitorServer serves once per lifetime — construct a fresh instance"
            )
        self._serve_entered = True
        server: _LifecycleOwnedServer | None = None
        task: "asyncio.Task[None] | None" = None
        try:
            try:
                # EVERYTHING up to `self._server = server` sits inside this
                # guard — config construction and task spawn included. Any
                # exit of the startup phase without `_ready` set (including
                # a cancellation delivered at the first await point) would
                # strand every wait_started() waiter forever. The one case
                # no in-function guard can cover: a serve task cancelled
                # before its FIRST step runs no body code at all (Python
                # throws into the created-but-never-started coroutine), so
                # callers must not selectively cancel a never-stepped serve
                # task while keeping its waiter alive. Safe in-tree: every
                # teardown path cancels whole lifecycles, so the waiter
                # (e.g. MonitorHandle.start's own task) is cancelled too.
                config = uvicorn.Config(
                    self._app,
                    host=self._bind_host,
                    port=self._port,
                    log_config=None,
                    # otto logs requests itself (one INFO per admitted IP, DEBUG per
                    # request) from the access-key middleware.
                    access_log=False,
                    ssl_certfile=str(self._tls_cert) if self._tls_cert else None,
                    ssl_keyfile=str(self._tls_key) if self._tls_key else None,
                )
                server = _LifecycleOwnedServer(config)

                # start the server in a background task
                self._loop = asyncio.get_running_loop()
                task = asyncio.create_task(
                    _run_uvicorn_translated(server, self._bind_host, self._port)
                )

                # Unbounded by design (float("inf") is the supported
                # spelling): startup latency is uvicorn's to spend, and the
                # task-death guard in the predicate is the loud exit for
                # every failure path.
                await wait_for_async(
                    functools.partial(_uvicorn_signalled_started, task, server),
                    float("inf"),
                    interval=0.05,
                    on_timeout="unreachable — the wait carries no deadline",
                )

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
                    print_literal(f"Server running at {all_urls[0]}", console=CONSOLE)
                else:
                    CONSOLE.print("Server running at:", highlight=False)
                    for u in all_urls:
                        print_literal(f"  {u}", console=CONSOLE)
                CONSOLE.print("Press Ctrl+C to stop", highlight=False)
                logger.info(
                    f"Monitor dashboard started on {self.origin} (access key omitted from logs)"
                )

                self._server = server
            except BaseException as exc:
                # Record the startup failure BEFORE releasing wait_started()
                # waiters: an external awaiter must observe the failure and
                # re-raise it, never hang on a flag nothing will flip.
                self._startup_error = exc
                raise
            finally:
                self._ready.set()

            # Shielded: a Task.cancel() on the *awaiting* coroutine delegates
            # straight to whatever Future it is suspended on — here, that
            # would be `task` itself, cascading into uvicorn's own main_loop
            # sleep and skipping its shutdown() entirely (uvicorn's _serve()
            # has no try/finally around main_loop(), so an uncaught
            # CancelledError there never reaches its socket-closing
            # shutdown()). Shielding lets the cancellation reach us here
            # (below, in except) without also cancelling `task`, so uvicorn
            # keeps running long enough to notice `should_exit` and drain
            # itself gracefully.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # First-signal teardown: ask uvicorn to drain gracefully (ending
            # the open streams, which would otherwise hold it) and wait for
            # it. This second await is part of the command body's
            # unwind, so the lifecycle teardown deadline still bounds it and
            # a second signal (or expiry) forces past it — do NOT shield.
            # Sentinel checks: a cancel delivered before the prologue built
            # the server or spawned the task has nothing to tear down.
            if server is not None:
                self._begin_drain(server)
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise
        finally:
            # EVERY exit, including the ones that raise. `lifespan.shutdown()`
            # is the LAST statement of uvicorn's own `shutdown()`, so any
            # serve task that dies after `lifespan.startup()` completed —
            # not the bind failure, which uvicorn does settle itself before
            # its `sys.exit`, but e.g. that `shutdown()` raising part-way —
            # leaves Starlette's `lifespan()` parked in `receive()` exactly as
            # the force path does, and so leaves the same ERROR traceback for
            # whoever cancels it at loop close. Only once the serve task is
            # finished, though: while it still runs uvicorn owns the lifespan
            # and will drive its own shutdown, and sending `lifespan.shutdown`
            # under a live server would tear the app down beneath it.
            if server is not None and (task is None or task.done()):
                await _settle_lifespan(server)

    def _begin_drain(self, server: uvicorn.Server) -> None:
        """Start uvicorn's graceful drain and close this server's streams (thread-safe).

        The one begin-drain step, shared by :meth:`stop` and the cancellation
        path of :meth:`serve`. The stream close is scheduled on the server's
        loop because it touches asyncio queues.
        """
        server.should_exit = True
        self._call_on_loop(self._drain.close)

    def _call_on_loop(self, callback: Callable[[], None]) -> None:
        """Schedule *callback* on the server's loop from any thread; a no-op once it has closed.

        The one way :meth:`stop` and :meth:`force_stop` reach the server's
        loop. A server whose loop has closed has nothing left to stop, so a
        late call stays harmless; only that closed-loop ``RuntimeError`` is
        tolerated (the loop may close on its own thread between the check and
        the call), and any other scheduling failure raises.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            if not loop.is_closed():
                raise

    def stop(self) -> None:
        """Signal the server to shut down and end its open streams (thread-safe)."""
        if self._server:
            self._begin_drain(self._server)

    def force_stop(self) -> None:
        """Shut down without waiting for open connections to drain (thread-safe).

        :meth:`stop` ends the open /api/stream responses and lets uvicorn
        finish every other request; this does not wait for any connection at
        all. It sets uvicorn's ``force_exit`` (skip the
        drain) and aborts open connection transports on the server's own loop
        (h11 never closes a mid-stream transport, so clients would otherwise
        not see the connection die). Used by test harnesses and Ctrl+C paths;
        prefer ``stop()`` when clients should finish cleanly.

        Accepting stops *first*, the listening sockets are closed one loop turn
        later, and only then are open transports aborted. The turn in between
        lets a connection asyncio has already accepted finish building its
        transport against a still-open listener; closing immediately would
        leave it half-built, unregistered and holding its fd (issues #319 and
        #320). A single abort pass is not enough either: uvicorn registers a
        connection in its protocol's
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

            listeners = list(getattr(server, "servers", []))

            def _close_listeners_and_abort() -> None:
                for listener in listeners:
                    listener.close()
                _abort_open_transports(_FORCE_STOP_ABORT_PASSES)

            def _stop_accepting() -> None:
                # Stop accepting *without* closing yet. asyncio accepts in a
                # selector callback but builds the transport in a task that
                # steps on the next turn, and that constructor asserts the
                # listener is still open (Server._attach). Closing here would
                # leave every accept already in flight half-built: no
                # connection_made, so never registered, so never aborted, and
                # holding its fd until GC (issues #319/#320). Removing the
                # readers cancels any accept callback still queued this turn,
                # so by the next turn the set of build tasks is fixed and they
                # all run ahead of the close scheduled here.
                for listener in listeners:
                    for sock in listener.sockets:
                        loop.remove_reader(sock.fileno())
                loop.call_soon(_close_listeners_and_abort)

            self._call_on_loop(_stop_accepting)
        self.stop()
