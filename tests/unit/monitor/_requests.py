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
from typing import Any

from otto.monitor.server import SOURCE_TOKEN_HEADER
from tests._fixtures._source_token import FROM_MODE, MODE_PATH, FromMode, resolve_token


async def asgi(
    app: Any,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    token: "str | FromMode | None" = FROM_MODE,
    content: "bytes | list[bytes] | None" = None,
    headers: "dict[str, str] | None" = None,
) -> tuple[int | None, Any]:
    """One in-loop request against *app*: ``(status, json-or-None)``.

    A write (any method but GET) carries ``SOURCE_TOKEN_HEADER``: by default
    the token ``/api/mode`` reports for the served source, as a tab sends it;
    ``token=None`` omits the header and a string sends that token.

    ``content`` sends a raw body in place of *body*: ``bytes`` with its
    ``Content-Length``, or a list of chunks with none (a chunked upload).
    ``headers`` adds further request headers.
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
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": pending.pop(0), "more_body": bool(pending)}

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
