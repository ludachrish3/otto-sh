// Thin, typed wrapper over `plotly.js-gl2d-dist-min` (the partial
// "scattergl only" Plotly bundle — otto's dashboard only ever draws
// `scattergl` traces, see `buildMetricTraces` below) plus the pure
// trace/layout/shape/annotation builders ported byte-for-byte from
// src/otto/monitor/static/dashboard.js's "Plotly helpers" section
// (`plotTheme`/`topMargin`/`sharedAxisStyle`/`legendRows`/`buildLayout`/
// `metaText`/`buildMetricTraces`/`hexToRgba`/`buildShapes`/
// `buildAnnotations`). See `plotly-gl2d.d.ts` for why the package's own
// (nonexistent) types aren't used.
import Plotly, {
  type PlotlyClickAnnotationEvent,
  type PlotlyData,
  type PlotlyLayout,
} from "plotly.js-gl2d-dist-min";

import type { MonitorEvent, Point } from "./api/client";
import type { Metric } from "./grouping";
import {
  LEGEND_CAP_ROWS,
  type LegendCandidate,
  retireStaleMetrics,
  selectLegendEntries,
} from "./retirement";
import { seriesKey } from "./store";

/**
 * dashboard.js's `initTabPlots()` config object, with Task 11's
 * window-resize fix applied: `responsive` is now `true` (legacy shipped
 * `false` — the #2 known frontend bug, "plots don't resize with the
 * window", `todo/TODO.md`). `responsive: true` makes the INITIAL draw
 * autosize to the container (rather than a fixed default width) and wires
 * Plotly's own window-resize listener as a first line of defense;
 * `ChartPanel`'s ResizeObserver (`plotly.resize`) is the primary, explicit
 * mechanism this task adds, covering container-size changes a `window`
 * resize event doesn't fire for (tab activation, expand/collapse) and
 * giving the resize pin a deterministic trigger to assert against.
 */
export const PLOT_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["lasso2d", "select2d"],
} as const;

/**
 * Typed `newPlot`/`react`/`extendTraces`/`relayout` helpers. Grouped under
 * one object (rather than four bare exports) so call sites read as
 * `plotly.react(...)` — unambiguous next to the `react` *library* import
 * that components using this module also have in scope.
 */
export const plotly = {
  newPlot: (div: HTMLDivElement, data: PlotlyData[], layout: PlotlyLayout): Promise<HTMLElement> =>
    Plotly.newPlot(div, data, layout, PLOT_CONFIG),
  react: (div: HTMLDivElement, data: PlotlyData[], layout: PlotlyLayout): Promise<HTMLElement> =>
    Plotly.react(div, data, layout),
  extendTraces: (
    div: HTMLDivElement,
    update: Record<string, unknown[][]>,
    indices: number[],
  ): void => {
    Plotly.extendTraces(div, update, indices);
  },
  relayout: (div: HTMLDivElement, update: Record<string, unknown>): Promise<HTMLElement> =>
    Plotly.relayout(div, update),
  /**
   * Task 11: re-measure `div`'s current box and redraw the plot to fit it —
   * `ChartPanel`'s ResizeObserver callback, the window-resize fix's actual
   * mechanism (see `PLOT_CONFIG`'s comment and `plotly-gl2d.d.ts`'s
   * `Plots.resize` doc for why this call over a hand-rolled `relayout`).
   */
  resize: (div: HTMLDivElement): Promise<void> => Plotly.Plots.resize(div),
  /**
   * dashboard.js's `mp.div.on('plotly_clickannotation', ...)`: plotly.js
   * attaches an event-emitter surface directly onto the graph div once it's
   * been drawn — not one of the four calls `PlotlyStatic` declares, so this
   * casts narrowly to that emitter shape rather than widening every other
   * call site's div type.
   */
  onClickAnnotation: (div: HTMLElement, cb: (event: PlotlyClickAnnotationEvent) => void): void => {
    (
      div as HTMLElement & {
        on: (
          name: "plotly_clickannotation",
          cb: (event: PlotlyClickAnnotationEvent) => void,
        ) => void;
      }
    ).on("plotly_clickannotation", cb);
  },
};

// ── dashboard.js's height-math constants ────────────────────────────────────
// Originally ported as-is (see dashboard.js's own comment above
// `CHART_AREA_HEIGHT`): total div height = CHART_AREA_HEIGHT + topMargin() +
// bottomMargin, where topMargin grew with event-annotation label length and
// bottomMargin grew with legend rows — one row per PID ever seen, without
// bound. That was the #1 known height-growth bug. Task 10 fixes it: `height`
// below is now a hard CONSTANT (never derived from event/trace counts), and
// the bottom margin always reserves exactly `LEGEND_CAP_ROWS` worth of
// legend space, whether 0, 1, or 2 rows are actually in use. `topMargin` and
// `legendRows` remain exported/tested as standalone pure helpers (still
// meaningful in isolation), but `buildLayout` no longer feeds their output
// into margin/height sizing.
export const CHART_AREA_HEIGHT = 160;
export const AXIS_BOTTOM_PX = 40;
export const LEGEND_ROW_PX = 20;
export const LEGEND_PAD_PX = 4;
export const ITEMS_PER_ROW = 6;

/** 2 rows * 6 items/row — the legend budget in entries (see retirement.ts's
 * `LEGEND_CAP_ROWS`, the policy value; this is its rendering-side conversion). */
export const LEGEND_CAP_ENTRIES = LEGEND_CAP_ROWS * ITEMS_PER_ROW;

/** Fixed regardless of event-annotation label length — see the height-math comment above. */
export const FIXED_TOP_MARGIN_PX = 36;

/** Fixed regardless of actual trace/legend-row count — always reserves the full legend cap. */
export const FIXED_BOTTOM_MARGIN_PX =
  AXIS_BOTTOM_PX + LEGEND_CAP_ROWS * LEGEND_ROW_PX + LEGEND_PAD_PX;

/** The one, constant `.metric-plot` div height — Task 10's height-growth fix. */
export const CONSTANT_CHART_HEIGHT_PX =
  CHART_AREA_HEIGHT + FIXED_TOP_MARGIN_PX + FIXED_BOTTOM_MARGIN_PX;

interface PlotTheme {
  paper: string;
  plot: string;
  grid: string;
  tick: string;
  axis: string;
  font: string;
}

/** dashboard.js's `plotTheme()` — reads `body.light` directly, same as legacy. */
export function plotTheme(): PlotTheme {
  const light = document.body.classList.contains("light");
  return {
    paper: light ? "#f5f6fa" : "#0f1117",
    plot: light ? "#eef0f7" : "#13151f",
    grid: light ? "#d0d3e8" : "#2a2d3e",
    tick: light ? "#555" : "#aaa",
    axis: light ? "#666" : "#888",
    font: light ? "#1a1a2e" : "#e0e0e0",
  };
}

/** dashboard.js's `topMargin()`. */
export function topMargin(events: MonitorEvent[]): number {
  if (events.length === 0) return 36;
  const maxLen = Math.max(...events.map((ev) => ev.label.length));
  // Annotations are rotated -45°; estimate vertical reach above anchor.
  return Math.max(36, Math.round(40 + maxLen * 4));
}

/** dashboard.js's `sharedAxisStyle()`. */
function sharedAxisStyle(t: PlotTheme): Record<string, unknown> {
  return { gridcolor: t.grid, zerolinecolor: t.grid, tickfont: { color: t.tick, size: 10 } };
}

/** dashboard.js's `legendRows()`. */
export function legendRows(traces: PlotlyData[]): number {
  const n = traces.filter((tr) => tr.showlegend !== false && tr.name).length;
  return n > 1 ? Math.ceil(n / ITEMS_PER_ROW) : 0;
}

/**
 * dashboard.js's `buildLayout()`, with Task 10's height-growth fix applied:
 * `margin.t`/`margin.b`/`height` are now the FIXED constants above, never
 * derived from `events`/`traces` — see the height-math comment above
 * `CHART_AREA_HEIGHT`. `events` substitutes for `state.events`
 * (`buildShapes`/`buildAnnotations` still read it; only the margin sizing
 * stopped depending on it).
 */
export function buildLayout(
  traces: PlotlyData[],
  opts: { yaxisTitle: string },
  events: MonitorEvent[],
): PlotlyLayout {
  const t = plotTheme();
  const rows = legendRows(traces);
  const layout: PlotlyLayout = {
    xaxis: { type: "date", hoverformat: "%H:%M:%S.%L", ...sharedAxisStyle(t) },
    paper_bgcolor: t.paper,
    plot_bgcolor: t.plot,
    font: { color: t.font },
    margin: { t: FIXED_TOP_MARGIN_PX, b: FIXED_BOTTOM_MARGIN_PX, l: 56, r: 20 },
    shapes: buildShapes(events),
    annotations: buildAnnotations(events),
    height: CONSTANT_CHART_HEIGHT_PX,
    yaxis: {
      title: { text: opts.yaxisTitle, font: { size: 11, color: t.axis } },
      rangemode: "tozero",
      ...sharedAxisStyle(t),
    },
    showlegend: rows > 0,
  };
  if (rows > 0) {
    // Legend anchor (top edge) is placed just below the x-axis tick labels.
    // y is in Plotly's plot-area coordinates: 0 = chart bottom, negative = below.
    layout.legend = {
      orientation: "h",
      y: -(AXIS_BOTTOM_PX / CHART_AREA_HEIGHT),
      yanchor: "top",
      font: { size: 10, color: t.font },
    };
  }
  return layout;
}

/** dashboard.js's `metaText()`. */
export function metaText(meta: Record<string, unknown> | null | undefined): string {
  if (!meta) return "";
  return Object.entries(meta)
    .map(([k, v]) => `${k}: ${v}`)
    .join("<br>");
}

/**
 * dashboard.js's `buildMetricTraces()`. `series`/`selectedHost` substitute
 * for `state.series`/`state.selectedHost`, via the same `seriesKey()` the
 * store exports — one definition, shared with the rest of the app.
 */
export function buildMetricTraces(
  metrics: Metric[],
  series: Record<string, Point[]>,
  selectedHost: string | null,
): PlotlyData[] {
  return metrics.map((metric) => {
    const key = seriesKey(selectedHost, metric.label);
    const pts = series[key] ?? [];
    const name = metric.label.startsWith("proc/") ? metric.label.slice(5) : metric.label;
    const trace: PlotlyData = {
      type: "scattergl",
      mode: "lines+markers",
      name,
      x: pts.map((p) => p.ts),
      y: pts.map((p) => p.value),
      connectgaps: false,
      line: { width: 1.5 },
      marker: { size: 3 },
    };
    if (pts.some((p) => p.meta)) {
      trace.text = pts.map((p) => metaText(p.meta));
      trace.hovertemplate = `<b>${name}</b>: %{y:.2f}${metric.unit}<br>%{text}<br>%{x}<extra></extra>`;
    } else {
      trace.hovertemplate = `<b>${name}</b>: %{y:.2f}${metric.unit}<br>%{x}<extra></extra>`;
    }
    return trace;
  });
}

/**
 * Task 10: the retirement + legend-cap policy applied at render time, on
 * top of `buildMetricTraces`/`buildLayout` — the one place `ChartPanel`'s
 * three render effects (mount / metrics-changed / refresh) get their
 * traces+layout from, so the policy can't be forgotten in one of them.
 * `metrics` is the chart group's FULL ever-seen list (never trimmed — see
 * grouping.ts); this only decides what gets drawn/legended THIS render.
 */
export function buildPanelRender(
  metrics: Metric[],
  series: Record<string, Point[]>,
  selectedHost: string | null,
  events: MonitorEvent[],
  yaxisTitle: string,
): { traces: PlotlyData[]; layout: PlotlyLayout } {
  const seriesByLabel: Record<string, Point[]> = {};
  for (const m of metrics) seriesByLabel[m.label] = series[seriesKey(selectedHost, m.label)] ?? [];

  const active = retireStaleMetrics(metrics, seriesByLabel);
  const traces = buildMetricTraces(active, series, selectedHost);

  const candidates: LegendCandidate[] = active.map((m) => {
    const pts = seriesByLabel[m.label];
    return { label: m.label, value: pts.length > 0 ? pts[pts.length - 1].value : -Infinity };
  });
  const keep = selectLegendEntries(candidates, LEGEND_CAP_ENTRIES);
  const cappedTraces = traces.map((tr, i) =>
    keep.has(active[i].label) ? tr : { ...tr, showlegend: false },
  );

  const layout = buildLayout(cappedTraces, { yaxisTitle }, events);
  return { traces: cappedTraces, layout };
}

/** dashboard.js's `hexToRgba()`. */
export function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** dashboard.js's `buildShapes()`. */
export function buildShapes(events: MonitorEvent[]): Record<string, unknown>[] {
  return events.flatMap((ev): Record<string, unknown>[] => {
    if (ev.end_timestamp) {
      // Span event: borderless filled rect + two vertical edge lines (left and right only).
      const edgeLine = { color: ev.color, width: 1, dash: ev.dash };
      return [
        {
          type: "rect",
          xref: "x",
          yref: "paper",
          x0: ev.timestamp,
          x1: ev.end_timestamp,
          y0: 0,
          y1: 1,
          fillcolor: hexToRgba(ev.color, 0.12),
          line: { width: 0 },
          layer: "below",
        },
        {
          type: "line",
          xref: "x",
          yref: "paper",
          x0: ev.timestamp,
          x1: ev.timestamp,
          y0: 0,
          y1: 1,
          line: edgeLine,
          layer: "below",
        },
        {
          type: "line",
          xref: "x",
          yref: "paper",
          x0: ev.end_timestamp,
          x1: ev.end_timestamp,
          y0: 0,
          y1: 1,
          line: edgeLine,
          layer: "below",
        },
      ];
    }
    // Instantaneous event: single vertical line.
    return [
      {
        type: "line",
        xref: "x",
        yref: "paper",
        x0: ev.timestamp,
        x1: ev.timestamp,
        y0: 0,
        y1: 1,
        line: { color: ev.color, width: 1.5, dash: ev.dash },
      },
    ];
  });
}

/** dashboard.js's `buildAnnotations()`. */
export function buildAnnotations(events: MonitorEvent[]): Record<string, unknown>[] {
  return events.map((ev) => ({
    xref: "x",
    yref: "paper",
    x: ev.timestamp,
    y: 1,
    yanchor: "bottom",
    text: ev.label,
    showarrow: false,
    textangle: -45,
    font: { size: 9, color: ev.color },
  }));
}

/**
 * The live-append fast path's `extendTraces` update payload — dashboard.js
 * builds this object literal inline at the tail of `appendMetricPoint()`.
 */
export function buildExtendUpdate(msg: {
  ts: string;
  value: number;
  meta?: Record<string, unknown>;
}): Record<string, unknown[][]> {
  const update: Record<string, unknown[][]> = { x: [[msg.ts]], y: [[msg.value]] };
  if (msg.meta) update.text = [[metaText(msg.meta)]];
  return update;
}
