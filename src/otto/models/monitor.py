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
  2026-07-12 §The stream speaks format:1). Its ``format`` is a declared format
  of its own, ``monitor-live-stream`` in ``api/public.toml``, versioned apart
  from the export document. It reuses ``MetricRecord`` / ``EventRecord`` /
  ``LogEventRecord`` / ``SessionMeta`` verbatim rather than mirroring their
  fields under new names, so the fragment cannot drift from the export's
  session payload it appends to.

Leaf isolation: this module imports only :mod:`otto.models.base`, pydantic,
and the stdlib — no runtime or ``otto.monitor`` edge — so it stays a pure leaf
inside the models package.

The collection-interval floor (:data:`otto.utils.MIN_INTERVAL_SECONDS`,
:func:`otto.utils.validate_interval`) is re-exported from ``otto.models`` but
is not defined or used here: ``otto test --help`` and ``otto monitor --help``
need the floor for their interval options, and this module costs pydantic and
the model classes to import. It regressed the import-budget guard once
already when it lived in the monitor runtime package (spec 2026-07-12
§monitor-live-streaming).
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import final

from ..utils import as_utc
from .base import OttoModel
from .event_style import (
    DEFAULT_EVENT_COLOR,
    DEFAULT_EVENT_DASH,
    check_event_dash,
    normalize_event_color,
)

AwareTimestamp = Annotated[datetime, AfterValidator(as_utc)]
"""A ``datetime`` that is always timezone-aware: a naive input means UTC and an
existing offset is kept (:func:`otto.utils.as_utc`). EVERY timestamp field in
these models uses it (session bounds, samples, log events and events), so a
legacy capture stored naive is served as UTC throughout and its events stay
inside its own window; a span's two ends, and a stored event against a write,
always compare as instants. Optional fields spell ``AwareTimestamp | None``: the optionality
stays outside the alias."""


@final
class MetricPoint(OttoModel):
    """A single charted sample: timestamp, numeric value, optional hover meta.

    Replaces the ``(datetime, float, dict | None)`` tuple stored per series.
    Consumers read ``.ts`` / ``.value`` / ``.meta`` instead of unpacking.
    """

    ts: AwareTimestamp
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


@final
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


class RowModel(OttoModel):
    """Lenient base for historical-data import/export rows.

    It overrides :class:`~otto.models.base.OttoModel`'s ``extra='forbid'``
    (which exists to turn a *config* typo into an error): data read-back is
    tolerant, so an unexpected key/column from a newer schema is ignored rather
    than rejected. This matches the pre-pydantic ``.get()``/``[]`` parsing and
    keeps older otto builds able to import exports written by newer ones. The
    rest of ``OttoModel``'s config, ``defer_build``, is inherited.
    """

    model_config = ConfigDict(extra="ignore")


@final
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

    timestamp: AwareTimestamp = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host: str = ""
    label: str
    value: float
    meta: dict[str, Any] | None = None
    source: str | None = None
    """Host id of the *reporting* host when this series came from an external
    management host (spec 2026-07-10 §3.1); ``None``/absent = self-reported.
    Rides only in JSON for now — the SQLite ``metrics`` table gains its column
    with the backend catch-up (spec §7)."""


@final
class EventRecord(RowModel):
    """One ``events`` row at the JSON / SQLite **import** boundary.

    Mirrors the ``MonitorEvent`` fields. Used to validate external event data
    before constructing the (unchanged, mutable) ``MonitorEvent`` dataclass —
    event *export* stays ``MonitorEvent.to_dict()``. ``timestamp`` is required
    (a row without one is skipped, as before); everything else defaults. ``id``
    is ``None`` when absent so the collector can assign its running id.

    ``color`` and ``dash`` are lenient here, unlike every write boundary: a
    missing or ``null`` value takes the registry default
    (:data:`~otto.models.event_style.DEFAULT_EVENT_COLOR` /
    :data:`~otto.models.event_style.DEFAULT_EVENT_DASH`), and a hex colour is
    normalised to lowercase ``#rrggbb``. Anything else, such as a CSS name
    (``"red"``) an older otto wrote, is KEPT UNCHANGED: loading an old file or
    archive must neither fail nor destroy the value. The dashboard's own
    import boundary draws such an event in the default colour and keeps the
    original for export.
    """

    id: int | None = None
    timestamp: AwareTimestamp = Field(validation_alias=AliasChoices("timestamp", "ts"))
    end_timestamp: AwareTimestamp | None = Field(
        default=None, validation_alias=AliasChoices("end_timestamp", "end_ts")
    )
    label: str = ""
    source: str = "manual"
    color: str = DEFAULT_EVENT_COLOR
    dash: str = DEFAULT_EVENT_DASH

    @field_validator("color", mode="before")
    @classmethod
    def _color(cls, value: object) -> object:
        if value is None:
            return DEFAULT_EVENT_COLOR
        if not isinstance(value, str):
            return value  # str validation reports it
        try:
            return normalize_event_color(value)
        except ValueError:
            return value  # a legacy value is kept, never rewritten (class docstring)

    @field_validator("dash", mode="before")
    @classmethod
    def _dash(cls, value: object) -> object:
        return DEFAULT_EVENT_DASH if value is None else value


def checked_label(value: str) -> str:
    """Return ``value``, refusing a blank label: the ONE label rule.

    ``EventCreateBody`` validates with it. Every update (an HTTP PATCH
    included) and every library write that stores a new or changed label (the collector's
    ``add_event``, ``mutate_event`` and ``restore_event``, the archive's
    insert, update and restore, and a revert's ``write``) reaches it through
    ``otto.monitor.event_ops.check_label_write``. ``EventRecord`` does not
    validate with it, so an old file still loads.
    """
    if not value.strip():
        raise ValueError("label must not be empty")
    return value


SPAN_ORDER_ERROR = "end_timestamp must be after timestamp"
"""The span-order refusal's text, whichever caller raises it."""


def span_is_forward(timestamp: datetime, end_timestamp: datetime | None) -> bool:
    """Report whether a span ends after it starts: the ONE span-order rule.

    A point event (no end) is always forward. ``EventCreateBody`` checks the
    pair it holds with it, and ``otto.monitor.event_ops.check_span`` the pair
    a create, an update merge or a revert is about to store. It lives here,
    below both, because models never import the monitor package.
    """
    return end_timestamp is None or end_timestamp > timestamp


class EventCreateBody(OttoModel):
    """``POST /api/session/{sid}/event`` request body (spec 2026-07-18).

    ``timestamp=None`` means "server-now" (the Mark-now flow). When both
    timestamps are present the span must be forward; the server re-checks the
    pair after resolving a ``None`` timestamp to now.

    ``defer_build=False``, overriding :class:`~otto.models.base.OttoModel`:
    FastAPI wraps a request-body parameter in its own
    ``TypeAdapter(Annotated[<model>, Field(alias=<param name>)])`` and builds
    it inside a ``warnings.catch_warnings()`` block that deliberately
    swallows pydantic's ``UnsupportedFieldAttributeWarning`` for that
    (harmless, FastAPI-only) alias. A deferred model's schema is not
    actually built there — pydantic hands back a placeholder and defers the
    real build to the first ``validate_python()`` call, which happens
    *outside* FastAPI's suppression window, on the request path. In
    production, with pydantic's default warning filters, that is one stray
    ``UnsupportedFieldAttributeWarning`` per server process (the first
    request that reaches this route) — harmless, just a log line. Under any
    warnings-as-errors runtime (otto's own test config, or a deployment run
    with ``-W error`` / ``PYTHONWARNINGS=error``), the SAME warning is fatal:
    every request 500s, not just the first, because the mock validator
    re-raises it on every rebuild attempt. Building eagerly, as every model
    did before this base gained ``defer_build``, keeps the build inside
    FastAPI's own suppression window where it belongs, in both regimes.
    """

    model_config = ConfigDict(defer_build=False)

    label: str
    timestamp: AwareTimestamp | None = None
    end_timestamp: AwareTimestamp | None = None
    color: str = DEFAULT_EVENT_COLOR
    dash: str = DEFAULT_EVENT_DASH

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return checked_label(value)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return normalize_event_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str) -> str:
        return check_event_dash(value)

    @model_validator(mode="after")
    def _span_forward(self) -> "EventCreateBody":
        if self.timestamp is not None and not span_is_forward(self.timestamp, self.end_timestamp):
            raise ValueError(SPAN_ORDER_ERROR)
        return self


class EventUpdateBody(OttoModel):
    """``PATCH /api/session/{sid}/event/{id}`` request body (spec 2026-07-18).

    Every field optional; ``model_fields_set`` distinguishes "absent"
    (unchanged) from an explicit JSON ``null``. Only ``end_timestamp`` uses
    that distinction — an explicit null CLEARS the end (span → point); for the
    other fields null means unchanged, same as absent. The merged
    start/end ordering check happens in the route, where the existing event's
    values are known. The label check likewise runs inside the mutation
    sequence, against the current event: a blank ``label`` is refused only
    when it differs from the event's current one
    (``otto.monitor.event_ops.check_label_write``), the same answer a
    library update gets: a PATCH that resends an older event's already-blank
    label alongside a colour is accepted.

    ``defer_build=False``: same FastAPI request-body reason as
    :class:`EventCreateBody` — see its docstring.
    """

    model_config = ConfigDict(defer_build=False)

    label: str | None = None
    timestamp: AwareTimestamp | None = None
    end_timestamp: AwareTimestamp | None = None
    color: str | None = None
    dash: str | None = None

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        return None if value is None else normalize_event_color(value)

    @field_validator("dash")
    @classmethod
    def _dash(cls, value: str | None) -> str | None:
        return None if value is None else check_event_dash(value)


class EventRevertBody(OttoModel):
    """``POST /api/session/{sid}/event/{id}/revert`` request body (#929).

    ``expect`` is the state the event must be in now and ``write`` the state
    to put it back to; ``None`` on either side means "no event" (see
    ``event_ops.revert_leg``). Any ``id`` inside either
    record is ignored in favour of the path's.

    ``defer_build=False``: same FastAPI request-body reason as
    :class:`EventCreateBody` — see its docstring.
    """

    model_config = ConfigDict(defer_build=False)

    expect: EventRecord | None
    write: EventRecord | None


@final
class LogEventRecord(RowModel):
    """One ``log_events`` row at the ``format:1`` JSON / v2 SQLite import-export boundary.

    Mirrors the parser-emitted ``LogEvent`` plus the host/tab the collector
    attaches. The JSON export format spells the time key ``timestamp``; the
    SQLite column is ``ts`` (its ``fields`` column is JSON-decoded by the
    loader before validation).
    """

    timestamp: AwareTimestamp = Field(validation_alias=AliasChoices("timestamp", "ts"))
    host: str = ""
    tab: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


@final
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


@final
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


@final
class LinkEndpointSnapshot(RowModel):
    """One end of a snapshotted link (mirrors ``otto.link.model.LinkEndpoint``)."""

    host: str
    interface: str | None = None
    ip: str = ""
    port: int | None = None


@final
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


@final
class LabSnapshot(RowModel):
    """A session's lab config as it was at run time (spec 2026-07-10 §3)."""

    elements: list[ElementRecord] = Field(default_factory=list)
    hosts: list[HostSnapshot] = Field(default_factory=list)
    links: list[LinkSnapshot] = Field(default_factory=list)


@final
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


@final
class ChartSpecRecord(ChartSpec):
    """Lenient read-back variant of :class:`ChartSpec` for export documents.

    Same fields; ``extra="ignore"`` so an older otto can read exports written
    by a newer one whose chart specs carry new fields (the :class:`RowModel`
    boundary philosophy). :class:`ChartSpec` itself stays ``extra="forbid"``
    as the otto-built internal parser-catalog contract.
    """

    model_config = ConfigDict(extra="ignore")


@final
class TabSpecRecord(TabSpec):
    """Lenient read-back variant of :class:`TabSpec` (see :class:`ChartSpecRecord`)."""

    model_config = ConfigDict(extra="ignore")


@final
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


@final
class SessionRecord(RowModel):
    """One self-contained monitoring session: config snapshot + data.

    ``end=None`` means a still-open session. ``chart_map`` maps bare series
    labels to chart keys (:attr:`ChartSpec.label`), as the dashboard does
    today via the ``monitor_sessions``/SSE wire.
    """

    id: str
    label: str | None = None
    note: str | None = None
    start: AwareTimestamp
    end: AwareTimestamp | None = None
    lab: LabSnapshot = Field(default_factory=LabSnapshot)
    meta: SessionMeta = Field(default_factory=SessionMeta)
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)
    tunnels: list[TunnelRecord] = Field(default_factory=list)
    observed_at: AwareTimestamp | None = None
    """When the server last observed this session, on the server's clock.

    A live snapshot stamps it; an archive or an older document leaves it unset.
    The dashboard's live window follows it, so a mark shows at once even when
    no metric has arrived for longer than the window (#866).
    """


@final
class MonitorExport(RowModel):
    """The versioned historical-export document (spec 2026-07-10 §3).

    ``format`` is **required with no default**: a legacy unversioned document
    (the field's absence is its marker) must fail loud here, never validate as
    an empty modern one. ``Literal[1]`` rejects future formats loud too.
    """

    # The Literal's arguments are MONITOR_EXPORT_READ_VERSIONS (otto.models.formats,
    # dump spec §13.1), spelled again: this module loads on budgeted CLI surfaces
    # and must not import the list. tests/unit/models/test_declared_version_fields.py
    # holds the two equal; add or drop a version in both places.
    format: Literal[1]
    sessions: list[SessionRecord]


class MonitorSessionFragment(RowModel):
    """An incremental update to ONE live monitor session.

    The fragments of ``GET /api/stream`` are a declared format of their own,
    ``monitor-live-stream`` in ``api/public.toml``, versioned apart from the
    :class:`MonitorExport` document whose sessions they grow (spec 2026-07-12
    §The stream speaks format:1). An absent ``format`` reads as version 1, its
    default.

    A fragment is a *partial* :class:`SessionRecord`: every payload field is
    optional and carries the SAME name and type as its counterpart there, so the
    client appends rather than translates. This is deliberate — a rename across
    a lenient boundary model once cost three fix waves (``MonitorMeta.metrics``
    vs ``SessionMeta.charts``), invisible to the type checker because both sides
    were ``str`` at the seam. The strongest defence is not a mapping function but
    the absence of a second model: these ARE the payload's models.

    ``deleted_event_ids`` is the one thing a partial record cannot express by
    presence, so it is explicit. Event *updates* need no separate kind — the
    client upserts by ``id``, so an edited event is just an event.

    ``tunnels`` is the one REPLACE-semantics payload field (the ``meta``
    precedent, not the append rule): ``None`` means "no tunnel update in this
    fragment"; a list — including ``[]`` — replaces the session's set
    wholesale. That is "last known state" expressed on the wire.

    ``observed_at`` is the server's clock when the fragment was published,
    the same field as :attr:`SessionRecord.observed_at`. Optional and
    additive, so the stream stays format 1.
    """

    # The Literal's arguments are MONITOR_STREAM_READ_VERSIONS (otto.models.formats,
    # dump spec §13.1), spelled again for the same reason as MonitorExport.format;
    # tests/unit/models/test_declared_version_fields.py holds the two equal.
    format: Literal[1] = 1
    session: str
    metrics: list[MetricRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    log_events: list[LogEventRecord] = Field(default_factory=list)
    deleted_event_ids: list[int] = Field(default_factory=list)
    chart_map: dict[str, str] = Field(default_factory=dict)
    meta: SessionMeta | None = None
    tunnels: list[TunnelRecord] | None = None
    observed_at: AwareTimestamp | None = None
