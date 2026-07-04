// The single zustand store backing the React dashboard. Reducers here
// mirror src/otto/monitor/static/dashboard.js's module-level `state` object
// and its SSE handlers (see dashboard.js §State, §startSSE,
// §appendMetricPoint) so the two frontends stay behaviorally identical
// while both exist (Phase 2 cutover deletes the legacy one — Task 9).
import { create } from "zustand";

import type { DataPayload, LogEventRow, MonitorEvent, Point } from "./api/client";
import type { MonitorDashboardApiMetaPayload } from "./api/types.gen";
import { appendRows, groupRowsFromData } from "./logevents";

export type ConnectionState = "connecting" | "live" | "historical" | "disconnected";

/** The `/api/stream` "metric" message shape — mirrors collector.py's `_record_point()` `msg` dict. */
export interface MetricMessage {
  type: "metric";
  host: string;
  label: string;
  chart: string;
  y_title: string;
  unit: string;
  key: string;
  ts: string;
  value: number;
  meta?: Record<string, unknown>;
}

/** The `/api/stream` "log_event" message — mirrors collector.py's `_record_log_events()` batch. */
export interface LogEventMessage {
  type: "log_event";
  host: string;
  tab: string;
  rows: { ts: string; fields: Record<string, string> }[];
}

interface MonitorActions {
  applyMeta: (meta: MonitorDashboardApiMetaPayload) => void;
  applyData: (data: DataPayload) => void;
  metricMsg: (msg: MetricMessage) => void;
  logEventMsg: (msg: LogEventMessage) => void;
  eventMsg: (event: MonitorEvent) => void;
  eventUpdated: (event: MonitorEvent) => void;
  eventDeleted: (id: number) => void;
  selectHost: (host: string | null) => void;
  selectTab: (tabId: string) => void;
  togglePause: () => void;
  setSpanStart: (id: number | null) => void;
  /** dashboard.js's `src.onopen` — connection resolves to live/historical once the SSE stream is up. */
  sseOpened: () => void;
  /** dashboard.js's `src.onerror` — disconnect transition + span abandonment. */
  sseErrored: () => void;
  /** dashboard.js's `openPopover(ev, mouseEvent)` — records which event is being edited and where the click landed (for viewport-clamped positioning in `EventPopover`). */
  openPopover: (id: number, x: number, y: number) => void;
  /** dashboard.js's `hidePopover()`. */
  closePopover: () => void;
  /** dashboard.js's `initTabCharts()`'s unconditional `clear-events-btn.disabled = false` line — fired once chart groups exist (historical: immediately; live: on first host pick). */
  chartsReady: () => void;
}

export interface MonitorState {
  meta: MonitorDashboardApiMetaPayload | null;
  /** `"host/label"` (or bare `label` for historical data) -> that series' points. */
  series: Record<string, Point[]>;
  events: MonitorEvent[];
  /** bare metric label -> chart key, hydrated from `/api/data`'s `chart_map`. */
  chartMap: Record<string, string>;
  /** `"host/tab"` -> that table's rows (newest last; capped at MAX_TABLE_ROWS). */
  logEvents: Record<string, LogEventRow[]>;
  activeTab: string | null;
  selectedHost: string | null;
  paused: boolean;
  connection: ConnectionState;
  spanStartId: number | null;
  /** dashboard.js's `state.editingEventId` — the id of the event currently open in the popover, or null. */
  editingEventId: number | null;
  /** Where the triggering `plotly_clickannotation` click landed — `EventPopover`'s input to dashboard.js's `openPopover()` viewport-clamp math. Set together with `editingEventId`. */
  popoverAnchor: { x: number; y: number } | null;
  /**
   * dashboard.js's `state.isLive` — true forever once the SSE stream has
   * reached "live" a single time. Distinct from `connection`, which DOES
   * fall back to 'disconnected' on error: the mark-event button
   * (`markEventBox`) is enabled off this flag and, per dashboard.js's
   * `src.onerror` (which only ever disables `spanEventBox`), is never
   * re-disabled on disconnect — only the span button is.
   */
  everLive: boolean;
  /** dashboard.js's `state.metricPlots.length > 0` — gates `#clear-events-btn`. */
  chartsInitialized: boolean;
  /**
   * The most recent `/api/stream` "metric" message, verbatim — a
   * notification channel for `ChartGrid` (Task 6), which needs the raw
   * message (its `chart`/`y_title`/`unit` fields, not just the appended
   * point) to run dashboard.js's `appendMetricPoint()` chart-group
   * bookkeeping and imperative `Plotly.extendTraces` fast path. `series` is
   * the durable store of point data; this is a "something just arrived"
   * signal layered on top, the same role `eventMsg`/`eventUpdated` already
   * play by storing the full event object rather than a derived diff.
   */
  lastMetric: MetricMessage | null;
  actions: MonitorActions;
}

/**
 * Returns the series key for the selected host + a metric label. Falls back
 * to the bare label when there is no host prefix (historical data).
 * Mirrors dashboard.js's `seriesKey()` exactly.
 */
export function seriesKey(selectedHost: string | null, label: string): string {
  return selectedHost ? `${selectedHost}/${label}` : label;
}

/** Status text shown at `#status-label` — mirrors dashboard.js's `src.onopen`/`onerror`/pause handler. */
export function statusText(connection: ConnectionState, paused: boolean): string {
  switch (connection) {
    case "connecting":
      return "Connecting…";
    case "historical":
      return "Historical";
    case "disconnected":
      return "Disconnected";
    case "live":
      return paused ? "Paused" : "Live";
  }
}

/**
 * `#status-dot` className — mirrors dashboard.js: the dot's base class is
 * 'live'/'history'/'disconnected' (or unset while connecting), and pausing
 * ADDS a 'paused' modifier onto the live class rather than replacing it.
 */
export function statusDotClass(connection: ConnectionState, paused: boolean): string {
  switch (connection) {
    case "connecting":
      return "";
    case "historical":
      return "history";
    case "disconnected":
      return "disconnected";
    case "live":
      return paused ? "live paused" : "live";
  }
}

export const useMonitorStore = create<MonitorState>()((set, get) => ({
  meta: null,
  series: {},
  events: [],
  chartMap: {},
  logEvents: {},
  activeTab: null,
  selectedHost: null,
  paused: false,
  connection: "connecting",
  spanStartId: null,
  editingEventId: null,
  popoverAnchor: null,
  everLive: false,
  chartsInitialized: false,
  lastMetric: null,
  actions: {
    applyMeta: (meta) => set({ meta }),

    applyData: (data) =>
      set({
        series: data.series,
        events: data.events,
        chartMap: data.chart_map,
        logEvents: groupRowsFromData(data.log_events ?? []),
      }),

    // dashboard.js's appendMetricPoint(): the point is pushed to state.series
    // unconditionally, THEN (only) chart-rendering is skipped while paused —
    // pause freezes charts, never data. The chart-rendering half (extendTraces,
    // new chart-group bookkeeping) belongs to ChartGrid/ChartPanel (Task 6);
    // this reducer owns only the always-append data half.
    metricMsg: (msg) =>
      set((state) => ({
        series: {
          ...state.series,
          [msg.key]: [
            ...(state.series[msg.key] ?? []),
            { ts: msg.ts, value: msg.value, meta: msg.meta ?? null },
          ],
        },
        lastMetric: msg,
      })),

    // Batched log_event frames append under their (host, tab) key. Not
    // pause-gated: pause freezes chart *rendering*; the table renders
    // straight from this slice, and v1 deliberately keeps it live.
    logEventMsg: (msg) =>
      set((state) => ({
        logEvents: appendRows(
          state.logEvents,
          msg.host,
          msg.tab,
          msg.rows.map((r) => ({
            timestamp: r.ts,
            host: msg.host,
            tab: msg.tab,
            fields: r.fields,
          })),
        ),
      })),

    // dashboard.js's addEventToPlot(): always pushes; the paused-gated half
    // is the refreshPlot() render call, again a ChartGrid concern.
    eventMsg: (event) => set((state) => ({ events: [...state.events, event] })),

    // dashboard.js's src.onmessage "event_updated" branch: replace in place
    // by id, no-op if the event is unknown.
    eventUpdated: (event) =>
      set((state) => {
        const idx = state.events.findIndex((e) => e.id === event.id);
        if (idx < 0) return {};
        const events = state.events.slice();
        events[idx] = event;
        return { events };
      }),

    // dashboard.js's src.onmessage "event_deleted" branch: also closes the
    // popover if the deleted event is the one being edited
    // (`if (state.editingEventId === msg.id) hidePopover();`).
    eventDeleted: (id) =>
      set((state) => ({
        events: state.events.filter((e) => e.id !== id),
        ...(state.editingEventId === id ? { editingEventId: null, popoverAnchor: null } : null),
      })),

    selectHost: (host) => set({ selectedHost: host }),

    selectTab: (tabId) => set({ activeTab: tabId }),

    togglePause: () => set((state) => ({ paused: !state.paused })),

    setSpanStart: (id) => set({ spanStartId: id }),

    sseOpened: () =>
      set((state) => {
        const live = get().meta?.live ?? false;
        return { connection: live ? "live" : "historical", everLive: state.everLive || live };
      }),

    // dashboard.js's src.onerror: always clears paused and abandons any open
    // span. The dot/label transition depends on whether the stream had ever
    // reached 'live' (dashboard.js's separate `state.isLive` flag) — our
    // `connection` enum only ever equals 'live' after that has happened, so
    // testing `connection === 'live'` here is the same gate without a second
    // flag FOR THIS PURPOSE (the mark-event button below needs its own
    // `everLive`, since — unlike this dot/label transition — it must NOT
    // flip back off when `connection` leaves 'live'). A stream that errors
    // before ever opening live (or one that was always historical) lands on
    // 'historical', matching dashboard.js's `meta.live && isLive` check
    // exactly (including its "never opened" quirk of falling through to the
    // Historical label). `everLive` itself is untouched here — see its
    // field doc.
    sseErrored: () =>
      set((state) => ({
        paused: false,
        spanStartId: null,
        connection: state.connection === "live" ? "disconnected" : "historical",
      })),

    openPopover: (id, x, y) => set({ editingEventId: id, popoverAnchor: { x, y } }),

    closePopover: () => set({ editingEventId: null, popoverAnchor: null }),

    chartsReady: () => set({ chartsInitialized: true }),
  },
}));

export function useMonitorActions(): MonitorActions {
  return useMonitorStore((s) => s.actions);
}
