# Monitor Phase 3 Plan B (Log-Sourced Data) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let otto monitor chart cron-digested CSV files and render log-file event streams as dashboard tables — the `parse_tick()` contract with data-carried timestamps, a shared high-water-mark helper, `CsvMetricParser`, the full log-event backend (store ring, SQLite table, batched `log_event` SSE, JSON/SQLite import-export, `RegexLogEventParser`), the `TabSpec.kind`/`columns` wire bump, and a minimal `EventTable` React component with Playwright pins.

**Architecture:** One new overridable method — `MetricParser.parse_tick()` — covers both new source kinds; the collector always calls it, routing `TimedSample`s to the metric store (honoring data-carried timestamps) and `LogEvent`s to a new per-(host, tab) ring + `log_events` SQLite table + batched SSE kind. Existing parsers hit a default adapter and behave bit-identically. Presentation stays declarative: a parser that declares `table_columns` contributes a `kind="table"` `TabSpec` instead of a `ChartSpec`, and the React dashboard renders those tabs with a new `EventTable` component fed from a new zustand slice.

**Tech Stack:** Python 3.10+, pydantic (wire models), aiosqlite, pytest + pytest-asyncio; React 19 + zustand 5 + vite/vitest; Playwright via the existing `DashboardHarness`/`FakeCollector` fixtures.

**Spec:** `docs/superpowers/specs/2026-07-03-monitor-metrics-phase3-design.md` — the "Log-sourced data" section plus its rows in the degradation matrix and Testing/Documentation sections. Plan A (merged, main tip `221990d`) covered everything else.

## Global Constraints

- **No `from __future__ import annotations`** — real 3.10+ annotations, module-top imports (Sphinx `-W` nitpicky gate). Quoting an individual annotation (`"list[LogEvent]"`) is fine and matches existing style in `collector.py`.
- **ruff `select=ALL` discipline**: after any `ruff format`, re-run `ruff check .` (format is not lint-neutral). Prefer idiomatic fixes over `noqa`; narrow per-site `noqa` with a reason comment is last resort.
- **`ty` runs only at `nox -s typecheck`** — budget a typecheck round after src edits (Task 10).
- **Commits**: embed the trailer in `-m` (the prepare-commit-msg hook needs /dev/tty and silently defaults): end every commit message with `Assisted-by: Claude Fable 5`.
- **Never `git add -u`** — add named files only.
- **Fresh worktree setup**: `uv sync` first (no `.venv` otherwise); frontend tasks need `cd web && npm ci` once, then `make web` builds the dist the dashboard e2e requires.
- **Per-task gate** = scoped pytest (or `cd web && npm run test` for frontend tasks); **final gate** (Task 10) = `make coverage` + `nox -s lint` + `nox -s typecheck` + `make docs` + `make web` + `make dashboard`.
- **Import budget**: `otto.monitor.parsers`/`collector`/`store` are on the eager `import otto` path (guard at `tests/unit/import_budget/`). Do **not** import `otto.monitor.log_sourced` from `parsers.py`, `collector.py`, `store.py`, or `otto/monitor/__init__.py` — users import it explicitly (`from otto.monitor.log_sourced import ...`), exactly like `otto.monitor.parsers` registration functions today. The contract types (`TimedSample`/`LogEvent`/`TickResult`) live in `parsers.py` precisely so the eager chain doesn't grow.
- **Timestamps**: naive datetimes are treated as UTC at parse time (`_as_utc`); nothing naive ever enters the store. All "now" values are `datetime.now(tz=timezone.utc)`.
- **Wire shapes are verbatim contracts** (SSE `log_event` message, `/api/data` `log_events` rows, `TabSpec.kind`/`columns`) — the exact dict shapes in Tasks 3 and 5 are what the frontend types in Task 6 mirror; do not rename keys.
- **Caps**: backend in-memory ring = 1000 rows per (host, tab) (`_LOG_RING_MAX`); frontend store/display = 500 (`MAX_TABLE_ROWS`). The DB keeps everything.
- **No heavy parallel test loops on the dev VM** — single `-n auto` passes via make/nox targets only. Dashboard e2e runs `-n 1` (xdist_group) via `make dashboard`.

## File Structure

| File | Role |
| --- | --- |
| `src/otto/monitor/parsers.py` (modify) | `TimedSample`/`LogEvent`/`TickResult` contract types; `MetricParser.parse_tick()` default adapter; `table_columns` attribute |
| `src/otto/monitor/log_sourced.py` (create) | `parse_timestamp()`, `HighWaterMark`, `CsvMetricParser`, `RegexLogEventParser` — never eagerly imported |
| `src/otto/monitor/collector.py` (modify) | `parse_tick` cutover, sample-`ts` routing, `_record_log_events()`, `get_log_events()`, table tabs in `get_meta_model()` |
| `src/otto/monitor/store.py` (modify) | per-(host, tab) log-event ring + snapshot |
| `src/otto/monitor/db.py` (modify) | `log_events` table + `write_log_event()` |
| `src/otto/monitor/history.py` (modify) | JSON + SQLite import/export of log events |
| `src/otto/monitor/server.py` (modify) | `/api/data` gains `log_events` |
| `src/otto/models/monitor.py` (modify) | `LogEventRecord`; `TabSpec.kind`/`columns` (wire bump) |
| `tests/_fixtures/_fake_collector.py` (modify) | `extra_parsers` + `push_log_events()` scripting helper |
| `web/src/logevents.ts` (create) | pure table bookkeeping: key/append/cap/filter (vitest-able, mirrors `grouping.ts`) |
| `web/src/store.ts`, `web/src/api/sse.ts`, `web/src/api/client.ts` (modify) | `logEvents` slice, `log_event` dispatch, `DataPayload.log_events` |
| `web/src/components/EventTable.tsx` (create) + `TabBar.tsx`/`ChartGrid.tsx`/`dashboard.css` (modify) | `kind="table"` tab rendering |
| `web/src/api/types.gen.ts` (regenerated) | committed TS types from the bumped schema (`scripts/gen_web_types.sh`) |
| `tests/unit/monitor/test_parsers.py`, `test_store.py`, `test_collector_warnings.py` (modify); `test_collector_log_events.py`, `test_log_sourced.py` (create); `test_meta_models.py`, `test_monitor_import_export.py`, `test_collector_db.py`, `test_server.py` (modify) | backend units |
| `web/src/__tests__/logevents.test.ts`, `eventtable.test.tsx` (create); `store.test.ts` (modify) | frontend units |
| `tests/e2e/monitor/dashboard/test_dashboard_table.py` (create) + `conftest.py` (modify) | Playwright pins |
| `docs/guide/monitor.md` (modify); `docs/api/monitor/log_sourced.rst` (create) + `docs/api/monitor/index.rst` (modify) | docs |

---

### Task 1: `parse_tick()` contract + default adapter + collector cutover + store ring

**Files:**
- Modify: `src/otto/monitor/parsers.py` (contract types after `ParseContext`, ~line 85; `MetricParser` additions)
- Modify: `src/otto/monitor/collector.py` (imports ~line 29; `_process_host_results` parse block ~lines 479–491; new `_record_log_events`)
- Modify: `src/otto/monitor/store.py` (ring + snapshot)
- Test: `tests/unit/monitor/test_parsers.py`, `tests/unit/monitor/test_store.py`, `tests/unit/monitor/test_collector_log_events.py` (create), `tests/unit/monitor/test_collector_warnings.py`

**Interfaces:**
- Consumes: existing `MetricParser.parse()`, `MetricStore`, `_note_health`, `_record_point`.
- Produces (all later tasks rely on these exact names):
  `TimedSample(ts: datetime | None, series: dict[str, MetricDataPoint])`,
  `LogEvent(ts: datetime, fields: dict[str, str])`,
  `TickResult(samples: list[TimedSample], events: list[LogEvent])`,
  `MetricParser.parse_tick(output: str, *, ctx: ParseContext) -> TickResult`,
  `MetricParser.table_columns: list[str] | None = None`,
  `MetricStore.append_log_event(host: str, tab: str, event: LogEvent) -> None`,
  `MetricStore.snapshot_log_events() -> list[tuple[str, str, LogEvent]]`,
  `MetricCollector._record_log_events(host_name: str, tab: str, events: list[LogEvent]) -> None` (async; store-only in this task — Task 3 adds DB + SSE).

- [ ] **Step 1: Write the failing tests**

In `tests/unit/monitor/test_parsers.py` (imports at top of the new test class's file section: `from otto.monitor.parsers import TickResult, TimedSample`):

```python
class TestParseTickDefaultAdapter:
    """parse_tick() default: wraps parse() as one untimed sample — existing parsers bit-identical."""

    _FREE_B = (
        "               total        used        free\n"
        "Mem:     16000000000  4000000000  12000000000\n"
        "Swap:     2000000000   500000000   1500000000\n"
    )

    def test_wraps_parse_as_one_untimed_sample(self) -> None:
        parser = MemParser()
        tick = parser.parse_tick(self._FREE_B, ctx=ParseContext())
        assert tick.events == []
        assert len(tick.samples) == 1
        assert tick.samples[0].ts is None
        assert tick.samples[0].series == parser.parse(self._FREE_B, ctx=ParseContext())

    def test_empty_parse_yields_no_samples(self) -> None:
        assert MemParser().parse_tick("garbage", ctx=ParseContext()) == TickResult(
            samples=[], events=[]
        )
```

In `tests/unit/monitor/test_store.py`:

```python
def _ev(second: int) -> LogEvent:
    return LogEvent(
        ts=datetime(2026, 7, 4, 12, 0, second, tzinfo=timezone.utc),
        fields={"message": f"row {second}"},
    )


class TestLogEventRing:
    def test_append_and_snapshot_roundtrip(self) -> None:
        store = MetricStore()
        store.append_log_event("host1", "syslog", _ev(1))
        store.append_log_event("host1", "syslog", _ev(2))
        store.append_log_event("host2", "syslog", _ev(3))
        assert store.snapshot_log_events() == [
            ("host1", "syslog", _ev(1)),
            ("host1", "syslog", _ev(2)),
            ("host2", "syslog", _ev(3)),
        ]

    def test_ring_caps_at_1000_dropping_oldest(self) -> None:
        store = MetricStore()
        for i in range(1001):
            store.append_log_event(
                "h",
                "t",
                LogEvent(
                    ts=datetime(2026, 7, 4, tzinfo=timezone.utc) + timedelta(seconds=i),
                    fields={"i": str(i)},
                ),
            )
        rows = [ev for _, _, ev in store.snapshot_log_events()]
        assert len(rows) == 1000
        assert rows[0].fields["i"] == "1"  # row 0 dropped
```

(Extend the file's imports with `datetime`/`timedelta`/`timezone` and `LogEvent` from `otto.monitor.parsers` as needed.)

Create `tests/unit/monitor/test_collector_log_events.py`. Follow `tests/unit/monitor/test_collector_warnings.py`'s existing pattern for driving `_process_host_results` directly (MagicMock host, `CommandResult(Status.Success, value=..., command=..., retcode=0)`, `MonitorTarget`):

```python
"""Collector routing for the parse_tick() contract: data-carried timestamps + log events."""

from datetime import datetime, timezone

import pytest
from typing_extensions import override

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import (
    LogEvent,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    TickResult,
    TimedSample,
)
from otto.result import CommandResult
from otto.utils import Status

TS1 = datetime(2026, 7, 4, 11, 0, tzinfo=timezone.utc)
TS2 = datetime(2026, 7, 4, 11, 5, tzinfo=timezone.utc)
TICK = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


class _BackdatedParser(MetricParser):
    """Emits two samples carrying their own (older) timestamps."""

    y_title = "V"
    unit = ""
    command = "cat /var/log/perf.csv"
    tab = "metrics"
    tab_label = "Metrics"
    chart = "Perf"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        return TickResult(
            samples=[
                TimedSample(ts=TS1, series={"Perf": MetricDataPoint(1.0)}),
                TimedSample(ts=TS2, series={"Perf": MetricDataPoint(2.0)}),
            ],
            events=[],
        )


class _EventParser(MetricParser):
    """Emits only log events."""

    y_title = ""
    unit = ""
    command = "tail -n 200 /var/log/app.log"
    tab = "applog"
    tab_label = "App log"
    chart = "App log"
    table_columns = ["message"]

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        return TickResult(
            samples=[], events=[LogEvent(ts=TS1, fields={"message": "hello"})]
        )


def _result(parser: MetricParser) -> CommandResult:
    return CommandResult(Status.Success, value="raw", command=parser.command, retcode=0)


async def _process(collector: MetricCollector, parser: MetricParser) -> None:
    await collector._process_host_results(
        "host1",
        TICK,
        [_result(parser)],
        {parser.command: parser},
        ctx=ParseContext(ts=TICK),
    )


@pytest.mark.asyncio
async def test_backdated_samples_keep_their_own_timestamps() -> None:
    collector = MetricCollector(hosts=[])
    await _process(collector, _BackdatedParser())
    pts = collector.get_series()["host1/Perf"]
    assert [(p.ts, p.value) for p in pts] == [(TS1, 1.0), (TS2, 2.0)]


@pytest.mark.asyncio
async def test_untimed_sample_gets_tick_timestamp() -> None:
    class _Plain(MetricParser):
        y_title = ""
        unit = ""
        command = "echo 1"
        chart = "Plain"

        @override
        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {"Plain": MetricDataPoint(1.0)}

    collector = MetricCollector(hosts=[])
    await _process(collector, _Plain())
    assert collector.get_series()["host1/Plain"][0].ts == TICK


@pytest.mark.asyncio
async def test_events_land_in_store_ring_tagged_with_parser_tab() -> None:
    collector = MetricCollector(hosts=[])
    await _process(collector, _EventParser())
    assert collector._store.snapshot_log_events() == [
        ("host1", "applog", LogEvent(ts=TS1, fields={"message": "hello"}))
    ]
```

In `tests/unit/monitor/test_collector_warnings.py`, add — reusing the file's existing `collector` fixture, `_tick(collector, parsers, results)` helper, and `_ok(cmd)` result builder (the file's warning strings reference host `test1`):

```python
class _EventsOnlyParser(MetricParser):
    """parse_tick emits only log events — must count as production."""

    y_title = ""
    unit = ""
    command = "tail -n 5 /var/log/app.log"
    tab = "applog"
    tab_label = "App log"
    chart = "App log"
    table_columns = ["message"]

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}

    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        return TickResult(
            samples=[],
            events=[
                LogEvent(
                    ts=datetime(2026, 7, 4, tzinfo=timezone.utc), fields={"message": "x"}
                )
            ],
        )


class TestEventsCountAsProduction:
    @pytest.mark.asyncio
    async def test_events_only_parser_never_trips_silent_backstop(self, collector, caplog):
        """The silent-parser backstop counts samples OR events as production."""
        parser = _EventsOnlyParser()
        parsers = {parser.command: parser}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(4):
                await _tick(collector, parsers, [_ok(parser.command)])
        assert not [r for r in caplog.records if "has produced no data" in r.message]
```

(Add the needed imports — `datetime`/`timezone`, `LogEvent`, `TickResult` — to the file's import block.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py -k ParseTickDefaultAdapter tests/unit/monitor/test_store.py -k LogEventRing tests/unit/monitor/test_collector_log_events.py -x -q`
Expected: ImportError (`TickResult` etc. don't exist yet).

- [ ] **Step 3: Implement the contract in `parsers.py`**

After `ParseContext` (below the `_BYTES_PER_UNIT`/`human_readable` block is fine; keep contract types together):

```python
class TimedSample(NamedTuple):
    """One timestamped batch of series points produced by :meth:`MetricParser.parse_tick`."""

    ts: datetime | None
    """Data-carried timestamp for every point in ``series``; ``None`` means
    "stamp with the collector's tick time" (what plain ``parse()`` output gets)."""

    series: dict[str, MetricDataPoint]
    """Series label → data point, exactly as ``parse()`` returns."""


class LogEvent(NamedTuple):
    """One columnar log-event row produced by :meth:`MetricParser.parse_tick`.

    A table row, not a chart point — rendered by ``kind="table"`` dashboard
    tabs. Deliberately separate from :class:`~otto.monitor.events.MonitorEvent`
    (the global, low-volume chart-marker annotation system): log events are
    per-host, high-volume, columnar data.
    """

    ts: datetime
    """Data-carried timestamp of the row."""

    fields: dict[str, str]
    """Column → value; the schema is declared by the parser's ``table_columns``."""


class TickResult(NamedTuple):
    """Everything a parser produced for one tick: timed samples and/or log events."""

    samples: list[TimedSample]
    events: list[LogEvent]
```

In `MetricParser`, after the `interval` attribute:

```python
    table_columns: list[str] | None = None
    """Table columns for log-event parsers — their tab renders as a table
    (``TabSpec.kind == "table"``) instead of charts. ``None`` (the default)
    for chart parsers. Table parsers must declare their own ``tab`` id;
    sharing a tab with chart parsers raises in ``get_meta_model()``."""
```

After the abstract `parse()` (and append one line to `parse()`'s docstring Returns section: *"Log-sourced parsers — which override* ``parse_tick()`` *— implement this as a trivial* ``return {}``*."*):

```python
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        """Convert one tick's command output into timed samples and/or log events.

        The collector always calls this, never :meth:`parse` directly. The
        default wraps :meth:`parse` as a single untimed sample, so existing
        parsers behave exactly as before. Log-sourced parsers (see
        :mod:`otto.monitor.log_sourced`) override this to emit data-carried
        timestamps — multiple samples per read, ascending — and/or columnar
        :class:`LogEvent` rows.
        """
        series = self.parse(output, ctx=ctx)
        return TickResult(
            samples=[TimedSample(ts=None, series=series)] if series else [], events=[]
        )
```

- [ ] **Step 4: Implement the store ring in `store.py`**

Add `from .parsers import LogEvent` to the imports (no cycle: `parsers` imports only `..registry` and `.rates`). Module-level, above the class:

```python
# Rows kept in memory per (host, tab) table — the DB keeps everything.
_LOG_RING_MAX = 1000
```

In `MetricStore.__init__`:

```python
        self.log_events: dict[tuple[str, str], deque[LogEvent]] = {}
```

New methods (after `append_point`):

```python
    def append_log_event(self, host: str, tab: str, event: LogEvent) -> None:
        """Store one log-event row in the per-(host, tab) ring (oldest drop at capacity)."""
        ring = self.log_events.setdefault((host, tab), deque(maxlen=_LOG_RING_MAX))
        ring.append(event)

    def snapshot_log_events(self) -> list[tuple[str, str, LogEvent]]:
        """Return every ring's rows as (host, tab, event) triples, insertion-ordered per ring."""
        return [
            (host, tab, event)
            for (host, tab), ring in self.log_events.items()
            for event in ring
        ]
```

- [ ] **Step 5: Cut the collector over to `parse_tick`**

In `collector.py`, extend the parsers import (line ~29) with `LogEvent`. Replace the tail of `_process_host_results` — the current block:

```python
            # Parsing is NOT success-gated: grep-style commands legitimately
            # exit nonzero while their (partial) output still carries series.
            # `or ""` defends against value=None the same way the log line
            # above does — parsers expect str, not str | None.
            points = parser.parse(cmd_result.value or "", ctx=ctx)
            # The never-produced backstop only counts SUCCEEDING ticks — a
            # failing command is layer 1's job above; double-warning one root
            # cause helps nobody.
            if cmd_result.retcode == 0:
                self._note_health(key, produced=bool(points), what=type(parser).__name__)
            if not points:
                continue
            for label, dp in points.items():
                await self._record_point(host_name, ts, label, dp, parser)
```

with:

```python
            # Parsing is NOT success-gated: grep-style commands legitimately
            # exit nonzero while their (partial) output still carries series.
            # `or ""` defends against value=None the same way the log line
            # above does — parsers expect str, not str | None.
            tick = parser.parse_tick(cmd_result.value or "", ctx=ctx)
            # The never-produced backstop only counts SUCCEEDING ticks — a
            # failing command is layer 1's job above; double-warning one root
            # cause helps nobody. Samples OR events count as production, so
            # table-only parsers don't false-positive the silent warning.
            if cmd_result.retcode == 0:
                self._note_health(
                    key,
                    produced=bool(tick.samples or tick.events),
                    what=type(parser).__name__,
                )
            for sample in tick.samples:
                sample_ts = sample.ts or ts
                for label, dp in sample.series.items():
                    await self._record_point(host_name, sample_ts, label, dp, parser)
            if tick.events:
                await self._record_log_events(host_name, parser.tab, tick.events)
```

Add the new method after `_record_point` (Task 3 extends it with DB + SSE):

```python
    async def _record_log_events(
        self, host_name: str, tab: str, events: "list[LogEvent]"
    ) -> None:
        """Store one tick's log-event rows for *host_name* (per-(host, tab) ring)."""
        for ev in events:
            self._store.append_log_event(host_name, tab, ev)
```

- [ ] **Step 6: Run the tests again**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: PASS — including all pre-existing parser/collector tests (the adapter must be bit-identical; if any existing test fails, the cutover is wrong, not the test).

- [ ] **Step 7: Commit**

```bash
git add src/otto/monitor/parsers.py src/otto/monitor/collector.py src/otto/monitor/store.py tests/unit/monitor/test_parsers.py tests/unit/monitor/test_store.py tests/unit/monitor/test_collector_log_events.py tests/unit/monitor/test_collector_warnings.py
git commit -m "feat(monitor): parse_tick contract — timed samples, log events, collector cutover

One new overridable on MetricParser; the default adapter wraps parse() so
existing parsers are bit-identical. Collector honors data-carried sample
timestamps and routes LogEvents to a per-(host, tab) store ring; the
silent-parser backstop counts samples or events as production.

Assisted-by: Claude Fable 5"
```

---

### Task 2: `HighWaterMark` + `CsvMetricParser` (`log_sourced.py`)

**Files:**
- Create: `src/otto/monitor/log_sourced.py`
- Test: `tests/unit/monitor/test_log_sourced.py` (create)

**Interfaces:**
- Consumes: Task 1's `TickResult`/`TimedSample`/`LogEvent`, `MetricParser`, `MetricDataPoint`, `ParseContext`.
- Produces: `parse_timestamp(text: str, fmt: str = "auto") -> datetime | None`;
  `HighWaterMark` with `advance(rows: Iterable[tuple[datetime, T]]) -> list[tuple[datetime, T]]`;
  `CsvMetricParser(command, columns, *, chart, tab="metrics", tab_label="Metrics", y_title="", unit="", interval=None)`.
  Task 4 adds `RegexLogEventParser` to this same module.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_log_sourced.py`:

```python
"""Log-sourced parsers: timestamp parsing, high-water dedup, CSV metrics."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.monitor.log_sourced import CsvMetricParser, HighWaterMark, parse_timestamp
from otto.monitor.parsers import ParseContext

T0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


class TestParseTimestamp:
    def test_auto_epoch(self) -> None:
        assert parse_timestamp("1751630400") == datetime.fromtimestamp(
            1751630400, tz=timezone.utc
        )

    def test_auto_iso_aware(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00+00:00") == T0

    def test_auto_iso_naive_is_utc(self) -> None:
        assert parse_timestamp("2026-07-04 12:00:00") == T0

    def test_iso_z_suffix(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00Z", "iso") == T0

    def test_epoch_mode_rejects_iso(self) -> None:
        assert parse_timestamp("2026-07-04T12:00:00", "epoch") is None

    def test_strptime_format(self) -> None:
        assert parse_timestamp("2026/07/04 12:00:00", "%Y/%m/%d %H:%M:%S") == T0

    def test_strptime_without_year_gets_current_utc_year(self) -> None:
        parsed = parse_timestamp("Jul  4 12:00:00", "%b %d %H:%M:%S")
        assert parsed is not None
        assert parsed.year == datetime.now(tz=timezone.utc).year
        assert (parsed.month, parsed.day, parsed.hour) == (7, 4, 12)

    def test_garbage_is_none(self) -> None:
        assert parse_timestamp("not a time") is None


class TestHighWaterMark:
    def test_first_pass_emits_all_sorted(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        rows = [(T0 + timedelta(seconds=2), "b"), (T0, "a")]
        assert hwm.advance(rows) == [(T0, "a"), (T0 + timedelta(seconds=2), "b")]

    def test_reread_of_same_window_emits_nothing(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        rows = [(T0, "a"), (T0 + timedelta(seconds=2), "b")]
        hwm.advance(rows)
        assert hwm.advance(rows) == []

    def test_overlapping_window_emits_only_newer(self) -> None:
        hwm: HighWaterMark = HighWaterMark()
        hwm.advance([(T0, "a"), (T0 + timedelta(seconds=2), "b")])
        third = (T0 + timedelta(seconds=4), "c")
        assert hwm.advance([(T0 + timedelta(seconds=2), "b"), third]) == [third]

    def test_rotation_new_rows_still_newer(self) -> None:
        """Rotation/truncation: the mark keys on timestamps, not offsets."""
        hwm: HighWaterMark = HighWaterMark()
        hwm.advance([(T0, "old")])
        fresh = (T0 + timedelta(seconds=1), "post-rotate")
        assert hwm.advance([fresh]) == [fresh]


def _csv() -> CsvMetricParser:
    return CsvMetricParser(
        "cat /var/log/perf/net.csv",
        columns=["rx_kbps", "tx_kbps"],
        chart="Cron net digest",
        tab="network",
        tab_label="Network",
        unit="kb/s",
        interval=60,
    )


class TestCsvMetricParser:
    def test_declares_its_registry_metadata(self) -> None:
        p = _csv()
        assert p.command == "cat /var/log/perf/net.csv"
        assert p.chart == "Cron net digest"
        assert p.interval == 60
        assert p.table_columns is None  # a CHART parser, not a table parser
        assert p.parse("anything", ctx=ParseContext()) == {}

    def test_epoch_and_iso_rows_become_timed_samples(self) -> None:
        out = "1751630400,10,20\n2026-07-04T12:00:05+00:00,11,21\n"
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert tick.events == []
        assert [s.ts for s in tick.samples] == [
            datetime.fromtimestamp(1751630400, tz=timezone.utc),
            datetime(2026, 7, 4, 12, 0, 5, tzinfo=timezone.utc),
        ]
        assert tick.samples[0].series["rx_kbps"].value == 10.0
        assert tick.samples[0].series["tx_kbps"].value == 20.0

    def test_samples_sorted_ascending(self) -> None:
        out = "2026-07-04T12:00:05,11,21\n2026-07-04T12:00:00,10,20\n"
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert [s.ts for s in tick.samples] == sorted(s.ts for s in tick.samples)

    def test_high_water_dedup_across_rereads(self) -> None:
        p = _csv()
        out = "2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11,21\n"
        assert len(p.parse_tick(out, ctx=ParseContext()).samples) == 2
        # Same window re-read: nothing new.
        assert p.parse_tick(out, ctx=ParseContext()).samples == []
        # One new line appended: only it emits.
        grown = out + "2026-07-04T12:00:10,12,22\n"
        fresh = p.parse_tick(grown, ctx=ParseContext()).samples
        assert [s.ts for s in fresh] == [datetime(2026, 7, 4, 12, 0, 10, tzinfo=timezone.utc)]

    def test_restart_backfills_full_window(self) -> None:
        """A fresh parser instance (monitor restart) re-emits the whole file."""
        out = "".join(f"2026-07-04T12:00:{s:02d},1,2\n" for s in range(10))
        assert len(_csv().parse_tick(out, ctx=ParseContext()).samples) == 10

    def test_header_torn_and_malformed_lines_skipped(self) -> None:
        out = (
            "timestamp,rx_kbps,tx_kbps\n"        # header: first col not a timestamp
            "2026-07-04T12:00:00,10,20\n"        # good
            "2026-07-04T12:00:05,11\n"           # column mismatch (torn/partial)
            "2026-07-04T12:00:10,eleven,21\n"    # non-numeric value
            "garbage\n"
        )
        tick = _csv().parse_tick(out, ctx=ParseContext())
        assert len(tick.samples) == 1
        assert tick.samples[0].ts == datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_torn_line_reemits_whole_next_tick(self) -> None:
        """The mark never passes a skipped line, so its completed form emits later."""
        p = _csv()
        p.parse_tick("2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11\n", ctx=ParseContext())
        fresh = p.parse_tick(
            "2026-07-04T12:00:00,10,20\n2026-07-04T12:00:05,11,21\n", ctx=ParseContext()
        ).samples
        assert [s.ts for s in fresh] == [datetime(2026, 7, 4, 12, 0, 5, tzinfo=timezone.utc)]

    def test_requires_at_least_one_column(self) -> None:
        with pytest.raises(ValueError, match="at least one value column"):
            CsvMetricParser("cat x.csv", columns=[], chart="X")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_log_sourced.py -x -q`
Expected: FAIL — `ModuleNotFoundError: otto.monitor.log_sourced`.

- [ ] **Step 3: Implement `log_sourced.py`**

```python
"""Log-sourced metric parsers — data-carried timestamps from files on the host.

Some systems don't expose live values: a cron job digests performance numbers
into a timestamped CSV file every few minutes, or the interesting record is a
log file's event stream. Both ride the existing shell acquisition path via
:meth:`~otto.monitor.parsers.MetricParser.parse_tick` — the command IS the
reduction step (``cat``/``tail``/``awk``/``grep``/``jq`` on the host ships
back only the lines otto needs; the design assumes source data is always
textually reducible on the host).

Register instances exactly like any other parser (one instance per file;
distinct commands are distinct registry keys)::

    from otto.monitor.log_sourced import CsvMetricParser
    from otto.monitor.parsers import register_parsers

    register_parsers([
        CsvMetricParser(
            "cat /var/log/perf/net.csv",
            columns=["rx_kbps", "tx_kbps"],
            chart="Cron net digest",
            tab="network",
            tab_label="Network",
            unit="kb/s",
            interval=60,
        ),
    ])

Timestamp convention: naive values are treated as UTC.

This module is never imported by otto's eager import chain (import-budget
guard) — import it explicitly from init modules or test code.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import TypeVar

from typing_extensions import override

from .parsers import (
    LogEvent,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    TickResult,
    TimedSample,
)

T = TypeVar("T")

_MISSING_YEAR = 1900  # strptime's default year when the format lacks %Y (classic syslog)


def _as_utc(dt: datetime) -> datetime:
    """Apply the naive-means-UTC convention."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def parse_timestamp(text: str, fmt: str = "auto") -> datetime | None:
    """Parse a data-carried timestamp; ``None`` (skip the row) when it doesn't parse.

    ``fmt``:

    - ``"auto"``: epoch seconds, else ISO-8601 (the CSV first-column convention);
    - ``"epoch"``: Unix epoch seconds (int or float);
    - ``"iso"``: ISO-8601 (a ``Z`` suffix is accepted on Python 3.10);
    - anything else: a ``strptime`` format. A format without a year directive
      (classic syslog) yields year 1900 — the current UTC year is substituted.

    Naive results are treated as UTC in every mode.
    """
    text = text.strip()
    if fmt in ("auto", "epoch"):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            if fmt == "epoch":
                return None
    if fmt in ("auto", "iso"):
        try:
            return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    try:
        parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 — naive-means-UTC applied below
    except ValueError:
        return None
    if parsed.year == _MISSING_YEAR and "%Y" not in fmt and "%y" not in fmt:
        parsed = parsed.replace(year=datetime.now(tz=timezone.utc).year)
    return _as_utc(parsed)


class HighWaterMark:
    """Timestamp high-water mark: makes re-reads of rolling files idempotent.

    Tracks the newest emitted row timestamp per parser instance (parser
    instances are per-target deep copies, so state never leaks across
    hosts). Rows at or below the mark were already emitted on a previous
    tick and are dropped; survivors come back sorted ascending and the mark
    advances to the newest survivor. Keyed on row timestamps, not file
    offsets, so log rotation/truncation needs no special handling — new
    rows are still newer.

    A torn last line (read mid-write) is skipped by the parser BEFORE this
    filter, so the mark never passes it — the completed line re-emits whole
    on the next tick. Boundary rule: a new row bearing exactly the mark's
    timestamp is dropped as already-seen (accepted trade-off; real sources
    have per-row-unique or sub-second timestamps).
    """

    def __init__(self) -> None:
        self._mark: datetime | None = None

    def advance(self, rows: Iterable[tuple[datetime, T]]) -> list[tuple[datetime, T]]:
        """Return only the rows newer than the mark, ascending; advance the mark."""
        fresh = sorted(
            (row for row in rows if self._mark is None or row[0] > self._mark),
            key=lambda row: row[0],
        )
        if fresh:
            self._mark = fresh[-1][0]
        return fresh


class CsvMetricParser(MetricParser):
    """Chart metrics from a cron-digested CSV file read over the shell.

    Line format: first column an ISO-8601 or epoch-seconds timestamp (naive
    = UTC), remaining columns numeric values matching *columns* (the series
    labels), comma-separated. Header, torn, and otherwise malformed lines
    are skipped — a mid-write read self-heals next tick because the
    high-water mark never passes a skipped line. Points carry their
    data-carried timestamps, so a file holding the last hour backfills the
    dashboard and DB with an hour of real history on monitor start.

    One instance per file: the command string is the parser registry key
    ("a couple of CSV files" = two registered instances). Register via
    :func:`~otto.monitor.parsers.register_parsers` or
    :func:`~otto.monitor.parsers.register_host_parsers`.

    Args:
        command: Shell command printing the CSV content (e.g. ``cat /var/log/perf/net.csv``).
        columns: Series label per value column, in file order (timestamp column excluded).
        chart: Chart group id the series render on (one chart per parser).
        tab: Dashboard tab id.
        tab_label: Human-readable tab button label.
        y_title: Y-axis title shown left of the chart.
        unit: Unit suffix for chart annotations.
        interval: Poll cadence override in seconds (e.g. 60 for a file written every 5 minutes).
    """

    def __init__(
        self,
        command: str,
        columns: Sequence[str],
        *,
        chart: str,
        tab: str = "metrics",
        tab_label: str = "Metrics",
        y_title: str = "",
        unit: str = "",
        interval: float | None = None,
    ) -> None:
        if not columns:
            raise ValueError("CsvMetricParser needs at least one value column")
        self.command = command
        self.chart = chart
        self.tab = tab
        self.tab_label = tab_label
        self.y_title = y_title
        self.unit = unit
        self.interval = interval
        self._columns = list(columns)
        self._hwm = HighWaterMark()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        """Unused — this parser produces timed samples via :meth:`parse_tick`."""
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        rows: list[tuple[datetime, dict[str, MetricDataPoint]]] = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != len(self._columns) + 1:
                continue  # header/torn/partial line — self-heals next tick
            ts = parse_timestamp(parts[0])
            if ts is None:
                continue  # header line: first column isn't a timestamp
            try:
                values = [float(part) for part in parts[1:]]
            except ValueError:
                continue
            series = {
                label: MetricDataPoint(value)
                for label, value in zip(self._columns, values, strict=True)
            }
            rows.append((ts, series))
        fresh = self._hwm.advance(rows)
        return TickResult(
            samples=[TimedSample(ts=ts, series=series) for ts, series in fresh],
            events=[],
        )
```

(`re` is imported now for Task 4's `RegexLogEventParser`; if ruff flags it unused at this point, add it in Task 4 instead.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/monitor/test_log_sourced.py -x -q`
Expected: PASS.

- [ ] **Step 5: Collector integration — CSV backfill end to end**

Append to `tests/unit/monitor/test_collector_log_events.py`:

```python
@pytest.mark.asyncio
async def test_csv_parser_backfills_store_with_data_timestamps() -> None:
    from otto.monitor.log_sourced import CsvMetricParser

    parser = CsvMetricParser("cat /var/log/perf.csv", columns=["v"], chart="Perf")
    out = "2026-07-04T11:00:00,1\n2026-07-04T11:05:00,2\n"
    collector = MetricCollector(hosts=[])
    await collector._process_host_results(
        "host1",
        TICK,
        [CommandResult(Status.Success, value=out, command=parser.command, retcode=0)],
        {parser.command: parser},
        ctx=ParseContext(ts=TICK),
    )
    pts = collector.get_series()["host1/v"]
    assert [(p.ts, p.value) for p in pts] == [(TS1, 1.0), (TS2, 2.0)]
```

Run: `uv run pytest tests/unit/monitor/test_collector_log_events.py -x -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/otto/monitor/log_sourced.py tests/unit/monitor/test_log_sourced.py tests/unit/monitor/test_collector_log_events.py
git commit -m "feat(monitor): CsvMetricParser + timestamp high-water mark

New otto.monitor.log_sourced module (kept off the eager import chain):
parse_timestamp (auto/epoch/iso/strptime, naive=UTC, syslog year fix),
HighWaterMark (row-timestamp idempotent re-reads, rotation-safe), and the
cron-digest CSV parser with data-carried timestamps and restart backfill.

Assisted-by: Claude Fable 5"
```

---

### Task 3: Log-event persistence + wire — DB table, `LogEventRecord`, batched SSE, `/api/data`, import/export

**Files:**
- Modify: `src/otto/monitor/db.py` (schema + `write_log_event`), `src/otto/models/monitor.py` (`LogEventRecord`), `src/otto/monitor/collector.py` (`_record_log_events` extension + `get_log_events`), `src/otto/monitor/history.py` (JSON + SQLite both directions), `src/otto/monitor/server.py` (`/api/data`)
- Test: `tests/unit/monitor/test_collector_db.py`, `tests/unit/monitor/test_monitor_import_export.py`, `tests/unit/monitor/test_collector_log_events.py`, `tests/e2e/monitor/dashboard/test_harness.py` (wire pins — hostless, no browser)

**Interfaces:**
- Consumes: Task 1's `LogEvent`, store ring, `_record_log_events`.
- Produces: `MetricDB.write_log_event(ts: datetime, host: str, tab: str, fields: dict[str, str]) -> None` (async);
  `LogEventRecord(RowModel)` with `timestamp` (alias `ts`), `host: str = ""`, `tab: str = ""`, `fields: dict[str, str]`;
  `MetricCollector.get_log_events() -> list[dict[str, Any]]` returning rows shaped
  `{"timestamp": iso, "host": str, "tab": str, "fields": {col: val}}`;
  SSE message `{"type": "log_event", "host": str, "tab": str, "rows": [{"ts": iso, "fields": {...}}]}` (one batch per parser per tick);
  `/api/data` gains `"log_events": [<get_log_events rows>]`.
  Tasks 6–8 mirror these shapes verbatim.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/monitor/test_collector_log_events.py`, add the SSE batch pin:

```python
@pytest.mark.asyncio
async def test_record_log_events_publishes_one_batched_sse_message() -> None:
    collector = MetricCollector(hosts=[])
    q = collector.subscribe()
    events = [
        LogEvent(ts=TS1, fields={"message": "a"}),
        LogEvent(ts=TS2, fields={"message": "b"}),
    ]
    await collector._record_log_events("host1", "syslog", events)
    msg = q.get_nowait()
    assert msg == {
        "type": "log_event",
        "host": "host1",
        "tab": "syslog",
        "rows": [
            {"ts": TS1.isoformat(), "fields": {"message": "a"}},
            {"ts": TS2.isoformat(), "fields": {"message": "b"}},
        ],
    }
    assert q.empty()  # batched: exactly one message for the tick


@pytest.mark.asyncio
async def test_get_log_events_shape() -> None:
    collector = MetricCollector(hosts=[])
    await collector._record_log_events("host1", "syslog", [LogEvent(ts=TS1, fields={"m": "x"})])
    assert collector.get_log_events() == [
        {"timestamp": TS1.isoformat(), "host": "host1", "tab": "syslog", "fields": {"m": "x"}}
    ]
```

In `tests/unit/monitor/test_collector_db.py`, following its existing tmp-path DB patterns:

```python
@pytest.mark.asyncio
async def test_log_events_persist_and_reload(tmp_path: Path) -> None:
    db_path = tmp_path / "m.db"
    db = MetricDB(str(db_path))
    await db.open()
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    await db.write_log_event(ts, "host1", "syslog", {"message": "hello"})
    await db.close()

    store = MetricStore()
    await load_sqlite_into(store, str(db_path))
    assert store.snapshot_log_events() == [
        ("host1", "syslog", LogEvent(ts=ts, fields={"message": "hello"}))
    ]


@pytest.mark.asyncio
async def test_pre_log_events_db_loads_without_the_table(tmp_path: Path) -> None:
    """A DB written before this feature (no log_events table) still loads."""
    db_path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY, ts TEXT, host TEXT, label TEXT, value REAL)")
    await conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, end_ts TEXT, label TEXT, source TEXT, color TEXT, dash TEXT)")
    await conn.commit()
    await conn.close()
    store = MetricStore()
    await load_sqlite_into(store, str(db_path))  # must not raise
    assert store.snapshot_log_events() == []
```

In `tests/unit/monitor/test_monitor_import_export.py`, following its round-trip style:

```python
@pytest.mark.asyncio
async def test_log_events_json_export_import_roundtrip(tmp_path: Path) -> None:
    collector = MetricCollector(hosts=[])
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    await collector._record_log_events("host1", "syslog", [LogEvent(ts=ts, fields={"m": "x"})])
    path = tmp_path / "export.json"
    collector.export_json(str(path))

    loaded = MetricCollector.from_json(str(path))
    assert loaded._store.snapshot_log_events() == [
        ("host1", "syslog", LogEvent(ts=ts, fields={"m": "x"}))
    ]
```

In `tests/e2e/monitor/dashboard/test_harness.py` (the module that pins the exact `/api/*` + SSE wire shapes; hostless, no browser): update the `DATA_KEYS` pin and add the log-event contract pins.

Change:

```python
DATA_KEYS = {"series", "events", "chart_map", "log_events"}
# "log_events" added in Phase 3 Plan B (log-sourced data) — deliberate contract evolution.
LOG_EVENT_ROW_KEYS = {"timestamp", "host", "tab", "fields"}
SSE_LOG_EVENT_KEYS = {"type", "host", "tab", "rows"}
```

(`test_data_wire_contract`'s `assert set(data) == DATA_KEYS` then passes only with the route change.) Add, following the file's `_get_json`/SSE-readline patterns (`FakeCollector.push_log_events` arrives in Task 5 — until then, script rows via `live_dash.run(live_dash.collector._record_log_events("host1", "syslog", [LogEvent(...)]))`; Task 5 may switch these pins to the public helper):

```python
def test_data_log_events_wire_contract(live_dash: DashboardHarness[FakeCollector]) -> None:
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    live_dash.run(
        live_dash.collector._record_log_events(
            "host1", "syslog", [LogEvent(ts=ts, fields={"message": "pinned"})]
        )
    )
    data = _get_json(live_dash.url + "/api/data")
    assert all(set(row) == LOG_EVENT_ROW_KEYS for row in data["log_events"])
    row = data["log_events"][0]
    assert row == {
        "timestamp": ts.isoformat(),
        "host": "host1",
        "tab": "syslog",
        "fields": {"message": "pinned"},
    }


def test_sse_stream_delivers_batched_log_events(
    live_dash: DashboardHarness[FakeCollector],
) -> None:
    port = urlsplit(live_dash.url).port
    assert port is not None
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        live_dash.run(
            live_dash.collector._record_log_events(
                "host1",
                "syslog",
                [
                    LogEvent(ts=ts, fields={"message": "a"}),
                    LogEvent(ts=ts, fields={"message": "b"}),
                ],
            )
        )
        payload: dict[str, Any] | None = None
        while payload is None:
            line = resp.readline().decode()
            assert line, "SSE stream closed before a log_event message arrived"
            if line.startswith("data:"):
                candidate = json.loads(line[len("data:") :])
                if candidate["type"] == "log_event":
                    payload = candidate
    finally:
        conn.close()
    assert set(payload) == SSE_LOG_EVENT_KEYS
    assert payload["host"] == "host1"
    assert payload["tab"] == "syslog"
    assert [r["fields"]["message"] for r in payload["rows"]] == ["a", "b"]
    assert all(set(r) == {"ts", "fields"} for r in payload["rows"])
```

(Import `timezone` and `LogEvent` — `from otto.monitor.parsers import LogEvent` — in the file's import block.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_collector_log_events.py tests/unit/monitor/test_collector_db.py tests/unit/monitor/test_monitor_import_export.py -x -q`
Expected: FAIL (`write_log_event` missing / no SSE publish / KeyError `log_events`).

- [ ] **Step 3: Implement**

`db.py` — add `import json` to the imports; append to `_SCHEMA` (inside the same string):

```sql
CREATE TABLE IF NOT EXISTS log_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL,
    host      TEXT    NOT NULL DEFAULT '',
    tab       TEXT    NOT NULL DEFAULT '',
    fields    TEXT    NOT NULL DEFAULT '{}'
);
```

(`CREATE TABLE IF NOT EXISTS` in the executescript is the migration for pre-existing DBs — same mechanism the other tables use.) Add after `write_point`:

```python
    async def write_log_event(
        self, ts: datetime, host: str, tab: str, fields: dict[str, str]
    ) -> None:
        """Insert one log-event row (fields JSON-encoded). No-op if not open."""
        if not self._conn:
            return
        await self._conn.execute(
            "INSERT INTO log_events (ts, host, tab, fields) VALUES (?, ?, ?, ?)",
            (ts.isoformat(), host, tab, json.dumps(fields)),
        )
        await self._conn.commit()
```

`models/monitor.py` — after `EventRecord`:

```python
class LogEventRecord(RowModel):
    """One ``log_events`` row at the JSON / SQLite import-export boundary.

    Mirrors the parser-emitted ``LogEvent`` plus the host/tab the collector
    attaches. The JSON ``--file`` format spells the time key ``timestamp``;
    the SQLite column is ``ts`` (its ``fields`` column is JSON-decoded by the
    loader before validation).
    """

    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host: str = ""
    tab: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
```

`collector.py` — extend `_record_log_events` (replacing Task 1's body) and add `get_log_events`:

```python
    async def _record_log_events(
        self, host_name: str, tab: str, events: "list[LogEvent]"
    ) -> None:
        """Store, persist, and publish one tick's log-event rows.

        SSE is batched: one ``log_event`` message per (host, parser, tick),
        not one per row — a ``tail -n 200`` backfill is one frame.
        """
        for ev in events:
            self._store.append_log_event(host_name, tab, ev)
            if self._db:
                await self._db.write_log_event(ev.ts, host_name, tab, ev.fields)
        self._publish(
            {
                "type": "log_event",
                "host": host_name,
                "tab": tab,
                "rows": [{"ts": ev.ts.isoformat(), "fields": dict(ev.fields)} for ev in events],
            }
        )
```

Next to `get_events`:

```python
    def get_log_events(self) -> "list[dict[str, Any]]":
        """JSON-safe log-event rows for ``/api/data`` and export.

        Shape per row: ``{"timestamp", "host", "tab", "fields"}`` —
        the ``LogEventRecord`` spelling, insertion-ordered per (host, tab) ring.
        """
        return [
            {"timestamp": ev.ts.isoformat(), "host": host, "tab": tab, "fields": dict(ev.fields)}
            for host, tab, ev in self._store.snapshot_log_events()
        ]
```

`history.py` — extend the models import with `LogEventRecord`, add `from .parsers import LogEvent`. In `load_json_into`, after the events loop:

```python
    for row in data.get("log_events", []):
        try:
            rec = LogEventRecord.model_validate(row)
        except ValidationError:
            continue
        store.append_log_event(rec.host, rec.tab, LogEvent(ts=rec.timestamp, fields=rec.fields))
```

In `load_sqlite_into`, after the events loop (inside the `async with` block):

```python
        # Older DBs predate the log_events table; probe before selecting.
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='log_events'"
        )
        if await cursor.fetchone() is not None:
            async for row in await conn.execute(
                "SELECT ts, host, tab, fields FROM log_events ORDER BY ts"
            ):
                data_row = dict(row)
                try:
                    data_row["fields"] = json.loads(data_row.get("fields") or "{}")
                    rec = LogEventRecord.model_validate(data_row)
                except (ValidationError, json.JSONDecodeError):
                    continue
                store.append_log_event(
                    rec.host, rec.tab, LogEvent(ts=rec.timestamp, fields=rec.fields)
                )
```

In `to_json`, add to the dumped dict (after `"events"`):

```python
            "log_events": [
                LogEventRecord(
                    timestamp=ev.ts, host=host, tab=tab, fields=dict(ev.fields)
                ).model_dump(mode="json")
                for host, tab, ev in store.snapshot_log_events()
            ],
```

`server.py` — in the `data()` route payload, after `"chart_map"`:

```python
            "log_events": collector.get_log_events(),
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/monitor/ tests/e2e/monitor/dashboard/test_harness.py -x -q`
Expected: PASS (including pre-existing import/export tests — old files without `log_events` keys must still load: `data.get("log_events", [])` — and the pre-existing wire pins, which only pass once `DATA_KEYS` and the route agree).

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/db.py src/otto/models/monitor.py src/otto/monitor/collector.py src/otto/monitor/history.py src/otto/monitor/server.py tests/unit/monitor/test_collector_log_events.py tests/unit/monitor/test_collector_db.py tests/unit/monitor/test_monitor_import_export.py tests/e2e/monitor/dashboard/test_harness.py
git commit -m "feat(monitor): log-event persistence and wire — DB table, batched SSE, /api/data

log_events SQLite table (JSON-encoded fields, IF-NOT-EXISTS migration),
LogEventRecord at the import/export boundary (JSON + SQLite both
directions), one batched log_event SSE frame per parser per tick, and
/api/data serving the ring snapshot.

Assisted-by: Claude Fable 5"
```

---

### Task 4: `RegexLogEventParser`

**Files:**
- Modify: `src/otto/monitor/log_sourced.py`
- Test: `tests/unit/monitor/test_log_sourced.py`

**Interfaces:**
- Consumes: Task 2's `HighWaterMark`, `parse_timestamp`; Task 1's `LogEvent`/`TickResult`/`table_columns`.
- Produces: `RegexLogEventParser(command, pattern, *, tab, tab_label, ts_group="ts", ts_format="iso", interval=None)` — sets `table_columns` to the pattern's non-timestamp named groups in pattern order; `chart = tab_label` (names the parser in health warnings only). Tasks 5 and 8 construct it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/monitor/test_log_sourced.py` (extend the file's imports with `RegexLogEventParser` and, from `otto.monitor.parsers`, `LogEvent`):

```python
SYSLOG_PATTERN = r"^(?P<ts>\S+) (?P<loghost>\S+) (?P<proc>[^:\[]+)(?:\[\d+\])?: (?P<message>.*)$"


def _syslog() -> RegexLogEventParser:
    return RegexLogEventParser(
        "tail -n 200 /var/log/syslog",
        SYSLOG_PATTERN,
        tab="syslog",
        tab_label="Syslog",
    )


class TestRegexLogEventParser:
    def test_declares_table_columns_in_pattern_order(self) -> None:
        p = _syslog()
        assert p.table_columns == ["loghost", "proc", "message"]
        assert p.tab == "syslog"
        assert p.chart == "Syslog"
        assert p.parse("anything", ctx=ParseContext()) == {}

    def test_named_groups_become_fields(self) -> None:
        line = "2026-07-04T12:00:00+00:00 vm1 sshd[142]: session opened\n"
        tick = _syslog().parse_tick(line, ctx=ParseContext())
        assert tick.samples == []
        assert tick.events == [
            LogEvent(
                ts=T0,
                fields={"loghost": "vm1", "proc": "sshd", "message": "session opened"},
            )
        ]

    def test_nonmatching_lines_skipped(self) -> None:
        out = "not a syslog line\n2026-07-04T12:00:00Z vm1 cron: job ran\n"
        tick = _syslog().parse_tick(out, ctx=ParseContext())
        assert len(tick.events) == 1
        assert tick.events[0].fields["proc"] == "cron"

    def test_events_sorted_ascending_and_hwm_dedups_rereads(self) -> None:
        p = _syslog()
        out = (
            "2026-07-04T12:00:05Z vm1 a: second\n"
            "2026-07-04T12:00:00Z vm1 a: first\n"
        )
        first = p.parse_tick(out, ctx=ParseContext()).events
        assert [e.fields["message"] for e in first] == ["first", "second"]
        assert p.parse_tick(out, ctx=ParseContext()).events == []
        grown = out + "2026-07-04T12:00:10Z vm1 a: third\n"
        assert [e.fields["message"] for e in p.parse_tick(grown, ctx=ParseContext()).events] == [
            "third"
        ]

    def test_strptime_ts_format(self) -> None:
        p = RegexLogEventParser(
            "tail -n 200 /var/log/messages",
            r"^(?P<ts>\w+ +\d+ [\d:]+) (?P<message>.*)$",
            tab="messages",
            tab_label="Messages",
            ts_format="%b %d %H:%M:%S",
        )
        tick = p.parse_tick("Jul  4 12:00:00 classic syslog body\n", ctx=ParseContext())
        assert len(tick.events) == 1
        assert tick.events[0].ts.year == datetime.now(tz=timezone.utc).year

    def test_unparsable_timestamp_skips_line(self) -> None:
        tick = _syslog().parse_tick("garbage vm1 a: hi\n", ctx=ParseContext())
        assert tick.events == []

    def test_ts_group_must_exist(self) -> None:
        with pytest.raises(ValueError, match="no named group 'ts'"):
            RegexLogEventParser("tail x", r"(?P<message>.*)", tab="t", tab_label="T")

    def test_needs_a_column_besides_the_timestamp(self) -> None:
        with pytest.raises(ValueError, match="at least one named group besides"):
            RegexLogEventParser("tail x", r"(?P<ts>\S+)", tab="t", tab_label="T")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_log_sourced.py -k Regex -x -q`
Expected: FAIL — `ImportError: RegexLogEventParser`.

- [ ] **Step 3: Implement**

Append to `log_sourced.py`:

```python
class RegexLogEventParser(MetricParser):
    """Columnar log-event rows from a log file read over the shell.

    Each line matching *pattern* becomes a table row: the named groups
    become the columns (``table_columns``, in pattern order), except
    *ts_group*, which carries the row timestamp. Non-matching lines are
    skipped — a wrong pattern therefore produces zero rows ever, which the
    collector's silent-parser warning surfaces by tick 3. Re-reads of the
    ``tail -n N`` window dedup on the row-timestamp high-water mark, so an
    append-only log of any size fits: the window bounds every read, the
    mark discards overlap.

    This parser contributes a ``kind="table"`` dashboard tab (its own —
    table parsers must not share a tab id with chart parsers) and no chart.

    Args:
        command: Shell command printing the log window (e.g. ``tail -n 200 /var/log/syslog``).
        pattern: Line regex with named groups; ``search``\\ ed per line.
        tab: Dashboard tab id for the table.
        tab_label: Human-readable tab button label.
        ts_group: Name of the group holding the timestamp.
        ts_format: ``"iso"``, ``"epoch"``, or a ``strptime`` format (see :func:`parse_timestamp`).
        interval: Poll cadence override in seconds.
    """

    def __init__(
        self,
        command: str,
        pattern: "str | re.Pattern[str]",
        *,
        tab: str,
        tab_label: str,
        ts_group: str = "ts",
        ts_format: str = "iso",
        interval: float | None = None,
    ) -> None:
        self.command = command
        self._pattern = re.compile(pattern) if isinstance(pattern, str) else pattern
        if ts_group not in self._pattern.groupindex:
            raise ValueError(f"pattern has no named group {ts_group!r} for the timestamp")
        self.table_columns = [g for g in self._pattern.groupindex if g != ts_group]
        if not self.table_columns:
            raise ValueError("pattern needs at least one named group besides the timestamp")
        self._ts_group = ts_group
        self._ts_format = ts_format
        self.tab = tab
        self.tab_label = tab_label
        self.chart = tab_label  # never charted; names the parser in health warnings
        self.y_title = ""
        self.unit = ""
        self.interval = interval
        self._hwm = HighWaterMark()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        """Unused — this parser produces log events via :meth:`parse_tick`."""
        return {}

    @override
    def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
        rows: list[tuple[datetime, dict[str, str]]] = []
        for line in output.splitlines():
            m = self._pattern.search(line)
            if m is None:
                continue
            ts = parse_timestamp(m.group(self._ts_group) or "", self._ts_format)
            if ts is None:
                continue
            rows.append((ts, {g: m.group(g) or "" for g in self.table_columns}))
        fresh = self._hwm.advance(rows)
        return TickResult(
            samples=[], events=[LogEvent(ts=ts, fields=fields) for ts, fields in fresh]
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/monitor/test_log_sourced.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/log_sourced.py tests/unit/monitor/test_log_sourced.py
git commit -m "feat(monitor): RegexLogEventParser — named groups become table columns

Line regex → LogEvent rows: one group carries the timestamp
(iso/epoch/strptime with the classic-syslog missing-year fix), the rest
declare the static table schema; tail-window re-reads dedup on the
high-water mark.

Assisted-by: Claude Fable 5"
```

---

### Task 5: `TabSpec.kind`/`columns` wire bump + table tabs in `/api/meta` + TS regen + `FakeCollector` helpers

**Files:**
- Modify: `src/otto/models/monitor.py` (`TabSpec`), `src/otto/monitor/collector.py` (`get_meta_model`), `tests/_fixtures/_fake_collector.py`
- Regenerate + commit: `web/src/api/types.gen.ts` (via `scripts/gen_web_types.sh`; needs `cd web && npm ci` done once)
- Test: `tests/unit/monitor/test_meta_models.py`, `tests/e2e/monitor/dashboard/test_harness.py` (META_TAB_KEYS pin)

**Interfaces:**
- Consumes: Task 1's `table_columns`; Task 4's `RegexLogEventParser` (test subject); Task 3's `_record_log_events` (FakeCollector helper).
- Produces: `TabSpec.kind: Literal["charts", "table"] = "charts"`, `TabSpec.columns: list[str] | None = None`;
  `get_meta_model()` emits a `kind="table"` tab (with `columns`, `metrics=[]`) per table parser, no `ChartSpec` for it, and raises `ValueError` on chart/table tab-id collisions (both orders);
  `FakeCollector(force_live=True, extra_parsers: Sequence[MetricParser] | None = None)`;
  `FakeCollector.push_log_events(host: str, *, tab: str, rows: list[tuple[datetime, dict[str, str]]]) -> None` (async, one batched SSE frame).
  Tasks 6–8 depend on the regenerated TS types and these helpers.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/monitor/test_meta_models.py` — update the existing pin and add table coverage:

```python
def test_tab_spec_wire_shape() -> None:
    tab = TabSpec(id="cpu", label="CPU", metrics=["CPU", "Load"])
    assert set(tab.model_dump(mode="json")) == {"id", "label", "metrics", "kind", "columns"}
    assert tab.kind == "charts"
    assert tab.columns is None


def _table_parser() -> "RegexLogEventParser":
    return RegexLogEventParser(
        "tail -n 200 /var/log/syslog",
        r"^(?P<ts>\S+) (?P<proc>\S+): (?P<message>.*)$",
        tab="syslog",
        tab_label="Syslog",
    )


def test_meta_table_parser_contributes_table_tab_and_no_chart_spec() -> None:
    fake = FakeCollector(extra_parsers=[_table_parser()])
    meta = fake.get_meta_model()
    tab = next(t for t in meta.tabs if t.id == "syslog")
    assert tab.kind == "table"
    assert tab.columns == ["proc", "message"]
    assert tab.metrics == []
    assert all(m.command != "tail -n 200 /var/log/syslog" for m in meta.metrics)
    # Chart tabs keep the default kind.
    assert next(t for t in meta.tabs if t.id == "cpu").kind == "charts"


def test_meta_table_tab_id_collision_raises_both_orders() -> None:
    class _ChartOnSyslogTab(MetricParser):
        y_title = ""
        unit = ""
        command = "echo 1"
        tab = "syslog"
        tab_label = "Syslog"
        chart = "Clash"

        def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
            return {}

    with pytest.raises(ValueError, match="syslog"):
        FakeCollector(extra_parsers=[_table_parser(), _ChartOnSyslogTab()]).get_meta_model()
    with pytest.raises(ValueError, match="syslog"):
        FakeCollector(extra_parsers=[_ChartOnSyslogTab(), _table_parser()]).get_meta_model()


@pytest.mark.asyncio
async def test_fake_collector_push_log_events_uses_production_path() -> None:
    fake = FakeCollector(extra_parsers=[_table_parser()])
    q = fake.subscribe()
    ts = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    await fake.push_log_events("host1", tab="syslog", rows=[(ts, {"proc": "sshd", "message": "hi"})])
    assert q.get_nowait()["type"] == "log_event"
    assert fake.get_log_events()[0]["host"] == "host1"
```

(Add the needed imports — `datetime`/`timezone`, `RegexLogEventParser` from `otto.monitor.log_sourced` — at the top of the file.)

In `tests/e2e/monitor/dashboard/test_harness.py`, update the tab pin:

```python
META_TAB_KEYS = {"id", "label", "metrics", "kind", "columns"}
# "kind"/"columns" added in Phase 3 Plan B (table tabs) — deliberate contract evolution.
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_meta_models.py tests/e2e/monitor/dashboard/test_harness.py -x -q`
Expected: FAIL (`kind` not a TabSpec field; `extra_parsers` unknown; `META_TAB_KEYS` mismatch).

- [ ] **Step 3: Implement the model bump**

`models/monitor.py` — add `Literal` to the `typing` import; extend `TabSpec`:

```python
class TabSpec(OttoModel):
    """One dashboard tab descriptor served by ``/api/meta``.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2. ``kind="table"`` tabs render an
    event table (schema in ``columns``) instead of charts, and carry
    ``metrics=[]``.
    """

    id: str
    label: str
    metrics: list[str]
    kind: Literal["charts", "table"] = "charts"
    columns: list[str] | None = None
```

- [ ] **Step 4: Implement the `get_meta_model` table branch**

In `collector.py`, replace the tab-building loop and the `metrics` comprehension:

```python
        # Build ordered tabs list from all views (shell parsers + SNMP
        # descriptors), preserving first-encountered tab order. A table
        # parser (table_columns set) contributes a kind="table" tab and no
        # ChartSpec; tables own their tab outright, so an id collision with
        # any other view is a config bug worth failing loudly on.
        tabs: dict[str, TabSpec] = {}
        for v in self._views:
            tab_id = getattr(v, "tab", "metrics")
            tab_label = getattr(v, "tab_label", "Metrics")
            table_columns = getattr(v, "table_columns", None)
            if table_columns is not None:
                if tab_id in tabs:
                    raise ValueError(
                        f"table parser tab {tab_id!r} collides with another tab; "
                        "table parsers must declare their own tab id"
                    )
                tabs[tab_id] = TabSpec(
                    id=tab_id,
                    label=tab_label,
                    metrics=[],
                    kind="table",
                    columns=list(table_columns),
                )
                continue
            if tab_id not in tabs:
                tabs[tab_id] = TabSpec(id=tab_id, label=tab_label, metrics=[])
            elif tabs[tab_id].kind == "table":
                raise ValueError(
                    f"chart parser tab {tab_id!r} collides with a table tab; "
                    "table parsers must have their own tab id"
                )
            tabs[tab_id].metrics.append(v.chart)

        metrics = [
            ChartSpec(
                label=v.chart,
                y_title=v.y_title,
                unit=v.unit,
                # Shell views key off the command; SNMP views off the OID.
                command=getattr(v, "command", None) or getattr(v, "oid", ""),
                chart=v.chart,
                interval=getattr(v, "interval", None),
            )
            for v in self._views
            if getattr(v, "table_columns", None) is None
        ]
```

- [ ] **Step 5: Implement the `FakeCollector` helpers**

`tests/_fixtures/_fake_collector.py` — extend imports (`Sequence` from `collections.abc`, `LogEvent`, `MetricParser`, `default_catalog` from `otto.monitor.parsers`); replace `__init__` and add the helper:

```python
    def __init__(
        self,
        *,
        force_live: bool = True,
        extra_parsers: "Sequence[MetricParser] | None" = None,
    ) -> None:
        parsers = [*default_catalog().values(), *extra_parsers] if extra_parsers else None
        super().__init__(hosts=[], parsers=parsers)
        self._force_live = force_live

    async def push_log_events(
        self, host: str, *, tab: str, rows: "list[tuple[datetime, dict[str, str]]]"
    ) -> None:
        """Record a batch of log-event rows exactly as a live tick would (ring + one SSE frame)."""
        await self._record_log_events(
            host, tab, [LogEvent(ts=ts, fields=fields) for ts, fields in rows]
        )
```

- [ ] **Step 6: Regenerate the TS types (wire-schema bump)**

```bash
cd web && npm ci && cd ..          # once per fresh worktree
scripts/gen_web_types.sh
git diff web/src/api/types.gen.ts  # verify: TabSpec gains kind?/columns?
```

Expected diff: the generated `TabSpec` interface grows `kind?: ...` and `columns?: ...` members (plus their named type aliases). Nothing else changes.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/unit/monitor/ tests/unit/models/ tests/e2e/monitor/dashboard/test_harness.py -x -q`
Expected: PASS — including `tests/unit/models/test_jsonschema.py` (the `monitor-meta` document regenerates transparently) and every wire pin. Optionally switch Task 3's `_record_log_events` scripting in the harness pins to the new public `push_log_events` helper.

- [ ] **Step 8: Commit**

```bash
git add src/otto/models/monitor.py src/otto/monitor/collector.py tests/_fixtures/_fake_collector.py web/src/api/types.gen.ts tests/unit/monitor/test_meta_models.py tests/e2e/monitor/dashboard/test_harness.py
git commit -m "feat(monitor)!: TabSpec kind/columns — table tabs on the /api/meta wire

Wire-schema bump (backward-compatible defaults): a parser declaring
table_columns contributes a kind=\"table\" TabSpec with its column schema
and no ChartSpec; chart/table tab-id collisions raise. TS types
regenerated through the Phase 2 drift gate; FakeCollector grows
extra_parsers + push_log_events for dashboard scripting.

Assisted-by: Claude Fable 5"
```

---

### Task 6: Frontend data layer — `logevents.ts`, store slice, SSE dispatch, `DataPayload`

**Files:**
- Create: `web/src/logevents.ts`
- Modify: `web/src/api/client.ts`, `web/src/store.ts`, `web/src/api/sse.ts`
- Test: `web/src/__tests__/logevents.test.ts` (create), `web/src/__tests__/store.test.ts` (modify)

**Interfaces:**
- Consumes: Task 3's wire shapes (verbatim), Task 5's regenerated types.
- Produces: `LogEventRow { timestamp, host, tab, fields }` in `client.ts`; `DataPayload.log_events: LogEventRow[]`;
  `MAX_TABLE_ROWS = 500`, `logKey(host, tab)`, `appendRows(existing, host, tab, rows)`, `groupRowsFromData(rows)`, `visibleRows(rows, filter)` in `logevents.ts`;
  store state `logEvents: Record<string, LogEventRow[]>` + action `logEventMsg(msg: LogEventMessage)`; `LogEventMessage` exported from `store.ts`. Task 7 renders from these.

- [ ] **Step 1: Write the failing tests**

Create `web/src/__tests__/logevents.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import type { LogEventRow } from "../api/client";
import { appendRows, groupRowsFromData, logKey, MAX_TABLE_ROWS, visibleRows } from "../logevents";

function row(n: number, host = "host1", tab = "syslog"): LogEventRow {
  return {
    timestamp: `2026-07-04T12:00:${String(n % 60).padStart(2, "0")}+00:00`,
    host,
    tab,
    fields: { proc: "sshd", message: `row ${n}` },
  };
}

describe("logKey", () => {
  it("joins host and tab", () => {
    expect(logKey("host1", "syslog")).toBe("host1/syslog");
  });
});

describe("appendRows", () => {
  it("appends under the (host, tab) key without touching other keys", () => {
    const first = appendRows({}, "host1", "syslog", [row(1)]);
    const second = appendRows(first, "host2", "syslog", [row(2, "host2")]);
    expect(second["host1/syslog"]).toHaveLength(1);
    expect(second["host2/syslog"]).toHaveLength(1);
  });

  it("caps at MAX_TABLE_ROWS keeping the newest", () => {
    const many = Array.from({ length: MAX_TABLE_ROWS + 20 }, (_, i) => row(i));
    const out = appendRows({}, "host1", "syslog", many);
    const kept = out["host1/syslog"];
    expect(kept).toHaveLength(MAX_TABLE_ROWS);
    expect(kept[kept.length - 1].fields.message).toBe(`row ${MAX_TABLE_ROWS + 19}`);
    expect(kept[0].fields.message).toBe("row 20");
  });

  it("returns the same object for an empty batch", () => {
    const existing = { "host1/syslog": [row(1)] };
    expect(appendRows(existing, "host1", "syslog", [])).toBe(existing);
  });
});

describe("groupRowsFromData", () => {
  it("groups a /api/data snapshot by (host, tab) and caps each", () => {
    const rows = [row(1), row(2, "host2"), row(3)];
    const grouped = groupRowsFromData(rows);
    expect(grouped["host1/syslog"].map((r) => r.fields.message)).toEqual(["row 1", "row 3"]);
    expect(grouped["host2/syslog"]).toHaveLength(1);
  });
});

describe("visibleRows", () => {
  it("returns newest-first", () => {
    expect(visibleRows([row(1), row(2)], "").map((r) => r.fields.message)).toEqual([
      "row 2",
      "row 1",
    ]);
  });

  it("filters case-insensitively across timestamp and field values", () => {
    const rows = [row(1), { ...row(2), fields: { proc: "cron", message: "JOB ran" } }];
    expect(visibleRows(rows, "job")).toHaveLength(1);
    expect(visibleRows(rows, "sshd")).toHaveLength(1);
    expect(visibleRows(rows, "12:00:01")).toHaveLength(1);
    expect(visibleRows(rows, "nomatch")).toHaveLength(0);
  });
});
```

In `web/src/__tests__/store.test.ts`, add (following the file's existing reducer-test style, resetting store state as its other tests do):

```ts
describe("logEventMsg", () => {
  it("appends batch rows under host/tab, tagging each row", () => {
    const { actions } = useMonitorStore.getState();
    actions.logEventMsg({
      type: "log_event",
      host: "host1",
      tab: "syslog",
      rows: [{ ts: "2026-07-04T12:00:00+00:00", fields: { message: "hi" } }],
    });
    const rows = useMonitorStore.getState().logEvents["host1/syslog"];
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      timestamp: "2026-07-04T12:00:00+00:00",
      host: "host1",
      tab: "syslog",
      fields: { message: "hi" },
    });
  });
});

describe("applyData with log_events", () => {
  it("hydrates the logEvents slice from the snapshot", () => {
    const { actions } = useMonitorStore.getState();
    actions.applyData({
      series: {},
      events: [],
      chart_map: {},
      log_events: [
        { timestamp: "2026-07-04T12:00:00+00:00", host: "h", tab: "t", fields: { m: "x" } },
      ],
    });
    expect(useMonitorStore.getState().logEvents["h/t"]).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npm run test`
Expected: FAIL — `../logevents` doesn't exist; `logEventMsg` not an action; `log_events` not in `DataPayload`.

- [ ] **Step 3: Implement**

`web/src/api/client.ts` — add after `MonitorEvent`:

```ts
/** One log-event row — mirrors `LogEventRecord` / `collector.get_log_events()`. */
export interface LogEventRow {
  timestamp: string;
  host: string;
  tab: string;
  fields: Record<string, string>;
}
```

and extend `DataPayload`:

```ts
export interface DataPayload {
  series: Record<string, Point[]>;
  events: MonitorEvent[];
  chart_map: Record<string, string>;
  log_events: LogEventRow[];
}
```

Create `web/src/logevents.ts`:

```ts
// Pure log-event table bookkeeping (no DOM, no zustand) — vitest-able in
// isolation, mirroring grouping.ts's role for charts. Rows live per
// (host, tab) key; the backend ring keeps 1000 per key and the DB keeps
// everything, so a 500-row client cap only trims what the table would
// never display anyway.
import type { LogEventRow } from "./api/client";

/** Client store + display cap: the newest rows kept per (host, tab). */
export const MAX_TABLE_ROWS = 500;

/** Store key for one host's one table tab. */
export function logKey(host: string, tab: string): string {
  return `${host}/${tab}`;
}

/** Append a batch for one (host, tab), keeping only the newest MAX_TABLE_ROWS. */
export function appendRows(
  existing: Record<string, LogEventRow[]>,
  host: string,
  tab: string,
  rows: LogEventRow[],
): Record<string, LogEventRow[]> {
  if (rows.length === 0) return existing;
  const key = logKey(host, tab);
  const merged = [...(existing[key] ?? []), ...rows];
  return { ...existing, [key]: merged.slice(-MAX_TABLE_ROWS) };
}

/** Group a /api/data `log_events` snapshot into the per-(host, tab) map. */
export function groupRowsFromData(rows: LogEventRow[]): Record<string, LogEventRow[]> {
  const out: Record<string, LogEventRow[]> = {};
  for (const row of rows) {
    (out[logKey(row.host, row.tab)] ??= []).push(row);
  }
  for (const key of Object.keys(out)) {
    out[key] = out[key].slice(-MAX_TABLE_ROWS);
  }
  return out;
}

/** Newest-first rows whose timestamp or field values contain `filter` (case-insensitive). */
export function visibleRows(rows: LogEventRow[], filter: string): LogEventRow[] {
  const needle = filter.trim().toLowerCase();
  const matched = needle
    ? rows.filter((r) =>
        [r.timestamp, ...Object.values(r.fields)].some((v) => v.toLowerCase().includes(needle)),
      )
    : rows;
  return matched.slice().reverse();
}
```

`web/src/store.ts`:
- Import `LogEventRow` (type) from `./api/client` and `appendRows`, `groupRowsFromData` from `./logevents`.
- Add next to `MetricMessage`:

```ts
/** The `/api/stream` "log_event" message — mirrors collector.py's `_record_log_events()` batch. */
export interface LogEventMessage {
  type: "log_event";
  host: string;
  tab: string;
  rows: { ts: string; fields: Record<string, string> }[];
}
```

- `MonitorActions` gains `logEventMsg: (msg: LogEventMessage) => void;`
- `MonitorState` gains:

```ts
  /** `"host/tab"` -> that table's rows (newest last; capped at MAX_TABLE_ROWS). */
  logEvents: Record<string, LogEventRow[]>;
```

- Initial state gains `logEvents: {},`
- `applyData` becomes:

```ts
    applyData: (data) =>
      set({
        series: data.series,
        events: data.events,
        chartMap: data.chart_map,
        logEvents: groupRowsFromData(data.log_events ?? []),
      }),
```

- New reducer after `metricMsg` (note: like `metricMsg`, this appends unconditionally — pause freezes charts, never data; the table is data, so v1 keeps it live while paused — a deliberate, documented choice):

```ts
    // Batched log_event frames append under their (host, tab) key. Not
    // pause-gated: pause freezes chart *rendering*; the table renders
    // straight from this slice, and v1 deliberately keeps it live.
    logEventMsg: (msg) =>
      set((state) => ({
        logEvents: appendRows(
          state.logEvents,
          msg.host,
          msg.tab,
          msg.rows.map((r) => ({
            timestamp: r.ts,
            host: msg.host,
            tab: msg.tab,
            fields: r.fields,
          })),
        ),
      })),
```

`web/src/api/sse.ts` — import `LogEventMessage` type from `../store`, extend the union and switch:

```ts
type StreamMessage =
  | MetricMessage
  | LogEventMessage
  | (MonitorEvent & { type: "event" })
  | (MonitorEvent & { type: "event_updated" })
  | EventDeletedMessage;
```

```ts
      case "log_event":
        actions.logEventMsg(msg);
        break;
```

- [ ] **Step 4: Run the tests + typecheck**

Run: `cd web && npm run test && npx tsc --noEmit`
Expected: PASS. `log_events` is a **required** `DataPayload` member (it mirrors the server, which always sends it), so any pre-existing test fixture passing an object literal to `applyData` must gain `log_events: []` — update those literals rather than weakening the type to optional. The reducer's `?? []` stays as runtime defensiveness only.

Note on spec coverage: "vitest covers … `log_event` SSE handling" is satisfied at the unit level by the `logEventMsg` reducer test (the message shape IS the SSE payload); the dispatch `case` itself is two lines and is exercised end-to-end by Task 8's live-append browser pin. Only add a `startSse` dispatch test if an existing `EventSource`-mocking pattern already exists in `web/src/__tests__/` — do not invent one for this.

- [ ] **Step 5: Commit**

```bash
git add web/src/logevents.ts web/src/api/client.ts web/src/store.ts web/src/api/sse.ts web/src/__tests__/logevents.test.ts web/src/__tests__/store.test.ts
git commit -m "feat(web): log-event data layer — store slice, SSE dispatch, /api/data hydration

Pure logevents.ts helpers (per-host/tab keying, 500-row cap, newest-first
substring filter) + zustand logEvents slice fed by the batched log_event
SSE kind and the /api/data snapshot.

Assisted-by: Claude Fable 5"
```

---

### Task 7: `EventTable` component + tab wiring + CSS

**Files:**
- Create: `web/src/components/EventTable.tsx`
- Modify: `web/src/components/TabBar.tsx`, `web/src/components/ChartGrid.tsx`, `web/src/dashboard.css`
- Test: `web/src/__tests__/eventtable.test.tsx` (create)

**Interfaces:**
- Consumes: Task 6's slice/helpers; Task 5's `TabSpec.kind`/`columns` TS types.
- Produces: `EventTable({ tab }: { tab: TabSpec })` rendering `.event-table` with `.event-table-filter` input and a `<table>`; table tabs visible in `TabBar` and rendered as `#tab-<id>` panels by `ChartGrid`. Task 8 pins these selectors.

- [ ] **Step 1: Write the failing test**

Create `web/src/__tests__/eventtable.test.tsx` (follow `app.test.tsx`'s testing-library + store-reset conventions):

```tsx
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TabSpec } from "../api/types.gen";
import EventTable from "../components/EventTable";
import { useMonitorStore } from "../store";

const TAB: TabSpec = { id: "syslog", label: "Syslog", metrics: [], kind: "table", columns: ["proc", "message"] };

function seed(): void {
  useMonitorStore.setState({
    selectedHost: "host1",
    logEvents: {
      "host1/syslog": [
        { timestamp: "2026-07-04T12:00:00+00:00", host: "host1", tab: "syslog", fields: { proc: "sshd", message: "older" } },
        { timestamp: "2026-07-04T12:00:05+00:00", host: "host1", tab: "syslog", fields: { proc: "cron", message: "newer" } },
      ],
    },
  });
}

afterEach(cleanup);

describe("EventTable", () => {
  it("renders declared columns and newest-first rows for the selected host", () => {
    seed();
    render(<EventTable tab={TAB} />);
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual(["Time", "proc", "message"]);
    const cells = screen.getAllByRole("row").slice(1).map((tr) => tr.textContent);
    expect(cells[0]).toContain("newer");
    expect(cells[1]).toContain("older");
    expect(cells[0]).toContain("12:00:05"); // UTC time cell
  });

  it("substring filter narrows rows", () => {
    seed();
    render(<EventTable tab={TAB} />);
    fireEvent.change(screen.getByPlaceholderText("Filter rows…"), { target: { value: "sshd" } });
    expect(screen.getAllByRole("row")).toHaveLength(2); // header + 1 match
  });

  it("shows nothing for a host without rows", () => {
    seed();
    useMonitorStore.setState({ selectedHost: "host2" });
    render(<EventTable tab={TAB} />);
    expect(screen.getAllByRole("row")).toHaveLength(1); // header only
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npm run test`
Expected: FAIL — `EventTable` doesn't exist.

- [ ] **Step 3: Implement the component**

Create `web/src/components/EventTable.tsx`:

```tsx
// `kind="table"` tab panel: newest-first log-event rows for the selected
// host, client-side substring filter, capped at MAX_TABLE_ROWS by the
// store slice. v1 by design: no sorting, pagination, or virtualization
// (Phase 4 UX territory), and rows keep flowing while charts are paused.
import { useState } from "react";

import type { LogEventRow } from "../api/client";
import type { TabSpec } from "../api/types.gen";
import { logKey, visibleRows } from "../logevents";
import { useMonitorStore } from "../store";

// Stable [] fallback — see TabBar.tsx's EMPTY_TABS comment (React #185).
const EMPTY_ROWS: LogEventRow[] = [];

/** "2026-07-04T12:00:03+00:00" → "12:00:03" (UTC — deterministic for pins). */
function timeCell(timestamp: string): string {
  return timestamp.slice(11, 19);
}

function EventTable({ tab }: { tab: TabSpec }) {
  const selectedHost = useMonitorStore((s) => s.selectedHost);
  const rows = useMonitorStore(
    (s) => s.logEvents[logKey(selectedHost ?? "", tab.id)] ?? EMPTY_ROWS,
  );
  const [filter, setFilter] = useState("");
  const columns = tab.columns ?? [];
  const visible = visibleRows(rows, filter);

  return (
    <div className="event-table">
      <input
        type="search"
        className="event-table-filter"
        placeholder="Filter rows…"
        value={filter}
        onChange={(e) => {
          setFilter(e.target.value);
        }}
      />
      <table>
        <thead>
          <tr>
            <th>Time</th>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map((row, i) => (
            // Index keys are safe: the list is always re-derived whole from
            // the store snapshot (no per-row identity to preserve).
            // eslint-disable-next-line react/no-array-index-key
            <tr key={i}>
              <td className="event-table-time">{timeCell(row.timestamp)}</td>
              {columns.map((c) => (
                <td key={c}>{row.fields[c] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default EventTable;
```

(If the repo's eslint config doesn't use that rule name, drop the disable line rather than inventing one.)

- [ ] **Step 4: Wire the tabs**

`web/src/components/TabBar.tsx` — table tabs have `metrics: []`, so the resolved-metrics visibility filter must not hide them:

```ts
  const visibleTabs = tabs.filter(
    (tab) => tab.kind === "table" || hasResolvedMetrics(tab, metrics),
  );
```

`web/src/components/ChartGrid.tsx`:
- Import `EventTable` (`import EventTable from "./EventTable";`).
- In the initial-build effect, fall back to a table tab when no chart tab produced groups (a table-only catalog still auto-activates):

```ts
    const initialTab = firstTabId ?? meta.tabs.find((t) => t.kind === "table")?.id;
    if (initialTab) selectTab(initialTab);
```

(replacing the existing `if (firstTabId) selectTab(firstTabId);`)
- After the chart `visibleTabs` computation, add table panels to the returned fragment (table tabs never appear in `visibleTabs` — they have no chart groups):

```tsx
      {meta.tabs
        .filter((t) => t.kind === "table")
        .map((tab) => (
          <div
            key={tab.id}
            id={`tab-${tab.id}`}
            className={activeTab === tab.id ? "tab-panel active" : "tab-panel"}
          >
            <EventTable tab={tab} />
          </div>
        ))}
```

`web/src/dashboard.css` — append (uses the existing theme custom properties, both themes inherit automatically):

```css
/* --- Event tables (kind="table" tabs) --------------------------------- */
.event-table { padding: 12px 16px; }
.event-table-filter {
  width: 260px;
  margin-bottom: 10px;
  padding: 6px 10px;
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border-ctrl);
  border-radius: 6px;
}
.event-table-filter:focus { outline: none; border-color: var(--border-focus); }
.event-table table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.event-table th {
  text-align: left;
  padding: 8px 12px;
  color: var(--text-head);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  background: var(--bg-panel);
}
.event-table td {
  padding: 6px 12px;
  color: var(--text);
  border-bottom: 1px solid var(--border);
  font-family: ui-monospace, monospace;
  word-break: break-word;
}
.event-table-time { white-space: nowrap; color: var(--text-muted); }
```

- [ ] **Step 5: Run the web tests + build**

Run: `cd web && npm run test && npm run build`
Expected: both PASS (tsc catches any type drift against the regenerated `types.gen.ts`).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/EventTable.tsx web/src/components/TabBar.tsx web/src/components/ChartGrid.tsx web/src/dashboard.css web/src/__tests__/eventtable.test.tsx
git commit -m "feat(web): EventTable — kind=\"table\" tabs render log-event rows

Newest-first rows for the selected host, case-insensitive substring
filter, UTC time cells; table tabs surface in TabBar despite empty
metrics and get their own #tab-<id> panels.

Assisted-by: Claude Fable 5"
```

---

### Task 8: Playwright pins — live table, SSE append, filter, cap, historical `--db`

**Files:**
- Modify: `tests/e2e/monitor/dashboard/conftest.py`
- Create: `tests/e2e/monitor/dashboard/test_dashboard_table.py`

**Interfaces:**
- Consumes: Task 5's `FakeCollector(extra_parsers=...)` + `push_log_events`; Task 4's `RegexLogEventParser`; Task 3's `MetricDB.write_log_event` + `MetricCollector.from_sqlite`; Task 7's selectors (`.tab-btn[data-tab="syslog"]`, `.event-table`, `.event-table-filter`, `tbody tr`).
- Produces: browser pins for every spec bullet: table render from scripted events, live SSE append, substring filter, 500-row display cap, historical `--db` table render.

- [ ] **Step 1: Add the fixtures**

In `tests/e2e/monitor/dashboard/conftest.py` add (imports: `asyncio`, `timedelta` already present, `MetricDB` from `otto.monitor.db`, `RegexLogEventParser` from `otto.monitor.log_sourced`, `default_catalog` from `otto.monitor.parsers`):

```python
SYSLOG_PATTERN = r"^(?P<ts>\S+) (?P<loghost>\S+) (?P<proc>[^:\[]+)(?:\[\d+\])?: (?P<message>.*)$"


def _table_parser() -> RegexLogEventParser:
    return RegexLogEventParser(
        "tail -n 200 /var/log/syslog",
        SYSLOG_PATTERN,
        tab="syslog",
        tab_label="Syslog",
    )


def _preload_table(harness: DashboardHarness[FakeCollector]) -> None:
    """Three syslog rows for host1 plus one for host2 (host-scoping pin)."""
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=15)
    rows = [
        (
            t0 + timedelta(seconds=5 * i),
            {"loghost": "vm1", "proc": "sshd", "message": f"session {i} opened"},
        )
        for i in range(3)
    ]
    harness.run(harness.collector.push_log_events("host1", tab="syslog", rows=rows))
    harness.run(
        harness.collector.push_log_events(
            "host2",
            tab="syslog",
            rows=[(t0, {"loghost": "vm2", "proc": "cron", "message": "job ran"})],
        )
    )


@pytest.fixture
def table_dash() -> Iterator[DashboardHarness[FakeCollector]]:
    harness = DashboardHarness(FakeCollector(extra_parsers=[_table_parser()])).start()
    _preload(harness)
    _preload_table(harness)
    yield harness
    harness.stop()


@pytest.fixture
def historical_table_dash(tmp_path: Path) -> Iterator[DashboardHarness[MetricCollector]]:
    """A --db-mode server whose SQLite file carries log events."""
    db_path = tmp_path / "metrics.db"

    async def _seed() -> None:
        db = MetricDB(str(db_path))
        await db.open()
        t0 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        await db.write_point(t0, "host1", "Overall CPU", 42.0)
        for i in range(3):
            await db.write_log_event(
                t0 + timedelta(seconds=i),
                "host1",
                "syslog",
                {"loghost": "vm1", "proc": "sshd", "message": f"historical row {i}"},
            )
        await db.close()

    asyncio.run(_seed())
    collector = asyncio.run(
        MetricCollector.from_sqlite(
            str(db_path), parsers=[*default_catalog().values(), _table_parser()]
        )
    )
    harness = DashboardHarness(collector).start()
    yield harness
    harness.stop()
```

- [ ] **Step 2: Write the pins**

Create `tests/e2e/monitor/dashboard/test_dashboard_table.py` (same markers as the sibling files):

```python
"""Event-table pins: render, live SSE append, filter, display cap, historical --db."""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]


def _open_table(page: Page, url: str, host: str = "host1") -> None:
    page.goto(url)
    page.select_option("#host-select", host)
    page.click('.tab-btn[data-tab="syslog"]')


def test_table_renders_scripted_rows_newest_first(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url)
    expect(page.locator(".event-table thead th")).to_have_text(
        ["Time", "loghost", "proc", "message"]
    )
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("session 2 opened")
    expect(rows.nth(2)).to_contain_text("session 0 opened")


def test_table_scopes_rows_to_selected_host(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url, host="host2")
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(1)
    expect(rows.nth(0)).to_contain_text("job ran")


def test_table_appends_live_sse_rows(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url)
    expect(page.locator(".event-table tbody tr")).to_have_count(3)
    table_dash.run(
        table_dash.collector.push_log_events(
            "host1",
            tab="syslog",
            rows=[
                (
                    datetime.now(tz=timezone.utc),
                    {"loghost": "vm1", "proc": "sshd", "message": "live append"},
                )
            ],
        )
    )
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(4)
    expect(rows.nth(0)).to_contain_text("live append")  # newest-first


def test_table_substring_filter(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    _open_table(page, table_dash.url)
    page.fill(".event-table-filter", "session 1")
    expect(page.locator(".event-table tbody tr")).to_have_count(1)
    page.fill(".event-table-filter", "")
    expect(page.locator(".event-table tbody tr")).to_have_count(3)


def test_table_display_cap_500(
    table_dash: DashboardHarness[FakeCollector], page: Page
) -> None:
    t0 = datetime.now(tz=timezone.utc)
    table_dash.run(
        table_dash.collector.push_log_events(
            "host1",
            tab="syslog",
            rows=[
                (
                    t0 + timedelta(milliseconds=i),
                    {"loghost": "vm1", "proc": "bulk", "message": f"bulk {i}"},
                )
                for i in range(520)
            ],
        )
    )
    _open_table(page, table_dash.url)
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(500)
    expect(rows.nth(0)).to_contain_text("bulk 519")


def test_table_renders_from_historical_db(
    historical_table_dash: DashboardHarness, page: Page
) -> None:
    _open_table(page, historical_table_dash.url)
    rows = page.locator(".event-table tbody tr")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_contain_text("historical row 2")
```

Note on `test_table_display_cap_500`: the 520-row batch is pushed **before** the page loads, so the pin exercises the `/api/data` hydration path (backend ring 1000 → client cap 500); `test_table_appends_live_sse_rows` covers the SSE path. If the `page` fixture in this suite navigates eagerly, mirror however `test_dashboard_live.py` orders fixture vs. goto.

- [ ] **Step 3: Build the dist and run the pins**

```bash
make web
uv run pytest tests/e2e/monitor/dashboard/test_dashboard_table.py -m browser -n 1 -q
```

Expected: PASS (needs `make browsers` once on a fresh machine). Then run the whole dashboard lane to catch regressions in the existing pins:

Run: `make dashboard`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/monitor/dashboard/conftest.py tests/e2e/monitor/dashboard/test_dashboard_table.py
git commit -m "test(monitor): Playwright pins for the event table

Scripted-rows render (newest-first, host-scoped), live SSE append,
substring filter, the 500-row display cap, and historical --db render
via a seeded MetricDB.

Assisted-by: Claude Fable 5"
```

---

### Task 9: Documentation — guide section + API page

**Files:**
- Modify: `docs/guide/monitor.md`
- Create: `docs/api/monitor/log_sourced.rst`
- Modify: `docs/api/monitor/index.rst` (toctree entry)

**Interfaces:**
- Consumes: everything shipped in Tasks 1–8 (names/signatures must match exactly — the Sphinx nitpicky gate resolves xrefs).

- [ ] **Step 1: API page**

Create `docs/api/monitor/log_sourced.rst` mirroring `docs/api/monitor/rates.rst`'s exact structure (title, `.. automodule:: otto.monitor.log_sourced` with the same options the sibling pages use). Add `log_sourced` to the toctree in `docs/api/monitor/index.rst` (alphabetical/neighbor-consistent placement). **This is the Plan A Task-14 lesson: a new module without its API page breaks `make docs` at the final gate — wire it now.**

- [ ] **Step 2: Guide section**

In `docs/guide/monitor.md`, insert a new `## Log-sourced data` section between `## Parser health` and `## SNMP monitoring`, containing:

1. **Intro** — some systems don't expose live values; cron-digested CSV files and log-file event streams both ride the shell path with data-carried timestamps. The command is the reduction step (`cat`/`tail`/`awk`/`grep`/`jq`); the design assumes data is textually reducible on the host — binary/irreducible formats are out of scope.
2. **`### CSV metric files`** — a `CsvMetricParser` registration example (use the module docstring's `register_parsers` example verbatim so docs and code agree); line-format rules (first column ISO-8601 or epoch, naive = UTC; header/torn/malformed lines skipped and self-healing); restart backfill behavior ("a file holding the last hour backfills the dashboard and DB with an hour of real history"); one-instance-per-file; per-parser `interval` for slow cadences. Include the example cron digest script:

   ```sh
   #!/bin/sh
   # Example cron digest: append "epoch,val1,val2", prune to the last hour.
   # Cron entry (every 5 minutes):  */5 * * * *  root  /usr/local/bin/perf_digest.sh
   FILE=/var/log/perf/net.csv
   printf '%s,%s,%s\n' "$(date -u +%s)" "$(cat /sys/class/net/eth0/statistics/rx_bytes)" \
       "$(cat /sys/class/net/eth0/statistics/tx_bytes)" >> "$FILE"
   tail -n 12 "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"   # 12 lines = 1 h at 5-min cadence
   ```

   with the note that provisioning it on a bed is a manual demo step — all otto tests use fixture-written files.
3. **`### Log-event tables`** — the worked **syslog** example: a `RegexLogEventParser` registration with the ISO-timestamp pattern used in the test suite (`SYSLOG_PATTERN`), noting named groups become table columns, `ts_group`/`ts_format` conventions (`"iso"`, `"epoch"`, `strptime`; classic-syslog formats without a year get the current UTC year), that the parser contributes a **table tab** on the dashboard (newest-first, substring filter, last ~500 rows displayed; the DB keeps everything and `--db` replays render tables too), and that table parsers must use their own tab id. Mention `LogEvent` data is deliberately separate from `MonitorEvent` chart markers.
4. **`### Timestamps`** — one short block: naive = UTC everywhere; rows must carry timestamps; high-water dedup keys on row timestamps (rotation-safe).
5. **`### Large files`** — append-only logs of any size fit via `tail -n N` + high-water dedup; command strings are static registry keys, so per-tick varying commands (byte-offset reads) are unsupported by design; large regenerated files fit by reducing at the source (`awk`/`jq`/product CLI) on a slower per-parser `interval` riding its own bucket.

Match the guide's existing voice and formatting (tables, `!!! note` admonitions if the file uses them — check first).

- [ ] **Step 3: Build the docs**

Run: `make docs`
Expected: build succeeds with **0 warnings** (nitpicky `-W`).

- [ ] **Step 4: Commit**

```bash
git add docs/guide/monitor.md docs/api/monitor/log_sourced.rst docs/api/monitor/index.rst
git commit -m "docs(monitor): log-sourced data guide — CSV digests, syslog event tables, large files

Assisted-by: Claude Fable 5"
```

---

### Task 10: Final gate + polish

**Files:** whatever the gates flag.

- [ ] **Step 1: Full test suite with coverage**

Run: `make coverage`
Expected: PASS, coverage ≥ the pre-branch baseline (main was 94.84%). This also runs the import-budget guard — if it flags `otto.monitor.log_sourced`, an eager import leaked in (fix the import, do NOT update the snapshot).

- [ ] **Step 2: Web checks**

Run: `make web && cd web && npm run test`
Expected: PASS — the `git diff --exit-code web/src/api/types.gen.ts` drift gate confirms Task 5's regeneration is committed and current; the air-gap check stays green (no external URLs were added).

- [ ] **Step 3: Lint + typecheck**

Run: `uv run nox -s lint` then `uv run nox -s typecheck`
Expected: PASS. (Lint = `ruff check` + `ruff format --check`; if formatting rewrites anything, re-run `ruff check .` afterward.)

- [ ] **Step 4: Docs**

Run: `make docs`
Expected: 0 warnings.

- [ ] **Step 5: Dashboard e2e lane**

Run: `make dashboard`
Expected: PASS (all pre-existing pins plus Task 8's).

- [ ] **Step 6: Fix-ups and final commit**

Fix anything the gates flagged (each fix re-runs its covering gate). Commit any residual fixes:

```bash
git add <named files only>
git commit -m "chore(monitor): final-gate fixes for log-sourced data

Assisted-by: Claude Fable 5"
```

---

## Deferred by design (do not implement)

From the spec's Future Work — reviewers should not flag their absence: log events as chart markers; table UX beyond v1 (sorting, pagination, virtualization, severity coloring); per-tick dynamic commands (byte-offset incremental reads); dedup of restart-backfilled duplicate DB rows across runs with the same `--db` (the backfill is a feature; the dashboard tolerates duplicate points); provisioning the cron demo script on a bed VM (manual, explicit go-ahead only); shipped tool auto-detection.
