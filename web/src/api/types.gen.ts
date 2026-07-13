/* AUTO-GENERATED from monitor-meta.schema.json — run scripts/gen_web_types.sh; do not edit. */

export type Hosts = string[];
export type Live = boolean;
export type Label = string;
export type YTitle = string;
export type Unit = string;
export type Command = string;
export type Chart = string;
export type Interval = number | null;
export type Metrics = ChartSpec[];
export type Id = string;
export type Label1 = string;
export type Metrics1 = string[];
export type Kind = "charts" | "table";
export type Columns = string[] | null;
export type Tabs = TabSpec[];
export type Interval1 = number | null;

/**
 * The typed internal contract: hosts, chart specs, and tab layout.
 *
 * The declarative contract the frontend renders from; TS types are
 * generated from this schema in Phase 2. This model is never served
 * directly — every dashboard boot (live or review) hydrates from
 * ``GET /api/monitor_sessions``, whose ``format:1`` shape carries this same
 * information reshaped into each session's :class:`SessionMeta`.
 *
 * ``interval`` is the global collection interval in seconds — ``None`` until
 * :meth:`~otto.monitor.collector.MetricCollector.run` has recorded one (a
 * collector that has not started live collection). Reviewed data (loaded
 * from ``otto monitor <source>``) carries this in its own
 * :class:`SessionMeta` instead — see :func:`otto.monitor.export.session_meta`.
 */
export interface MonitorDashboardChartTabLayoutModel {
  hosts: Hosts;
  live: Live;
  metrics: Metrics;
  tabs: Tabs;
  interval?: Interval1;
}
/**
 * One dashboard chart descriptor: otto's typed, internal parser-catalog view.
 *
 * The declarative contract the frontend renders from; TS types are
 * generated from this schema in Phase 2. Not served at any endpoint of its
 * own — it is reshaped into :class:`ChartSpecRecord` inside
 * :class:`SessionMeta` for the ``monitor_sessions``/SSE wire (see
 * :func:`otto.monitor.export.session_meta`).
 */
export interface ChartSpec {
  label: Label;
  y_title: YTitle;
  unit: Unit;
  command: Command;
  chart: Chart;
  interval?: Interval;
}
/**
 * One dashboard tab descriptor: otto's typed, internal parser-catalog view.
 *
 * The declarative contract the frontend renders from; TS types are
 * generated from this schema in Phase 2. ``kind="table"`` tabs render an
 * event table (schema in ``columns``) instead of charts, and carry
 * ``metrics=[]``. Not served at any endpoint of its own — see
 * :class:`ChartSpec` for the wire path.
 */
export interface TabSpec {
  id: Id;
  label: Label1;
  metrics: Metrics1;
  kind?: Kind;
  columns?: Columns;
}
