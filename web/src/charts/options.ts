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

/** `+`/`-` zoom buttons (Task 11): new span = span×factor about the window's
 * own center — factor 0.5 zooms in, 2 zooms out. Returns null (skip) once the
 * zoomed-in span would fall below the same 1000ms floor ChartPanel's drag-zoom
 * debounce already treats as noise (MIN_ZOOM_DELTA_MS). */
export function zoomAbout(window: TimeRange, factor: number): TimeRange | null {
  const span = (window.to - window.from) * factor;
  if (span < 1000) return null; // MIN_ZOOM_DELTA_MS: below this a zoom is noise
  const center = (window.from + window.to) / 2;
  return { from: Math.round(center - span / 2), to: Math.round(center + span / 2) };
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
  theme: Pick<ChartTheme, "muted" | "ink">,
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
        // color/fontSize (Task 11): markArea labels used to inherit
        // ECharts' default label color regardless of theme — illegible
        // against a dark surface. theme.ink is the same token buildStackOption's
        // tooltip textStyle already uses for dark-mode-safe text.
        label: {
          position: ["50%", lanes[i] * LANE_LABEL_STEP_PX],
          color: theme.ink,
          fontSize: 10,
        },
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
  theme: Pick<ChartTheme, "muted" | "ink">;
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
    // Task 11: the wheel is freed for page scroll (it used to fight it —
    // zoomOnMouseWheel/moveOnMouseWheel both false), and pan is Ctrl-drag —
    // ECharts' modifier set has no meta key, so Ctrl is the one pan gesture
    // available on every platform (documented in Task 14). +/- buttons
    // (SubjectPage) cover zoom instead of the wheel.
    //
    // `zoomLock: true` (Task 13, found only by driving a REAL browser — the
    // vitest suite mocks ECharts entirely and can't see this): an "inside"
    // dataZoom's `zoomOnMouseWheel`/`moveOnMouseWheel` flags only gate
    // whether it actually DISPATCHES a zoom/pan action on wheel — they do
    // NOT stop it from claiming the wheel event in the first place.
    // ECharts' RoamController (helper/RoamController.js) enables its
    // mousewheel listener with a HARDCODED `{zoomOnMouseWheel: true, ...}`
    // whenever a dataZoom's own `controlType` resolves to `true` (full
    // roam) — our per-model `false`s are consulted only deep inside that
    // listener, by which point it has already called `preventDefault()` +
    // `stopPropagation()` on the wheel event (RoamController's own code
    // comment even warns "if 'zoom' is not needed, 'zoom' should not be
    // enabled, otherwise default mousewheel behaviour (scroll page) will be
    // disabled" — but nothing here disables it without this flag). Setting
    // `zoomLock` makes `controlType` resolve to `'move'` instead, which
    // installs ONLY the drag-pan (mousedown/mousemove/mouseup) listeners,
    // never the wheel one — the wheel then never leaves the browser's
    // native scroll at all. Dropping the roam controller's OWN drag-zoom
    // capability costs nothing: a global-armed brush cursor (below) already
    // intercepts every plain drag before dataZoom's roam controller would
    // ever see it.
    //
    // `moveOnMouseMove: "ctrl"` below is now DECLARATIVE only, not the
    // actual pan mechanism — a second Task 13 finding: this dataZoom's own
    // percent-range pan is structurally a no-op here regardless (its
    // `[0, 100]` always equals `xAxis.min`/`max`'s current window, so there
    // is never room within its own extent to shift). ChartPanel.tsx
    // implements the real Ctrl-drag pan by hand (see its init effect); this
    // flag is left in place as the still-accurate statement of intent/
    // config surface, not as working code — removing it would not change
    // any observed behavior.
    dataZoom: [
      {
        type: "inside",
        filterMode: "none",
        zoomOnMouseWheel: false,
        moveOnMouseMove: "ctrl",
        moveOnMouseWheel: false,
        zoomLock: true,
      },
    ],
    // Task 12 (Monitor Plan 5c): a `lineX` brush select, armed by ChartPanel
    // via takeGlobalCursor — dragging across the plot draws this ghost
    // rectangle, and brushEnd routes the selected range to either a
    // zoom-select or a sweep-to-mark, depending on whether marking is armed.
    brush: {
      xAxisIndex: 0,
      // Ghost styling for the in-flight sweep; series stay fully painted
      // (outOfBrush colorAlpha 1 — brushing here SELECTS a range, it never
      // filters data).
      brushStyle: { borderWidth: 1, color: "rgba(124, 92, 255, 0.08)", borderColor: "#7c5cff" },
      outOfBrush: { colorAlpha: 1 },
    },
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
