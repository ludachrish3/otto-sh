"""Pydantic boundary models for the monitor subsystem.

Three seams:

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

* :class:`MonitorSessionFragment` — the live SSE wire boundary (spec
  2026-07-12 §The stream speaks format:1). It reuses ``MetricRecord`` /
  ``EventRecord`` / ``LogEventRecord`` / ``SessionMeta`` verbatim rather than
  mirroring their fields under new names, so the fragment cannot drift from
  the ``format:1`` payload it appends to.

:data:`MIN_INTERVAL_SECONDS` / :func:`validate_interval` also live here rather
than in ``otto.monitor`` — see their docstrings for why.

Leaf isolation: this module imports only :mod:`otto.models.base`, pydantic, and
the stdlib — no runtime or ``otto.monitor`` edge — so it stays a pure leaf inside
the models package. This matters beyond tidiness: ``otto.cli.test`` needs
``MIN_INTERVAL_SECONDS`` for its ``--monitor-interval`` option but must NOT pay
for importing the monitor runtime package (collector/db/snmp/aiosqlite, ...)
just to render ``--help`` — that regressed the import-budget guard once
already (spec 2026-07-12 §monitor-live-streaming, the ``otto.monitor.interval``
module). Keeping the floor in this already-leaf module gives every caller
(CLI, library, pytest plugin) one definition without paying that cost.
"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from .base import OttoModel

MIN_INTERVAL_SECONDS: float = 1.0
"""The collection-interval floor — one home for the constant and the check.

An interval below one second is not meaningful in practice: a host must be
given time to answer every query in the interval without being taxed by the
polling itself. The floor is enforced where a *human* names an interval — the
CLI, the library, the pytest plugin — and NOT in
:class:`~otto.monitor.collector.MetricCollector`, which is the mechanism
rather than a knob: the monitor tests drive it at 0.01-0.2s against fake
hosts, where no real host is ever polled.
"""


def validate_interval(seconds: float) -> float:
    """Return *seconds*, or raise ``ValueError`` if it is below the floor."""
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"monitor interval must be at least {MIN_INTERVAL_SECONDS}s, got {seconds}s — "
            "a host needs time to answer every query in the interval without being "
            "taxed by the polling itself."
        )
    return seconds


class MetricPoint(OttoModel):
    """A single charted sample: timestamp, numeric value, optional hover meta.

    Replaces the ``(datetime, float, dict | None)`` tuple stored per series.
    Consumers read ``.ts`` / ``.value`` / ``.meta`` instead of unpacking.
    """

    ts: datetime
    value: float
    meta: dict[str, Any] | None = None


DEFAULT_MAX_SERIES_PER_CHART = 8
"""Default per-chart series cap. A chart shows at most this many series (the
frontend truncates the rest with an overflow note). A parser sets
``max_series = None`` to opt its chart out of the cap entirely."""


class ChartSpec(OttoModel):
    """One dashboard chart descriptor: otto's typed, internal parser-catalog view.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2. Not served at any endpoint of its
    own — it is reshaped into :class:`ChartSpecRecord` inside
    :class:`SessionMeta` for the ``monitor_sessions``/SSE wire (see
    :func:`otto.monitor.export.session_meta`).
    """

    label: str
    y_title: str
    unit: str
    command: str
    chart: str
    interval: float | None = None
    max_series: int | None = DEFAULT_MAX_SERIES_PER_CHART

    @model_serializer(mode="wrap")
    def _serialize_max_series(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, Any]:
        """Make an explicit ``max_series=None`` (uncapped) survive ``exclude_none``.

        ``document_json`` (see :func:`otto.monitor.export.document_json`) dumps
        with ``exclude_none=True`` so absent-optional fields stay absent on the
        wire; that same flag would otherwise drop a *meaningful* ``None`` here
        (uncapped chart) indistinguishably from a field that was never set, and
        the read side refills the pydantic default (``DEFAULT_MAX_SERIES_PER_CHART``)
        on the next load — silently re-capping an uncapped chart (e.g.
        PerCoreCpuParser's "CPU" chart) every time a saved export round-trips.
        Re-inserting the key after the handler runs keeps "missing" meaning
        "default-capped" (old exports predating this field) while an explicit
        ``None`` — this model's actual uncapped-chart state — comes back as
        ``None`` too. Applies to every dump site (``ChartSpecRecord`` inherits
        it) since the fix is model-level, not per call site.
        """
        data = handler(self)
        if self.max_series is None and info.exclude_none:
            data["max_series"] = None
        return data


class TabSpec(OttoModel):
    """One dashboard tab descriptor: otto's typed, internal parser-catalog view.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2. ``kind="table"`` tabs render an
    event table (schema in ``columns``) instead of charts, and carry
    ``metrics=[]``. Not served at any endpoint of its own — see
    :class:`ChartSpec` for the wire path.
    """

    id: str
    label: str
    metrics: list[str]
    kind: Literal["charts", "table"] = "charts"
    columns: list[str] | None = None


class MonitorMeta(OttoModel):
    """The typed internal contract: hosts, chart specs, and tab layout.

    The declarative contract the frontend renders from; TS types are
    generated from this schema in Phase 2. This model is never served
    directly — every dashboard boot (live or review) hydrates from
    ``GET /api/monitor_sessions``, whose ``format:1`` shape carries this same
    information reshaped into each session's :class:`SessionMeta`.

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


VALID_DASH_STYLES = frozenset({"solid", "dot", "dash", "longdash", "dashdot", "longdashdot"})
"""Legal event dash styles. Lives in this leaf module (not otto.monitor.events,
which re-imports it) so the HTTP body models below can validate against it
without this module growing an otto.monitor edge — the same leaf-isolation
rule that keeps MIN_INTERVAL_SECONDS here (see module docstring)."""

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _checked_label(value: str) -> str:
    if not value.strip():
        raise ValueError("label must not be empty")
    return value


def _checked_color(value: str) -> str:
    if not _HEX_COLOR_RE.match(value):
        raise ValueError(f"color must be #rrggbb, got {value!r}")
    return value


def _checked_dash(value: str) -> str:
    if value not in VALID_DASH_STYLES:
        raise ValueError(f"dash must be one of {sorted(VALID_DASH_STYLES)}, got {value!r}")
    return value


class EventCreateBody(OttoModel):
    """``POST /api/session/{sid}/event`` request body (spec 2026-07-18).

    ``timestamp=None`` means "server-now" (the Mark-now flow). When both
    timestamps are present the span must be forward; the server re-checks the
    pair after resolving a ``None`` timestamp to now.
    """

    label: str
    timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    color: str = "#888888"
    dash: str = "dash"

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _checked_label(value)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _checked_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str) -> str:
        return _checked_dash(value)

    @model_validator(mode="after")
    def _span_forward(self) -> "EventCreateBody":
        if (
            self.timestamp is not None
            and self.end_timestamp is not None
            and self.end_timestamp <= self.timestamp
        ):
            raise ValueError("end_timestamp must be after timestamp")
        return self


class EventUpdateBody(OttoModel):
    """``PATCH /api/session/{sid}/event/{id}`` request body (spec 2026-07-18).

    Every field optional; ``model_fields_set`` distinguishes "absent"
    (unchanged) from an explicit JSON ``null``. Only ``end_timestamp`` uses
    that distinction — an explicit null CLEARS the end (span → point); for the
    other fields null means unchanged, same as absent. The merged
    start/end ordering check happens in the route, where the existing event's
    values are known.
    """

    label: str | None = None
    timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    color: str | None = None
    dash: str | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _checked_label(value)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return None if value is None else _checked_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str | None) -> str | None:
        return None if value is None else _checked_dash(value)


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

    Mirrors the runtime ``otto.link.model.Link``. The snapshot is a
    static-config document: ``implicit`` + ``declared`` only. Dynamic tunnels
    are runtime state and ride ``SessionRecord.tunnels`` as first-class
    :class:`TunnelRecord` rows instead (spec 2026-07-16) — the runtime
    ``Provenance.DYNAMIC`` enum value survives for the link-conflict rules,
    but it never reaches this wire. ``impair`` is the *declared* in-path
    middlebox host id — static config, unlike applied netem parameters.
    """

    id: str
    endpoints: list[LinkEndpointSnapshot] = Field(min_length=2, max_length=2)
    protocol: str = "tcp"
    provenance: Literal["implicit", "declared"] = "declared"
    name: str | None = None
    impair: str | None = None


class LabSnapshot(RowModel):
    """A session's lab config as it was at run time (spec 2026-07-10 §3)."""

    elements: list[ElementRecord] = Field(default_factory=list)
    hosts: list[HostSnapshot] = Field(default_factory=list)
    links: list[LinkSnapshot] = Field(default_factory=list)


class TunnelRecord(RowModel):
    """One live tunnel's last known state (spec 2026-07-16 §1).

    ``hops`` is the ordered host-id chain of ``otto.tunnel.model.Tunnel.path``
    — ``hops[0]`` the entry end, ``hops[-1]`` the exit end; the topology view
    consumes consecutive pairs. Host ids share the id space of
    :class:`LinkEndpointSnapshot.host`. ``status`` is derived from discovery
    fields, never parsed from the human ``DiscoveredTunnel.status`` string.
    """

    id: str
    protocol: str = "udp"
    service_port: int
    hops: list[str] = Field(min_length=2)
    status: Literal["ok", "degraded", "uncertain"] = "ok"
    carriers_present: int = 0
    carriers_expected: int = 0
    age_seconds: float | None = None


class ChartSpecRecord(ChartSpec):
    """Lenient read-back variant of :class:`ChartSpec` for export documents.

    Same fields; ``extra="ignore"`` so an older otto can read exports written
    by a newer one whose chart specs carry new fields (the :class:`RowModel`
    boundary philosophy). :class:`ChartSpec` itself stays ``extra="forbid"``
    as the otto-built internal parser-catalog contract.
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
    labels to chart keys (:attr:`ChartSpec.label`), as the dashboard does
    today via the ``monitor_sessions``/SSE wire.
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
    tunnels: list[TunnelRecord] = Field(default_factory=list)


class MonitorExport(RowModel):
    """The versioned historical-export document (spec 2026-07-10 §3).

    ``format`` is **required with no default**: a legacy unversioned document
    (the field's absence is its marker) must fail loud here, never validate as
    an empty modern one. ``Literal[1]`` rejects future formats loud too.
    """

    format: Literal[1]
    sessions: list[SessionRecord]


class MonitorSessionFragment(RowModel):
    """An incremental update to ONE live monitor session.

    Spec 2026-07-12 §The stream speaks format:1.

    A fragment is a *partial* :class:`SessionRecord`: every payload field is
    optional and carries the SAME name and type as its counterpart there, so the
    client appends rather than translates. This is deliberate — Plan 5a lost
    three fix waves to a rename across a lenient boundary model
    (``MonitorMeta.metrics`` vs ``SessionMeta.charts``), invisible to the type
    checker because both sides were ``str`` at the seam. The strongest defence is
    not a mapping function but the absence of a second model: these ARE the
    payload's models.

    ``deleted_event_ids`` is the one thing a partial record cannot express by
    presence, so it is explicit. Event *updates* need no separate kind — the
    client upserts by ``id``, so an edited event is just an event.

    ``tunnels`` is the one REPLACE-semantics payload field (the ``meta``
    precedent, not the append rule): ``None`` means "no tunnel update in this
    fragment"; a list — including ``[]`` — replaces the session's set
    wholesale. That is "last known state" expressed on the wire.
    """

    format: Literal[1] = 1
    session: str
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    deleted_event_ids: list[int] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)
    meta: SessionMeta | None = None
    tunnels: list[TunnelRecord] | None = None
