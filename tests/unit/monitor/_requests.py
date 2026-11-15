"""In-loop requests against a monitor app.

A bare top-level module, like ``_archive``: ``tests/unit/monitor`` has no
``__init__.py``, so its siblings import it as ``from _requests import asgi``.

``asgi`` makes a request inside the test's own loop (no server, no socket),
so a test can hold the mutation lock or a write and race a second request
into the window. The token a write sends follows the one rule in
``tests._fixtures._source_token``.
"""

import asyncio
import json
from typing import Any, Literal

from otto.monitor.server import SOURCE_TOKEN_HEADER
from tests._fixtures._source_token import FROM_MODE, MODE_PATH, FromMode, resolve_token

BodyEnding = Literal["complete", "disconnect", "hang"]
"""How a request body ends (``asgi``'s ``ending``)."""


async def asgi(
    app: Any,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    token: "str | FromMode | None" = FROM_MODE,
    content: "bytes | list[bytes] | None" = None,
    headers: "dict[str, str] | None" = None,
    ending: BodyEnding = "complete",
) -> tuple[int | None, Any]:
    """One in-loop request against *app*: ``(status, json-or-None)``.

    A write (any method but GET) carries ``SOURCE_TOKEN_HEADER``: by default
    the token ``/api/mode`` reports for the served source, as a tab sends it;
    ``token=None`` omits the header and a string sends that token.

    ``content`` sends a raw body in place of *body*: ``bytes`` with its
    ``Content-Length``, or a list of chunks with none (a chunked upload).
    ``headers`` adds further request headers.

    ``ending`` is how the body ends: ``complete`` (the last chunk ends it),
    ``disconnect`` (the client goes away after the chunks, mid-body) or
    ``hang`` (no more of the body ever arrives, so a reader waits until the
    request is cancelled).
    """
    if body is not None:
        content = json.dumps(body).encode()
    sent: list[tuple[bytes, bytes]] = []
    if content is not None:
        sent.append((b"content-type", b"application/json"))
    if isinstance(content, bytes):
        sent.append((b"content-length", str(len(content)).encode()))
    for name, value in (headers or {}).items():
        sent.append((name.lower().encode(), value.encode()))

    async def get_mode() -> object:
        _, mode = await asgi(app, "GET", MODE_PATH)
        return mode

    token = await resolve_token(token, method, get_mode)
    if token is not None:
        sent.append((SOURCE_TOKEN_HEADER.lower().encode(), token.encode()))
    pending = [content] if isinstance(content, bytes) else list(content or [b""])
    status: int | None = None
    chunks: list[bytes] = []

    async def receive() -> dict[str, object]:
        if pending:
            more = bool(pending[1:]) or ending != "complete"
            return {"type": "http.request", "body": pending.pop(0), "more_body": more}
        if ending == "disconnect":
            return {"type": "http.disconnect"}
        if ending == "hang":
            await asyncio.Event().wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "query_string": b"key=k",
        "headers": sent,
        "root_path": "",
        "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
    raw = b"".join(chunks)
    return status, (json.loads(raw) if raw else None)


async def settle() -> None:
    """Let every runnable task advance as far as it can (no I/O in play)."""
    for _ in range(50):
        await asyncio.sleep(0)
