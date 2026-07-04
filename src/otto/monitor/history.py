"""Historical import/export for monitor data.

Pure functions over MetricStore: the JSON ``--file`` format and the SQLite
database written by live collection. Lenient on read (RowModel), exact on
write — the export is byte-compatible with what ``--file`` accepts.
"""

import json
from collections import deque
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import ValidationError

from ..models import EventRecord, LogEventRecord, MetricPoint, MetricRecord
from .events import MonitorEvent
from .parsers import LogEvent
from .store import MetricStore


def load_json_into(store: MetricStore, path: str) -> None:
    """Load historical metrics and events from a JSON file into *store*.

    Expected format::

        {
            "metrics": [{"timestamp": "...", "label": "...", "value": 42.0}, ...],
            "events": [
                {"timestamp": "...", "label": "...", "source": "...", "color": "..."},
                ...,
            ],
        }

    The ``host`` field is optional for backward compatibility.
    """
    with Path(path).open() as f:
        data = json.load(f)
    for point in data.get("metrics", []):
        try:
            rec = MetricRecord.model_validate(point)
        except ValidationError:
            continue
        key = f"{rec.host}/{rec.label}" if rec.host else rec.label
        if key not in store.series:
            store.series[key] = deque()
        store.series[key].append(
            MetricPoint.model_validate({"ts": rec.timestamp, "value": rec.value, "meta": rec.meta})
        )
    for label, chart in data.get("chart_map", {}).items():
        store.chart_map[label] = chart
    for ev in data.get("events", []):
        try:
            rec = EventRecord.model_validate(ev)
        except ValidationError:
            continue
        event = MonitorEvent(
            timestamp=rec.timestamp,
            label=rec.label,
            source=rec.source,
            color=rec.color,
            dash=rec.dash,
            end_timestamp=rec.end_timestamp,
        )
        if rec.id is not None:
            event.id = rec.id
            store.note_imported_event(event)
        else:
            store.add_event(event, rowid=0)
    for row in data.get("log_events", []):
        try:
            rec = LogEventRecord.model_validate(row)
        except ValidationError:
            continue
        store.append_log_event(rec.host, rec.tab, LogEvent(ts=rec.timestamp, fields=rec.fields))


async def load_sqlite_into(store: MetricStore, path: str) -> None:
    """Load historical metrics and events from a SQLite database into *store*."""
    async with aiosqlite.connect(path) as conn:
        conn.row_factory = aiosqlite.Row
        # Support both the new schema (with host column) and the old schema (without)
        col_names = {row[1] async for row in await conn.execute("PRAGMA table_info(metrics)")}
        has_host = "host" in col_names
        query = (
            "SELECT ts, host, label, value FROM metrics ORDER BY ts"
            if has_host
            else "SELECT ts, label, value FROM metrics ORDER BY ts"
        )
        async for row in await conn.execute(query):
            try:
                rec = MetricRecord.model_validate(dict(row))
            except ValidationError:
                continue
            key = f"{rec.host}/{rec.label}" if rec.host else rec.label
            if key not in store.series:
                store.series[key] = deque()
            store.series[key].append(
                MetricPoint.model_validate({"ts": rec.timestamp, "value": rec.value, "meta": None})
            )
        event_cols = {row[1] async for row in await conn.execute("PRAGMA table_info(events)")}
        has_end_ts = "end_ts" in event_cols
        events_query = (
            "SELECT id, ts, end_ts, label, source, color, dash FROM events ORDER BY ts"
            if has_end_ts
            else "SELECT id, ts, label, source, color, dash FROM events ORDER BY ts"
        )
        async for row in await conn.execute(events_query):
            try:
                rec = EventRecord.model_validate(dict(row))
            except ValidationError:
                continue
            event = MonitorEvent(
                timestamp=rec.timestamp,
                label=rec.label,
                source=rec.source,
                color=rec.color,
                dash=rec.dash,
                end_timestamp=rec.end_timestamp,
            )
            if rec.id is not None:
                event.id = rec.id
                # Per-event counter advance: with ids out of ts-order the
                # counter can end HIGHER than the legacy batch max()+1 — never
                # lower, so no collision; deliberately monotone (pinned by
                # test_from_sqlite_out_of_order_ids_keep_counter_collision_free).
                store.note_imported_event(event)
            else:
                store.add_event(event, rowid=0)
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


def to_json(store: MetricStore) -> str:
    """Serialize all metrics and events in *store* to a JSON string compatible with ``--file``."""
    metrics: list[dict[str, Any]] = []
    for key, pts in store.series.items():
        host = key.split("/")[0] if "/" in key else ""
        label = key.split("/", 1)[1] if "/" in key else key
        metrics.extend(
            MetricRecord(
                timestamp=pt.ts, host=host, label=label, value=pt.value, meta=pt.meta
            ).model_dump(mode="json", exclude_none=True)
            for pt in pts
        )
    return json.dumps(
        {
            "metrics": metrics,
            "events": [e.to_dict() for e in store.events()],
            "chart_map": dict(store.chart_map),
            "log_events": [
                LogEventRecord(
                    timestamp=ev.ts, host=host, tab=tab, fields=dict(ev.fields)
                ).model_dump(mode="json")
                for host, tab, ev in store.snapshot_log_events()
            ],
        },
        indent=2,
    )
