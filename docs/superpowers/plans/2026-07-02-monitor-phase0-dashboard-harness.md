# Monitor Phase 0: Dashboard Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pytest-playwright harness (FakeCollector + threaded MonitorServer) that smoke-pins the current vanilla-JS dashboard's behavior so the Phase 1 backend refactor and Phase 2 React port have a regression net.

**Architecture:** `FakeCollector` subclasses the real `MetricCollector` (constructed with `hosts=[]`) and injects points through the production `_record_point()` path, so the store, chart map, event CRUD, SSE publish, and JSON export under test are the real code — only host polling is replaced. `DashboardHarness` runs `MonitorServer` on a background thread's event loop; tests marshal coroutines onto it via `run_coroutine_threadsafe`. Browser tests are characterization tests: they pin what the dashboard *does today* (except known bugs, which are not pinned).

**Tech Stack:** pytest-playwright (Chromium only in Phase 0; WebKit arrives with Phase 2's Safari fix), pytest-asyncio (strict mode, existing), stdlib `urllib`/`http.client` for API-level assertions (no new HTTP client dep).

**Spec:** `docs/superpowers/specs/2026-07-02-monitor-revamp-roadmap-design.md` (Phase 0 + section 5).

## Global Constraints

- Execute in an isolated worktree (superpowers:using-git-worktrees). A fresh worktree has no `.venv`: run `uv sync` first, then `make browsers` (Task 1 adds it) before any browser test.
- Browser tests carry `pytest.mark.xdist_group("dashboard")` so `--dist loadgroup` pins them to ONE xdist worker — never parallel browsers on the dev VM.
- Missing browser binaries must FAIL loudly (pytest-playwright's default error names the install command) — never `pytest.skip`.
- Every e2e test must declare exactly one primary resource marker (`hostless` here); `browser` is additive, like `hops` (enforced by `tests/e2e/conftest.py`).
- Repo lint is strict (`select=ALL` minus deny-list): run `uv run ruff check . && uv run ruff format --check .` after each task; `ty` runs only at `nox -s typecheck` — budget one typecheck round at the end.
- Tests must not write inside the repo — use `tmp_path` only.
- pytest `addopts` already includes coverage and `-n auto`; run single files with plain `uv run pytest <path>` (options apply automatically).
- Known-buggy behaviors (chart height growth, no window resize, Safari overdraw) are explicitly NOT pinned — do not write tests asserting today's broken layout math.

## File Structure

```text
tests/_fixtures/_fake_collector.py          # FakeCollector (MetricCollector subclass)
tests/_fixtures/_dashboard_harness.py       # DashboardHarness (threaded MonitorServer)
tests/unit/monitor/test_fake_collector.py   # FakeCollector unit tests (no server/browser)
tests/e2e/monitor/dashboard/__init__.py
tests/e2e/monitor/dashboard/conftest.py     # live_dash / historical_dash fixtures
tests/e2e/monitor/dashboard/data/historical.json   # static --file-format fixture
tests/e2e/monitor/dashboard/test_harness.py        # harness + wire-contract pins (no browser)
tests/e2e/monitor/dashboard/test_dashboard_live.py       # browser: render, append, tabs, pause
tests/e2e/monitor/dashboard/test_dashboard_events.py     # browser: event CRUD round-trips
tests/e2e/monitor/dashboard/test_dashboard_historical.py # browser: historical chrome, export, theme
```

Modified: `pyproject.toml` (dep + marker), `Makefile` (browsers/dashboard targets, M_HOSTLESS), `noxfile.py` (HOSTLESS_TEST_ARGS, dashboard session), `.github/workflows/ci.yml` (dashboard job), `tests/e2e/conftest.py` (docstring only).

---

### Task 1: Dependencies, `browser` marker, `make browsers`

**Files:**
- Modify: `pyproject.toml` (dev dependency-group, `markers` list)
- Modify: `Makefile` (`browsers` target, `.PHONY` line)
- Modify: `tests/e2e/conftest.py` (docstring only)

**Interfaces:**
- Produces: `pytest.mark.browser` marker; `make browsers` target; `playwright`/`pytest-playwright` importable in the dev env.

- [ ] **Step 1: Add pytest-playwright to the dev group**

In `pyproject.toml` `[dependency-groups] dev`, insert (alphabetical position, after `pytest-cov`):

```toml
    # Browser e2e for the monitor dashboard (tests/e2e/monitor/dashboard).
    # pytest-playwright pulls in playwright; browser binaries are installed
    # separately via `make browsers` (playwright install chromium).
    "pytest-playwright>=0.7.1",
```

- [ ] **Step 2: Register the `browser` marker**

In the `markers` list in `pyproject.toml`, after the `hostless` line:

```toml
    "browser: needs a Playwright browser binary (make browsers) — additive resource refinement, deselect with -m 'not browser'",
```

- [ ] **Step 3: Add the `browsers` Makefile target**

Add `browsers` to the `.PHONY` line, and near the other tool-setup targets:

```makefile
browsers: ## (Setup) Install the Playwright Chromium binary used by the dashboard e2e tests
	uv run playwright install chromium
```

- [ ] **Step 4: Note the new additive marker in the e2e conftest docstring**

In `tests/e2e/conftest.py`, update the "All other axes" sentence to include `browser`:

```text
   other axes (``e2e`` level, ``xdist_group``, ``browser``, ``stability``,
   ``timeout``, ``retry``) are ignored.
```

- [ ] **Step 5: Sync, install, verify**

Run: `uv sync && make browsers`
Expected: playwright downloads Chromium (~170 MB, needs network) or reports it present.

Run: `uv run pytest --markers | grep browser`
Expected: the new marker line prints.

Run: `uv run python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop(); print('chromium ok')"`
Expected: `chromium ok`

- [ ] **Step 6: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add pyproject.toml uv.lock Makefile tests/e2e/conftest.py
git commit -m "test: add pytest-playwright dev dep, browser marker, make browsers"
```

---

### Task 2: FakeCollector

**Files:**
- Create: `tests/_fixtures/_fake_collector.py`
- Test: `tests/unit/monitor/test_fake_collector.py`

**Interfaces:**
- Consumes: `otto.monitor.collector.MetricCollector` (`_record_point`, `_parsers`, `get_meta`), `otto.monitor.parsers.MetricDataPoint`.
- Produces: `FakeCollector(force_live: bool = True)` with `async push(host: str, label: str, value: float, *, chart: str = "cpu", meta: dict[str, Any] | None = None, ts: datetime | None = None) -> None` and `CHART_COMMANDS: dict[str, str]` mapping `{"cpu", "memory", "disk", "load"}` → DEFAULT_PARSERS command keys.

- [ ] **Step 1: Write the failing unit tests**

`tests/unit/monitor/test_fake_collector.py` (match the async-marker style of `tests/unit/monitor/test_collector_db.py` — check its decorator and mirror it; shown here as `@pytest.mark.asyncio`):

```python
"""FakeCollector sanity: pushes ride the real MetricCollector record path."""

import pytest

from otto.monitor.collector import MetricCollector
from tests._fixtures._fake_collector import FakeCollector


@pytest.mark.asyncio
async def test_push_stores_series_and_chart_map() -> None:
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    await fake.push("host1", "Memory Usage", 61.0, chart="memory", meta={"Used": "1 G"})

    series = fake.get_series()
    assert [p.value for p in series["host1/Overall CPU"]] == [42.5]
    assert series["host1/Memory Usage"][0].meta == {"Used": "1 G"}
    assert fake.get_chart_map()["Overall CPU"] == "CPU"
    assert fake.get_chart_map()["Memory Usage"] == "Memory Usage"


@pytest.mark.asyncio
async def test_meta_matches_real_collector_except_forced_live() -> None:
    """Drift guard: FakeCollector must present exactly the real collector's meta."""
    fake = FakeCollector()
    await fake.push("host1", "Overall CPU", 42.5)
    real = MetricCollector(hosts=[])

    fake_meta = fake.get_meta()
    real_meta = real.get_meta()
    assert fake_meta["live"] is True
    assert real_meta["live"] is False  # hosts=[] means historical for the real one
    assert fake_meta["hosts"] == ["host1"]  # derived from pushed series keys
    # Everything except live/hosts is byte-identical to production meta.
    for key in ("metrics", "tabs"):
        assert fake_meta[key] == real_meta[key]


@pytest.mark.asyncio
async def test_push_publishes_sse_payload() -> None:
    fake = FakeCollector()
    q = fake.subscribe()
    await fake.push("host1", "Overall CPU", 42.5)
    msg = q.get_nowait()
    assert msg["type"] == "metric"
    assert msg["key"] == "host1/Overall CPU"
    assert msg["chart"] == "CPU"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_fake_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests._fixtures._fake_collector'`

- [ ] **Step 3: Implement FakeCollector**

`tests/_fixtures/_fake_collector.py`:

```python
"""FakeCollector — scripted stand-in for live collection in dashboard tests.

Subclasses the real :class:`~otto.monitor.collector.MetricCollector` so the
store, chart map, event CRUD, SSE publish, and JSON export paths under test
are the production ones — only the "poll a host" step is replaced by
:meth:`FakeCollector.push`.
"""

from datetime import datetime, timezone
from typing import Any

from typing_extensions import override

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import MetricDataPoint

# Friendly chart name → DEFAULT_PARSERS key (the dict key IS the shell command).
CHART_COMMANDS: dict[str, str] = {
    "cpu": "top -d 0.5 -bn2",
    "memory": "free -b",
    "disk": "df -h",
    "load": "cat /proc/loadavg",
}


class FakeCollector(MetricCollector):
    """A MetricCollector that never talks to hosts: tests push points directly."""

    def __init__(self, *, force_live: bool = True) -> None:
        # hosts=[] gives the real DEFAULT_PARSERS views, so /api/meta serves
        # the production tabs/metrics catalog.
        super().__init__(hosts=[])
        self._force_live = force_live

    @override
    def get_meta(self) -> dict[str, Any]:
        """Production meta, with ``live`` forced (hosts=[] would report historical)."""
        meta = super().get_meta()
        meta["live"] = self._force_live
        return meta

    async def push(
        self,
        host: str,
        label: str,
        value: float,
        *,
        chart: str = "cpu",
        meta: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Record one point exactly as a live tick would (store + SSE publish)."""
        view = self._parsers[CHART_COMMANDS[chart]]
        await self._record_point(
            host,
            ts or datetime.now(tz=timezone.utc),
            label,
            MetricDataPoint(value=value, meta=meta),
            view,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_fake_collector.py -v`
Expected: 3 PASS

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add tests/_fixtures/_fake_collector.py tests/unit/monitor/test_fake_collector.py
git commit -m "test: FakeCollector drives the real record path for dashboard tests"
```

---

### Task 3: DashboardHarness + fixtures + wire-contract pins

**Files:**
- Create: `tests/_fixtures/_dashboard_harness.py`
- Create: `tests/e2e/monitor/dashboard/__init__.py` (empty)
- Create: `tests/e2e/monitor/dashboard/conftest.py`
- Create: `tests/e2e/monitor/dashboard/data/historical.json`
- Test: `tests/e2e/monitor/dashboard/test_harness.py`

**Interfaces:**
- Consumes: `FakeCollector` (Task 2), `otto.monitor.server.MonitorServer`, `otto.monitor.collector.MetricCollector.from_json`.
- Produces:
  - `DashboardHarness(Generic[C])` with `.collector: C`, `.url: str`, `start() -> DashboardHarness[C]`, `run(coro) -> T` (marshals onto the server loop), `stop()`.
  - Fixtures `live_dash: DashboardHarness[FakeCollector]` (hosts host1/host2, 3 preloaded ticks of cpu+procs+memory+load) and `historical_dash: DashboardHarness[MetricCollector]` (loaded from `data/historical.json`).
  - `HISTORICAL_JSON: Path` constant in conftest.

- [ ] **Step 1: Write the failing harness tests**

`tests/e2e/monitor/dashboard/test_harness.py`:

```python
"""Harness self-tests + the wire-contract pins Phase 1 must keep green.

No browser: these run everywhere the hostless gate runs. The *_KEYS sets pin
the exact JSON shapes of /api/meta, /api/data, and SSE metric messages — the
contract the Phase 1 backend refactor and Phase 2 React port build against.
"""

import http.client
import json
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import pytest

from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [pytest.mark.hostless, pytest.mark.xdist_group("dashboard")]

META_KEYS = {"hosts", "live", "metrics", "tabs"}
META_METRIC_KEYS = {"label", "y_title", "unit", "command", "chart"}
META_TAB_KEYS = {"id", "label", "metrics"}
DATA_KEYS = {"series", "events", "chart_map"}
EVENT_KEYS = {"id", "timestamp", "label", "source", "color", "dash", "end_timestamp"}
SSE_METRIC_KEYS = {"type", "host", "label", "chart", "y_title", "unit", "key", "ts", "value"}


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — local test server
        return json.load(resp)


def test_serves_meta_and_data(live_dash: DashboardHarness[FakeCollector]) -> None:
    meta = _get_json(live_dash.url + "/api/meta")
    assert meta["live"] is True
    assert meta["hosts"] == ["host1", "host2"]
    data = _get_json(live_dash.url + "/api/data")
    assert len(data["series"]["host1/Overall CPU"]) == 3  # the preloaded ticks


def test_meta_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    meta = _get_json(live_dash.url + "/api/meta")
    assert set(meta) == META_KEYS
    assert all(set(m) == META_METRIC_KEYS for m in meta["metrics"])
    assert all(set(t) == META_TAB_KEYS for t in meta["tabs"])
    assert [t["id"] for t in meta["tabs"]] == ["cpu", "memory", "disk"]


def test_data_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    live_dash.run(live_dash.collector.add_event(label="pinned", color="#112233", dash="dot"))
    data = _get_json(live_dash.url + "/api/data")
    assert set(data) == DATA_KEYS
    # Points carry ts/value always; meta only when present (exclude_none).
    point_keys = {k for pts in data["series"].values() for p in pts for k in p}
    assert {"ts", "value"} <= point_keys <= {"ts", "value", "meta"}
    assert all(set(e) == EVENT_KEYS for e in data["events"])


def test_sse_stream_delivers_metric_messages(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()  # subscribe() has run once headers arrive
        live_dash.run(live_dash.collector.push("host1", "Overall CPU", 42.0))
        payload: dict[str, Any] | None = None
        while payload is None:
            # HTTPResponse.readline() de-chunks; never read resp.fp (raw
            # socket file) or you'll see chunked-transfer framing lines.
            line = resp.readline().decode()
            assert line, "SSE stream closed before a metric message arrived"
            if line.startswith("data:"):
                payload = json.loads(line[len("data:") :])
    finally:
        conn.close()
    assert set(payload) == SSE_METRIC_KEYS
    assert payload["type"] == "metric"
    assert payload["key"] == "host1/Overall CPU"


def test_historical_fixture_loads(historical_dash: DashboardHarness[MetricCollector]) -> None:
    meta = _get_json(historical_dash.url + "/api/meta")
    assert meta["live"] is False
    assert meta["hosts"] == []  # bare labels → no host derived → historical UI
    data = _get_json(historical_dash.url + "/api/data")
    assert set(data["series"]) == {"Overall CPU", "Load (1m)", "Memory Usage"}
    assert len(data["events"]) == 2


def test_stop_joins_server_thread(live_dash: DashboardHarness[FakeCollector]) -> None:
    live_dash.stop()  # idempotent with the fixture finalizer
    assert not live_dash.thread_alive
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests._fixtures._dashboard_harness'`

- [ ] **Step 3: Implement DashboardHarness**

`tests/_fixtures/_dashboard_harness.py`:

```python
"""DashboardHarness — MonitorServer on a background thread for dashboard tests.

The server (and every collector coroutine) runs on the thread's own event
loop; test-side helpers marshal onto it with ``run_coroutine_threadsafe`` so
the collector is only ever touched from one loop.
"""

import asyncio
import threading
import time
from collections.abc import Coroutine
from typing import Any, Generic, TypeVar

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer

C = TypeVar("C", bound=MetricCollector)
T = TypeVar("T")

_STARTUP_TIMEOUT = 15.0


class DashboardHarness(Generic[C]):
    """Serve *collector*'s dashboard on ``127.0.0.1:<ephemeral>``."""

    def __init__(self, collector: C) -> None:
        self.collector: C = collector
        self.server = MonitorServer(collector, host="127.0.0.1", port=0)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Base URL (valid once start() has returned)."""
        return self.server.url

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "DashboardHarness[C]":
        self._thread = threading.Thread(
            target=self._serve, name="dashboard-harness", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError(f"MonitorServer did not start within {_STARTUP_TIMEOUT}s")
            time.sleep(0.02)
        return self

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.server.serve())
        finally:
            self._loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run *coro* on the server's loop and return its result."""
        if self._loop is None:
            raise RuntimeError("harness not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)

    def stop(self) -> None:
        """Signal shutdown and join the server thread (idempotent)."""
        if self._thread is None:
            return
        self.server.stop()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("dashboard harness thread did not exit within 10s")
        self._thread = None
```

- [ ] **Step 4: Create the package init and conftest**

`tests/e2e/monitor/dashboard/__init__.py`: empty file.

`tests/e2e/monitor/dashboard/conftest.py`:

```python
"""Dashboard e2e fixtures: a scripted live server and a historical server."""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

HISTORICAL_JSON = Path(__file__).parent / "data" / "historical.json"

_PROC_META = {
    "Command": "stress",
    "User": "root",
    "Mem": "1.0%",
    "RSS": "10 M",
    "Stat": "R",
    "CPU Time": "0:01.00",
}


def _preload(harness: DashboardHarness[FakeCollector]) -> None:
    """Three 5s-spaced ticks for two hosts: overall CPU, two procs, memory, load."""
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=15)
    push = harness.collector.push
    for tick in range(3):
        ts = t0 + timedelta(seconds=5 * tick)
        for host in ("host1", "host2"):
            harness.run(push(host, "Overall CPU", 20.0 + tick, ts=ts))
            harness.run(push(host, "proc/101", 5.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "proc/202", 3.0 + tick, meta=_PROC_META, ts=ts))
            harness.run(push(host, "Memory Usage", 40.0 + tick, chart="memory", ts=ts))
            harness.run(push(host, "Load (1m)", 0.5 + tick, chart="load", ts=ts))


@pytest.fixture
def live_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    harness = DashboardHarness(FakeCollector()).start()
    _preload(harness)
    yield harness
    harness.stop()


@pytest.fixture
def historical_dash() -> Iterator[DashboardHarness[MetricCollector]]:
    harness = DashboardHarness(MetricCollector.from_json(str(HISTORICAL_JSON))).start()
    yield harness
    harness.stop()
```

- [ ] **Step 5: Create the historical fixture file**

`tests/e2e/monitor/dashboard/data/historical.json` — bare labels (no `host/` prefix), which is what makes the dashboard take its historical no-host-selector path; also pins the `--file` import format:

```json
{
  "metrics": [
    {"timestamp": "2026-07-01T10:00:00+00:00", "label": "Overall CPU", "value": 12.5},
    {"timestamp": "2026-07-01T10:00:05+00:00", "label": "Overall CPU", "value": 55.0},
    {"timestamp": "2026-07-01T10:00:10+00:00", "label": "Overall CPU", "value": 31.0},
    {"timestamp": "2026-07-01T10:00:00+00:00", "label": "Load (1m)", "value": 0.42},
    {"timestamp": "2026-07-01T10:00:05+00:00", "label": "Load (1m)", "value": 0.61},
    {"timestamp": "2026-07-01T10:00:10+00:00", "label": "Load (1m)", "value": 0.55},
    {"timestamp": "2026-07-01T10:00:00+00:00", "label": "Memory Usage", "value": 47.1, "meta": {"Used": "7.5 G", "Total": "16 G"}},
    {"timestamp": "2026-07-01T10:00:05+00:00", "label": "Memory Usage", "value": 47.9, "meta": {"Used": "7.7 G", "Total": "16 G"}},
    {"timestamp": "2026-07-01T10:00:10+00:00", "label": "Memory Usage", "value": 48.4, "meta": {"Used": "7.7 G", "Total": "16 G"}}
  ],
  "events": [
    {"id": 1, "timestamp": "2026-07-01T10:00:04+00:00", "label": "Reboot", "source": "manual", "color": "#d62728", "dash": "dash"},
    {"id": 2, "timestamp": "2026-07-01T10:00:06+00:00", "end_timestamp": "2026-07-01T10:00:09+00:00", "label": "Maintenance", "source": "manual", "color": "#1f77b4", "dash": "dot"}
  ],
  "chart_map": {
    "Overall CPU": "CPU",
    "Load (1m)": "Load",
    "Memory Usage": "Memory Usage"
  }
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -v`
Expected: 6 PASS. If `test_sse_stream_delivers_metric_messages` hangs, the keepalive timeout (15 s) exceeds the read timeout — the push should arrive within milliseconds; debug with `-x --timeout 60`.

- [ ] **Step 7: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add tests/_fixtures/_dashboard_harness.py tests/e2e/monitor/dashboard/
git commit -m "test: threaded dashboard harness + API wire-contract pins"
```

---

### Task 4: Browser smoke test — load, render, host selection

**Files:**
- Test: `tests/e2e/monitor/dashboard/test_dashboard_live.py`

**Interfaces:**
- Consumes: `live_dash` fixture (Task 3); pytest-playwright's `page: Page` fixture.
- Produces: `_overall_cpu_len(page) -> int` helper reused by Tasks 5.

These are characterization tests against `src/otto/monitor/static/dashboard.js`. Expected behaviors (verified by reading the source): title is `Otto Monitor`; `#status-label` shows `Live` once SSE opens (meta.live true); in live multi-host mode NO charts render until a host is selected (`populateHostSelect` placeholder); selecting a host builds tabs `CPU`/`Memory`/`Disk` and chart divs; the CPU tab holds two chart groups (CPU, Load). If a run disagrees, inspect the dashboard in a headed browser (`--headed`) before changing the assertion — the pin must document real behavior.

- [ ] **Step 1: Write the smoke test**

```python
"""Pins the live-dashboard behaviors that must survive the React port."""

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _overall_cpu_len(page: Page) -> int:
    """Length of the 'Overall CPU' trace on the CPU chart (-1 if absent)."""
    return page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )


def _open_host(page: Page, dash: DashboardHarness[FakeCollector], host: str = "host1") -> None:
    page.goto(dash.url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", host)
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()


def test_loads_live_and_renders_after_host_selection(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    page.goto(live_dash.url)
    expect(page).to_have_title("Otto Monitor")
    expect(page.locator("#status-label")).to_have_text("Live")
    # Live multi-host mode defers chart creation until a host is chosen.
    expect(page.locator(".metric-plot")).to_have_count(0)

    page.select_option("#host-select", "host1")
    expect(page.locator(".tab-btn")).to_have_text(["CPU", "Memory", "Disk"])
    # CPU tab holds two chart groups: CPU (overall+procs) and Load.
    expect(page.locator("#tab-cpu .metric-plot")).to_have_count(2)
    assert _overall_cpu_len(page) == 3  # the three preloaded ticks
```

- [ ] **Step 2: Run and stabilize**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_dashboard_live.py -v`
Expected: 1 PASS (a real Chromium launches headless). On assertion mismatch, re-run with `--headed --slowmo 250` and inspect; adjust the pin only to match actual current behavior.

- [ ] **Step 3: Lint and commit**

Run: `uv run ruff check . && uv run ruff format --check .`

```bash
git add tests/e2e/monitor/dashboard/test_dashboard_live.py
git commit -m "test: browser smoke pin — dashboard load, host selection, chart render"
```

---

### Task 5: Live behavior pins — SSE append, tab switching, pause/resume

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_dashboard_live.py` (append tests)

**Interfaces:**
- Consumes: `_overall_cpu_len`, `_open_host` (Task 4), `live_dash.run()`/`collector.push()`.

- [ ] **Step 1: Add the three tests**

Append to `test_dashboard_live.py`:

```python
def test_sse_metric_extends_live_trace(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    assert _overall_cpu_len(page) == 3

    live_dash.run(live_dash.collector.push("host1", "Overall CPU", 77.0))
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr && tr.x.length === 4;"
        "}"
    )

    # A point for the *unselected* host must not touch host1's charts.
    live_dash.run(live_dash.collector.push("host2", "Overall CPU", 90.0))
    page.wait_for_timeout(300)
    assert _overall_cpu_len(page) == 4


def test_tab_switching_lazily_initializes_charts(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    # Memory panel exists but is inactive (charts deferred until visible).
    expect(page.locator("#tab-memory")).not_to_have_class("tab-panel active")

    page.click(".tab-btn[data-tab='memory']")
    expect(page.locator("#tab-memory")).to_have_class("tab-panel active")
    memory_len = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-memory .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Memory Usage');"
        "  return tr ? tr.x.length : -1;"
        "}"
    )
    assert memory_len == 3


def test_pause_freezes_and_resume_catches_up(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open_host(page, live_dash)
    page.click("#pause-btn")
    expect(page.locator("#status-label")).to_have_text("Paused")

    live_dash.run(live_dash.collector.push("host1", "Overall CPU", 77.0))
    page.wait_for_timeout(500)  # SSE delivery window — chart must NOT move
    assert _overall_cpu_len(page) == 3

    page.click("#pause-btn")  # resume triggers a full refreshPlot from state
    expect(page.locator("#status-label")).to_have_text("Live")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const tr = (gd?.data || []).find(t => t.name === 'Overall CPU');"
        "  return tr && tr.x.length === 4;"
        "}"
    )


def test_server_shutdown_shows_disconnected(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    """The SSE-error path: status flips to Disconnected and pause disables."""
    _open_host(page, live_dash)
    live_dash.stop()  # idempotent with the fixture finalizer
    expect(page.locator("#status-label")).to_have_text("Disconnected")
    expect(page.locator("#pause-btn")).to_be_disabled()
```

- [ ] **Step 2: Run and stabilize**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_dashboard_live.py -v`
Expected: 5 PASS. Note `to_have_class` matches the FULL class string — if it flakes on ordering, switch to `expect(...).to_have_class(re.compile(r"\bactive\b"))` (import `re`).

- [ ] **Step 3: Lint and commit**

```bash
git add tests/e2e/monitor/dashboard/test_dashboard_live.py
git commit -m "test: pin SSE append, tab switching, pause/resume behavior"
```

---

### Task 6: Event round-trip pins

**Files:**
- Test: `tests/e2e/monitor/dashboard/test_dashboard_events.py`

**Interfaces:**
- Consumes: `live_dash` fixture; `collector.add_event/update_event` via `harness.run()`.

- [ ] **Step 1: Write the event tests**

```python
"""Pins event CRUD round-trips: UI → API → SSE → chart shapes/annotations."""

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _annotation_labels(page: Page) -> list[str]:
    return page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return ((gd?.layout || {}).annotations || []).map(a => a.text);"
        "}"
    )


def _open(page: Page, dash: DashboardHarness[FakeCollector]) -> None:
    page.goto(dash.url)
    expect(page.locator("#status-label")).to_have_text("Live")
    page.select_option("#host-select", "host1")
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()


def test_mark_event_via_ui_draws_annotation(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open(page, live_dash)
    page.fill("#event-label", "Router rebooted")
    page.click("#event-btn")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const anns = ((gd?.layout || {}).annotations || []);"
        "  return anns.length === 1 && anns[0].text === 'Router rebooted';"
        "}"
    )
    events = live_dash.collector.get_events()
    assert [e.label for e in events] == ["Router rebooted"]
    assert events[0].source == "manual"


def test_span_event_draws_shaded_region(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    _open(page, live_dash)
    page.fill("#span-label", "Maintenance")
    page.click("#span-btn")
    expect(page.locator("#span-btn")).to_have_text("End event")
    page.click("#span-btn")
    expect(page.locator("#span-btn")).to_have_text("Start event")
    # Span = borderless rect + two edge lines (3 shapes) once end_ts round-trips.
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const shapes = ((gd?.layout || {}).shapes || []);"
        "  return shapes.some(s => s.type === 'rect');"
        "}"
    )
    assert live_dash.collector.get_events()[0].end_timestamp is not None


def test_backend_event_update_reflects_in_ui(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    ev = live_dash.run(
        live_dash.collector.add_event(label="before", color="#ff0000", dash="dash")
    )
    _open(page, live_dash)
    assert _annotation_labels(page) == ["before"]

    live_dash.run(
        live_dash.collector.update_event(ev.id, label="after", color="#00ff00", dash="dot")
    )
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  const anns = ((gd?.layout || {}).annotations || []);"
        "  return anns.length === 1 && anns[0].text === 'after';"
        "}"
    )


def test_clear_events_deletes_after_confirm(
    page: Page, live_dash: DashboardHarness[FakeCollector]
) -> None:
    live_dash.run(live_dash.collector.add_event(label="one", color="#ff0000", dash="dash"))
    live_dash.run(live_dash.collector.add_event(label="two", color="#00ff00", dash="dot"))
    _open(page, live_dash)
    assert len(_annotation_labels(page)) == 2

    page.on("dialog", lambda dialog: dialog.accept())  # confirm() dialog
    page.click("#clear-events-btn")
    page.wait_for_function(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return (((gd?.layout || {}).annotations || []).length === 0);"
        "}"
    )
    assert live_dash.collector.get_events() == []
```

- [ ] **Step 2: Run and stabilize**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_dashboard_events.py -v`
Expected: 4 PASS. `get_events()` is read cross-thread — a snapshot copy of a list the loop mutates; if an assertion races, wrap it in `live_dash.run()` via a tiny coroutine instead.

- [ ] **Step 3: Lint and commit**

```bash
git add tests/e2e/monitor/dashboard/test_dashboard_events.py
git commit -m "test: pin event mark/span/update/clear round-trips"
```

---

### Task 7: Historical mode, export round-trip, theme persistence

**Files:**
- Test: `tests/e2e/monitor/dashboard/test_dashboard_historical.py`

**Interfaces:**
- Consumes: `historical_dash` + `live_dash` fixtures; `MetricCollector.from_json` (export round-trip); `tmp_path`.

- [ ] **Step 1: Write the tests**

```python
"""Pins historical-mode chrome, the export → import round-trip, and theming."""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from otto.monitor.collector import MetricCollector
from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def test_historical_mode_chrome(
    page: Page, historical_dash: DashboardHarness[MetricCollector]
) -> None:
    page.goto(historical_dash.url)
    expect(page.locator("#status-label")).to_have_text("Historical")
    expect(page.locator("body")).to_have_class("historical")
    # No live hosts: charts render immediately, selector shows the placeholder.
    expect(page.locator("#tab-cpu .metric-plot").first).to_be_visible()
    expect(page.locator("#host-select option")).to_have_text(["historical"])
    expect(page.locator("#pause-btn")).to_be_disabled()
    # Both fixture events render as annotations.
    labels = page.evaluate(
        "() => {"
        "  const gd = document.querySelector('#tab-cpu .metric-plot');"
        "  return ((gd?.layout || {}).annotations || []).map(a => a.text);"
        "}"
    )
    assert sorted(labels) == ["Maintenance", "Reboot"]


def test_export_json_reimports_losslessly(
    page: Page, live_dash: DashboardHarness[FakeCollector], tmp_path: Path
) -> None:
    resp = page.request.get(live_dash.url + "/api/export/json")
    assert resp.ok
    exported = resp.json()
    assert set(exported) == {"metrics", "events", "chart_map"}

    out = tmp_path / "exported.json"
    out.write_text(json.dumps(exported))
    reloaded = MetricCollector.from_json(str(out))
    assert reloaded.get_series().keys() == live_dash.collector.get_series().keys()
    assert reloaded.get_chart_map() == live_dash.collector.get_chart_map()


def test_theme_toggle_persists_across_reload(
    page: Page, historical_dash: DashboardHarness[MetricCollector]
) -> None:
    # Class ORDER differs between toggle ("historical light") and reload
    # ("light historical" — the localStorage restore runs before init()
    # adds `historical`), so match with regexes, never full strings.
    light = re.compile(r"\blight\b")
    page.goto(historical_dash.url)
    expect(page.locator("body")).not_to_have_class(light)
    page.click("#theme-btn")
    expect(page.locator("body")).to_have_class(light)
    page.reload()
    expect(page.locator("body")).to_have_class(light)
```

- [ ] **Step 2: Run and stabilize**

Run: `uv run pytest tests/e2e/monitor/dashboard/test_dashboard_historical.py -v`
Expected: 3 PASS. `to_have_class` asserts the full class attribute — `body` starts with `historical` (set by init) and gains `light`; if class order differs at runtime, use `re.compile(r"\blight\b")`.

- [ ] **Step 3: Lint and commit**

```bash
git add tests/e2e/monitor/dashboard/test_dashboard_historical.py
git commit -m "test: pin historical chrome, export round-trip, theme persistence"
```

---

### Task 8: Gate wiring — Makefile, nox, CI

**Files:**
- Modify: `Makefile` (`M_HOSTLESS`, new `dashboard` target, `.PHONY`)
- Modify: `noxfile.py` (`HOSTLESS_TEST_ARGS`, new `dashboard` session)
- Modify: `.github/workflows/ci.yml` (new `dashboard` job)

**Interfaces:**
- Consumes: `browser` marker (Task 1), dashboard tests (Tasks 4–7).
- Produces: `make dashboard`, `nox -s dashboard`, CI `dashboard` job; hostless gate excludes `browser`.

- [ ] **Step 1: Exclude `browser` from the hostless slices**

Makefile (the comment block above already says to keep the axes in sync with noxfile.py):

```makefile
M_HOSTLESS := not integration and not embedded and not stability and not browser
```

noxfile.py:

```python
HOSTLESS_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded and not stability and not browser",
    "--cov-fail-under=85",
)
```

- [ ] **Step 2: Add the `dashboard` Makefile target**

Add `dashboard` to `.PHONY`; place the target next to the other coverage targets:

```makefile
dashboard: ## Run the browser e2e suite for the monitor dashboard (needs `make browsers` once). JUnit XML in reports/junit/dashboard/.
	$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard -m browser --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard)
```

- [ ] **Step 3: Add the single-Python nox session**

In `noxfile.py` (browser behavior doesn't vary by Python; one version suffices):

```python
@nox_uv.session(python=["3.12"], uv_groups=["dev"])
def dashboard(session: nox.Session) -> None:
    """Run the monitor-dashboard browser e2e suite (Chromium via Playwright).

    Kept out of the hostless gate (and its 5-Python CI matrix) so only this
    session needs a browser binary. Installs Chromium idempotently first.
    """
    session.run("playwright", "install", "chromium")
    session.run(
        "pytest",
        "tests/e2e/monitor/dashboard",
        "-m",
        "browser",
        _junitxml(session, "dashboard"),
        *session.posargs,
    )
```

- [ ] **Step 4: Add the CI job**

In `.github/workflows/ci.yml`, after the `tests` job (Chromium's system libraries need `--with-deps` on the runner):

```yaml
  dashboard:
    name: dashboard-e2e
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.0
      - uses: astral-sh/setup-uv@v8.2.0
        with:
          enable-cache: true
      - run: uv python install 3.12
      - name: Install Chromium with system dependencies
        run: uv run --group dev playwright install --with-deps chromium
      - name: Run dashboard browser e2e
        run: uv run nox -s dashboard
```

- [ ] **Step 5: Verify both lanes locally**

Run: `make dashboard`
Expected: all browser tests pass; JUnit XML at `reports/junit/dashboard/`.

Run: `uv run pytest tests/e2e/monitor/dashboard -m "not integration and not embedded and not stability and not browser" --collect-only -q | tail -3`
Expected: only the `test_harness.py` tests collect (browser tests deselected) — confirms the hostless gate stays browser-free.

- [ ] **Step 6: Full gate check**

Run: `make coverage` (full tier on the dev VM — dashboard tests now run inside it, pinned to one worker)
Expected: green, coverage ≥ 94.

Run: `uv run nox -s typecheck` (budgeted ty round for all new test code)
Expected: green; fix any strict-typing findings in the new files.

Run: `uv run nox -s lint`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add Makefile noxfile.py .github/workflows/ci.yml
git commit -m "test: wire dashboard browser suite into make/nox/CI, keep hostless gate browser-free"
```

---

## Verification checklist (whole plan)

- `make dashboard` — all browser pins green.
- `make coverage` — full suite green with dashboard tests included.
- `uv run pytest tests/unit/monitor/test_fake_collector.py tests/e2e/monitor/dashboard/test_harness.py` — contract pins green without a browser.
- `uv run nox -s lint typecheck` — strict gates green.
- The wire-contract key sets in `test_harness.py` are the seam Phase 1 must keep green; the browser pins are the seam Phase 2 must keep green.
