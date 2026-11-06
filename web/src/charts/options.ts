// Pure ECharts option builders — the plotly.ts idiom carried over: plain
// objects out, no echarts import, fully unit-testable. Dataviz mark specs
// are encoded here: 2px lines, no point symbols (hover emphasis only),
// hairline recessive grid, axis text in muted ink (never series colors),
// one y-axis per chart, crosshair tooltip on by default.
import type { EventDash } from "../api/eventStyle.gen";
import type { NormalizedSession, TimeRange } from "../data/exportDoc";
import { parseTs } from "../data/time";
import { dashType, MARKER_LINE_WIDTH, SPAN_FILL_ALPHA, withAlpha } from "./markerStyle";
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
  /** The registry's dash style (api/eventStyle.gen.ts), resolved by
   * normalizeEventStyle where the event entered the store. */
  dash: EventDash;
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
      color: ev.color,
      fromMs,
      toMs,
      dash: ev.dash,
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

/** `+`/`-` zoom buttons: new span = span×factor about the window's
 * own center — factor 0.5 zooms in, 2 zooms out. Returns null (skip) once the
 * zoomed-in span would fall below the same 1000ms floor ChartPanel's drag-zoom
 * debounce already treats as noise (MIN_ZOOM_DELTA_MS). */
export function zoomAbout(window: TimeRange, factor: number): TimeRange | null {
  const span = (window.to - window.from) * factor;
  if (span < 1000) return null; // MIN_ZOOM_DELTA_MS: below this a zoom is noise
  const center = (window.from + window.to) / 2;
  return { from: Math.round(center - span / 2), to: Math.round(center + span / 2) };
}

/** Vertical pixel step between stacked label lanes — see `layoutWith`. Also
 * the height of a label's hit target over the chart (ClusterOverflow). */
export const LANE_LABEL_STEP_PX = 14;

/** The plot's margins inside the chart container — the one source for the
 * grid. left: 88 (not the ~56 the y-axis labels alone need) reserves a gutter
 * for the per-chart +/- zoom button column (SubjectPage), which is pinned
 * top-left over this margin — the extra width keeps the buttons clear of the
 * y-axis name, tick labels, and the hover crosshair y-value label (TODO item
 * 2). ChartPanel subtracts left + right from its container width to get the
 * plot width `layoutWith` lays labels out across. */
export const GRID = { left: 88, right: 16, top: 28, bottom: 28 } as const;

/** A label's rendered pixel width at the label font (LABEL_FONT_*). */
export type MeasureText = (text: string) => number;

/** The ONE event-label font: `createTextMeasure` measures at it and every
 * markLine/markArea label draws in it. The app's Inter stack (app.css's
 * --font-sans); ECharts would otherwise default to `sans-serif`, and a width
 * measured in one face would not fit the text drawn in another. */
export const LABEL_FONT_FAMILY = '"Inter Variable", system-ui, sans-serif';
export const LABEL_FONT_SIZE = 10;

/** The measurer where no canvas exists (jsdom, and buildStackOption's default
 * before ChartPanel re-applies a measured layout): ~6 px per character. */
export const approxMeasure: MeasureText = (t) => t.length * 6;

/** Plot width buildStackOption lays labels out for when its caller has not
 * measured one; ChartPanel re-applies the measured layout on top. */
export const DEFAULT_PLOT_WIDTH_PX = 800;

/** At most this many label lanes; labels that would need a fourth are hidden
 * and counted on the lane-2 label above them. */
export const LABEL_LANES_MAX = 3;
/** Longest a label may draw; longer text is cut and ends with an ellipsis. */
export const LABEL_MAX_PX = 160;
/** Gap from the plot's top edge to lane 0's top. */
const LABEL_TOP_PX = 4;
/** Width of the surface-coloured halo drawn around label text. */
const LABEL_HALO_PX = 3;
/** Clear space kept between two labels sharing a lane. */
const LABEL_GAP_PX = 4;
const ELLIPSIS = "…";
/** The widest overflow suffix — reserved in every label's extent. */
const MAX_SUFFIX = " (+99)";
const MAX_OVERFLOW = 99;

/** Where one event's label goes. `leftPx` is relative to the plot's left
 * edge; `lane` is -1 for a label that takes no lane (an empty one). */
export interface LabelPlacement {
  id: number;
  lane: number;
  leftPx: number;
  widthPx: number;
  text: string;
  hidden: boolean;
  /** The marker ids this label's " (+N)" counts, in chronological order
   * (`labelOrder`). Empty for every label that carries no count. */
  overflowIds: number[];
}

/** A placement handed back TOGETHER WITH the marker it places. */
export interface PlacedLabel {
  marker: EventMarker;
  placement: LabelPlacement;
}

/** The one order event labels are laid out in (#920): chronological first,
 * so the earliest events keep the visible lanes whatever their text width.
 * On equal times a wire-id marker (id >= 0) goes before an id-less one
 * (eventMarkers' negative synthetics), then ids ascend, then input order. */
function labelOrder(
  a: { marker: EventMarker; index: number },
  b: { marker: EventMarker; index: number },
): number {
  const byTime = a.marker.fromMs - b.marker.fromMs;
  if (byTime !== 0) return byTime;
  const synthetic = Number(a.marker.id < 0) - Number(b.marker.id < 0);
  if (synthetic !== 0) return synthetic;
  const byId = a.marker.id - b.marker.id;
  return byId !== 0 ? byId : a.index - b.index;
}

/** Top of lane `lane`, in px below the plot's top edge. */
export function laneTopPx(lane: number): number {
  return LABEL_TOP_PX + lane * LANE_LABEL_STEP_PX;
}

/** The pixel x a marker's label is centred on, plus (for a span) the
 * clipped start its markArea polygon is drawn from — the origin a markArea
 * label position is relative to. One function so the layout and the drawing
 * never disagree about where an event sits. */
function labelAnchor(
  m: EventMarker,
  window: TimeRange,
  plotWidthPx: number,
): { anchorPx: number; spanStartPx: number } {
  const span = window.to - window.from || 1; // a zero-width window must not divide by zero
  const px = (ms: number) => ((ms - window.from) / span) * plotWidthPx;
  if (m.toMs === null) {
    const anchorPx = px(m.fromMs);
    return { anchorPx, spanStartPx: anchorPx };
  }
  const spanStartPx = px(Math.max(m.fromMs, window.from));
  const spanEndPx = px(Math.min(m.toMs, window.to));
  return { anchorPx: (spanStartPx + spanEndPx) / 2, spanStartPx };
}

/** An event label as the chart draws it: on ONE line. Every line break,
 * with the whitespace around it, becomes a single space. The layout gives
 * each label one lane of LANE_LABEL_STEP_PX, and ECharts would draw a `\n`
 * as extra lines down into the next lane. Chart labels only: the editor,
 * tooltips and export keep the event's own text. */
function chartLabelText(label: string): string {
  return label.replace(/[^\S\r\n]*(?:\r\n|\r|\n)\s*/gu, " ");
}

/** `text` cut (with an ellipsis) to at most `budgetPx` wide: the longest
 * prefix whose `prefix + "…"` fits. A binary search over the prefix length,
 * so a very long label costs about log2(length) measurements, not one per
 * character. */
function truncate(text: string, budgetPx: number, measure: MeasureText): string {
  if (measure(text) <= budgetPx) return text;
  const chars = Array.from(text); // code points, so a cut never splits a surrogate pair
  const cut = (keep: number) => chars.slice(0, keep).join("") + ELLIPSIS;
  // Invariant: a `lo`-character prefix fits (0 always counts: a bare "…"),
  // and a `hi`-character one does not (the whole text alone is too wide).
  let lo = 0;
  let hi = chars.length;
  while (hi - lo > 1) {
    const mid = (lo + hi) >>> 1;
    if (measure(cut(mid)) <= budgetPx) lo = mid;
    else hi = mid;
  }
  return cut(lo);
}

/** One annotation layout for every event label (points and spans together):
 * measured widths, kept inside the plot, at most LABEL_LANES_MAX lanes, with
 * a `(+N)` count standing in for the labels a dense cluster hides. Pure — the
 * caller supplies the plot width and the measurer. Each placement comes back
 * TOGETHER WITH its marker (the same structural pairing `withLanes` gives its
 * own callers), in input order. ChartPanel computes it once and feeds the
 * canvas (`windowPatch`), the overflow buttons and the label stamp from the
 * one result. Precondition: `markers` are already window-filtered by
 * `eventMarkers`. */
export function layoutWith(
  markers: EventMarker[],
  window: TimeRange,
  plotWidthPx: number,
  measure: MeasureText,
): PlacedLabel[] {
  const suffixPx = measure(MAX_SUFFIX);
  // Too narrow to show even "… (+99)": nothing fits, so nothing draws.
  const fits = plotWidthPx >= measure(ELLIPSIS + MAX_SUFFIX);
  const budgetPx = Math.min(LABEL_MAX_PX, plotWidthPx) - suffixPx;
  // Each label's EXTENT reserves the widest suffix up front, so appending a
  // count after lane assignment can never displace anything (one stable pass).
  const items = markers.map((marker, index) => {
    const hidden: LabelPlacement = {
      id: marker.id,
      lane: -1,
      leftPx: 0,
      widthPx: 0,
      text: "",
      hidden: true,
      overflowIds: [],
    };
    if (!fits || marker.label === "") return { marker, index, placement: hidden, extent: null };
    const text = truncate(chartLabelText(marker.label), budgetPx, measure);
    const widthPx = measure(text);
    const extentPx = widthPx + suffixPx;
    const { anchorPx } = labelAnchor(marker, window, plotWidthPx);
    // Centred on the anchor, then shifted (never re-aligned) only as far as
    // keeping the whole extent inside the plot requires.
    const leftPx = Math.min(Math.max(anchorPx - widthPx / 2, 0), plotWidthPx - extentPx);
    return {
      marker,
      index,
      placement: { ...hidden, leftPx, widthPx, text, hidden: false },
      // The gap is part of the lane interval only, never the drawn width.
      extent: { fromMs: leftPx, toMs: leftPx + extentPx + LABEL_GAP_PX },
    };
  });
  // The same greedy interval colouring spans use, run over pixel extents, but
  // placed in `labelOrder`: the earliest events take the low lanes (#920).
  const laned = withLanes(
    items.flatMap((item) => (item.extent ? [{ ...item.extent, item }] : [])),
    { order: (a, b) => labelOrder(a.item, b.item) },
  );
  for (const { event, lane } of laned) {
    event.item.placement.lane = lane;
    event.item.placement.hidden = lane >= LABEL_LANES_MAX;
  }
  // Each hidden label is counted on the visible label in the highest lane
  // whose extent overlaps it; ties go to the earliest such label. First-fit
  // guarantees one exists: a label only reaches lane LABEL_LANES_MAX when
  // every lower lane already holds an interval overlapping it.
  const visible = laned.filter((l) => l.lane < LABEL_LANES_MAX);
  const overs = laned
    .filter((l) => l.lane >= LABEL_LANES_MAX)
    .sort((a, b) => labelOrder(a.event.item, b.event.item));
  for (const over of overs) {
    let host: (typeof laned)[number] | undefined;
    for (const l of visible) {
      if (!(l.event.fromMs < over.event.toMs && over.event.fromMs < l.event.toMs)) continue;
      const better =
        host === undefined ||
        l.lane > host.lane ||
        (l.lane === host.lane && labelOrder(l.event.item, host.event.item) < 0);
      if (better) host = l;
    }
    host?.event.item.placement.overflowIds.push(over.event.item.marker.id);
  }
  for (const { event } of visible) {
    const p = event.item.placement;
    if (p.overflowIds.length === 0) continue;
    p.text = `${p.text} (+${Math.min(p.overflowIds.length, MAX_OVERFLOW)})`;
    p.widthPx = measure(p.text);
  }
  return items.map(({ marker, placement }) => ({ marker, placement }));
}

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
 * Returns lanes in the SAME order as `events` (not sorted order). Pure and
 * synchronous — no ECharts/DOM dependency — so it's unit-testable directly,
 * independent of eventOverlay's wiring below. A caller that needs the lanes
 * next to the events they belong to should call `withLanes` rather than
 * zipping this array back on by index. */
export function assignLanes(events: { fromMs: number; toMs: number }[]): number[] {
  return withLanes(events).map((laned) => laned.lane);
}

/** `assignLanes`' algorithm, handing each event back TOGETHER WITH its lane
 * instead of as a parallel array. `layoutWith` consumes this form — over
 * label pixel extents — so the label/lane pairing is structural: a
 * `lanes[i]` read alongside `labels[i]`
 * asks the reader to take the alignment on faith, and it is the alignment —
 * not the colouring — that a future edit is most likely to break silently.
 * `assignLanes` stays the exported, directly-unit-tested statement of the
 * "one lane per input, in input order" contract.
 *
 * `order` places the events in that order instead of by start (#920: label
 * lanes go to the earliest events, not the leftmost extents). First-fit is
 * the same; only the "is this lane free" test differs. In start order a
 * lane's most recently placed occupant is the only one that can still cover
 * the new start, so its end decides — today's rule, untouched. In any other
 * order a later event can fall in a GAP between two occupants, so each lane
 * keeps its occupants sorted and is binary-searched for an overlap.
 * Precondition for `order`: positive-width intervals (label extents always
 * are), which keeps each lane's ends sorted along with its starts. */
export function withLanes<T extends { fromMs: number; toMs: number }>(
  events: T[],
  opts?: { order?: (a: T, b: T) => number },
): { event: T; lane: number }[] {
  const custom = opts?.order;
  // Sort the events themselves, each carrying its original index, rather than
  // sorting a list of indices and reading back through `events[a]`: the
  // comparator then works on real objects, and the scatter write below is the
  // only place an index appears at all.
  const order = events
    .map((event, index) => ({ event, index }))
    .sort((a, b) => {
      if (custom) return custom(a.event, b.event) || a.index - b.index;
      const byStart = a.event.fromMs - b.event.fromMs;
      return byStart !== 0 ? byStart : a.event.toMs - b.event.toMs;
    });
  // lanes[lane] = that lane's occupants: in placement order by default (so
  // the last is the most recently placed), sorted by start under `order`.
  const lanes: T[][] = [];
  const laned: { event: T; lane: number }[] = new Array(events.length);
  for (const { event, index } of order) {
    // Lowest-numbered free lane, or a fresh lane if every one is occupied.
    const firstFree = lanes.findIndex((held) =>
      custom ? !overlapsAny(held, event) : !lastStillCovers(held, event),
    );
    const lane = firstFree === -1 ? lanes.length : firstFree;
    // Every index appears in `order` exactly once, so `laned` is dense on
    // return even though it starts life as a hole-filled `new Array`.
    laned[index] = { event, lane };
    const occupants = lanes[lane] ?? [];
    lanes[lane] = occupants;
    if (custom)
      occupants.splice(
        firstWhere(occupants, (o) => o.fromMs > event.fromMs),
        0,
        event,
      );
    else occupants.push(event);
  }
  return laned;
}

/** Start order's lane test: the lane's most recently placed occupant still
 * covers the event's start. `laneEnd > event.fromMs` is "still occupied",
 * kept in that form rather than `laneEnd <= event.fromMs` so that a NaN end
 * (Date.parse of a malformed wire timestamp) stays on the side it always
 * has: free. */
function lastStillCovers(occupants: { toMs: number }[], event: { fromMs: number }): boolean {
  const last = occupants.at(-1);
  return last !== undefined && last.toMs > event.fromMs;
}

/** Any-order lane test: some occupant overlaps the half-open event. In a
 * start-sorted lane of disjoint intervals the first occupant ending after the
 * event's start is the only candidate. */
function overlapsAny(
  occupants: { fromMs: number; toMs: number }[],
  event: { fromMs: number; toMs: number },
): boolean {
  const candidate = occupants[firstWhere(occupants, (o) => o.toMs > event.fromMs)];
  return candidate !== undefined && candidate.fromMs < event.toMs;
}

/** Index of the first element satisfying `pred` — which must be false, then
 * true, along `xs` — by binary search; `xs.length` when none does. */
function firstWhere<T>(xs: T[], pred: (x: T) => boolean): number {
  let lo = 0;
  let hi = xs.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const x = xs[mid];
    if (x !== undefined && pred(x)) hi = mid;
    else lo = mid + 1;
  }
  return lo;
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

/** What `eventOverlay` lays its labels out against: the visible window, the
 * plot's pixel width and the label measurer (see `layoutWith`). */
export interface OverlayLayout {
  window: TimeRange;
  plotWidthPx: number;
  measure: MeasureText;
  /** `layoutWith(events, window, plotWidthPx, measure)`, when the caller has
   * already computed it (ChartPanel shares one layout between the canvas
   * and its overflow buttons). Omitted, the overlay computes it. */
  placed?: PlacedLabel[] | undefined;
}

export function eventOverlay(
  events: EventMarker[],
  theme: Pick<ChartTheme, "muted" | "ink" | "surface">,
  layout: OverlayLayout,
): EventOverlay {
  const { window, plotWidthPx, measure } = layout;
  // Points and spans are laid out TOGETHER, so a point label and a span label
  // that would overprint take different lanes. A hidden label still draws its
  // line or area; only the text is suppressed.
  const placed = layout.placed ?? layoutWith(events, window, plotWidthPx, measure);
  // The one label text style. The halo (a surface-coloured text border) keeps
  // a label legible where its own marker line, a neighbour's line or series
  // data runs through it — labels sit inside the plot's top band.
  const font = {
    fontFamily: LABEL_FONT_FAMILY,
    fontSize: LABEL_FONT_SIZE,
    textBorderColor: theme.surface,
    textBorderWidth: LABEL_HALO_PX,
  };
  const markLine = {
    symbol: "none",
    animation: false,
    label: { color: theme.muted, ...font },
    data: placed
      .filter(({ marker }) => marker.toMs === null)
      .map(({ marker: e, placement: p }) => {
        const { anchorPx } = labelAnchor(e, window, plotWidthPx);
        return {
          xAxis: e.fromMs,
          name: e.label,
          // The marker's id, for a later click on the line to resolve the
          // event it draws (no handler reads it yet).
          eventId: e.id,
          lineStyle: { color: e.color, type: dashType(e.dash), width: MARKER_LINE_WIDTH },
          // "end" is the line's top; a negative distance pulls the label back
          // down INSIDE the plot (clear of the y-axis name above it), and the
          // offset moves its left edge from the line to the layout's leftPx.
          label: {
            show: !p.hidden,
            position: "end",
            distance: -laneTopPx(p.lane),
            verticalAlign: "top",
            align: "left",
            offset: [p.leftPx - anchorPx, 0],
            formatter: () => p.text,
            ...font,
          },
        };
      }),
  };
  // flatMap rather than filter so `toMs` is narrowed to a real number once,
  // here, for the markArea's closing coordinate.
  const spans = placed.flatMap(({ marker: e, placement }) =>
    e.toMs === null ? [] : [{ e, toMs: e.toMs, placement }],
  );
  const markArea = {
    silent: true,
    animation: false,
    data: spans.map(({ e, toMs, placement: p }) => {
      const { spanStartPx } = labelAnchor(e, window, plotWidthPx);
      return [
        {
          xAxis: e.fromMs,
          name: e.label,
          eventId: e.id, // as on a markLine datum
          // Faint fill (alpha baked into the colour, NOT itemStyle.opacity, which
          // would fade the border too) + a full-strength dashed border in the
          // event's dash style, so a span reads with the same dash as a point
          // marker (TODO item 5). borderType rings all four edges — ECharts has no
          // per-side border and the span is full-height, so the top/bottom lines
          // land on the plot edges; accepted (the chosen dash on every edge).
          itemStyle: {
            color: withAlpha(e.color, SPAN_FILL_ALPHA),
            borderColor: e.color,
            borderType: dashType(e.dash),
            borderWidth: MARKER_LINE_WIDTH,
          },
          // A coordinate-array position is relative to the area's polygon
          // origin — its CLIPPED start, at the plot top — so the layout's
          // plot-relative leftPx is rebased onto it. color: markArea labels
          // used to inherit ECharts' default label color regardless of theme —
          // illegible against a dark surface; theme.ink is the same token
          // buildStackOption's tooltip textStyle uses for dark-mode-safe text.
          label: {
            show: !p.hidden,
            position: [p.leftPx - spanStartPx, laneTopPx(p.lane)],
            align: "left",
            verticalAlign: "top",
            formatter: () => p.text,
            color: theme.ink,
            ...font,
          },
        },
        { xAxis: toMs },
      ];
    }),
  };
  return { markLine, markArea };
}

/** Cheap incremental patch for a live tick that only moved the window (a
 * DIFFERENT series ticked, but displayEndMs — and therefore liveRange — is
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
  theme: Pick<ChartTheme, "muted" | "ink" | "surface">;
  anchorSeriesId: string | null;
  plotWidthPx: number;
  measure: MeasureText;
  /** The layout already computed for these inputs (see OverlayLayout). */
  placed?: PlacedLabel[] | undefined;
}): Record<string, unknown> {
  const { window, events, theme, anchorSeriesId, plotWidthPx, measure, placed } = args;
  return {
    xAxis: { min: window.from, max: window.to },
    // Built in one expression rather than assigned onto a mutable record, so
    // the patch's whole shape is visible at a glance and the "no anchor
    // series => no series key at all" rule (a merge patch carrying
    // `series: undefined` is not the same thing) is stated once, here.
    ...(anchorSeriesId !== null
      ? {
          series: [
            {
              id: anchorSeriesId,
              ...eventOverlay(events, theme, { window, plotWidthPx, measure, placed }),
            },
          ],
        }
      : {}),
  };
}

/** Merge patch toggling this instance's y-axis pointer between the resting
 * suppressed state (`show:false`, see buildStackOption's yAxis.axisPointer)
 * and the ECharts default `"auto"` ("show when the tooltip cross asks"),
 * which restores the full x+y crosshair and the floating y-value label while
 * the cursor is inside this chart. Meant for
 * `chart.setOption(yAxisPointerPatch(...), { notMerge: false })` — ChartPanel
 * applies it on mouseenter/mouseleave and re-asserts it after every notMerge
 * rebuild (which installs a fresh model carrying the resting state). */
export function yAxisPointerPatch(hovered: boolean): Record<string, unknown> {
  return { yAxis: { axisPointer: { show: hovered ? "auto" : false } } };
}

export function buildStackOption(args: {
  unit: string;
  yTitle: string;
  series: SeriesInput[];
  window: TimeRange;
  events: EventMarker[];
  theme: ChartTheme;
  /** The plot's measured width; ChartPanel re-applies its own measured layout
   * after every full replacement, so the default only shows before that. */
  plotWidthPx?: number;
  measure?: MeasureText;
}): Record<string, unknown> {
  const { unit, yTitle, series, window, events, theme } = args;
  const plotWidthPx = args.plotWidthPx ?? DEFAULT_PLOT_WIDTH_PX;
  const measure = args.measure ?? approxMeasure;
  const { markLine, markArea } = eventOverlay(events, theme, { window, plotWidthPx, measure });
  return {
    animation: false,
    grid: { ...GRID }, // see GRID for why left is 88

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
      // Resting state of the hover-scoped y crosshair (TODO item 4): the
      // charts are echarts.connect-ed (the synced-crosshair feature), and a
      // connected group broadcasts the axisPointer to every instance — which
      // mirrored the hovered chart's y crosshair + label onto all the others
      // at a height meaningless against their y-scales. An explicit
      // show:false is the strict cure on the receiving side: ECharts'
      // collectAxesInfo (component/axisPointer/modelHelper) drops such an
      // axis from axesInfo BEFORE any trigger logic, so neither the tooltip
      // "cross" above nor a connect broadcast can ever draw a y line/label
      // here. ChartPanel flips it to "auto" (yAxisPointerPatch) on the one
      // chart under the cursor, which keeps the full cross — and the floating
      // y-value label — on exactly that chart.
      axisPointer: { show: false },
    },
    // The wheel is freed for page scroll (it used to fight it —
    // zoomOnMouseWheel/moveOnMouseWheel both false), and pan is Ctrl-drag —
    // ECharts' modifier set has no meta key, so Ctrl is the one pan gesture
    // available on every platform (documented in docs/cli/monitor/dashboard.md).
    // +/- buttons (SubjectPage) cover zoom instead of the wheel.
    //
    // `zoomLock: true` (found only by driving a REAL browser — the
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
    // actual pan mechanism — a second browser-lane finding: this dataZoom's own
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
    // A `lineX` brush select, armed by ChartPanel via takeGlobalCursor —
    // dragging across the plot draws this ghost rectangle, and brushEnd
    // routes the selected range to either a zoom-select or a sweep-to-mark,
    // depending on whether marking is armed.
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
      ...(i === 0 && (markLine.data.length > 0 || markArea.data.length > 0)
        ? { markLine, markArea }
        : {}),
    })),
  };
}
