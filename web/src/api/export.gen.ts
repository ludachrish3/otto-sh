/* AUTO-GENERATED from monitor-export.schema.json — run scripts/gen_web_types.sh; do not edit. */

export type Format = 1;
export type Id = string;
export type Label = string | null;
export type Note = string | null;
export type Start = string;
export type End = string | null;
export type Id1 = string;
export type Type = "physical" | "logical";
export type Description = string | null;
export type Elements = ElementRecord[];
export type Id2 = string;
export type Element = string;
export type Name = string | null;
export type Board = string | null;
export type Slot = number | null;
export type Hop = string | null;
export type OsType = string;
export type OsName = string | null;
export type OsVersion = string | null;
export type Ip = string;
export type Labs = string[];
export type IsVirtual = boolean;
export type Hosts = HostSnapshot[];
export type Id3 = string;
/**
 * @minItems 2
 * @maxItems 2
 */
export type Endpoints = [LinkEndpointSnapshot, LinkEndpointSnapshot];
export type Host = string;
export type Interface = string | null;
export type Ip1 = string;
export type Port = number | null;
export type Protocol = string;
export type Provenance = "implicit" | "declared";
export type Name1 = string | null;
export type Impair = string | null;
export type Links = LinkSnapshot[];
export type Interval = number | null;
export type Label1 = string;
export type YTitle = string;
export type Unit = string;
export type Command = string;
export type Chart = string;
export type Interval1 = number | null;
export type MaxSeries = number | null;
export type Charts = ChartSpecRecord[];
export type Id4 = string;
export type Label2 = string;
export type Metrics = string[];
export type Kind = "charts" | "table";
export type Columns = string[] | null;
export type Tabs = TabSpecRecord[];
export type Timestamp = string;
export type Host1 = string;
export type Label3 = string;
export type Value = number;
export type Meta = {
  [k: string]: unknown;
} | null;
export type Source = string | null;
export type Metrics1 = MetricRecord[];
export type Id5 = number | null;
export type Timestamp1 = string;
export type EndTimestamp = string | null;
export type Label4 = string;
export type Source1 = string;
export type Color = string;
export type Dash = string;
export type Events = EventRecord[];
export type Timestamp2 = string;
export type Host2 = string;
export type Tab = string;
export type LogEvents = LogEventRecord[];
export type Id6 = string;
export type Protocol1 = string;
export type ServicePort = number;
/**
 * @minItems 2
 */
export type Hops = [string, string, ...string[]];
export type Status = "ok" | "degraded" | "uncertain";
export type CarriersPresent = number;
export type CarriersExpected = number;
export type AgeSeconds = number | null;
export type Tunnels = TunnelRecord[];
export type Sessions = SessionRecord[];
export type Format1 = 1;
export type Session = string;
export type Metrics2 = MetricRecord[];
export type Events1 = EventRecord[];
export type LogEvents1 = LogEventRecord[];
export type DeletedEventIds = number[];
export type Tunnels1 = TunnelRecord[] | null;
export type Label5 = string;
export type Timestamp3 = string | null;
export type EndTimestamp1 = string | null;
export type Color1 = string;
export type Dash1 = string;
export type Label6 = string | null;
export type Timestamp4 = string | null;
export type EndTimestamp2 = string | null;
export type Color2 = string | null;
export type Dash2 = string | null;

/**
 * The versioned historical-export document (spec 2026-07-10 §3).
 *
 * ``format`` is **required with no default**: a legacy unversioned document
 * (the field's absence is its marker) must fail loud here, never validate as
 * an empty modern one. ``Literal[1]`` rejects future formats loud too.
 */
export interface MonitorHistoricalExportDocument {
  format: Format;
  sessions: Sessions;
  [k: string]: unknown;
}
/**
 * One self-contained monitoring session: config snapshot + data.
 *
 * ``end=None`` means a still-open session. ``chart_map`` maps bare series
 * labels to chart keys (:attr:`ChartSpec.label`), as the dashboard does
 * today via the ``monitor_sessions``/SSE wire.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "SessionRecord".
 */
export interface SessionRecord {
  id: Id;
  label?: Label;
  note?: Note;
  start: Start;
  end?: End;
  lab?: LabSnapshot;
  meta?: SessionMeta;
  metrics?: Metrics1;
  events?: Events;
  log_events?: LogEvents;
  chart_map?: ChartMap;
  tunnels?: Tunnels;
  [k: string]: unknown;
}
/**
 * A session's lab config as it was at run time (spec 2026-07-10 §3).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "LabSnapshot".
 */
export interface LabSnapshot {
  elements?: Elements;
  hosts?: Hosts;
  links?: Links;
  [k: string]: unknown;
}
/**
 * One optional ``lab.elements`` entry in the export snapshot.
 *
 * ``id`` is the element name — the same string member hosts carry in
 * :attr:`HostSnapshot.element`. Elements *not* listed are derived from hosts
 * (any member with a ``slot`` → physical presentation; a single member →
 * singleton behavior). An explicit entry with zero member hosts renders as an
 * empty element (e.g. an unpopulated chassis). ``singleton`` is always
 * derived from membership count, never stored (spec 2026-07-10 §2).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "ElementRecord".
 */
export interface ElementRecord {
  id: Id1;
  type?: Type;
  description?: Description;
  [k: string]: unknown;
}
/**
 * The view-relevant subset of a host's config, frozen into a session.
 *
 * Deliberately **never** credentials (spec 2026-07-10 §3.1). ``interfaces``
 * is flattened to ``netdev -> ip`` (the frontend needs no more). Lenient
 * read-back like every export row (:class:`RowModel`).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "HostSnapshot".
 */
export interface HostSnapshot {
  id: Id2;
  element: Element;
  name?: Name;
  board?: Board;
  slot?: Slot;
  hop?: Hop;
  os_type?: OsType;
  os_name?: OsName;
  os_version?: OsVersion;
  ip?: Ip;
  interfaces?: Interfaces;
  labs?: Labs;
  is_virtual?: IsVirtual;
  [k: string]: unknown;
}
export interface Interfaces {
  [k: string]: string;
}
/**
 * One static link frozen into a session's lab snapshot.
 *
 * Mirrors the runtime ``otto.link.model.Link``. The snapshot is a
 * static-config document: ``implicit`` + ``declared`` only. Dynamic tunnels
 * are runtime state and ride ``SessionRecord.tunnels`` as first-class
 * :class:`TunnelRecord` rows instead (spec 2026-07-16) — the runtime
 * ``Provenance.DYNAMIC`` enum value survives for the link-conflict rules,
 * but it never reaches this wire. ``impair`` is the *declared* in-path
 * middlebox host id — static config, unlike applied netem parameters.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "LinkSnapshot".
 */
export interface LinkSnapshot {
  id: Id3;
  endpoints: Endpoints;
  protocol?: Protocol;
  provenance?: Provenance;
  name?: Name1;
  impair?: Impair;
  [k: string]: unknown;
}
/**
 * One end of a snapshotted link (mirrors ``otto.link.model.LinkEndpoint``).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "LinkEndpointSnapshot".
 */
export interface LinkEndpointSnapshot {
  host: Host;
  interface?: Interface;
  ip?: Ip1;
  port?: Port;
  [k: string]: unknown;
}
/**
 * Presentation meta frozen at run time: chart/tab specs + intervals.
 *
 * Client-side Import has no parser catalog to rebuild specs from, derived
 * health needs per-series cadences, and chart definitions drift over months
 * exactly like lab configs (spec 2026-07-10 §2, §4) — hence the lenient
 * ``*Record`` spec variants, not the strict live-meta classes.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "SessionMeta".
 */
export interface SessionMeta {
  interval?: Interval;
  charts?: Charts;
  tabs?: Tabs;
  [k: string]: unknown;
}
/**
 * Lenient read-back variant of :class:`ChartSpec` for export documents.
 *
 * Same fields; ``extra="ignore"`` so an older otto can read exports written
 * by a newer one whose chart specs carry new fields (the :class:`RowModel`
 * boundary philosophy). :class:`ChartSpec` itself stays ``extra="forbid"``
 * as the otto-built internal parser-catalog contract.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "ChartSpecRecord".
 */
export interface ChartSpecRecord {
  label: Label1;
  y_title: YTitle;
  unit: Unit;
  command: Command;
  chart: Chart;
  interval?: Interval1;
  max_series?: MaxSeries;
  [k: string]: unknown;
}
/**
 * Lenient read-back variant of :class:`TabSpec` (see :class:`ChartSpecRecord`).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "TabSpecRecord".
 */
export interface TabSpecRecord {
  id: Id4;
  label: Label2;
  metrics: Metrics;
  kind?: Kind;
  columns?: Columns;
  [k: string]: unknown;
}
/**
 * One ``metrics`` row at the ``format:1`` JSON / v2 SQLite import-export boundary.
 *
 * The JSON export format spells the time key ``timestamp``; the SQLite
 * ``metrics`` table column is ``ts``. The ``validation_alias`` accepts both, so
 * a single model validates either seam. ``host`` is optional for the
 * pre-host-column schema; ``meta`` rides only in JSON (the DB has no meta
 * column). Exporting with ``model_dump(mode='json', exclude_none=True)`` emits
 * the JSON spelling and omits ``meta`` when ``None`` (``host=''`` is still
 * emitted — empty string is not ``None``).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "MetricRecord".
 */
export interface MetricRecord {
  timestamp: Timestamp;
  host?: Host1;
  label: Label3;
  value: Value;
  meta?: Meta;
  source?: Source;
  [k: string]: unknown;
}
/**
 * One ``events`` row at the JSON / SQLite **import** boundary.
 *
 * Mirrors the ``MonitorEvent`` fields. Used to validate external event data
 * before constructing the (unchanged, mutable) ``MonitorEvent`` dataclass —
 * event *export* stays ``MonitorEvent.to_dict()``. ``timestamp`` is required
 * (a row without one is skipped, as before); everything else defaults. ``id``
 * is ``None`` when absent so the collector can assign its running id.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "EventRecord".
 */
export interface EventRecord {
  id?: Id5;
  timestamp: Timestamp1;
  end_timestamp?: EndTimestamp;
  label?: Label4;
  source?: Source1;
  color?: Color;
  dash?: Dash;
  [k: string]: unknown;
}
/**
 * One ``log_events`` row at the ``format:1`` JSON / v2 SQLite import-export boundary.
 *
 * Mirrors the parser-emitted ``LogEvent`` plus the host/tab the collector
 * attaches. The JSON export format spells the time key ``timestamp``; the
 * SQLite column is ``ts`` (its ``fields`` column is JSON-decoded by the
 * loader before validation).
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "LogEventRecord".
 */
export interface LogEventRecord {
  timestamp: Timestamp2;
  host?: Host2;
  tab?: Tab;
  fields?: Fields;
  [k: string]: unknown;
}
export interface Fields {
  [k: string]: string;
}
/**
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "ChartMap".
 */
export interface ChartMap {
  [k: string]: string;
}
/**
 * One live tunnel's last known state (spec 2026-07-16 §1).
 *
 * ``hops`` is the ordered host-id chain of ``otto.tunnel.model.Tunnel.path``
 * — ``hops[0]`` the entry end, ``hops[-1]`` the exit end; the topology view
 * consumes consecutive pairs. Host ids share the id space of
 * :class:`LinkEndpointSnapshot.host`. ``status`` is derived from discovery
 * fields, never parsed from the human ``DiscoveredTunnel.status`` string.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "TunnelRecord".
 */
export interface TunnelRecord {
  id: Id6;
  protocol?: Protocol1;
  service_port: ServicePort;
  hops: Hops;
  status?: Status;
  carriers_present?: CarriersPresent;
  carriers_expected?: CarriersExpected;
  age_seconds?: AgeSeconds;
  [k: string]: unknown;
}
/**
 * An incremental update to ONE live monitor session.
 *
 * Spec 2026-07-12 §The stream speaks format:1.
 *
 * A fragment is a *partial* :class:`SessionRecord`: every payload field is
 * optional and carries the SAME name and type as its counterpart there, so the
 * client appends rather than translates. This is deliberate — Plan 5a lost
 * three fix waves to a rename across a lenient boundary model
 * (``MonitorMeta.metrics`` vs ``SessionMeta.charts``), invisible to the type
 * checker because both sides were ``str`` at the seam. The strongest defence is
 * not a mapping function but the absence of a second model: these ARE the
 * payload's models.
 *
 * ``deleted_event_ids`` is the one thing a partial record cannot express by
 * presence, so it is explicit. Event *updates* need no separate kind — the
 * client upserts by ``id``, so an edited event is just an event.
 *
 * ``tunnels`` is the one REPLACE-semantics payload field (the ``meta``
 * precedent, not the append rule): ``None`` means "no tunnel update in this
 * fragment"; a list — including ``[]`` — replaces the session's set
 * wholesale. That is "last known state" expressed on the wire.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "MonitorSessionFragment".
 */
export interface MonitorSessionFragment {
  format?: Format1;
  session: Session;
  metrics?: Metrics2;
  events?: Events1;
  log_events?: LogEvents1;
  deleted_event_ids?: DeletedEventIds;
  chart_map?: ChartMap;
  meta?: SessionMeta | null;
  tunnels?: Tunnels1;
  [k: string]: unknown;
}
/**
 * ``POST /api/session/{sid}/event`` request body (spec 2026-07-18).
 *
 * ``timestamp=None`` means "server-now" (the Mark-now flow). When both
 * timestamps are present the span must be forward; the server re-checks the
 * pair after resolving a ``None`` timestamp to now.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "EventCreateBody".
 */
export interface EventCreateBody {
  label: Label5;
  timestamp?: Timestamp3;
  end_timestamp?: EndTimestamp1;
  color?: Color1;
  dash?: Dash1;
}
/**
 * ``PATCH /api/session/{sid}/event/{id}`` request body (spec 2026-07-18).
 *
 * Every field optional; ``model_fields_set`` distinguishes "absent"
 * (unchanged) from an explicit JSON ``null``. Only ``end_timestamp`` uses
 * that distinction — an explicit null CLEARS the end (span → point); for the
 * other fields null means unchanged, same as absent. The merged
 * start/end ordering check happens in the route, where the existing event's
 * values are known.
 *
 * This interface was referenced by `MonitorHistoricalExportDocument`'s JSON-Schema
 * via the `definition` "EventUpdateBody".
 */
export interface EventUpdateBody {
  label?: Label6;
  timestamp?: Timestamp4;
  end_timestamp?: EndTimestamp2;
  color?: Color2;
  dash?: Dash2;
}
