// REST calls against otto's monitor server. Wire shapes here mirror
// src/otto/monitor/server.py's /api/meta and /api/data responses (and, by
// extension, dashboard.js's `state.series`/`state.events`/`state.chartMap`).
// `MonitorDashboardApiMetaPayload` is generated from the pydantic model
// (types.gen.ts); the shapes below are NOT schema-generated because
// /api/data has no pydantic response_model yet — they are hand-kept in sync
// with server.py's `data()` route and models/monitor.py's `MetricPoint`/
// `MonitorEvent.to_dict()`.
import type { MonitorDashboardApiMetaPayload } from "./types.gen";

/** One charted sample — mirrors `MetricPoint.model_dump()` (models/monitor.py). */
export interface Point {
  ts: string;
  value: number;
  meta: Record<string, unknown> | null;
}

/** A timeline marker — mirrors `MonitorEvent.to_dict()` (monitor/events.py). */
export interface MonitorEvent {
  id: number;
  timestamp: string;
  label: string;
  source: string;
  color: string;
  dash: string;
  end_timestamp: string | null;
}

/** One log-event row — mirrors `LogEventRecord` / `collector.get_log_events()`. */
export interface LogEventRow {
  timestamp: string;
  host: string;
  tab: string;
  fields: Record<string, string>;
}

/** The `/api/data` snapshot payload — mirrors server.py's `data()` route. */
export interface DataPayload {
  series: Record<string, Point[]>;
  events: MonitorEvent[];
  chart_map: Record<string, string>;
  log_events: LogEventRow[];
}

export async function fetchMeta(): Promise<MonitorDashboardApiMetaPayload> {
  const res = await fetch("/api/meta");
  return (await res.json()) as MonitorDashboardApiMetaPayload;
}

export async function fetchData(): Promise<DataPayload> {
  const res = await fetch("/api/data");
  return (await res.json()) as DataPayload;
}
