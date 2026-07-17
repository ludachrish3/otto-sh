# Monitor Access Key + Optional TLS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate every monitor-dashboard route behind a per-run random access key (Jupyter-style token→cookie handoff) and add optional TLS configured via a new `[monitor]` table in `.otto/settings.toml`.

**Architecture:** `MonitorServer` generates a `secrets.token_urlsafe(16)` key at construction and prints it in its URLs (`?key=…`); one pure-ASGI middleware inside `_build_app` validates key-or-cookie on every request and mints the cookie on any correctly-keyed request. TLS is a `MonitorSettingsSpec` on `SettingsModel` plumbed through `Repo` (mirroring `docker_settings`) into `uvicorn.Config`'s ssl args. The frontend (`web/`) is untouched.

**Tech Stack:** FastAPI/Starlette (pure-ASGI middleware — NOT `BaseHTTPMiddleware`, which has known pitfalls with SSE streaming), uvicorn ssl args, pydantic settings specs, openssl CLI for test certs.

**Spec:** `docs/superpowers/specs/2026-07-16-monitor-access-key-tls-design.md` — read it first.

## Global Constraints

- NEVER add `from __future__ import annotations` (breaks Sphinx nitpicky `-W`).
- `OttoModel` base already sets `extra='forbid'` — new specs inherit it; do not override.
- `ty` runs only at `nox -s typecheck` — run it after every task that edits `src/`.
- `nox -s lint` = `ruff check` AND `ruff format --check` — run `ruff format` on touched files before committing.
- No httpx/TestClient in this venv — HTTP-level unit tests use the raw-ASGI pattern (see `tests/unit/monitor/test_server.py::TestDeleteEndpoint`) or urllib against a live server.
- A bare `pytest tests/e2e/monitor/dashboard` runs CHROMIUM ONLY — the browser gate is `nox -s dashboard` (chromium+firefox+webkit).
- Work happens on a worktree branch (create via superpowers:using-git-worktrees). Self-commit is OK there: conventional-commit prefix + `Assisted-by: Claude <noreply@anthropic.com>` trailer.
- Final gate before merge: `make coverage`, then verify failures (if any) via `scripts/junit_failures.py reports/junit/...` — `make coverage | tail` eats make's exit code.
- The key must NEVER appear in per-request log output; it appears only in the printed URLs.

---

### Task 1: `[monitor]` settings table (spec + runtime + Repo plumb)

**Files:**
- Modify: `src/otto/models/settings.py` (add `MonitorSettingsSpec`; add `monitor` field to `SettingsModel` next to `docker`)
- Modify: `src/otto/config/repo.py` (add `MonitorSettings` frozen dataclass next to `DockerSettings`; add `Repo.monitor_settings` field; populate in `parse_settings`)
- Modify: `src/otto/config/__init__.py` (re-export `MonitorSettings` alongside `DockerSettings`)
- Test: `tests/unit/models/test_settings.py` (add a `TestMonitorSettings` class)

**Interfaces:**
- Consumes: existing `OttoModel`, `SettingsModel`, `Repo.parse_settings` patterns.
- Produces: `MonitorSettings` frozen dataclass with `tls_cert: Path | None = None`, `tls_key: Path | None = None` (importable from `otto.config`); `Repo.monitor_settings: MonitorSettings`; `MonitorSettingsSpec.to_runtime() -> MonitorSettings`. Task 5 consumes `repo.monitor_settings`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/models/test_settings.py`:

```python
class TestMonitorSettings:
    """The [monitor] table: TLS cert/key paths (spec section 'settings.toml surface')."""

    def test_defaults_to_no_tls(self):
        model = SettingsModel.model_validate({"name": "r", "version": "1.0.0"})
        runtime = model.monitor.to_runtime()
        assert runtime.tls_cert is None
        assert runtime.tls_key is None

    def test_paths_are_expanduser_expanded(self):
        model = SettingsModel.model_validate(
            {
                "name": "r",
                "version": "1.0.0",
                "monitor": {
                    "tls_cert": "~/.config/otto/tls/monitor-cert.pem",
                    "tls_key": "~/.config/otto/tls/monitor-key.pem",
                },
            }
        )
        runtime = model.monitor.to_runtime()
        assert runtime.tls_cert == Path.home() / ".config/otto/tls/monitor-cert.pem"
        assert runtime.tls_key == Path.home() / ".config/otto/tls/monitor-key.pem"

    def test_cert_without_key_is_allowed(self):
        """A single PEM may bundle cert+key — tls_key stays optional."""
        model = SettingsModel.model_validate(
            {"name": "r", "version": "1.0.0", "monitor": {"tls_cert": "/x/cert.pem"}}
        )
        assert model.monitor.to_runtime().tls_key is None

    def test_key_without_cert_is_rejected(self):
        with pytest.raises(ValidationError, match="tls_key"):
            SettingsModel.model_validate(
                {"name": "r", "version": "1.0.0", "monitor": {"tls_key": "/x/key.pem"}}
            )

    def test_unknown_monitor_key_is_rejected(self):
        """extra='forbid' inherited from OttoModel must cover the new table."""
        with pytest.raises(ValidationError, match="tls_cret"):
            SettingsModel.model_validate(
                {"name": "r", "version": "1.0.0", "monitor": {"tls_cret": "/typo.pem"}}
            )
```

(Match the file's existing imports — it already imports `pytest`, `ValidationError`, `SettingsModel`; add `from pathlib import Path` if absent.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/models/test_settings.py::TestMonitorSettings -v`
Expected: FAIL — `SettingsModel` has no field `monitor` (extra='forbid' ValidationError on the populated cases, AttributeError on the default case).

- [ ] **Step 3: Implement.** In `src/otto/config/repo.py`, next to `DockerSettings`:

```python
@dataclass(frozen=True)
class MonitorSettings:
    """Per-repo monitor configuration parsed from `[monitor]` in `settings.toml`.

    ``tls_cert``/``tls_key`` point at a PEM certificate/key on the machine that
    runs ``otto monitor`` (conventionally under ``~/.config/otto/tls/`` — the
    committed settings value is shared team-wide, so it must not name a
    machine-local absolute path). ``tls_key`` may stay ``None`` when the cert
    PEM bundles the private key.
    """

    tls_cert: Path | None = None
    tls_key: Path | None = None
```

Add to `Repo` (next to `docker_settings`):

```python
    monitor_settings: MonitorSettings = field(default_factory=MonitorSettings)
    """Parsed `[monitor]` table — optional TLS cert/key for the dashboard server."""
```

In `Repo.parse_settings`, after the `docker_settings` line:

```python
        self.monitor_settings = model.monitor.to_runtime()
```

In `src/otto/models/settings.py` (place next to `DockerSettingsSpec`; lazy runtime import like its siblings; add `MonitorSettings` to the `TYPE_CHECKING` import from `..config.repo`):

```python
class MonitorSettingsSpec(OttoModel):
    """Boundary spec for the ``[monitor]`` section of ``settings.toml``.

    TLS for the dashboard server. Paths are ``expanduser()``-expanded here
    (settings expansion only handles ``${sut_dir}``): the committed value is
    shared by the whole team, so it conventionally points under
    ``~/.config/otto/tls/`` — identical text, per-user resolution. ``tls_key``
    without ``tls_cert`` is rejected; ``tls_cert`` alone is fine (bundled PEM).
    """

    tls_cert: Path | None = None
    tls_key: Path | None = None

    @field_validator("tls_cert", "tls_key")
    @classmethod
    def _expand_user(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else v

    @model_validator(mode="after")
    def _key_requires_cert(self) -> "MonitorSettingsSpec":
        if self.tls_key is not None and self.tls_cert is None:
            raise ValueError(
                "[monitor] tls_key is set but tls_cert is not — set tls_cert "
                "(it may be a combined PEM, making tls_key unnecessary)."
            )
        return self

    def to_runtime(self) -> "MonitorSettings":
        """Build the ``MonitorSettings`` runtime dataclass from the validated spec fields."""
        from ..config.repo import MonitorSettings

        return MonitorSettings(tls_cert=self.tls_cert, tls_key=self.tls_key)
```

Add to `SettingsModel` (next to `docker`):

```python
    monitor: MonitorSettingsSpec = MonitorSettingsSpec()
```

In `src/otto/config/__init__.py`, add a re-export next to `DockerSettings`:

```python
from .repo import (
    MonitorSettings as MonitorSettings,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/models/test_settings.py -v`
Expected: all PASS (new class and pre-existing tests).

- [ ] **Step 5: Typecheck + lint + commit**

```bash
nox -s typecheck && ruff format src/otto/models/settings.py src/otto/config/repo.py && nox -s lint
git add -A && git commit -m "feat(monitor): [monitor] settings table with tls_cert/tls_key

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 2: Access-key middleware + keyed URLs in MonitorServer

**Files:**
- Modify: `src/otto/monitor/server.py`
- Test (create): `tests/unit/monitor/test_server_auth.py`
- Modify: `tests/unit/monitor/test_server.py` (the direct `_build_app(collector)` caller in `TestDeleteEndpoint` and any raw-URL fetches — see Step 6)

**Interfaces:**
- Consumes: existing `_build_app`, `MonitorServer`.
- Produces (Tasks 3–6 rely on these exact names):
  - `MonitorServer.key -> str` (read-only property, the per-run token)
  - `MonitorServer.origin -> str` (`http://<host>:<port>`, no key — Task 4 flips the scheme to https under TLS)
  - `MonitorServer.url -> str` (now `<origin>/?key=<key>`), `MonitorServer.urls -> list[str]` (each keyed)
  - `_build_app(collector, *, key: str, secure_cookie: bool = False, ...)` — `key` is REQUIRED
  - Cookie name helper `_cookie_name(port: int | None) -> str` returning `otto_monitor_<port>`

- [ ] **Step 1: Write the failing tests** — create `tests/unit/monitor/test_server_auth.py`:

```python
"""Access-key gate for the monitor dashboard (spec 2026-07-16).

Everything is exercised at the raw-ASGI level (no httpx in this venv), the
same pattern TestDeleteEndpoint in test_server.py uses. The middleware is
pure ASGI (not BaseHTTPMiddleware) so the SSE stream is gated identically.
"""

import asyncio
import json

import pytest

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
        status, headers, body = await _asgi_get(app, "/api/mode")
        assert status == 403
        assert json.loads(body) == {"error": "missing or invalid access key"}

    @pytest.mark.asyncio
    async def test_wrong_key_is_403(self):
        app = _build_app(_collector(), key=TEST_KEY)
        status, _, _ = await _asgi_get(app, "/api/mode", query=b"key=wrong")
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/monitor/test_server_auth.py -v`
Expected: FAIL — `_build_app() got an unexpected keyword argument 'key'`, `MonitorServer has no attribute 'key'`.

- [ ] **Step 3: Implement in `src/otto/monitor/server.py`.**

Add `import secrets` to the imports and a module-level middleware + helpers (place above `_build_app`):

```python
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

    def __init__(self, app, *, key: str, secure_cookie: bool) -> None:
        self._app = app
        self._key = key
        self._secure = secure_cookie

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        cookie_name = _cookie_name(request.url.port)
        supplied = request.query_params.get("key")
        if supplied is not None and secrets.compare_digest(supplied, self._key):
            cookie = f"{cookie_name}={self._key}; Path=/; HttpOnly; SameSite=Lax"
            if self._secure:
                cookie += "; Secure"

            async def send_with_cookie(message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    headers.append("set-cookie", cookie)
                await send(message)

            await self._app(scope, receive, send_with_cookie)
            return
        from_cookie = request.cookies.get(cookie_name)
        if from_cookie is not None and secrets.compare_digest(from_cookie, self._key):
            await self._app(scope, receive, send)
            return
        if request.url.path.startswith("/api/"):
            response: Response = JSONResponse(
                {"error": "missing or invalid access key"}, status_code=403
            )
        else:
            response = HTMLResponse(_FORBIDDEN_HTML, status_code=403)
        await response(scope, receive, send)
```

Imports needed: `from starlette.datastructures import MutableHeaders` (add next to the existing `from starlette.requests import Request`).

Extend `_build_app`'s signature (key REQUIRED, keyword-only like the rest):

```python
def _build_app(
    collector: MetricCollector,
    *,
    key: str,
    secure_cookie: bool = False,
    mode: Literal["live", "review"] = "live",
    ...
```

and register the middleware right after `app = FastAPI(...)` (add_middleware wraps outermost, so it also covers the `/static` mount):

```python
    app.add_middleware(_AccessKeyMiddleware, key=key, secure_cookie=secure_cookie)
```

In `MonitorServer.__init__`, before the `_build_app` call:

```python
        self._key = secrets.token_urlsafe(16)
```

and pass `key=self._key` to `_build_app`. Add the properties and rework `url`/`urls`:

```python
    @property
    def key(self) -> str:
        """The per-run access key (printed once in the URLs, required by every request)."""
        return self._key

    @property
    def origin(self) -> str:
        """Scheme+host+port with NO key — for callers composing their own paths."""
        host = self._bind_host
        if host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            host = ips[0] if ips else self._bind_host
        return f"http://{host}:{self._port}"

    @property
    def url(self) -> str:
        """Primary self-authenticating URL (first non-loopback IP, ``?key=`` appended)."""
        return f"{self.origin}/?key={self._key}"

    @property
    def urls(self) -> list[str]:
        """All reachable URLs (one per non-loopback interface), each ``?key=``-keyed."""
        if self._bind_host in ("0.0.0.0", "::"):  # noqa: S104 — intentional all-interface bind
            ips = _get_all_ips()
            if ips:
                return [f"http://{ip}:{self._port}/?key={self._key}" for ip in ips]
        return [f"http://{self._bind_host}:{self._port}/?key={self._key}"]
```

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/pytest tests/unit/monitor/test_server_auth.py -v`
Expected: all PASS.

- [ ] **Step 5: Fix the callers this breaks.** Run the whole monitor unit package:

Run: `.venv/bin/pytest tests/unit/monitor -v 2>&1 | tail -30`

Expected failures and their fixes:
- `test_server.py::TestDeleteEndpoint` calls `_build_app(collector)` → pass `key="k"` and add `"query_string": b"key=k"` to its hand-built scope (or a cookie header).
- Any test fetching `http://127.0.0.1:{port}/...` via urllib against a served `MonitorServer` → append `?key={server.key}` (grep the package: `grep -rn "urllib\|http://127" tests/unit/monitor/`).
- `test_collector_db.py` lines ~382–400 assert on `server.url` display-host resolution → assertions must expect the `/?key=` suffix; use `server.origin` where the test only cares about host:port.
- `test_stream_fragments.py` constructs a `MonitorServer` — construction is unaffected (key is internal); only fix if it fetches over HTTP.

Do NOT weaken any existing assertion — extend expected strings with the key suffix.

- [ ] **Step 6: Run the full monitor unit package to green**

Run: `.venv/bin/pytest tests/unit/monitor -q`
Expected: all PASS.

- [ ] **Step 7: Typecheck + lint + commit**

```bash
nox -s typecheck && ruff format src/otto/monitor/server.py tests/unit/monitor/ && nox -s lint
git add -A && git commit -m "feat(monitor): gate every dashboard route behind a per-run access key

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 3: Sweep remaining direct-HTTP callers (e2e harness + fixtures)

**Files:**
- Modify: `tests/_fixtures/_dashboard_harness.py` (add `api_url` helper)
- Modify: `tests/e2e/monitor/dashboard/test_harness.py` (its `_get_json(... .url + "/api/...")` call sites)
- Possibly modify: other files found by the grep in Step 1.

**Interfaces:**
- Consumes: `MonitorServer.origin` / `.key` from Task 2.
- Produces: `DashboardHarness.api_url(path: str) -> str` returning `f"{server.origin}{path}?key={server.key}"`. Task 6's e2e test uses `DashboardHarness.url` (already keyed, unchanged shape).

- [ ] **Step 1: Find every direct composer of monitor URLs**

Run: `grep -rn "\.url + \|\.url}\|urlopen\|urlsplit" tests/e2e/monitor tests/_fixtures/_dashboard_harness.py`

Known sites: `test_harness.py:245,261,271,280,294` (`_get_json(<dash>.url + "/api/...")`). `urlsplit(<dash>.url).port` sites need no change (query doesn't affect `.port`). Playwright `page.goto(<dash>.url)` sites need no change (the keyed URL is exactly what a user opens; the browser then holds the cookie).

- [ ] **Step 2: Add the helper to `DashboardHarness`**

```python
    def api_url(self, path: str) -> str:
        """Absolute, self-authenticating URL for a direct API hit (no browser cookie).

        ``url`` is the user-facing page URL (``/?key=…``); appending a path to
        it would corrupt the query string, so API-poking tests compose from
        ``origin`` + path and re-attach the key explicitly.
        """
        return f"{self.server.origin}{path}?key={self.server.key}"
```

- [ ] **Step 3: Update the `test_harness.py` call sites**, e.g.:

```python
    payload = _get_json(live_dash.api_url("/api/mode"))
```

(same shape for `/api/monitor_sessions` and `/api/export/json` sites, and any POST helper found by the grep).

- [ ] **Step 4: Run the hostless e2e lane**

Run: `.venv/bin/pytest tests/e2e/monitor/dashboard/test_harness.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
ruff format tests/ && nox -s lint
git add -A && git commit -m "test(monitor): route direct API hits through keyed api_url helper

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 4: TLS support in MonitorServer

**Files:**
- Modify: `src/otto/monitor/server.py` (`MonitorServer.__init__` gains `tls_cert`/`tls_key`; `serve()` passes ssl args; `origin` scheme; `secure_cookie` passthrough)
- Test: `tests/unit/monitor/test_server_tls.py` (create)

**Interfaces:**
- Consumes: Task 2's `origin`/`url`/`key`, `_build_app(..., secure_cookie=...)`.
- Produces: `MonitorServer(..., tls_cert: Path | None = None, tls_key: Path | None = None)`. Task 5 passes `MonitorSettings.tls_cert/.tls_key` here. No file-existence checking here — that is the CLI's job (Task 5); uvicorn failing on a bad path is acceptable at this layer.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/monitor/test_server_tls.py`:

```python
"""TLS-mode MonitorServer: https URLs, Secure cookie, real TLS handshake.

Certificates are throwaway self-signed PEMs minted into tmp_path with the
openssl CLI (universally present on the Linux targets this repo supports;
no trustme/cryptography dev-dep needed).
"""

import asyncio
import ssl
import subprocess
import urllib.request
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer


def _make_cert(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-sha256", "-days", "2",
            "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


class TestTlsServer:
    def test_origin_and_url_use_https(self, tmp_path):
        cert, key = _make_cert(tmp_path)
        server = MonitorServer(
            _collector(), host="127.0.0.1", port=7777, tls_cert=cert, tls_key=key
        )
        assert server.origin == "https://127.0.0.1:7777"
        assert server.url.startswith("https://")

    def test_plain_server_stays_http(self):
        server = MonitorServer(_collector(), host="127.0.0.1", port=7777)
        assert server.origin.startswith("http://")

    @pytest.mark.asyncio
    async def test_serves_real_tls_with_secure_cookie(self, tmp_path):
        cert, key = _make_cert(tmp_path)
        server = MonitorServer(
            _collector(), host="127.0.0.1", port=0, tls_cert=cert, tls_key=key
        )
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
        try:
            ctx = ssl.create_default_context(cafile=str(cert))

            def _fetch():
                req = urllib.request.urlopen(  # noqa: S310 — https URL under test
                    f"{server.origin}/api/mode?key={server.key}", context=ctx, timeout=10
                )
                return req.status, req.headers.get("set-cookie", "")

            status, set_cookie = await asyncio.to_thread(_fetch)
            assert status == 200
            assert "Secure" in set_cookie
        finally:
            server.force_stop()
            await asyncio.gather(task, return_exceptions=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/monitor/test_server_tls.py -v`
Expected: FAIL — `MonitorServer() got an unexpected keyword argument 'tls_cert'`.

- [ ] **Step 3: Implement.** In `MonitorServer.__init__`, add keyword params and stash them (before the `_build_app` call):

```python
        tls_cert: Path | None = None,
        tls_key: Path | None = None,
```

```python
        self._tls_cert = tls_cert
        self._tls_key = tls_key
```

Pass `secure_cookie=tls_cert is not None` to `_build_app`. In `origin`, derive the scheme:

```python
        scheme = "https" if self._tls_cert is not None else "http"
```

(and use it in `origin`; `url` composes from `origin` already; `urls` needs the same scheme variable). In `serve()`, extend the config:

```python
        config = uvicorn.Config(
            self._app,
            host=self._bind_host,
            port=self._port,
            log_config=None,
            ssl_certfile=str(self._tls_cert) if self._tls_cert else None,
            ssl_keyfile=str(self._tls_key) if self._tls_key else None,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/monitor/test_server_tls.py tests/unit/monitor/test_server_auth.py -v`
Expected: all PASS.

- [ ] **Step 5: Typecheck + lint + commit**

```bash
nox -s typecheck && ruff format src/otto/monitor/server.py tests/unit/monitor/ && nox -s lint
git add -A && git commit -m "feat(monitor): optional TLS (uvicorn ssl args, https URLs, Secure cookie)

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 5: CLI resolution + wiring (live + review)

**Files:**
- Modify: `src/otto/cli/monitor.py` (`_resolve_monitor_tls()` helper; pass TLS into both `MonitorServer` construction sites)
- Test: `tests/unit/cli/test_monitor.py` (add a `TestResolveMonitorTls` class)

**Interfaces:**
- Consumes: `otto.config.get_repos()`, `Repo.monitor_settings` (Task 1), `MonitorServer(tls_cert=, tls_key=)` (Task 4).
- Produces: `_resolve_monitor_tls() -> MonitorSettings | None` — `None` means "serve plain HTTP". Exits 1 (typer.Exit) on multi-repo disagreement or a missing cert/key file. Both the live branch and `_serve_review` call it.

- [ ] **Step 1: Write the failing tests** — add to `tests/unit/cli/test_monitor.py` (match the file's existing import style; `MonitorSettings` comes from `otto.config`):

```python
class TestResolveMonitorTls:
    """[monitor] TLS resolution: single source of truth, fail-loud (spec 'Runtime behavior')."""

    @staticmethod
    def _repo(name: str, cert=None, key=None):
        from types import SimpleNamespace

        from otto.config import MonitorSettings

        return SimpleNamespace(name=name, monitor_settings=MonitorSettings(tls_cert=cert, tls_key=key))

    def test_no_repo_declares_tls_returns_none(self, monkeypatch):
        import otto.config

        from otto.cli.monitor import _resolve_monitor_tls

        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a"), self._repo("b")])
        assert _resolve_monitor_tls() is None

    def test_single_declaration_with_real_files_applies(self, monkeypatch, tmp_path):
        import otto.config

        from otto.cli.monitor import _resolve_monitor_tls

        cert = tmp_path / "cert.pem"
        cert.write_text("dummy")
        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a", cert=cert)])
        resolved = _resolve_monitor_tls()
        assert resolved is not None and resolved.tls_cert == cert

    def test_identical_declarations_apply(self, monkeypatch, tmp_path):
        import otto.config

        from otto.cli.monitor import _resolve_monitor_tls

        cert = tmp_path / "cert.pem"
        cert.write_text("dummy")
        monkeypatch.setattr(
            otto.config,
            "get_repos",
            lambda: [self._repo("a", cert=cert), self._repo("b", cert=cert)],
        )
        assert _resolve_monitor_tls() is not None

    def test_disagreeing_declarations_exit_1_naming_repos(self, monkeypatch, tmp_path, capsys):
        import typer

        import otto.config

        from otto.cli.monitor import _resolve_monitor_tls

        c1, c2 = tmp_path / "c1.pem", tmp_path / "c2.pem"
        c1.write_text("x"), c2.write_text("y")
        monkeypatch.setattr(
            otto.config,
            "get_repos",
            lambda: [self._repo("alpha", cert=c1), self._repo("beta", cert=c2)],
        )
        with pytest.raises(typer.Exit) as excinfo:
            _resolve_monitor_tls()
        assert excinfo.value.exit_code == 1
        err = capsys.readouterr().err
        assert "alpha" in err and "beta" in err

    def test_missing_cert_file_exits_1_naming_path(self, monkeypatch, tmp_path, capsys):
        import typer

        import otto.config

        from otto.cli.monitor import _resolve_monitor_tls

        ghost = tmp_path / "nope.pem"
        monkeypatch.setattr(otto.config, "get_repos", lambda: [self._repo("a", cert=ghost)])
        with pytest.raises(typer.Exit) as excinfo:
            _resolve_monitor_tls()
        assert excinfo.value.exit_code == 1
        assert str(ghost) in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/cli/test_monitor.py::TestResolveMonitorTls -v`
Expected: FAIL — `cannot import name '_resolve_monitor_tls'`.

- [ ] **Step 3: Implement in `src/otto/cli/monitor.py`.** Helper (module level; lazy imports per this file's convention; note `import otto.config` + attribute access so the tests' monkeypatch of `otto.config.get_repos` is seen):

```python
def _resolve_monitor_tls() -> "MonitorSettings | None":
    """Resolve the [monitor] TLS declaration across all configured repos.

    Fail-loud rules from the spec: more than one repo declaring *different*
    values is a configuration error (name the repos, exit 1); a declared
    cert/key path whose file is missing is an error (never a silent fall-back
    to HTTP — a quiet security downgrade); no declaration at all means plain
    HTTP, which is simply "not configured".
    """
    import otto.config

    declaring = [
        (r.name, r.monitor_settings)
        for r in otto.config.get_repos()
        if r.monitor_settings.tls_cert is not None
    ]
    if not declaring:
        return None
    if len({(ms.tls_cert, ms.tls_key) for _, ms in declaring}) > 1:
        names = ", ".join(sorted(name for name, _ in declaring))
        typer.echo(
            f"[monitor] TLS settings disagree across repos ({names}); "
            "make them identical or declare TLS in only one settings.toml.",
            err=True,
        )
        raise typer.Exit(1)
    settings = declaring[0][1]
    for field_name, path in (("tls_cert", settings.tls_cert), ("tls_key", settings.tls_key)):
        if path is not None and not path.is_file():
            typer.echo(
                f"[monitor] {field_name} {path} does not exist or is not a file — "
                "fix .otto/settings.toml or create the certificate "
                "(see the monitor guide's 'Securing the dashboard' section).",
                err=True,
            )
            raise typer.Exit(1)
    return settings
```

Add `MonitorSettings` to the `TYPE_CHECKING` block (`from ..config import MonitorSettings`).

Wire the **live** branch — replace the `MonitorServer(collector, mode="live", frame=frame, lab=lab)` construction with:

```python
    tls = _resolve_monitor_tls() if ctx.meta.get("_otto_root_options") is not None else None
    asyncio.run(
        _run_monitor(
            collector=collector,
            server=MonitorServer(
                collector,
                mode="live",
                frame=frame,
                lab=lab,
                tls_cert=tls.tls_cert if tls else None,
                tls_key=tls.tls_key if tls else None,
            ),
            interval=timedelta(seconds=interval),
            db=monitor_db,
        )
    )
```

(The `_otto_root_options` guard matches this file's existing idiom: a hand-built-context unit-test invocation has no bootstrapped repos to resolve from.)

Wire the **review** branch — inside the existing `if ctx.meta.get("_otto_root_options") is not None:` block, resolve `tls = _resolve_monitor_tls()`, else `tls = None`; extend `_serve_review`:

```python
async def _serve_review(
    export: MonitorExport, source_name: str, tls: "MonitorSettings | None" = None
) -> None:
    """Serve a previously saved format:1 export (no live collection)."""
    from ..monitor.server import MonitorServer

    server = MonitorServer(
        collector=MetricCollector(targets=[]),
        mode="review",
        document=export,
        source_name=source_name,
        tls_cert=tls.tls_cert if tls else None,
        tls_key=tls.tls_key if tls else None,
    )
    await server.serve()
```

and pass `tls` at its call site.

- [ ] **Step 4: Run the CLI monitor tests**

Run: `.venv/bin/pytest tests/unit/cli/test_monitor.py -v`
Expected: all PASS (new class and pre-existing tests — the guard keeps hand-built-context tests on the `tls=None` path).

- [ ] **Step 5: Typecheck + lint + commit**

```bash
nox -s typecheck && ruff format src/otto/cli/monitor.py tests/unit/cli/test_monitor.py && nox -s lint
git add -A && git commit -m "feat(cli): resolve [monitor] TLS settings for otto monitor (live + review)

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 6: Browser e2e — bare URL is refused, keyed URL works

**Files:**
- Create: `tests/e2e/monitor/dashboard/test_access_key.py`

**Interfaces:**
- Consumes: `shell_dash` fixture (conftest), keyed `DashboardHarness.url`.
- Produces: nothing downstream — this is the real-browser proof.

- [ ] **Step 1: Write the test**

```python
"""Access-key gate through a real browser (spec 2026-07-16).

The keyed-URL happy path is implicitly exercised by every other browser spec
in this directory (their page.goto(<dash>.url) now carries ?key=…); this
module pins the refusal side: the BARE origin — what a port-scanner or a
teammate guessing the address gets — must render the 403 hint page, not the
dashboard shell.
"""

from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.browser


def test_bare_url_renders_403_hint_not_dashboard(shell_dash, page) -> None:
    parts = urlsplit(shell_dash.url)
    bare = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    response = page.goto(bare)
    assert response is not None and response.status == 403
    content = page.content()
    assert "otto monitor" in content  # the hint names the command that prints the key
    assert "Access key required" in content


def test_keyed_url_boots_the_shell_and_cookie_covers_reload(shell_dash, page) -> None:
    """One keyed navigation, then a BARE reload must still work via the cookie."""
    page.goto(shell_dash.url)
    page.wait_for_selector("#root")
    parts = urlsplit(shell_dash.url)
    page.goto(urlunsplit((parts.scheme, parts.netloc, "/", "", "")))
    assert page.wait_for_selector("#root") is not None
```

(Verify the shell's root selector before relying on `#root`: `grep -o 'id="[a-z]*"' web/index.html` — use whatever the actual mount node is; other specs in this directory show the established wait idiom.)

- [ ] **Step 2: Run the chromium lane for a fast signal**

Run: `.venv/bin/pytest tests/e2e/monitor/dashboard/test_access_key.py -v`
Expected: PASS (chromium only).

- [ ] **Step 3: Prove the guard can fail** (house rule: a regression guard must be proven red against the code it guards). Temporarily add `await self._app(scope, receive, send); return` as the first line of `_AccessKeyMiddleware.__call__`, run `test_bare_url_renders_403_hint_not_dashboard`, confirm it FAILS, then revert the mutation (`git checkout -- src/otto/monitor/server.py`). Never commit the mutation.

- [ ] **Step 4: Run the FULL browser matrix (the real gate)**

Run: `nox -s dashboard`
Expected: chromium + firefox + webkit all PASS — the whole directory, not just the new file. If anything fails, triage via `scripts/junit_failures.py`.

- [ ] **Step 5: Commit**

```bash
ruff format tests/e2e/monitor/dashboard/test_access_key.py && nox -s lint
git add -A && git commit -m "test(monitor): browser e2e pins the bare-URL 403 and cookie handoff

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 7: Documentation — guide section + settings reference

**Files:**
- Modify: `docs/guide/monitor.md` (new "Securing the dashboard" section)

**Interfaces:**
- Consumes: the spec's "Certificate creation steps" section — reproduce it faithfully.
- Produces: user-facing docs; nothing downstream.

- [ ] **Step 1: Write the section.** Append to `docs/guide/monitor.md` (mirror the file's existing heading style). Required content, in this order:

1. **Access key** — every `otto monitor` run generates a fresh key; the printed URLs carry `?key=…`; opening a keyed URL once sets a browser cookie for the rest of the run; the bare address renders a 403 hint page. There are no flags to disable or pin the key.
2. **Enabling TLS** — the `[monitor]` table, verbatim example:

```toml
[monitor]
tls_cert = "~/.config/otto/tls/monitor-cert.pem"
tls_key  = "~/.config/otto/tls/monitor-key.pem"   # omit if the cert PEM bundles the key
```

   plus the behavior rules: settings.toml is committed and team-shared, so the value points at a per-user conventional path; TLS configured-but-broken exits 1 (never a silent HTTP fall-back); with multiple `OTTO_SUT_DIRS` repos, disagreeing `[monitor]` tables are an error.
3. **Who creates which certificate** — reproduce the spec's three-scope table (team-wide CA / per-machine leaf / per-repo settings entry) and the two "why not" bullets.
4. **Creating the certificates** — reproduce the spec's four steps verbatim (CA creation, per-platform trust install, leaf issuance with IP SANs and the 825-day Apple cap note, install + chmod 600), including the note that the SAN list must cover every URL `otto monitor` prints and that DHCP-reassigned IPs mean re-issuing the leaf.

- [ ] **Step 2: Clean-rebuild the docs (incremental `-W` misses broken refs — house rule)**

```bash
rm -rf docs/_build && make docs
```

Expected: build succeeds with zero warnings.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "docs(monitor): securing the dashboard — access key + TLS cert walkthrough

Assisted-by: Claude <noreply@anthropic.com>"
```

---

### Task 8: Full gate + merge readiness

- [ ] **Step 1: Full coverage gate**

Run: `make coverage` (then check `scripts/junit_failures.py reports/junit/...` — do not trust `| tail`'s exit code).
Expected: green across the suite.

- [ ] **Step 2: Verify end-to-end by hand** (the `verify` skill's spirit): `make web` if dist is stale, then `.venv/bin/otto monitor <some .json export>` in a fixture repo — confirm the printed URL carries `?key=`, opening it works, and the bare URL 403s. With a throwaway `[monitor]` cert in a scratch settings.toml, confirm `https://` URLs and the missing-file exit-1 message.

- [ ] **Step 3: Hand off** via superpowers:finishing-a-development-branch (merge/PR decision is Chris's).

## Self-review notes

- Spec coverage: token lifecycle (T2), middleware + 403 shapes + cookie (T2), port-scoped cookie (T2), SSE gating (T2), settings surface + expanduser + key-without-cert (T1), multi-repo rule + fail-loud missing file (T5), https/Secure/ssl args (T4), review-mode parity (T5), e2e bare-URL + cookie handoff (T6), docs incl. cert steps + scope table (T7). Frontend untouched throughout — no task touches `web/`.
- The `?key=` URL change is the one breaking surface; T2 Step 5 and T3 sweep every composer found by grep, with `origin`/`api_url` as the sanctioned escape hatches.
