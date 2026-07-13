// Pure ECharts option builders — the plotly.ts idiom carried over: plain
// objects out, no echarts import, fully unit-testable. Dataviz mark specs
// are encoded here: 2px lines, no point symbols (hover emphasis only),
// hairline recessive grid, axis text in muted ink (never series colors),
// one y-axis per chart, crosshair tooltip on by default.
import type { NormalizedSession, TimeRange } from "../data/exportDoc";
import { parseTs } from "../data/time";
import { SERIES_DARK, SERIES_LIGHT } from "./palette";

export interface ChartTheme {
  ink: string;
  muted: string;
  grid: string;
  axis: string;
  surface: string;
  series: readonly string[];
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
      }
    : {
        ink: "#111827",
        muted: "#6b7280",
        grid: "#e5e7eb",
        axis: "#d1d5db",
        surface: "#ffffff",
        series: SERIES_LIGHT,
      };
}

export interface SeriesInput {
  key: string;
  name: string;
  /** Entity-bound palette slot from the UNFILTERED tree — color follows
   * the entity; filtering must never repaint survivors. */
  slot: number;
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
  const markArea = {
    silent: true,
    animation: false,
    data: events
      .filter((e) => e.toMs !== null)
      .map((e) => [
        { xAxis: e.fromMs, name: e.label, itemStyle: { color: e.color, opacity: 0.12 } },
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
      lineStyle: { width: 2 },
      itemStyle: { color: theme.series[s.slot % theme.series.length] },
      data: s.points,
      ...(i === 0 && (markLine.data.length || markArea.data.length) ? { markLine, markArea } : {}),
    })),
  };
}
