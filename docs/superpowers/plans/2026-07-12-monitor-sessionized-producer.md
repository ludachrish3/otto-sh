# Monitor Sessionized Producer (Plan 5a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live monitor runs become persisted sessions; a producer turns them into `format: 1` documents; `otto monitor <source>` serves review mode with the document auto-loaded into the shell.

**Architecture:** Sessions are framed at the edges — the collector/store stay session-blind. `session.py` owns the frame + lab snapshot; `db.py` becomes schema-v2 (multi-session archive, bound to one frame per live run); `export.py` is the pure producer to `MonitorExport`; the server gains `/api/mode` + `/api/document` and switches `/api/export/json` to format:1; the CLI gains an explicit `--live` flag (off by default) and a review-mode source positional; the shell gains its first (soft-failing) boot fetch.

**Tech Stack:** aiosqlite, pydantic (`otto.models.monitor`), Typer, FastAPI, React/zustand (boot hydration only).

**Spec:** `docs/superpowers/specs/2026-07-12-monitor-sessionized-producer-design.md` (approved; includes the breaking-changes list).

## Global Constraints

- Worktree: `/home/vagrant/otto-sh/.claude/worktrees/monitor-session-producer`, branch `worktree-monitor-session-producer` (continue on tip `143d5a5`).
- **Legacy read support is DROPPED by decision** — never add compatibility shims, migrations, or legacy-format parsing. Fail-loud errors must name the break (see Task 1's exact message).
- Python: ruff format + check clean; `ty` clean; no `from __future__ import annotations`; no new `noqa`; Google-style docstrings on public surfaces (Sphinx nitpicky builds them).
- Commit style: conventional prefix + `Assisted-by: Claude Fable 5` trailer embedded in `-m`; explicit `git add` per file; never `git add -u`; never `!` markers (breaking changes are recorded in the spec + final squash message, not per-commit markers).
- Gates per task: scoped `uv run pytest <paths>` for Python, `cd web && npx vitest run <file> && npm run check:fix && npm run check && npm run typecheck` for web. NEVER run `make coverage` inside a task; the full sweeps live in Task 8. `make dashboard` only in Tasks 7–8.
- Wire shapes are pinned by `tests/e2e/monitor/dashboard/test_harness.py` — Task 4 REWRITES the legacy pins deliberately (the one sanctioned edit of that file this phase); no other task touches it.
- All timestamps ISO-8601 UTC on the wire (`datetime.isoformat()`); the frontend parses with `parseTs`.
- `make schema` must be a zero-diff no-op every task (no model changes are planned; if a task finds it needs one, STOP and report).
- Live-bed rules (Task 9 only): peer lab VMs 10.10.200.x, read-only collection, NO VM power operations, light load, generous timeouts, never kill a wedged run at a tight timeout.

---

### Task 1: Session frame + SQLite schema v2

**Files:**
- Create: `src/otto/monitor/session.py`
- Rewrite: `src/otto/monitor/db.py` (schema + constructor contract)
- Test: `tests/unit/monitor/test_session_frame.py`, `tests/unit/monitor/test_db_v2.py`
- Update: any existing tests constructing `MetricDB(path)` (find with `grep -rn "MetricDB(" tests/`) — they gain a frame argument.

**Interfaces:**
- Consumes: `otto.models.monitor` (`LabSnapshot`, `SessionMeta`) — serialized forms only.
- Produces (Tasks 3/4/5 rely on):
  - `SessionFrame` dataclass: `id: str`, `label: str | None`, `note: str | None`, `start: datetime`, `end: datetime | None = None`; factory `new_frame(label: str | None, note: str | None, *, now: datetime | None = None) -> SessionFrame` (id = `start.strftime("%Y-%m-%dT%H-%M-%SZ")`, UTC).
  - `MetricDB(path: str, frame: SessionFrame, lab_json: str, meta_json: str)` — bound to ONE live session; `open()` creates the session row; `finalize(end: datetime)` stamps `end`; all writers stamp `session_id`.
  - Module function `read_sessions(path: str) -> list[SessionRow]` where `SessionRow` is a small dataclass `{id, label, note, start, end, lab_json, meta_json, metrics, events, log_events}` (rows as plain tuples/dicts, documented below) — synchronous sqlite3 read (review path has no event loop requirement).
  - `SCHEMA_VERSION = 2`; exception type `UnsupportedDBError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/monitor/test_session_frame.py`:

```python
"""SessionFrame — identity and stamping (spec 2026-07-12 §Sessionization)."""

from datetime import datetime, timezone

from otto.monitor.session import SessionFrame, new_frame


def test_new_frame_id_is_utc_timestamp_slug():
    now = datetime(2026, 7, 12, 14, 30, 5, tzinfo=timezone.utc)
    frame = new_frame(label="fan fix", note=None, now=now)
    assert frame.id == "2026-07-12T14-30-05Z"
    assert frame.start == now
    assert frame.end is None
    assert frame.label == "fan fix"
    assert frame.note is None


def test_new_frame_defaults_to_wall_clock_utc():
    frame = new_frame(label=None, note=None)
    assert frame.start.tzinfo is not None
    assert frame.id.endswith("Z")
```

`tests/unit/monitor/test_db_v2.py`:

```python
"""MetricDB schema v2 — session-bound writes, multi-session archives,
fail-loud on anything that is not v2 (spec 2026-07-12: legacy read DROPPED)."""

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from otto.monitor.db import MetricDB, UnsupportedDBError, read_sessions
from otto.monitor.events import MonitorEvent
from otto.monitor.session import new_frame

UTC = timezone.utc
T0 = datetime(2026, 7, 12, 8, 0, 0, tzinfo=UTC)


def frame_at(label, minutes=0):
    return new_frame(label=label, note=f"note for {label}", now=T0 + timedelta(minutes=minutes))


async def write_session(path, label, minutes=0, finalize=True):
    db = MetricDB(str(path), frame_at(label, minutes), lab_json="{}", meta_json="{}")
    await db.open()
    await db.write_point(T0 + timedelta(minutes=minutes), "h1", "CPU %", 42.0)
    await db.write_log_event(T0 + timedelta(minutes=minutes), "h1", "kernel", {"msg": "x"})
    rowid = await db.write_event(
        MonitorEvent(timestamp=T0 + timedelta(minutes=minutes), label="mark", source="manual")
    )
    assert rowid > 0
    if finalize:
        await db.finalize(T0 + timedelta(minutes=minutes + 5))
    await db.close()


@pytest.mark.asyncio
async def test_open_creates_session_row_and_writes_stamp_session_id(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "run-1")
    sessions = read_sessions(str(path))
    assert [s.label for s in sessions] == ["run-1"]
    s = sessions[0]
    assert s.note == "note for run-1"
    assert s.end is not None
    assert len(s.metrics) == 1 and len(s.events) == 1 and len(s.log_events) == 1


@pytest.mark.asyncio
async def test_multi_session_append_preserves_prior_sessions(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "run-1", minutes=0)
    await write_session(path, "run-2", minutes=60)
    sessions = read_sessions(str(path))
    assert [s.label for s in sessions] == ["run-1", "run-2"]
    assert all(len(s.metrics) == 1 for s in sessions)  # rows partitioned, not shared


@pytest.mark.asyncio
async def test_crash_leaves_end_null(tmp_path):
    path = tmp_path / "lab.db"
    await write_session(path, "crashed", finalize=False)
    (session,) = read_sessions(str(path))
    assert session.end is None  # reader-side fallback is the PRODUCER's job (Task 3)


@pytest.mark.asyncio
async def test_legacy_flat_db_refused_loud(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY, ts TEXT, host TEXT, label TEXT, value REAL)")
    conn.commit()
    conn.close()
    db = MetricDB(str(path), frame_at("x"), lab_json="{}", meta_json="{}")
    with pytest.raises(UnsupportedDBError, match="pre-session schema.*not supported"):
        await db.open()
    with pytest.raises(UnsupportedDBError, match="pre-session schema.*not supported"):
        read_sessions(str(path))


@pytest.mark.asyncio
async def test_future_schema_version_refused_loud(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.execute("CREATE TABLE sessions (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(UnsupportedDBError, match="schema version 99"):
        read_sessions(str(path))


def test_read_sessions_missing_file_raises(tmp_path):
    with pytest.raises(UnsupportedDBError, match="not a monitor database"):
        read_sessions(str(tmp_path / "nope.db"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/monitor/test_session_frame.py tests/unit/monitor/test_db_v2.py -x -q`
Expected: FAIL — `session.py` doesn't exist; `MetricDB` signature mismatch.

- [ ] **Step 3: Implement**

Create `src/otto/monitor/session.py`:

```python
"""Session framing for live monitor runs (spec 2026-07-12).

The collector stays session-blind: one process run == one live session, and
the frame is stamped at the edges (CLI at launch, shutdown hook at exit).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SessionFrame:
    """Identity + lifetime of one live monitoring session.

    ``end is None`` means still-open — a crash never rewrites history, and
    readers fall back to the last sample's timestamp (producer's job).
    """

    id: str
    label: str | None
    note: str | None
    start: datetime
    end: datetime | None = field(default=None)


def new_frame(
    label: str | None,
    note: str | None,
    *,
    now: datetime | None = None,
) -> SessionFrame:
    """Create a frame stamped at *now* (wall-clock UTC when omitted).

    The id is the UTC start time as a filesystem/URL-safe slug — unique per
    database because two live runs can't write one file (flock guard).
    """
    start = now if now is not None else datetime.now(tz=timezone.utc)
    return SessionFrame(
        id=start.strftime("%Y-%m-%dT%H-%M-%SZ"),
        label=label,
        note=note,
        start=start,
    )
```

Rewrite `src/otto/monitor/db.py` — keep the module docstring's first paragraph, the flock guard, the WAL-vs-DELETE choice, and all writer bodies; change the schema, the constructor, and add the session lifecycle + reader:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id        TEXT    PRIMARY KEY,
    label     TEXT,
    note      TEXT,
    start     TEXT    NOT NULL,
    end       TEXT,
    lab_json  TEXT    NOT NULL DEFAULT '{}',
    meta_json TEXT    NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    label      TEXT    NOT NULL,
    value      REAL    NOT NULL,
    source     TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    end_ts     TEXT,
    label      TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'manual',
    color      TEXT    NOT NULL DEFAULT '#888888',
    dash       TEXT    NOT NULL DEFAULT 'dash'
);
CREATE TABLE IF NOT EXISTS log_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    tab        TEXT    NOT NULL DEFAULT '',
    fields     TEXT    NOT NULL DEFAULT '{}'
);
"""

SCHEMA_VERSION = 2


class UnsupportedDBError(RuntimeError):
    """Raised for any monitor database that is not schema v2.

    Legacy pre-session databases are deliberately unsupported (spec
    2026-07-12: no migration path); the message must say so.
    """
```

Key implementation points (write these exactly, adapting only line placement):

- `__init__(self, path: str, frame: SessionFrame, lab_json: str, meta_json: str)`.
- `open()` — after the existing flock + journal-mode logic:
  ```python
  version = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]
  tables = {
      row[0]
      async for row in await conn.execute(
          "SELECT name FROM sqlite_master WHERE type='table'"
      )
  }
  if tables and version != SCHEMA_VERSION:
      raise UnsupportedDBError(
          f"'{self._path}' uses a pre-session schema (or schema version "
          f"{version}); otto no longer reads pre-session monitor databases "
          "and provides no migration — use a fresh --db file (not supported: "
          "converting legacy captures)."
      )
  await conn.executescript(_SCHEMA)
  await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
  await conn.execute(
      "INSERT INTO sessions (id, label, note, start, end, lab_json, meta_json)"
      " VALUES (?, ?, ?, ?, NULL, ?, ?)",
      (self._frame.id, self._frame.label, self._frame.note,
       self._frame.start.isoformat(), self._lab_json, self._meta_json),
  )
  await conn.commit()
  ```
  (The old `end_ts` ALTER-TABLE migration block is DELETED — v2 creates it in-schema.)
- Every `INSERT` in `write_point` / `write_log_event` / `write_event` gains a
  `session_id` column bound to `self._frame.id`; `write_point` also writes
  `source=None` for now (the column exists per contract §7; live source
  attribution arrives in 5b).
- New `async def finalize(self, end: datetime) -> None`: `UPDATE sessions SET end = ? WHERE id = ?`; no-op when the connection is closed.
- New module-level reader (synchronous — review mode runs before any event loop matters):
  ```python
  @dataclass
  class SessionRow:
      id: str
      label: str | None
      note: str | None
      start: str
      end: str | None
      lab_json: str
      meta_json: str
      metrics: list[tuple[str, str, str, float, str | None]]   # ts, host, label, value, source
      events: list[tuple[int, str, str | None, str, str, str, str]]  # id, ts, end_ts, label, source, color, dash
      log_events: list[tuple[str, str, str, str]]              # ts, host, tab, fields_json


  def read_sessions(path: str) -> list[SessionRow]:
      """Read every session (ordered by start) from a v2 archive. Fail-loud otherwise."""
  ```
  Body: `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` inside `try/except sqlite3.OperationalError` → `UnsupportedDBError(f"'{path}' is not a monitor database (cannot open read-only)")`; check `PRAGMA user_version` + table presence with the SAME error text as `open()` (extract a shared `_check_version(version, tables, path)` helper both paths call); then one query per table filtered by `session_id`, ordered by `ts`.

Update `factory.py`'s `build_monitor_collector` call sites ONLY if it constructs `MetricDB` internally — if it does, thread `frame/lab_json/meta_json` through as parameters with no default (callers must supply); if the CLI constructs the DB, prefer moving construction to the CLI (Task 5 wires it) and have the factory accept `db: MetricDB | None = None` instead of `db_path`. Inspect `factory.py` first and pick the smaller diff; disclose the choice in your report.

- [ ] **Step 4: Verify green**

Run: `uv run pytest tests/unit/monitor/ -x -q && uv run ruff format --check src/otto/monitor/ tests/unit/monitor/ && uv run ruff check src/otto/monitor/ tests/unit/monitor/`
Expected: PASS, including every pre-existing `tests/unit/monitor/` test (update any that constructed `MetricDB(path)` to pass a frame — the grep in **Files** finds them).
Then: `uv run nox -s typecheck` (ty runs only here — budget one round).

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/session.py src/otto/monitor/db.py tests/unit/monitor/test_session_frame.py tests/unit/monitor/test_db_v2.py <updated existing tests> <factory.py if touched>
git commit -m "feat(monitor): session frame + sessionized SQLite schema v2

One live run = one session; frames stamped at the edges, collector
untouched. v2 archives are multi-session; anything else fails loud —
legacy pre-session databases are no longer readable (spec 2026-07-12).

Assisted-by: Claude Fable 5"
```

---

### Task 2: Lab snapshot builder

**Files:**
- Extend: `src/otto/monitor/session.py`
- Test: `tests/unit/monitor/test_lab_snapshot.py`

**Interfaces:**
- Consumes: `otto.models.host.RemoteHost` (fields: `element`, `board`, `slot`, `hop`, `ip`, plus os/interface fields — verify exact names against `src/otto/models/host.py:200-230` before writing); `otto.link.derive.implicit_links(hosts)` and `resolve_declared_links(...)` (verify exact signatures in `src/otto/link/derive.py` — ADAPTATION PROTOCOL: if the signatures differ from this plan's calls, adapt the plumbing, keep the semantics, disclose in your report); `LabSnapshot`, `HostSnapshot`, `LinkSnapshot`, `LinkEndpointSnapshot` from `otto.models.monitor`.
- Produces (Task 5 relies on): `snapshot_lab(hosts: Sequence[RemoteHost], declared: list[Link]) -> LabSnapshot` and `snapshot_lab_json(...) -> str` (its `model_dump_json` form); a private `_link_snapshot(link: Link) -> LinkSnapshot` mirroring `scripts/gen_monitor_fixtures.py:95`'s field mapping.

Semantics (binding, from the contract spec §2/§3):
- Hosts → `HostSnapshot`: id, element, board, slot, hop, ip, os fields, `interfaces` flattened `netdev -> ip`, `labs`, `is_virtual`. **Never credentials.**
- Links: implicit-from-hop (`implicit_links`) + declared — **dynamic/tunnel links excluded** (do not call any tunnel/discovery API); `impair` middlebox reference passes through.
- `elements` stays `[]` — explicit `ElementRecord`s are a config feature real labs don't declare yet; the frontend derives elements from `host.element` (same behavior as fixtures with empty `elements`). One-line comment saying exactly this.

- [ ] **Step 1: Write the failing test**

`tests/unit/monitor/test_lab_snapshot.py` — build two fake `RemoteHost`-shaped objects (use the real model class construction idiom found in existing host tests — `grep -rn "RemoteHost(" tests/unit/ | head -3` and copy one): `gw` (element "gw", ip, no hop) and `n1` (element "rack", slot 2, hop "gw"). Assert:

```python
def test_snapshot_hosts_and_implicit_links():
    snap = snapshot_lab([gw, n1], declared=[])
    assert {h.id for h in snap.hosts} == {"gw", "rack_n1"}
    n1_snap = next(h for h in snap.hosts if h.hop == "gw")
    assert n1_snap.slot == 2
    assert snap.elements == []
    assert [l.provenance for l in snap.links] == ["implicit"]
    (link,) = snap.links
    assert {link.endpoints[0].host, link.endpoints[1].host} == {"gw", "rack_n1"}


def test_snapshot_never_carries_credentials():
    snap = snapshot_lab([gw], declared=[])
    dumped = snap.model_dump_json()
    assert "password" not in dumped and "username" not in dumped


def test_declared_link_impair_passthrough():
    link = Link(a=..., b=..., protocol="udp", provenance=Provenance.DECLARED, impair="mb-1")
    snap = snapshot_lab([gw, n1], declared=[link])
    declared = [l for l in snap.links if l.provenance == "declared"]
    assert declared[0].impair == "mb-1"
```

(Fill the `Link(...)` endpoint construction from `src/otto/link/model.py`'s `LinkEndpoint` — the test must construct a REAL `Link`, not a stub.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/monitor/test_lab_snapshot.py -x -q` → FAIL (no `snapshot_lab`).

- [ ] **Step 3: Implement** — append to `session.py`:

```python
def snapshot_lab(hosts: Sequence["RemoteHost"], declared: list["Link"]) -> LabSnapshot:
    """Freeze the view-relevant lab config into a session snapshot.

    Static links only (implicit hop edges + declared routes; contract spec
    2026-07-10 §2) — dynamic/tunnel links are runtime state and never enter
    a snapshot. Credentials never leave the host object. ``elements`` stays
    empty: real labs declare membership per-host (``element`` field) and the
    frontend derives the grouping, exactly as with generator fixtures.
    """
    host_snaps = [_host_snapshot(h) for h in hosts]
    links = [_link_snapshot(l) for l in implicit_links({h.id: h for h in hosts})]
    links += [_link_snapshot(l) for l in declared]
    return LabSnapshot(hosts=host_snaps, elements=[], links=links)
```

with `_host_snapshot` mapping the verified `RemoteHost` field names into `HostSnapshot` and `_link_snapshot` copying `scripts/gen_monitor_fixtures.py:95`'s mapping (id, endpoints (host/interface/ip/port), protocol, provenance value, name, impair). Where the CLI gets `declared` from: `resolve_declared_links` over the active lab config — that call happens in Task 5 (CLI), not here; this module stays pure.

- [ ] **Step 4: Verify green** — `uv run pytest tests/unit/monitor/ -x -q && uv run ruff format --check src/otto/monitor/ tests/unit/monitor/ && uv run ruff check src/otto/monitor/ tests/unit/monitor/`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/session.py tests/unit/monitor/test_lab_snapshot.py
git commit -m "feat(monitor): lab snapshot builder — static links, no credentials

Assisted-by: Claude Fable 5"
```

---

### Task 3: The producer (`export.py`)

**Files:**
- Create: `src/otto/monitor/export.py`
- Test: `tests/unit/monitor/test_export_producer.py`

**Interfaces:**
- Consumes: `SessionFrame` (T1), `read_sessions`/`SessionRow` (T1), `MetricCollector.get_series()/get_events()/get_log_events()/get_chart_map()/get_meta_model()` (existing, collector.py:625-660), `MonitorExport`/`SessionRecord`/`SessionMeta`/`ChartSpecRecord`/`TabSpecRecord`/`MetricRecord`/`EventRecord`/`LogEventRecord`.
- Produces (Tasks 4/5 rely on):
  - `build_live_export(frame: SessionFrame, collector: MetricCollector, lab: LabSnapshot, *, now: datetime | None = None) -> MonitorExport` — one still-open session (`end=None` when the frame is open; pass `now` only for tests).
  - `build_db_export(path: str) -> MonitorExport` — every session in a v2 archive; **a null `end` falls back to the session's last sample timestamp** (crash tolerance, spec §Sessionization); a session with no samples falls back to `start`.
  - `document_json(export: MonitorExport) -> str` (`model_dump_json(exclude_none=True)` — match what the fixture generator emits so the frontend's dense-normalization sees identical shapes; check the generator's dump call and copy its arguments exactly).

Semantics (binding):
- Live reshape: `get_series()` keys are `"host/label"` — split on the FIRST `/` only (labels may contain slashes; hosts may not — assert this against `MetricStore`'s keying at store.py:22 and say so in a comment). Each `MetricPoint` → `MetricRecord(timestamp, host, label, value)`.
- `get_meta_model()` → `SessionMeta(interval=…, charts=[ChartSpecRecord(**spec.model_dump())…], tabs=[…])`.
- Events → `EventRecord` with their existing integer ids; log events → `LogEventRecord`.
- Everything validates through the pydantic models — construct them, never hand-build dicts.

- [ ] **Step 1: Write the failing tests** — `tests/unit/monitor/test_export_producer.py`:

```python
"""Producer: live store / v2 archive → format:1 MonitorExport."""
```

Cases (write all five; construct a real `MetricCollector` with no targets — `MetricCollector(targets=[])` idiom exists in unit tests, `grep -rn "MetricCollector(" tests/unit/monitor/ | head -3` — and feed its store directly via `_record_point`-adjacent public paths or `add_event`):
1. `test_live_export_wraps_one_open_session` — frame without end → `export.sessions[0].end is None`; `format == 1`; `MonitorExport.model_validate_json(document_json(export))` round-trips.
2. `test_live_export_splits_series_keys_on_first_slash_only` — series key `"h1/proc/io read"` → host `"h1"`, label `"proc/io read"`.
3. `test_live_export_carries_meta_and_chart_map`.
4. `test_db_export_reads_multi_session_archive` — reuse T1's `write_session` helper (import it or inline two sessions), assert two `SessionRecord`s with correct labels/notes and `lab_json` round-tripping into `SessionRecord.lab`.
5. `test_db_export_null_end_falls_back_to_last_sample` — unfinalized session whose last metric ts is T+5m → `session.end == T+5m`; a session with zero samples → `end == start`.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/monitor/test_export_producer.py -x -q` → FAIL.

- [ ] **Step 3: Implement** `src/otto/monitor/export.py` — pure module, ~120 lines, docstring: "The format:1 producer (spec 2026-07-12). Pure reshaping; validation is the pydantic models' job — the schema drift guards police this module for free." Implementation follows directly from the semantics block; `build_db_export` parses `SessionRow` tuples into records and `LabSnapshot.model_validate_json(row.lab_json)` / `SessionMeta.model_validate_json(row.meta_json)`.

- [ ] **Step 4: Verify green** — `uv run pytest tests/unit/monitor/ -x -q && uv run ruff format --check src/otto/monitor/ tests/unit/monitor/ && uv run ruff check src/otto/monitor/ tests/unit/monitor/`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/export.py tests/unit/monitor/test_export_producer.py
git commit -m "feat(monitor): format:1 producer — live store and v2 archives

Assisted-by: Claude Fable 5"
```

---

### Task 4: Server endpoints + harness pin rewrite

**Files:**
- Modify: `src/otto/monitor/server.py` (`_build_app`, `MonitorServer.__init__`)
- Modify: `src/otto/monitor/history.py` → DELETE the legacy read/write surface (see below)
- Rewrite pins: `tests/e2e/monitor/dashboard/test_harness.py` (the ONE sanctioned edit)

**Interfaces:**
- Consumes: `MonitorExport`, `document_json` (T3).
- Produces (Tasks 5/6/7 rely on):
  - `MonitorServer(collector, host=..., port=..., *, mode: Literal["live", "review"] = "live", document: MonitorExport | None = None, source_name: str | None = None)`.
  - `GET /api/mode` → `{"mode": "live"|"review", "source": str|None}` (200 in both modes).
  - `GET /api/document` → review: the document JSON (`media_type="application/json"`, body = `document_json(document)`); live: 404 `{"detail": "no document in live mode"}`.
  - `GET /api/export/json` → live: `document_json(build_live_export(frame, collector, lab))` — the server needs the frame+lab too: extend `__init__` with `frame: SessionFrame | None = None, lab: LabSnapshot | None = None` (live mode); review: same body as `/api/document`. `Content-Disposition: attachment; filename="monitor-export.json"` preserved from the current implementation.
- Deletions in this task: `history.py`'s `to_json`/`export_json` (the export endpoint's last legacy consumer dies here). Keep `from_json`/`from_sqlite`/`load_*` until Task 5 removes their CLI callers, then delete them THERE (each deletion rides the commit that removes its last caller). If `history.py` ends up empty after Task 5, Task 5 deletes the file.

- [ ] **Step 1: Rewrite the harness pins first (they are the failing tests).** In `test_harness.py`: delete `test_export_import_round_trip_preserves_values` and `test_historical_fixture_loads` (their subjects no longer exist); add:

```python
MODE_KEYS = {"mode", "source"}


def test_mode_wire_contract_live(...):   # existing live-harness fixture idiom
    payload = ...  # GET /api/mode
    assert set(payload) == MODE_KEYS
    assert payload["mode"] == "live"
    assert payload["source"] is None


def test_document_404_in_live_mode(...):
    # GET /api/document → 404 with {"detail": ...}


def test_export_json_emits_format_1(...):
    payload = ...  # GET /api/export/json
    assert payload["format"] == 1
    assert isinstance(payload["sessions"], list) and len(payload["sessions"]) == 1
    session = payload["sessions"][0]
    assert {"id", "start", "lab", "meta", "metrics", "chart_map"} <= set(session)
```

plus a review-mode fixture (construct `MonitorServer(collector=empty, mode="review", document=<two-session MonitorExport built inline via the models>, source_name="x.db")`) pinning `/api/mode` review shape and `/api/document` == the document round-tripped. Follow the file's existing raw-HTTP idioms exactly (it reads chunked streams by hand — copy the GET helper already there). Every other pre-existing pin (meta, data, SSE) stays byte-identical.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -x -q` → new pins FAIL (endpoints missing).

- [ ] **Step 3: Implement** in `server.py`: the three endpoints per the Produces block; `__init__` stores `mode/document/source_name/frame/lab`; `/api/export/json`'s live branch imports `build_live_export` lazily (server must not import the producer at module import if that would create a cycle — check; if no cycle, top-level import). The live branch asserts `frame is not None and lab is not None` with a clear RuntimeError (programming error — the CLI always supplies them; say so in the message).

- [ ] **Step 4: Verify green** — `uv run pytest tests/e2e/monitor/dashboard/test_harness.py tests/unit/monitor/ -x -q && uv run ruff format --check src/otto/monitor/ tests/e2e/monitor/dashboard/test_harness.py && uv run ruff check src/otto/monitor/ tests/e2e/monitor/dashboard/test_harness.py`.

- [ ] **Step 5: Commit**

```bash
git add src/otto/monitor/server.py src/otto/monitor/history.py tests/e2e/monitor/dashboard/test_harness.py
git commit -m "feat(monitor): /api/mode + /api/document; /api/export/json emits format:1

Harness pins rewritten one era newer: legacy flat-export and --file
replay pins die with their subjects; mode/document/format:1 pins land.

Assisted-by: Claude Fable 5"
```

---

### Task 5: CLI split

**Files:**
- Rewrite: `src/otto/cli/monitor.py`
- Modify: `src/otto/monitor/factory.py` (db threading per T1's choice), `src/otto/monitor/history.py` (delete remaining legacy loaders + the file if empty), `src/otto/monitor/collector.py` (delete `from_json`/`from_sqlite` classmethods)
- Modify: `src/otto/cli/builtin_commands.py` if the registration comment describes `--file` (check `sed -n '20,30p'`)
- Test: `tests/e2e/cli/test_monitor_cli.py` (new; follow the CLI-subprocess e2e idioms in `tests/e2e/cli/` — CliRunner or subprocess per the established pattern there)

**Interfaces:**
- Consumes: everything from T1–T4.
- Produces: the user-facing CLI — ONE command, no subcommands (decided with Chris: a `--live` flag avoids the subcommand-vs-filename token ambiguity and Typer's fragile callback+subcommand coexistence). The existing single `@monitor_app.command()` keeps its structure and gains a positional + a flag:
  - `otto monitor --live [--hosts REGEX] [--interval/-i S] [--db PATH] [--label TEXT] [--note TEXT]` — reservation gate, host selection, `new_frame(label, note)`, `snapshot_lab(selected, resolve_declared_links(<active lab config>))` (consult how the link CLI resolves declared links from config — `grep -rn "resolve_declared_links" src/otto/ | grep -v test` — and copy that call), `meta_json` from a collector built first (`get_meta_model().model_dump_json()`), `MetricDB(path, frame, lab_json, meta_json)` when `--db` given, `MonitorServer(collector, mode="live", frame=frame, lab=lab)`, and a `finally:` that awaits `db.finalize(datetime.now(tz=timezone.utc))` on clean shutdown. **The CLI must NOT pass a chart_map**: unlike `lab_json`/`meta_json` (both knowable up front), the series-label → chart-key map only exists once points start arriving, so the collector writes it into the session row itself as each new label appears (`MetricDB.write_chart_map`, called from `_record_point`) — that is also what keeps a crashed session's grouping intact.
  - `otto monitor <source>` — `source: Annotated[Optional[Path], typer.Argument(exists=True, help=...)] = None` on the SAME command. Dispatch order at the top of the body: `--live` AND `source` → `typer.echo("--live and a review source are mutually exclusive.", err=True)` + exit 2; neither → `typer.echo(ctx.get_help())` + exit 2; source only → dispatch by suffix: `.json` → `MonitorExport.model_validate_json(path.read_bytes())` (pydantic `ValidationError` → clear error naming format:1 + exit 1), `.db` → `build_db_export(str(path))` (`UnsupportedDBError` → its message + exit 1), anything else → error naming the two accepted suffixes. Then `MonitorServer(collector=MetricCollector(targets=[]), mode="review", document=export, source_name=path.name)` and `asyncio.run(server.serve())`.
  - `--live` is `Annotated[bool, typer.Option("--live", help="Collect from lab hosts (explicit opt-in; never the default).")] = False`.
  - The module docstring's usage examples are REWRITTEN to the new forms (they currently show `--file` and bare live — the docstring is user-facing help).
  - `--file` and both `_load_historical`/`_serve_historical` die; `history.py` legacy loaders + collector classmethods die with them.

- [ ] **Step 1: Write the failing CLI tests** — `tests/e2e/cli/test_monitor_cli.py`:

1. `test_bare_monitor_prints_usage_exit_2` — output names BOTH `--live` and the `<source>` positional.
2. `test_live_and_source_mutually_exclusive` — `otto monitor --live x.db` (file exists) → exit 2, message says mutually exclusive.
3. `test_source_rejects_unknown_suffix` — `otto monitor x.csv` → exit 1, message names `.json`/`.db`.
4. `test_source_rejects_legacy_json` — a flat legacy JSON file (`{"metrics": [], "events": []}`) → exit 1, error mentions `format`.
5. `test_source_rejects_missing_file` — Typer `exists=True` on the positional.
6. `test_live_requires_reservation_gate` — assert the gate call happens before host selection (follow how existing CLI tests fake/assert the gate — `grep -rn "reservation" tests/e2e/cli/ tests/unit/cli/ | head -5` and copy the established fixture).
(Serving-loop behavior is NOT tested here — the Playwright task covers it; these tests must not start uvicorn.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/e2e/cli/test_monitor_cli.py -x -q` → FAIL.

- [ ] **Step 3: Implement** per the Produces block. Typer note (repo-established): only `Optional[X]` annotations, never unions; `typer.Context` is Typer's vendored click fork — never real `click.*` types.

- [ ] **Step 4: Verify green** — `uv run pytest tests/e2e/cli/test_monitor_cli.py tests/unit/monitor/ tests/e2e/monitor/dashboard/test_harness.py -x -q`, ruff format+check on touched files, then `uv run nox -s typecheck`. Also `grep -rn "from_json\|from_sqlite\|_load_historical\|--file" src/otto/ docs/ --include="*.py" --include="*.md" | grep -i monitor` — zero live references (docs hits are Task 8's job; list them in your report instead of fixing).

- [ ] **Step 5: Commit**

```bash
git add src/otto/cli/monitor.py src/otto/monitor/factory.py src/otto/monitor/history.py src/otto/monitor/collector.py tests/e2e/cli/test_monitor_cli.py <builtin_commands.py if touched>
git commit -m "feat(cli): explicit --live opt-in vs review-source positional

Bare invocation prints usage; --live and a source are mutually
exclusive; --file dies; legacy loaders deleted with
their last callers (spec 2026-07-12 breaking-changes list).

Assisted-by: Claude Fable 5"
```

---

### Task 6: Shell boot hydration + note tooltip

**Files:**
- Create: `web/src/data/bootstrap.ts`
- Modify: `web/src/App.tsx` (mount the bootstrap), `web/src/ui/Select.tsx` (optional `title` on items), `web/src/shell/ReviewBar.tsx` (pass note as title)
- Test: `web/src/__tests__/bootstrap.test.ts`, extend `web/src/__tests__/reviewbar.test.tsx`

**Interfaces:**
- Consumes: `useReviewStore.getState().actions.importText(text, sourceName) -> boolean` (existing).
- Produces:
  - `bootstrapFromServer(): Promise<void>` — fetch `/api/mode`; ANY failure (network, non-200, non-JSON, unknown shape) → return silently (the soft-fail contract — static serving and the offline pin depend on it). If `mode === "review"`: fetch `/api/document`; on 200, `importText(bodyText, source ?? "server")` — the SAME validation/warnings path Import uses. Any document failure → silent return (the shell stays on the Import front door; never an error banner for a missing server).
  - `SelectItem` gains `title?: string`; the option element renders `title={item.title}` when present.
  - ReviewBar session picker: `items={sessions.map((s) => ({ id: s.id, label: s.label ?? s.id, title: s.note ?? undefined }))}` — `note` is on the normalized session record (`exportDoc.ts:132`).

- [ ] **Step 1: Write the failing tests**

`web/src/__tests__/bootstrap.test.ts` (vitest, `vi.stubGlobal("fetch", ...)`; reset `useReviewStore` in `afterEach` per the established idiom):
1. mode fetch rejects → store untouched, no throw.
2. mode 404 → store untouched.
3. mode `{"mode":"live","source":null}` → no document fetch (assert fetch called once).
4. mode review + document 200 with a minimal valid format:1 body → `sessions.length === 1`, `sourceName` === the mode payload's `source`.
5. mode review + document 500 → store untouched, no throw.

`reviewbar.test.tsx` addition: with a two-session document where session 2 has `note: "why this run"`, open the session picker and assert the option for session 2 carries `title="why this run"` (react-aria options render in a portal — the file already has the CSS.escape polyfill and portal idioms; follow them).

- [ ] **Step 2: Run to verify failure** — `cd web && npx vitest run src/__tests__/bootstrap.test.ts src/__tests__/reviewbar.test.tsx` → FAIL.

- [ ] **Step 3: Implement**

`web/src/data/bootstrap.ts` (~40 lines): header comment states the soft-fail contract and why ("the shell's ONLY boot fetch; a static file server must behave exactly as before this module existed"). `App.tsx`: `useEffect(() => { void bootstrapFromServer(); }, [])` — a one-shot, fire-and-forget mount effect placed in the `App` component; note that `importText` failures surface through the store's existing `importError`, which is correct (a server-supplied malformed document SHOULD show the banner — distinguish transport failures (silent) from validation failures (banner) and encode exactly that: transport/HTTP errors return silently BEFORE calling importText; importText itself is only called with a 200 body).

`Select.tsx`: `title?: string` on `SelectItem`; `<ListBoxItem ... textValue={item.label}>` gains `title={item.title}` — check the actual option JSX and put the attribute on the element that becomes the DOM option.

- [ ] **Step 4: Verify green** — `cd web && npx vitest run src/__tests__/bootstrap.test.ts src/__tests__/reviewbar.test.tsx && npm run test && npm run check:fix && npm run check && npm run typecheck && npm run build` (build proves no bundling surprise from the new fetch module).

- [ ] **Step 5: Commit**

```bash
git add web/src/data/bootstrap.ts web/src/App.tsx web/src/ui/Select.tsx web/src/shell/ReviewBar.tsx web/src/__tests__/bootstrap.test.ts web/src/__tests__/reviewbar.test.tsx
git commit -m "feat(web): boot hydration from /api/mode + /api/document; session-note tooltips

The shell's first boot fetch, failing soft — static serving and the
offline pin behave exactly as before.

Assisted-by: Claude Fable 5"
```

---

### Task 7: Playwright review-mode boot specs

**Files:**
- Modify: `tests/_fixtures/_dashboard_harness.py` (or wherever `DashboardHarness` constructs `MonitorServer` — allow passing `mode/document/source_name`), `tests/e2e/monitor/dashboard/conftest.py` (new fixture `review_dash` serving a two-session document), `tests/e2e/monitor/dashboard/test_review_shell.py` (append specs)

**Interfaces:**
- Consumes: T4's `MonitorServer` kwargs; T6's boot behavior; the existing `shell_dash` fixture + `_import_fixture` idioms.
- Produces: two browser-marked specs:

1. `test_review_mode_boots_hydrated(review_dash, page)` — `page.goto(harness.url)`; assert `review-bar` appears WITHOUT any Import interaction; `session-picker` visible; open it and assert two options, and the noted session's option carries the `title` attribute; `source-name` shows the fixture source string; navigate to `#/topology` and back (smoke that hydrated data drives the full shell).
2. `test_live_mode_still_boots_empty(shell_dash, page)` — pins that a live-mode server (mode endpoint returns live) boots to the Import front door exactly as today (`import-input` visible, no `review-bar`).

The `review_dash` document: build a two-session `MonitorExport` in the fixture from the committed `web/fixtures/minimal.json` (parse, duplicate the session with a new id/label and `note="second run"`) — no new fixture file, no generator change.

- [ ] **Step 1: Write the failing specs**, following the file's established helpers (module docstring documents the react-aria option idioms — options via `get_by_role("option", ...)`).
- [ ] **Step 2: `make web`** (rebuild dist with T6's bundle) — air-gap must stay green (the new module adds no URLs; if it fails, STOP).
- [ ] **Step 3: `make dashboard`** → expected: all previous + 2 new pass (count the lane before/after; state both numbers in your report).
- [ ] **Step 4: harness sanity + ruff** — `uv run pytest tests/e2e/monitor/dashboard/test_harness.py -x -q && uv run ruff format --check tests/ && uv run ruff check tests/`.
- [ ] **Step 5: Commit**

```bash
git add tests/_fixtures/_dashboard_harness.py tests/e2e/monitor/dashboard/conftest.py tests/e2e/monitor/dashboard/test_review_shell.py
git commit -m "test(dashboard): review-mode boot hydration + live-mode empty-boot pins

Assisted-by: Claude Fable 5"
```

---

### Task 8: Docs + gates + ratchet

**Files:**
- Modify: `docs/guide/monitor.md` (replace the "no producer exists" paragraph: real instructions — `otto monitor --live --db lab.db --label "fan fix" --note "..."`, then `otto monitor lab.db`; document the CLI split and the breaking changes; keep the guide's voice), plus every stale `--file`/legacy-format mention Task 5's grep reported (check `docs/architecture/lifecycles/monitor.md` too — it was flagged stale once already).
- Modify: `web/vite.config.ts` only if measured coverage moved (bootstrap.ts is small and tested — expect a no-op or a tiny raise; follow the precedent comment format with exact numbers).

Steps:
1. `rm -rf docs/_static/generated && uv run nox -s docs` — fresh-state docs build; the capture must still pass (T6's soft-fail is what keeps it working — this run is that claim's proof). Zero `-W` warnings.
2. `make coverage-hostless` — full hostless suite.
3. `uv run nox -s lint typecheck`.
4. `make web` (drift + build + air-gap ×2).
5. `cd web && npm run test:coverage` — recalibrate thresholds only if needed, precedent format.
6. `make dashboard` — one confirming run.
7. `make import-snapshot` — zero diff expected (new `session.py`/`export.py` are NOT imported at CLI startup unless `otto monitor` runs — verify the lazy-import discipline; a diff here means an import leaked to module scope somewhere hot: fix the import placement, don't re-snapshot).
8. `make schema` — zero diff (no model changes).
9. Commit docs (+ ratchet if touched):

```bash
git add docs/guide/monitor.md <other stale docs> <vite.config.ts if touched>
git commit -m "docs(guide): monitor sessions — live capture and review workflow

Assisted-by: Claude Fable 5"
```

---

### Task 9: Live-bed proof (scoped, final)

**Rules (binding):** peer lab VMs at 10.10.200.x are REAL — read-only collection only, NO VM power operations, light load (default 5 s interval, a few minutes), never kill a wedged run at a tight timeout (let it finish or report).

Steps:
1. From the worktree: `uv run otto monitor --live --db /tmp/claude-1000/p5a-livebed.db --label "p5a live-bed proof" --note "sessionized producer verification" --interval 5` against the configured lab (consult the repo's standard lab config for the peer VMs — `grep -rn "10.10.200" *.toml labs/ 2>/dev/null | head -3` — and pass `--lab` the way the CLI e2e suite does). Let it collect ~3 minutes, then SIGINT cleanly.
2. `uv run otto monitor /tmp/claude-1000/p5a-livebed.db` in the background; `curl -s localhost:<port>/api/mode` → review; `curl -s localhost:<port>/api/document | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['format'], len(d['sessions']), d['sessions'][0]['label'], len(d['sessions'][0]['metrics']), len(d['sessions'][0]['lab']['hosts']), len(d['sessions'][0]['lab']['links']))"` — format 1, ≥1 session, real host count, ≥1 implicit link (the peer VMs hop through nothing — links may legitimately be 0; if so verify `hosts` non-empty and say so), metrics > 0, session `end` set.
3. Run ONE browser check: the dashboard-harness Playwright review spec already proves shell hydration; here just `curl` the served `index.html` (200) — no manual browser automation needed.
4. Shut down; write the observed numbers into the task report. Any failure → STOP, report BLOCKED with the full output (a live-bed failure is a real bug by policy, never "flaky").
5. No commit (nothing changes) — the report is the deliverable; note `/tmp/claude-1000/p5a-livebed.db` as a demo artifact for Chris.

---

## Self-review notes (writing-plans checklist)

- **Spec coverage:** CLI `--live`/source shape (T5), mode/document/export endpoints (T4), schema v2 + refuse-legacy (T1), lab snapshot (T2), producer + crash fallback (T3), boot hydration + soft-fail + note tooltip (T6), Playwright boots (T7), harness pin rewrite (T4), deletions riding last-caller commits (T4/T5), docs + gates (T8), live-bed (T9). Breaking-changes list: all land (T5 `--live` requirement + bare usage + `--file` removal; T1 legacy db; T4 export payload).
- **Deliberate simplifications vs spec:** none — the spec's `configs`-table flattening and elements-stay-empty decisions are carried verbatim (T1/T2).
- **Known verify-at-implementation points (adaptation protocol, disclose in reports):** `resolve_declared_links`/`implicit_links` exact signatures (T2/T5); `RemoteHost` os/interface field names (T2); `MetricCollector(targets=[])` construction idiom (T3/T4); factory-vs-CLI `MetricDB` construction (T1 chooses); none for the CLI shape — the `--live` flag + positional live on the existing single command, no Typer group restructuring (decided with Chris after the subcommand design's token-ambiguity risk was raised).
- **Type consistency:** `SessionFrame`/`new_frame` (T1) ↔ producer (T3) ↔ server kwargs (T4) ↔ CLI (T5); `read_sessions`/`SessionRow` (T1) ↔ `build_db_export` (T3); `document_json` (T3) ↔ endpoints (T4); `SelectItem.title` (T6) ↔ Playwright title assertion (T7).
