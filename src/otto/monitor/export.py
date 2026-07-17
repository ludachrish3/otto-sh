"""The format:1 producer (spec 2026-07-12).

Pure reshaping; validation is the pydantic models' job — the schema drift
guards police this module for free.
"""

import json
from datetime import datetime

from ..models import (
    ChartSpecRecord,
    EventRecord,
    LabSnapshot,
    LogEventRecord,
    MetricRecord,
    MonitorExport,
    SessionMeta,
    SessionRecord,
    TabSpecRecord,
    TunnelRecord,
)
from .collector import MetricCollector
from .db import MetricDB, SessionRow, read_sessions
from .session import SessionFrame


def _split_series_key(key: str) -> tuple[str, str]:
    """Split a ``get_series()`` key into ``(host, label)``.

    Live keys are ``"host/label"`` — built by ``MetricCollector._record_point``
    as ``key = f"{host_name}/{label}"`` (collector.py) and read back the same
    way by ``MetricStore.hosts_from_series`` (``key.split("/")[0]``, store.py).
    A host id is never allowed to contain ``/`` (host-id grammar), but a
    metric label legitimately can (e.g. ``"proc/io read"``), so splitting on
    the FIRST slash only is the correct reshape. A key with no ``/`` at all
    (unreachable from ``_record_point`` today, since every key it builds
    carries a host prefix) is still matched defensively as ``host=""``,
    rather than ``str.partition``'s bare default (which would put the whole
    key in ``host`` and leave ``label`` empty).
    """
    host, sep, label = key.partition("/")
    return (host, label) if sep else ("", host)


def _series_records(collector: MetricCollector) -> list[MetricRecord]:
    """Reshape ``get_series()`` into flat, per-point :class:`MetricRecord` rows."""
    records: list[MetricRecord] = []
    for key, points in collector.get_series().items():
        host, label = _split_series_key(key)
        records.extend(
            MetricRecord(timestamp=p.ts, host=host, label=label, value=p.value, meta=p.meta)
            for p in points
        )
    return records


def _event_records(collector: MetricCollector) -> list[EventRecord]:
    """Reshape ``get_events()`` into :class:`EventRecord` rows, ids intact."""
    return [
        EventRecord(
            id=ev.id,
            timestamp=ev.timestamp,
            end_timestamp=ev.end_timestamp,
            label=ev.label,
            source=ev.source,
            color=ev.color,
            dash=ev.dash,
        )
        for ev in collector.get_events()
    ]


def _log_event_records(collector: MetricCollector) -> list[LogEventRecord]:
    """Validate ``get_log_events()``'s JSON-safe rows into :class:`LogEventRecord`."""
    return [LogEventRecord.model_validate(row) for row in collector.get_log_events()]


def session_meta(
    collector: MetricCollector,
    *,
    interval: float | None = None,
) -> SessionMeta:
    """Reshape ``get_meta_model()`` into an archival session meta.

    The result is a lenient :class:`~otto.models.monitor.SessionMeta`.

    **The single source of this reshape — never hand-roll it.** The two models
    do NOT share a field name for the chart list: ``MonitorMeta`` calls it
    ``metrics``, ``SessionMeta`` calls it ``charts``. Because ``SessionMeta`` is
    a lenient :class:`~otto.models.monitor.RowModel` (``extra='ignore'``),
    feeding it a raw ``MonitorMeta`` dump *validates silently* and drops every
    chart spec — ``tabs`` survives (same name), so the damage looks partial and
    is easy to miss. A session persisted that way renders with no chart
    grouping and no units, exactly as an empty ``chart_map`` does.

    ``charts``/``tabs`` come from the parser catalog rather than from collected
    data, so they are fully known even before the first tick — which is what
    lets the ``--db`` paths persist ``meta_json`` at construction time.

    Args:
        collector: The collector whose parser catalog describes this session.
        interval: The run's collection interval in seconds. **Pass this
            whenever the meta is built before :meth:`MetricCollector.run` has
            started** — i.e. from the construction-time ``--db`` call sites in
            :mod:`otto.cli.monitor` (its ``--interval`` option) and
            :mod:`otto.suite.plugin` (its ``--monitor-interval``). The
            collector only records its own ``interval`` once ``run()`` begins,
            so reading it off the model at construction yields ``None``
            *permanently* for a DB archive: nothing repairs it later (unlike
            ``chart_map``, which the collector rewrites incrementally). A null
            interval is not cosmetic — the frontend resolves cadence as
            ``chart.interval ?? session.meta.interval``
            (``web/src/data/health.ts``), so with both null ``cadenceMs()``
            returns null and derived health/staleness is unresolvable for the
            whole session. Omit it only when the collector has already run and
            its own recorded value is authoritative (:func:`build_live_export`).
    """
    meta = collector.get_meta_model()
    return SessionMeta(
        interval=interval if interval is not None else meta.interval,
        charts=[ChartSpecRecord(**spec.model_dump()) for spec in meta.metrics],
        tabs=[TabSpecRecord(**spec.model_dump()) for spec in meta.tabs],
    )


def build_session_metric_db(
    path: str,
    frame: SessionFrame,
    lab: LabSnapshot,
    meta_collector: MetricCollector,
    *,
    interval: float,
) -> MetricDB:
    """Construct an unopened, archive-real v2 :class:`~otto.monitor.db.MetricDB`.

    **The shared construction for every ``--db``-backed session-monitor call
    site — never hand-roll ``MetricDB(..., lab_json="{}", meta_json="{}")``.**
    Used by :meth:`otto.suite.suite.OttoSuite.start_monitor`,
    :class:`otto.suite.plugin.OttoPlugin`'s ``--monitor --monitor-output
    *.db`` session fixture, and ``otto.cli.monitor``'s ``--live --db`` path —
    closing a long-standing triplication: all three call sites used to build
    their own ``MetricDB``. One of them (``OttoSuite.start_monitor``) had
    drifted and kept persisting ``lab_json="{}"``/``meta_json="{}"`` — the
    degraded-archive shape this producer phase spent three fix waves
    eliminating elsewhere (no chart specs, no units, null interval, no lab
    topology on replay); the CLI's copy was already correct, just duplicated.

    Args:
        path: Filesystem path for the SQLite archive.
        frame: This run's session identity (see
            :func:`otto.monitor.session.new_frame`).
        lab: The frozen lab config for this session (see
            :func:`otto.monitor.session.snapshot_lab`).
        meta_collector: A throwaway collector, built the same way (same
            hosts/targets/parsers) as the real collector that will go on to
            own this ``MetricDB`` — never run, never handed this database.
            Needed because ``MetricDB``'s constructor needs ``meta_json`` up
            front, but the real collector can't be constructed until the
            ``MetricDB`` object it will own already exists.
        interval: The run's collection interval in seconds. **Must** be
            passed explicitly — see :func:`session_meta`'s docstring for why
            a null interval here would persist forever and leave the
            replayed session's derived health unresolvable.

    Returns:
        An unopened :class:`~otto.monitor.db.MetricDB` — call sites open it
        themselves, directly or by handing it to a
        :class:`~otto.monitor.collector.MetricCollector` as ``db=``.
    """
    meta_json = session_meta(meta_collector, interval=interval).model_dump_json()
    return MetricDB(path, frame, lab_json=lab.model_dump_json(), meta_json=meta_json)


def build_live_export(
    frame: SessionFrame,
    collector: MetricCollector,
    lab: LabSnapshot,
) -> MonitorExport:
    """Wrap one live collector's in-memory state into a format:1 document.

    Always exactly one session, taking its identity straight from *frame*:
    ``end`` mirrors ``frame.end`` verbatim, so a still-open frame (``end is
    None``) produces a still-open session — a live export is never "crashed",
    so no last-sample fallback applies here (that's :func:`build_db_export`'s
    job, for archives a process actually walked away from). Nothing here reads
    the wall clock: *frame* fully determines the session's bounds.

    Args:
        frame: This run's session identity/lifetime (see
            :mod:`otto.monitor.session`).
        collector: The live (or scripted) collector to snapshot.
        lab: The frozen lab config for this session (see
            :func:`otto.monitor.session.snapshot_lab`).

    Returns:
        The single-session :class:`~otto.models.monitor.MonitorExport`.
    """
    session = SessionRecord(
        id=frame.id,
        label=frame.label,
        note=frame.note,
        start=frame.start,
        end=frame.end,
        lab=lab,
        meta=session_meta(collector),
        metrics=_series_records(collector),
        events=_event_records(collector),
        log_events=_log_event_records(collector),
        chart_map=collector.get_chart_map(),
        tunnels=collector.get_tunnel_records(),
    )
    return MonitorExport(format=1, sessions=[session])


def _ts(value: str) -> datetime:
    """Parse one archived ISO-8601 timestamp column into a ``datetime``.

    Every timestamp in a v2 archive was written by db.py as ``.isoformat()``,
    so ``fromisoformat`` round-trips it exactly (tz-offset included). Parsing
    here — rather than handing the raw string to a pydantic field declared
    ``datetime`` and letting it coerce — keeps the record constructors
    type-honest, which is what lets ``ty`` police this module.
    """
    return datetime.fromisoformat(value)


def _fallback_end(row: SessionRow) -> datetime:
    """Crash-tolerant session end (the producer's job, not db.py's).

    db.py leaves a crashed session's ``end`` NULL on purpose (see
    :meth:`MetricDB.finalize`). A null ``end`` falls back to the last
    sample's timestamp (``row.metrics`` is SELECTed ``ORDER BY ts``, so the
    last tuple is the latest); a session with zero samples at all falls back
    to its own ``start``.
    """
    if row.end is not None:
        return _ts(row.end)
    if row.metrics:
        return _ts(row.metrics[-1][0])  # (ts, host, label, value, source) — ts first
    return _ts(row.start)


def _session_record(row: SessionRow) -> SessionRecord:
    """Reshape one archived session row into an export session record.

    Maps a :class:`~otto.monitor.db.SessionRow` onto a
    :class:`~otto.models.monitor.SessionRecord`.

    ``chart_map`` comes straight off its own persisted column: it is not
    derivable from anything else in the archive (``SessionMeta.charts`` holds
    one entry per rendered CHART, while metric rows carry per-SERIES labels,
    and the two rarely match — ``TopCpuParser`` emits ``"Overall CPU"`` and
    ``"proc/<pid>"`` into chart ``"CPU"``). Emitting an empty map instead
    would not merely lose grouping: the frontend falls back to
    ``chartMap[label] ?? label`` (``web/src/data/seriesTree.ts``), so every
    series would become its own ungrouped, unit-less chart.
    """
    metrics = [
        MetricRecord(timestamp=_ts(ts), host=host, label=label, value=value, source=source)
        for ts, host, label, value, source in row.metrics
    ]
    events = [
        EventRecord(
            id=event_id,
            timestamp=_ts(ts),
            end_timestamp=_ts(end_ts) if end_ts is not None else None,
            label=label,
            source=source,
            color=color,
            dash=dash,
        )
        for event_id, ts, end_ts, label, source, color, dash in row.events
    ]
    log_events = [
        LogEventRecord(timestamp=_ts(ts), host=host, tab=tab, fields=json.loads(fields_json))
        for ts, host, tab, fields_json in row.log_events
    ]
    return SessionRecord(
        id=row.id,
        label=row.label,
        note=row.note,
        start=_ts(row.start),
        end=_fallback_end(row),
        lab=LabSnapshot.model_validate_json(row.lab_json),
        meta=SessionMeta.model_validate_json(row.meta_json),
        metrics=metrics,
        events=events,
        log_events=log_events,
        chart_map=json.loads(row.chart_map_json),
        tunnels=[TunnelRecord.model_validate(t) for t in json.loads(row.tunnels_json)],
    )


def build_db_export(path: str) -> MonitorExport:
    """Read every session from a v2 archive into one format:1 document.

    Args:
        path: Filesystem path to the v2 SQLite archive (see
            :func:`otto.monitor.db.read_sessions`; fails loud on anything
            that is not schema v2).

    Returns:
        A :class:`~otto.models.monitor.MonitorExport` with one
        :class:`~otto.models.monitor.SessionRecord` per archived session, in
        the archive's own (start-ordered) order.
    """
    return MonitorExport(format=1, sessions=[_session_record(row) for row in read_sessions(path)])


def document_json(export: MonitorExport) -> str:
    """Serialize *export* exactly as ``scripts/gen_monitor_fixtures.py`` does.

    The generator's ``dumps()`` calls ``doc.model_dump(mode="json",
    exclude_none=True)`` then ``json.dumps(...)``; ``model_dump_json`` is
    pydantic's direct equivalent of that same ``mode="json"`` shape, so
    passing the identical ``exclude_none=True`` argument here guarantees the
    frontend's normalizer (``web/src/data/exportDoc.ts``) sees identical field
    presence/absence for a live/DB export as it does for the committed
    fixtures, byte-for-byte whitespace aside.
    """
    return export.model_dump_json(exclude_none=True)
