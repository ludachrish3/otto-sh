"""
MetricCollector — collects metrics from multiple UnixHosts via asyncio.gather().

On each tick all hosts are polled simultaneously so results share one timestamp.

Live collection only: a collector polls hosts via ``asyncio.gather()`` per
tick, optionally persisting to a session-bound :class:`~otto.monitor.db.MetricDB`
(schema v2). Review (historical) data is a separate concern — see
:mod:`otto.monitor.export` (``build_db_export``/``build_live_export``) for the
format:1 producer that reads/wraps collected data, and :mod:`otto.cli.monitor`
for how a saved export is loaded back for review. A bare ``MetricCollector``
with no live targets (e.g. ``MetricCollector(targets=[])``) still declares its
parser catalog so ``get_meta_model()`` remains well-formed.
"""

import asyncio
import contextlib
import copy
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from ..models import ChartSpec, MetricPoint, MonitorMeta, TabSpec
from ..result import CommandResult, Results
from .broadcast import Broadcaster
from .db import MetricDB
from .events import MonitorEvent
from .parsers import (
    DEFAULT_PARSERS,
    LogEvent,
    MetricDataPoint,
    MetricParser,
    ParseContext,
    default_catalog,
)
from .snmp import SnmpMetric, SnmpSource, process_snmp_values, resolve_snmp_metric
from .store import MetricStore

if TYPE_CHECKING:
    from ..host.remote_host import RemoteHost


class MetricView(Protocol):
    """The presentation surface a series needs to be charted.

    Both :class:`~otto.monitor.parsers.MetricParser` and
    :class:`~otto.monitor.snmp.SnmpMetric` satisfy this structurally, so the
    record/publish path is identical whether a point came from a shell command
    or an SNMP OID.

    The members are read-only properties, not plain attributes: the collector
    only ever reads them, and a mutable attribute member would demand writes
    that :class:`~otto.monitor.snmp.SnmpMetric` (``frozen=True``) cannot accept.
    """

    @property
    def chart(self) -> str:
        """Title of the chart this series is drawn on."""
        ...

    @property
    def y_title(self) -> str:
        """Label for the chart's y-axis."""
        ...

    @property
    def unit(self) -> str:
        """Unit the values are expressed in (e.g. ``%``, ``MiB``)."""
        ...

    @property
    def tab(self) -> str:
        """Id of the dashboard tab the chart belongs to."""
        ...

    @property
    def tab_label(self) -> str:
        """Human-readable name of that dashboard tab."""
        ...


logger = logging.getLogger(__name__)

# Ticks a parser may produce nothing before the "silent parser" warning fires.
# Deliberately "never produced by tick K", not "K consecutive empties": rate
# parsers legitimately return {} on their baseline tick and sparse log-sourced
# parsers go quiet between writes — only a source that has NEVER produced is
# suspect. K=3 clears the baseline tick with margin.
_SILENT_PARSER_TICKS = 3


@dataclass
class MonitorTarget:
    """Pairs a UnixHost with the parser dict to use when collecting from it.

    By default all hosts use DEFAULT_PARSERS.  Pass a custom dict to add new
    metrics or override built-in commands for a specific host::

        MonitorTarget(
            host=gpu_host,
            parsers={
                **DEFAULT_PARSERS,
                "nvidia-smi --query-gpu=utilization.gpu ...": NvidiaGpuParser(),
            },
        )

    In most cases you do not construct these directly — use
    :func:`~otto.monitor.parsers.register_host_parsers` from an init module and
    let the CLI build targets automatically.

    Set ``snmp`` to collect from this host over SNMP instead of by running shell
    commands — the host then needs no shell parsers, and ``parsers`` is ignored.
    This is what lets otto monitor a host (embedded or Unix) over a channel
    separate from command execution.
    """

    host: "RemoteHost"
    parsers: dict[str, MetricParser] = field(default_factory=lambda: copy.deepcopy(DEFAULT_PARSERS))
    core_count: int = field(default=1)
    snmp: SnmpSource | None = field(default=None)


class MetricCollector:
    """
    Collects numeric metrics from multiple UnixHosts and stores time-series data.

    On each tick, all hosts are polled simultaneously via asyncio.gather() so that
    results from every host share the same timestamp.

    Series keys have the form ``"hostname/metric_label"`` (e.g. ``"router1/CPU %"``).

    Args:
        hosts: Remote hosts to monitor. Pass ``None`` (or omit) when loading historical data.
        parsers: Metric parser instances. Defaults to DEFAULT_PARSERS (cpu, mem, disk, load).
        db: Optional session-bound :class:`~otto.monitor.db.MetricDB` for persistence
            (unopened — this collector opens it lazily via :meth:`init_db`). ``None``
            means in-memory only. The collector itself stays session-blind: framing
            (choosing the label/note and constructing the ``MetricDB``) is the
            caller's job, not the collector's — one process run is one live session.
    """

    def __init__(
        self,
        hosts: "Sequence[RemoteHost] | None" = None,
        parsers: list[MetricParser] | None = None,
        db: MetricDB | None = None,
        targets: "list[MonitorTarget] | None" = None,
    ) -> None:
        parser_dict: dict[str, MetricParser] = {}
        if targets is not None:
            self._targets = targets
        else:
            parser_dict = (
                {p.command: p for p in parsers} if parsers is not None else default_catalog()
            )
            self._targets = [MonitorTarget(host=h, parsers=parser_dict) for h in (hosts or [])]

        # Unified presentation metadata for the dashboard, drawn from both
        # collection modes: shell parsers (deduped by command) and SNMP OID
        # descriptors (deduped by OID). SNMP targets contribute no shell
        # parsers — their ``parsers`` default is never consulted.
        seen_commands: set[str] = set()
        unified: list[MetricParser] = []
        seen_oids: set[str] = set()
        snmp_views: list[SnmpMetric] = []
        for t in self._targets:
            if t.snmp is not None:
                for oid in t.snmp.oids:
                    if oid not in seen_oids:
                        seen_oids.add(oid)
                        view = resolve_snmp_metric(oid)
                        if view.meta_of is None:
                            snmp_views.append(view)
            else:
                for p in t.parsers.values():
                    if p.command not in seen_commands:
                        seen_commands.add(p.command)
                        unified.append(p)
        self._parsers: dict[str, MetricParser] = {p.command: p for p in unified}

        # A collector built via `hosts=[]` with no live targets (e.g. a
        # scripted test collector) still declares its parser CATALOG: /api/meta
        # must describe tabs/charts even with nothing collected yet. This does
        # NOT fire for review mode's `MetricCollector(targets=[])` (targets is
        # explicitly `[]`, not `None`) — that collector intentionally serves
        # empty meta, since review mode renders from the loaded `document`
        # (see otto.monitor.export/otto.cli.monitor), not from /api/meta.
        if not self._targets and targets is None:
            unified = list(parser_dict.values())
            self._parsers = dict(parser_dict)

        # list[MetricView] is invariant, so build by extending from the
        # concrete lists (Iterable[T] is covariant) rather than splatting.
        self._views: list[MetricView] = []
        self._views.extend(unified)
        self._views.extend(snmp_views)

        self._hosts = [t.host for t in self._targets]
        self._pending_db = db

        # All series keyed by "hostname/label" — e.g. "router1/CPU %" for regular
        # metrics or "router1/proc/python" for per-process CPU.  Each point is a
        # ``MetricPoint`` so that every series shares exactly one data structure
        # regardless of its type.
        # Series are created lazily in _process_host_results() as data arrives,
        # so multi-series parsers (which produce labels not known until parse() runs)
        # are handled naturally without upfront enumeration.
        # In-memory series/chart-map/event bookkeeping — see MetricStore.
        self._store = MetricStore()

        # SSE fan-out to subscriber queues
        self._broadcast = Broadcaster()

        # Persistent DB store — opened by init_db(), closed by close_db().
        self._db: MetricDB | None = None

        # Global collection interval in seconds, recorded by run() before the
        # collection loop starts. None until a live run happens — a review
        # collector (targets=[], see otto.cli.monitor) and scripted test
        # collectors that never call run() report it as None via
        # get_meta_model().
        self._global_interval: float | None = None

        # Parser-health state, keyed (host_name, command) — or (host_name, oid)
        # for the SNMP layer. Command failures are edge-triggered: _failing
        # counts consecutive failed ticks per key; the 0->1 transition warns,
        # the pop-on-success warns the recovery with the outage length. The
        # never-produced backstop below stays warn-once per run.
        self._failing: dict[tuple[str, str], int] = {}
        self._warned_silent: set[tuple[str, str]] = set()
        self._health_ticks: dict[tuple[str, str], int] = {}
        self._health_produced: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def init_db(self) -> None:
        """Open the persistent DB (no-op without a ``db`` at construction). See MetricDB.

        Must be awaited before any DB writes.  Called automatically by
        :meth:`run`; callers that skip ``run()`` (e.g. tests) should call
        this explicitly.
        """
        if self._db is not None or self._pending_db is None:
            return
        await self._pending_db.open()
        self._db = self._pending_db

    async def close_db(self) -> None:
        """Close the persistent DB connection and release the file lock."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def close(self) -> None:
        """Close the DB connection and all live host sessions.

        Must be awaited before the surrounding event loop exits — otherwise
        subprocess transports owned by LocalSession are GC'd after the loop
        is closed and raise "Event loop is closed" from __del__.
        """
        await asyncio.gather(
            *(t.host.close() for t in self._targets),
            return_exceptions=True,
        )
        await self.close_db()

    # ------------------------------------------------------------------
    # Live collection
    # ------------------------------------------------------------------

    async def _collect_one(
        self,
        target: MonitorTarget,
        timeout: float,
        commands: "list[str] | None" = None,
    ) -> "Results | dict[str, float | None] | None":
        """Collect metrics from a single host with a per-tick timeout.

        SNMP targets GET their OIDs (bounded by ``timeout`` so a stuck relay is
        skipped for the tick, mirroring the shell path) and return the raw
        ``{oid: value}`` dict — descriptor resolution, rate conversion, and
        ``meta_of`` routing happen downstream in :meth:`_process_snmp_results`,
        which needs the target for its per-target ``RateTracker``. Shell
        targets pass *timeout* to :meth:`~otto.host.host.BaseHost.run` as a
        deadline-based budget shared across all parser commands; when a
        command exceeds it, ``run``'s own ``wait_for`` fires and triggers
        Ctrl+C session recovery (see :meth:`ShellSession._recover_session`) so
        the session stays healthy.

        *commands* restricts the batch to a subset of the target's parser
        commands — used by :meth:`run` when a parser's own ``interval`` puts
        its command on a different bucket than the rest of the target's
        commands. ``None`` collects every one of the target's commands, as
        before (SNMP targets ignore it — they have no per-command intervals).
        """
        if target.snmp is not None:
            return await asyncio.wait_for(
                target.snmp.client.get(target.snmp.oids),
                timeout,
            )
        return await target.host.run(
            commands if commands is not None else list(target.parsers.keys()),
            timeout=timeout,
        )

    async def _collect_bucket(
        self,
        entries: "list[tuple[MonitorTarget, list[str] | None]]",
        timeout: float,
    ) -> None:
        """Collect and process one tick for a single interval bucket.

        Shared tick body for every bucket loop spawned by :meth:`run` —
        parameterized by *entries* (the ``(target, commands)`` pairs riding
        this bucket) so a parser's faster or slower ``interval`` only affects
        its own bucket's cadence.
        """
        results = await asyncio.gather(
            *(self._collect_one(target, timeout, commands) for target, commands in entries),
            return_exceptions=True,
        )
        ts = datetime.now(tz=timezone.utc)
        for (target, _commands), result in zip(entries, results, strict=True):
            match result:
                case Results() as res:
                    await self._process_host_results(
                        target.host.id,
                        ts,
                        list(res),
                        target.parsers,
                        ctx=ParseContext(core_count=target.core_count, ts=ts),
                    )
                case dict() as values:
                    await self._process_snmp_results(target, ts, values)
                case BaseException():
                    logger.warning(
                        "Monitor: error collecting from %s: %s", target.host.name, result
                    )
                case _:
                    continue

    async def run(
        self,
        interval: timedelta = timedelta(seconds=5),
        duration: timedelta | None = None,
    ) -> None:
        """
        Collect metrics from all hosts on each tick.

        Each host's collection is bounded by the interval — if a host does not
        respond in time, it is skipped for that tick and the session is
        recovered automatically.  This prevents a single slow host from
        blocking collection on all other hosts.

        Commands are bucketed by effective interval (``parser.interval or
        interval``) and each bucket runs its own collection loop concurrently,
        so a parser that declares a faster interval ticks more often without
        speeding up the rest of that host's commands. SNMP targets always ride
        the global (*interval*) bucket. With no per-parser intervals set,
        there is exactly one bucket and behavior is identical to a single
        global loop.

        Runs inside the caller's event loop.  Cancel the task wrapping this
        coroutine (or cancel it directly) to stop collection gracefully.

        Args:
            interval: Collection interval as a timedelta.
            duration: Optional total run time.  ``None`` means run forever.
        """
        if not self._targets:
            raise RuntimeError("Cannot start live collection: no hosts provided")

        await self.init_db()

        # One-time setup: determine core count for each shell host. It is threaded
        # into ParseContext at each collection call site below (e.g. TopCpuParser
        # uses it to normalize per-process CPU%).
        # grep -c ^processor /proc/cpuinfo is universally available on Linux but
        # meaningless for SNMP targets (no shell), so they are skipped — their
        # core_count stays 1 and the SNMP descriptors don't use it.
        shell_targets = [t for t in self._targets if t.snmp is None]
        setup_results = await asyncio.gather(
            *[target.host.run(["grep -c ^processor /proc/cpuinfo"]) for target in shell_targets],
            return_exceptions=True,
        )
        for target, result in zip(shell_targets, setup_results, strict=True):
            match result:
                case Results() as res if len(res) == 1:
                    with contextlib.suppress(ValueError):  # keep default of 1
                        target.core_count = int(res.only.value.strip())
                case BaseException():
                    logger.warning(
                        "Monitor: could not determine core count for %s, defaulting to 1",
                        target.host.name,
                    )

        secs = interval.total_seconds()
        self._global_interval = secs
        start = datetime.now(tz=timezone.utc)

        # Bucket each target's commands by effective interval. SNMP targets
        # ride the global bucket (their source has no per-command intervals).
        buckets: dict[float, list[tuple[MonitorTarget, list[str] | None]]] = {}
        for target in self._targets:
            if target.snmp is not None:
                buckets.setdefault(secs, []).append((target, None))
                continue
            by_interval: dict[float, list[str]] = {}
            for cmd, parser in target.parsers.items():
                by_interval.setdefault(parser.interval or secs, []).append(cmd)
            for bucket_secs, cmds in by_interval.items():
                buckets.setdefault(bucket_secs, []).append((target, cmds))

        async def _bucket_loop(
            bucket_secs: float, entries: "list[tuple[MonitorTarget, list[str] | None]]"
        ) -> None:
            # Initial collection: no sleep, publish as soon as commands return.
            await self._collect_bucket(entries, bucket_secs)
            while duration is None or datetime.now(tz=timezone.utc) - start < duration:
                # Sleep and collect concurrently (as the pre-bucket loop did):
                # the tick period is max(interval, collect_time), not their sum.
                await asyncio.gather(
                    asyncio.sleep(bucket_secs),
                    self._collect_bucket(entries, bucket_secs),
                )

        # Known dormant risk (no shipped parser sets .interval yet, so this
        # gather has exactly one bucket today): if a bucket loop ever escapes
        # with a processing exception (parser.parse or a DB write raising —
        # collection errors are contained per-tick inside _collect_bucket),
        # gather() re-raises without cancelling sibling bucket loops, and the
        # CLI's collection_task.cancel() is a no-op on the already-failed
        # task — orphaned buckets would keep polling until process exit.
        # First real multi-bucket activation should harden this (cancel
        # siblings on first exception).
        await asyncio.gather(*(_bucket_loop(s, e) for s, e in buckets.items()))

    async def _record_point(
        self,
        host_name: str,
        ts: datetime,
        label: str,
        dp: MetricDataPoint,
        view: MetricView,
    ) -> None:
        """Store one data point, persist it, and publish it to dashboards.

        Shared by the shell and SNMP collection paths — *view* supplies the
        chart/unit/title presentation regardless of where the point came from.
        """
        key = f"{host_name}/{label}"
        # Hot path: model_construct skips validation (the values are otto's own).
        point = MetricPoint.model_construct(ts=ts, value=dp.value, meta=dp.meta)
        # The store's chart_map only learns a label when its first point lands
        # (append_point), which is strictly AFTER the DB's session row was
        # INSERTed by open() — so the map can never be seeded at construction
        # and must be written out here instead. Fire only on the transition
        # (new label, or a label re-charted), never per point: that is a
        # handful of UPDATEs per run, and it means a session that CRASHES
        # (end left NULL) still carries its grouping. Writing it at finalize
        # instead would lose exactly that case.
        map_changed = self._store.chart_map.get(label) != view.chart
        self._store.append_point(key, point, label=label, chart=view.chart)
        if self._db:
            await self._db.write_point(ts, host_name, label, dp.value)
            if map_changed:
                await self._db.write_chart_map(json.dumps(self._store.chart_map))
        msg: dict[str, Any] = {
            "type": "metric",
            "host": host_name,
            "label": label,
            "chart": view.chart,
            "y_title": view.y_title,
            "unit": view.unit,
            "key": key,
            "ts": ts.isoformat(),
            "value": dp.value,
        }
        if dp.meta is not None:
            msg["meta"] = dp.meta
        self._publish(msg)

    async def _record_log_events(self, host_name: str, tab: str, events: "list[LogEvent]") -> None:
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

    async def _process_host_results(
        self,
        host_name: str,
        ts: datetime,
        cmd_results: "list[CommandResult]",
        parsers: dict[str, MetricParser],
        *,
        ctx: ParseContext,
    ) -> None:
        for cmd_result in cmd_results:
            parser = parsers.get(cmd_result.command)
            if parser is None:
                continue
            key = (host_name, cmd_result.command)
            if cmd_result.retcode != 0:
                # Edge-triggered: warn on each ok->failed transition so
                # transient failures stay visible whenever they happen; a
                # sustained outage logs once (plus its recovery below).
                failed_ticks = self._failing.get(key, 0)
                if failed_ticks == 0:
                    first_line = str(cmd_result.value or "").strip().splitlines()[:1]
                    logger.warning(
                        "Monitor: '%s' failed on %s (exit %d): %s — %s metrics will be missing",
                        cmd_result.command,
                        host_name,
                        cmd_result.retcode,
                        first_line[0] if first_line else "",
                        parser.chart,
                    )
                self._failing[key] = failed_ticks + 1
            else:
                failed_ticks = self._failing.pop(key, 0)
                if failed_ticks:
                    logger.warning(
                        "Monitor: '%s' recovered on %s after %d failed tick(s)",
                        cmd_result.command,
                        host_name,
                        failed_ticks,
                    )
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

    def _note_health(self, key: tuple[str, str], *, produced: bool, what: str) -> None:
        """Track never-produced-by-tick-K per (host, command/oid); warn once."""
        if produced:
            self._health_produced.add(key)
            return
        if key in self._health_produced or key in self._warned_silent:
            return
        ticks = self._health_ticks.get(key, 0) + 1
        self._health_ticks[key] = ticks
        if ticks >= _SILENT_PARSER_TICKS:
            self._warned_silent.add(key)
            logger.warning(
                "Monitor: parser %s ('%s') has produced no data on %s after %d ticks",
                what,
                key[1],
                key[0],
                _SILENT_PARSER_TICKS,
            )

    async def _process_snmp_results(
        self,
        target: MonitorTarget,
        ts: datetime,
        values: dict[str, float | None],
    ) -> None:
        if target.snmp is None:  # routing invariant from _collect_bucket; keeps ty narrow
            return
        host_name = target.host.id
        for oid, raw in values.items():
            # Not success-gated by design (unlike the shell path above): SNMP
            # has no retcode concept, and transport/PDU failures already warn
            # per batch inside SnmpClient.get, so there is no separate
            # failure layer to avoid double-warning here.
            self._note_health((host_name, oid), produced=raw is not None, what="SNMP OID")
        triples = process_snmp_values(values, rates=target.snmp.rates, ts=ts)
        for label, dp, view in triples:
            await self._record_point(host_name, ts, label, dp, view)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def add_event(
        self,
        label: str,
        timestamp: datetime | None = None,
        color: str = "#888888",
        dash: str = "dash",
        source: str = "manual",
        end_timestamp: datetime | None = None,
    ) -> MonitorEvent:
        """Record a labeled event and push it to all dashboard subscribers."""
        event = MonitorEvent(
            timestamp=timestamp or datetime.now(tz=timezone.utc),
            label=label,
            source=source,
            color=color,
            dash=dash,
            end_timestamp=end_timestamp,
        )
        rowid = await self._db.write_event(event) if self._db else 0
        event = self._store.add_event(event, rowid)
        self._publish({"type": "event", **event.to_dict()})
        return event

    async def delete_event(self, event_id: int) -> bool:
        """Remove an event by id. Returns True if found and removed, False otherwise."""
        if not self._store.remove_event(event_id):
            return False
        if self._db:
            await self._db.delete_event(event_id)
        self._publish({"type": "event_deleted", "id": event_id})
        return True

    async def update_event(
        self,
        event_id: int,
        label: str,
        color: str,
        dash: str,
        end_timestamp: datetime | None = None,
    ) -> "MonitorEvent | None":
        """Update an existing event's label, color, dash, and end_timestamp. Returns the updated event or None."""  # noqa: E501 — long one-liner docstring
        ev = self._store.find_event(event_id)
        if ev is None:
            return None
        ev.label = label
        ev.color = color
        ev.dash = dash
        ev.end_timestamp = end_timestamp
        if self._db:
            await self._db.update_event(ev)
        self._publish({"type": "event_updated", **ev.to_dict()})
        return ev

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_series(self) -> dict[str, list[MetricPoint]]:
        """Return a snapshot of all series (metrics and per-process).

        Format: ``{"hostname/label": [MetricPoint(ts, value, meta), ...]}``
        """
        return self._store.snapshot_series()

    def get_chart_map(self) -> dict[str, str]:
        """Return a mapping of series label → chart group key.

        Used by the dashboard to assign historical series (loaded from ``/api/data``)
        to their correct Plotly chart containers without requiring ``series_labels()``
        on each parser.  The map is built lazily as data arrives in
        ``_process_host_results()``.
        """
        return self._store.snapshot_chart_map()

    def get_events(self) -> list[MonitorEvent]:
        """Return all recorded events in chronological order."""
        return self._store.events()

    def get_log_events(self) -> "list[dict[str, Any]]":
        """JSON-safe log-event rows for ``/api/data`` and export.

        Shape per row: ``{"timestamp", "host", "tab", "fields"}`` —
        the ``LogEventRecord`` spelling, insertion-ordered per (host, tab) ring.
        """
        return [
            {"timestamp": ev.ts.isoformat(), "host": host, "tab": tab, "fields": dict(ev.fields)}
            for host, tab, ev in self._store.snapshot_log_events()
        ]

    def get_meta_model(self) -> MonitorMeta:
        """Return the typed /api/meta payload (see get_meta for the dict form)."""
        # Derive host names from series keys (all series, including proc)
        hosts = self._store.hosts_from_series()
        # Fall back to the list of live hosts if no data has arrived yet
        if not hosts and self._hosts:
            hosts = [h.name for h in self._hosts]

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
        return MonitorMeta(
            hosts=hosts,
            live=bool(self._hosts),  # False for a review/scripted collector (no live hosts)
            metrics=metrics,
            tabs=list(tabs.values()),
            interval=self._global_interval,
        )

    def get_meta(self) -> dict[str, Any]:
        """Return metadata for the dashboard (host names, metric labels/units, tabs)."""
        return self.get_meta_model().model_dump(mode="json")

    # ------------------------------------------------------------------
    # SSE pub/sub
    # ------------------------------------------------------------------

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        """Register a new SSE subscriber and return its queue."""
        return self._broadcast.subscribe()

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        """Remove ``q`` from the SSE subscriber list so it receives no further pushes."""
        self._broadcast.unsubscribe(q)

    def _publish(self, payload: dict[str, Any]) -> None:
        self._broadcast.publish(payload)
