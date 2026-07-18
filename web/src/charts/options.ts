// Pure ECharts option builders — the plotly.ts idiom carried over: plain
// objects out, no echarts import, fully unit-testable. Dataviz mark specs
// are encoded here: 2px lines, no point symbols (hover emphasis only),
// hairline recessive grid, axis text in muted ink (never series colors),
// one y-axis per chart, crosshair tooltip on by default.
import type { NormalizedSession, TimeRange } from "../data/exportDoc";
import { parseTs } from "../data/time";
import { MUTED_SERIES_DARK, MUTED_SERIES_LIGHT, SERIES_DARK, SERIES_LIGHT } from "./palette";

export interface ChartTheme {
  ink: string;
  muted: string;
  grid: string;
  axis: string;
  surface: string;
  series: readonly string[];
  mutedSeries: string;
}

/** Tailwind gray scale values, inlined: charts render to canvas and
 * cannot consume CSS classes. Keep in sync with app.css's body colors. */
export function chartTheme(dark: boolean): ChartTheme {
  return dark
    ? {
        ink: "#f3f4f6",
        muted: "#9ca3af",
        grid: "#1f2937",
        axis: "#374151",
        surface: "#030712",
        series: SERIES_DARK,
        mutedSeries: MUTED_SERIES_DARK,
      }
    : {
        ink: "#111827",
        muted: "#6b7280",
        grid: "#e5e7eb",
        axis: "#d1d5db",
        surface: "#ffffff",
        series: SERIES_LIGHT,
        mutedSeries: MUTED_SERIES_LIGHT,
      };
}

export interface SeriesInput {
  key: string;
  name: string;
  /** Entity-bound palette slot from the UNFILTERED tree — color follows
   * the entity; filtering must never repaint survivors. */
  slot: number;
  /** When true, render as a low-emphasis band member (single muted color,
   * thin line) rather than a distinct palette slot. Used for per-core CPU on
   * high-core hosts. */
  muted?: boolean;
  points: [number, number][];
}

export interface EventMarker {
  id: number;
  label: string;
  color: string;
  fromMs: number;
  toMs: number | null;
}

/** Window-overlap filter over the session's wire event rows. */
export function eventMarkers(
  events: NormalizedSession["events"],
  window: TimeRange,
): EventMarker[] {
  const out: EventMarker[] = [];
  for (const ev of events) {
    const fromMs = parseTs(ev.timestamp);
    const toMs = ev.end_timestamp != null ? parseTs(ev.end_timestamp) : null;
    const overlaps =
      toMs === null
        ? fromMs >= window.from && fromMs <= window.to
        : fromMs <= window.to && toMs >= window.from;
    if (!overlaps) continue;
    out.push({
      // Real wire ids are non-negative; negative synthetics can't collide.
      id: ev.id ?? -1 - out.length,
      label: ev.label ?? "",
      color: ev.color ?? "#7c5cff",
      fromMs,
      toMs,
    });
  }
  return out;
}

export function zoomToRange(startPct: number, endPct: number, window: TimeRange): TimeRange {
  const span = window.to - window.from;
  return {
    from: Math.round(window.from + (startPct / 100) * span),
    to: Math.round(window.from + (endPct / 100) * span),
  };
}

/** Vertical pixel step between stacked markArea labels — see `assignLanes`. */
const LANE_LABEL_STEP_PX = 14;

/** Greedy interval-graph lane assignment: the pure core of the overlapping-
 * span-event label fix (found by a visual gate against kitchen-sink.json's
 * "stress run" 09:25-09:35 and "log capture" 09:30-09:40 — two overlapping
 * span events whose markArea labels used to render at the same default
 * position and collide into unreadable mush).
 *
 * Intervals are half-open `[fromMs, toMs)`: an event starting exactly when
 * another ends does NOT overlap it. Sorts by start (ties by end) and gives
 * each event the lowest-numbered lane whose most recently placed occupant
 * has already ended — standard greedy interval-graph colouring, minimal in
 * the number of lanes used. Two events whose intervals overlap always land
 * in distinct lanes; events that never overlap anything all settle into
 * lane 0, so the common (non-overlapping) case is unchanged.
 *
 * Returns lanes in the SAME order as `events` (not sorted order), so a
 * caller can zip the result straight back onto its own array. Pure and
 * synchronous — no ECharts/DOM dependency — so it's unit-testable directly,
 * independent of eventOverlay's wiring below. */
export function assignLanes(events: { fromMs: number; toMs: number }[]): number[] {
  const order = events
    .map((_, i) => i)
    .sort((a, b) => {
      const byStart = events[a].fromMs - events[b].fromMs;
      return byStart !== 0 ? byStart : events[a].toMs - events[b].toMs;
    });
  const laneEnds: number[] = []; // laneEnds[lane] = end (toMs) of that lane's last occupant
  const lanes: number[] = new Array(events.length);
  for (const i of order) {
    const ev = events[i];
    let lane = 0;
    while (lane < laneEnds.length && laneEnds[lane] > ev.fromMs) lane++;
    lanes[i] = lane;
    laneEnds[lane] = ev.toMs;
  }
  return lanes;
}

/** markLine/markArea for a chart's anchor (index-0) series — split out of
 * buildStackOption so ChartPanel can re-apply just this overlay as a cheap
 * merge patch (see windowPatch) whenever the window slides, without
 * rebuilding the full series data/styling option. Both call sites must stay
 * on this one implementation so the incremental patch and the full rebuild
 * never draw different markers for the same window. */
export interface EventOverlay {
  markLine: { symbol: string; animation: boolean; label: Record<string, unknown>; data: unknown[] };
  markArea: { silent: boolean; animation: boolean; data: unknown[] };
}

export function eventOverlay(
  events: EventMarker[],
  theme: Pick<ChartTheme, "muted">,
): EventOverlay {
  const markLine = {
    symbol: "none",
    animation: false,
    label: { formatter: "{b}", color: theme.muted, fontSize: 10 },
    data: events
      .filter((e) => e.toMs === null)
      .map((e) => ({
        xAxis: e.fromMs,
        name: e.label,
        lineStyle: { color: e.color, type: "dashed", width: 1 },
      })),
  };
  // Span events only (instant events use markLine above, untouched). Lanes
  // are assigned over exactly the set being drawn here — deterministic and
  // stateless, recomputed fresh on every call (no identity to keep in sync
  // as the window/event set changes).
  const spans = events.filter((e) => e.toMs !== null);
  const lanes = assignLanes(spans.map((e) => ({ fromMs: e.fromMs, toMs: e.toMs as number })));
  const markArea = {
    silent: true,
    animation: false,
    data: spans.map((e, i) => [
      {
        xAxis: e.fromMs,
        name: e.label,
        itemStyle: { color: e.color, opacity: 0.12 },
        // Stack overlapping events' labels instead of colliding: each lane
        // sits LANE_LABEL_STEP_PX further down from the region's top edge.
        // Position-only — text, colour and the markLine path above are
        // untouched (a layout fix, not a restyle).
        label: { position: ["50%", lanes[i] * LANE_LABEL_STEP_PX] },
      },
      { xAxis: e.toMs as number },
    ]),
  };
  return { markLine, markArea };
}

/** Cheap incremental patch for a live tick that only moved the window (a
 * DIFFERENT series ticked, but session.endMs — and therefore liveRange — is
 * global, so this chart's x-axis is stale even though its own series didn't
 * change). Meant for `chart.setOption(patch, { notMerge: false })`: it only
 * touches xAxis bounds and the anchor series' markLine/markArea, never
 * series `data`, so it costs O(events) instead of O(points) — see
 * ChartPanel.tsx. `anchorSeriesId` is the id (SeriesInput.key) of the
 * chart's index-0 series, i.e. the one buildStackOption attaches
 * markLine/markArea to; pass null when the chart has no series yet. */
export function windowPatch(args: {
  window: TimeRange;
  events: EventMarker[];
  theme: Pick<ChartTheme, "muted">;
  anchorSeriesId: string | null;
}): Record<string, unknown> {
  const { window, events, theme, anchorSeriesId } = args;
  const patch: Record<string, unknown> = {
    xAxis: { min: window.from, max: window.to },
  };
  if (anchorSeriesId !== null) {
    patch.series = [{ id: anchorSeriesId, ...eventOverlay(events, theme) }];
  }
  return patch;
}

export function buildStackOption(args: {
  unit: string;
  yTitle: string;
  series: SeriesInput[];
  window: TimeRange;
  events: EventMarker[];
  theme: ChartTheme;
}): Record<string, unknown> {
  const { unit, yTitle, series, window, events, theme } = args;
  const { markLine, markArea } = eventOverlay(events, theme);
  return {
    animation: false,
    grid: { left: 56, right: 16, top: 28, bottom: 28 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: theme.axis } },
      backgroundColor: theme.surface,
      borderColor: theme.grid,
      textStyle: { color: theme.ink, fontSize: 12 },
      valueFormatter: (v: unknown) =>
        `${typeof v === "number" ? Math.round(v * 100) / 100 : v}${unit ? ` ${unit}` : ""}`,
    },
    xAxis: {
      type: "time",
      min: window.from,
      max: window.to,
      axisLine: { lineStyle: { color: theme.axis } },
      axisLabel: { color: theme.muted, fontSize: 10, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      name: yTitle,
      nameTextStyle: { color: theme.muted, fontSize: 10, align: "left" },
      axisLabel: { color: theme.muted, fontSize: 10 },
      splitLine: { lineStyle: { color: theme.grid, width: 1 } },
    },
    dataZoom: [
      { type: "inside", filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: false },
    ],
    series: series.map((s, i) => ({
      id: s.key,
      name: s.name,
      type: "line",
      showSymbol: false,
      sampling: "lttb",
      emphasis: { focus: "series", itemStyle: { borderWidth: 2 } },
      lineStyle: { width: s.muted ? 1 : 2, opacity: s.muted ? 0.5 : 1 },
      itemStyle: {
        color: s.muted ? theme.mutedSeries : theme.series[s.slot % theme.series.length],
      },
      data: s.points,
      ...(i === 0 && (markLine.data.length || markArea.data.length) ? { markLine, markArea } : {}),
    })),
  };
}
