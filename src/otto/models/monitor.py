"""Pydantic boundary models for the monitor subsystem.

Two seams:

* :class:`MetricPoint` — the in-memory series element (replaces the old
  ``(ts, value, meta)`` 3-tuple in ``MetricStore.series``). It is an
  :class:`~otto.models.base.OttoModel` (``extra='forbid'``) because otto is the
  only thing that builds it: the live append path uses ``model_construct`` (no
  validation, hot loop) and the import path uses ``model_validate``.

* :class:`MetricRecord` / :class:`EventRecord` / :class:`LogEventRecord` —
  flat records at the ``format:1`` JSON export and v2 SQLite session-archive
  import/export boundary (``otto monitor <source>``/``otto monitor --live
  --db``). These read *historical, external* data, so they are deliberately
  **lenient** (``extra='ignore'``, via :class:`RowModel`): an unknown column
  from a newer schema is dropped, not rejected, exactly as the old
  ``.get()``/``[]`` parsing did. Field names follow the JSON spelling; a
  ``validation_alias`` also accepts the SQLite column spelling
  (``ts``/``end_ts``) so one model validates both seams.

Leaf isolation: this module imports only :mod:`otto.models.base`, pydantic, and
the stdlib — no runtime or ``otto.monitor`` edge — so it stays a pure leaf inside
the models package.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .base import OttoModel


class MetricPoint(OttoModel):
    """A single charted sample: timestamp, numeric value, optional hover meta.

    Replaces the ``(datetime, float, dict | None)`` tuple stored per series.
    Consumers read ``.ts`` / ``.value`` / ``.meta`` instead of unpacking.
    """

    ts: datetime
    value: float
    meta: dict[str, Any] | None = None


class ChartSpec(OttoModel):
    """One dashboard chart descriptor served by ``/api/meta``.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2.
    """

    label: str
    y_title: str
    unit: str
    command: str
    chart: str
    interval: float | None = None


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


class MonitorMeta(OttoModel):
    """The typed ``/api/meta`` payload: hosts, chart specs, and tab layout.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2.

    ``interval`` is the global collection interval in seconds — ``None`` until
    :meth:`~otto.monitor.collector.MetricCollector.run` has recorded one (a
    collector that has not started live collection). Reviewed data (loaded
    from ``otto monitor <source>``) carries this in its own
    :class:`SessionMeta` instead — see :func:`otto.monitor.export.session_meta`.
    """

    hosts: list[str]
    live: bool
    metrics: list[ChartSpec]
    tabs: list[TabSpec]
    interval: float | None = None


class RowModel(BaseModel):
    """Lenient base for historical-data import/export rows.

    Unlike :class:`~otto.models.base.OttoModel` (``extra='forbid'``, which exists
    to turn a *config* typo into an error), data read-back is tolerant: an
    unexpected key/column from a newer schema is ignored rather than rejected.
    This matches the pre-pydantic ``.get()``/``[]`` parsing and keeps older otto
    builds able to import exports written by newer ones.
    """

    model_config = ConfigDict(extra="ignore")


class MetricRecord(RowModel):
    """One ``metrics`` row at the ``format:1`` JSON / v2 SQLite import-export boundary.

    The JSON export format spells the time key ``timestamp``; the SQLite
    ``metrics`` table column is ``ts``. The ``validation_alias`` accepts both, so
    a single model validates either seam. ``host`` is optional for the
    pre-host-column schema; ``meta`` rides only in JSON (the DB has no meta
    column). Exporting with ``model_dump(mode='json', exclude_none=True)`` emits
    the JSON spelling and omits ``meta`` when ``None`` (``host=''`` is still
    emitted — empty string is not ``None``).
    """

    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host: str = ""
    label: str
    value: float
    meta: dict[str, Any] | None = None
    source: str | None = None
    """Host id of the *reporting* host when this series came from an external
    management host (spec 2026-07-10 §3.1); ``None``/absent = self-reported.
    Rides only in JSON for now — the SQLite ``metrics`` table gains its column
    with the backend catch-up (spec §7)."""


class EventRecord(RowModel):
    """One ``events`` row at the JSON / SQLite **import** boundary.

    Mirrors the ``MonitorEvent`` fields. Used to validate external event data
    before constructing the (unchanged, mutable) ``MonitorEvent`` dataclass —
    event *export* stays ``MonitorEvent.to_dict()``. ``timestamp`` is required
    (a row without one is skipped, as before); everything else defaults. ``id``
    is ``None`` when absent so the collector can assign its running id.
    """

    id: int | None = None
    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    end_timestamp: datetime | None = Field(
        default=None, validation_alias=AliasChoices("end_timestamp", "end_ts")
    )
    label: str = ""
    source: str = "manual"
    color: str = "#888888"
    dash: str = "dash"


class LogEventRecord(RowModel):
    """One ``log_events`` row at the ``format:1`` JSON / v2 SQLite import-export boundary.

    Mirrors the parser-emitted ``LogEvent`` plus the host/tab the collector
    attaches. The JSON export format spells the time key ``timestamp``; the
    SQLite column is ``ts`` (its ``fields`` column is JSON-decoded by the
    loader before validation).
    """

    timestamp: datetime = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host: str = ""
    tab: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class ElementRecord(RowModel):
    """One optional ``lab.elements`` entry in the export snapshot.

    ``id`` is the element name — the same string member hosts carry in
    :attr:`HostSnapshot.element`. Elements *not* listed are derived from hosts
    (any member with a ``slot`` → physical presentation; a single member →
    singleton behavior). An explicit entry with zero member hosts renders as an
    empty element (e.g. an unpopulated chassis). ``singleton`` is always
    derived from membership count, never stored (spec 2026-07-10 §2).
    """

    id: str
    type: Literal["physical", "logical"] = "logical"
    description: str | None = None


class HostSnapshot(RowModel):
    """The view-relevant subset of a host's config, frozen into a session.

    Deliberately **never** credentials (spec 2026-07-10 §3.1). ``interfaces``
    is flattened to ``netdev -> ip`` (the frontend needs no more). Lenient
    read-back like every export row (:class:`RowModel`).
    """

    id: str
    element: str
    name: str | None = None
    board: str | None = None
    slot: int | None = None
    hop: str | None = None
    os_type: str = "unix"
    os_name: str | None = None
    os_version: str | None = None
    ip: str = ""
    interfaces: dict[str, str] = Field(default_factory=dict)
    labs: list[str] = Field(default_factory=list)
    is_virtual: bool = False


class LinkEndpointSnapshot(RowModel):
    """One end of a snapshotted link (mirrors ``otto.link.model.LinkEndpoint``)."""

    host: str
    interface: str | None = None
    ip: str = ""
    port: int | None = None


class LinkSnapshot(RowModel):
    """One static link frozen into a session's lab snapshot.

    Mirrors the runtime ``otto.link.model.Link``. Real exporters write only
    ``implicit`` + ``declared`` provenances — the snapshot is a static-config
    document and dynamic tunnels are runtime state (spec 2026-07-10 §2); the
    ``dynamic`` value stays for parity with the runtime enum (and the live
    topology view). ``impair`` is the *declared* in-path middlebox host id —
    static config, unlike applied netem parameters.
    """

    id: str
    endpoints: list[LinkEndpointSnapshot] = Field(min_length=2, max_length=2)
    protocol: str = "tcp"
    provenance: Literal["implicit", "declared", "dynamic"] = "declared"
    name: str | None = None
    impair: str | None = None


class LabSnapshot(RowModel):
    """A session's lab config as it was at run time (spec 2026-07-10 §3)."""

    elements: list[ElementRecord] = Field(default_factory=list)
    hosts: list[HostSnapshot] = Field(default_factory=list)
    links: list[LinkSnapshot] = Field(default_factory=list)


class ChartSpecRecord(ChartSpec):
    """Lenient read-back variant of :class:`ChartSpec` for export documents.

    Same fields; ``extra="ignore"`` so an older otto can read exports written
    by a newer one whose chart specs carry new fields (the :class:`RowModel`
    boundary philosophy). :class:`ChartSpec` itself stays ``extra="forbid"``
    as the otto-built live ``/api/meta`` contract.
    """

    model_config = ConfigDict(extra="ignore")


class TabSpecRecord(TabSpec):
    """Lenient read-back variant of :class:`TabSpec` (see :class:`ChartSpecRecord`)."""

    model_config = ConfigDict(extra="ignore")


class SessionMeta(RowModel):
    """Presentation meta frozen at run time: chart/tab specs + intervals.

    Client-side Import has no parser catalog to rebuild specs from, derived
    health needs per-series cadences, and chart definitions drift over months
    exactly like lab configs (spec 2026-07-10 §2, §4) — hence the lenient
    ``*Record`` spec variants, not the strict live-meta classes.
    """

    interval: float | None = None
    charts: list[ChartSpecRecord] = Field(default_factory=list)
    tabs: list[TabSpecRecord] = Field(default_factory=list)


class SessionRecord(RowModel):
    """One self-contained monitoring session: config snapshot + data.

    ``end=None`` means a still-open session. ``chart_map`` maps bare series
    labels to chart keys (:attr:`ChartSpec.label`), as ``/api/data`` does today.
    """

    id: str
    label: str | None = None
    note: str | None = None
    start: datetime
    end: datetime | None = None
    lab: LabSnapshot = Field(default_factory=LabSnapshot)
    meta: SessionMeta = Field(default_factory=SessionMeta)
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)


class MonitorExport(RowModel):
    """The versioned historical-export document (spec 2026-07-10 §3).

    ``format`` is **required with no default**: a legacy unversioned document
    (the field's absence is its marker) must fail loud here, never validate as
    an empty modern one. ``Literal[1]`` rejects future formats loud too.
    """

    format: Literal[1]
    sessions: list[SessionRecord]
