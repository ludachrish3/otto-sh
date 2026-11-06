import { describe, expect, it, vi } from "vitest";
import { DEFAULT_EVENT_COLOR, type EventDash } from "../api/eventStyle.gen";
import { createTextMeasure } from "../charts/measure";
import {
  approxMeasure,
  assignLanes,
  buildStackOption,
  chartTheme,
  type EventMarker,
  type EventOverlay,
  eventMarkers,
  eventOverlay,
  LABEL_FONT_FAMILY,
  LABEL_FONT_SIZE,
  LABEL_LANES_MAX,
  LABEL_MAX_PX,
  type LabelPlacement,
  layoutWith,
  type MeasureText,
  windowPatch,
  withLanes,
  zoomAbout,
  zoomToRange,
} from "../charts/options";
import { MAX_SERIES_PER_CHART, SERIES_DARK, SERIES_LIGHT } from "../charts/palette";
import { normalizeEventStyle } from "../data/eventStyle";
import { parseExportDocument } from "../data/exportDoc";

const WINDOW = { from: 1_000_000, to: 2_000_000 };
const theme = chartTheme(false);
/** Layout for the pre-#863 eventOverlay cases, whose events sit in 0..5000 ms. */
const LAYOUT = { window: { from: 0, to: 5_000 }, plotWidthPx: 800, measure: approxMeasure };

/** `layoutWith`'s placements alone, in input order. */
function layoutPlacements(...args: Parameters<typeof layoutWith>): LabelPlacement[] {
  return layoutWith(...args).map((l) => l.placement);
}

/** `xs[i]`, failing the test (rather than the type check) when it is absent. */
function nth<T>(xs: readonly T[], i = 0): T {
  const x = xs[i];
  if (x === undefined) throw new Error(`no element ${i} in a list of ${xs.length}`);
  return x;
}

function series(slot: number) {
  return {
    key: `s${slot}`,
    name: `series ${slot}`,
    slot,
    points: [[1_500_000, 42]] as [number, number][],
  };
}

describe("palette", () => {
  it("has exactly 8 fixed slots per mode", () => {
    expect(SERIES_LIGHT).toHaveLength(8);
    expect(SERIES_DARK).toHaveLength(8);
    expect(MAX_SERIES_PER_CHART).toBe(8);
  });
});

describe("buildStackOption", () => {
  it("binds color to the entity slot, not the render position", () => {
    // Series 0 filtered out: series 2 must KEEP slot-2's color (never repaint
    // survivors — dataviz rule).
    const opt = buildStackOption({
      unit: "%",
      yTitle: "CPU %",
      series: [series(2), series(5)],
      window: WINDOW,
      events: [],
      theme,
    }) as { series: { itemStyle: { color: string }; lineStyle: { width: number } }[] };
    expect(opt.series[0].itemStyle.color).toBe(theme.series[2]);
    expect(opt.series[1].itemStyle.color).toBe(theme.series[5]);
    expect(opt.series[0].lineStyle.width).toBe(2);
  });

  it("pins the x axis to the window regardless of data extent", () => {
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as { xAxis: { min: number; max: number; type: string } };
    expect(opt.xAxis.type).toBe("time");
    expect(opt.xAxis.min).toBe(WINDOW.from);
    expect(opt.xAxis.max).toBe(WINDOW.to);
  });

  it("reserves a left gutter wide enough for the +/- zoom button column", () => {
    // The zoom column (SubjectPage) is pinned top-LEFT over grid.left. A bare
    // y-axis needs only ~56px; grid.left is widened past that so the buttons
    // (right edge ~32px in) clear the y-axis labels/name/hover crosshair label
    // that live against the axis line (TODO item 2). Regressing grid.left back
    // toward ~56 would put the buttons over the labels again.
    const opt = buildStackOption({
      unit: "%",
      yTitle: "CPU %",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as { grid: { left: number } };
    expect(opt.grid.left).toBeGreaterThanOrEqual(80);
  });

  it("keeps the cross axisPointer but parks the y-axis pointer off at rest", () => {
    // The charts ARE echarts.connect-ed (synced crosshair + tooltips across the
    // stack — the feature). "cross" keeps the y crosshair + floating y label on
    // the hovered chart. yAxis.axisPointer.show:false is the strict half:
    // ECharts' collectAxesInfo (axisPointer/modelHelper) drops an axis with an
    // explicit show:false from axesInfo BEFORE any trigger logic, so neither
    // the tooltip cross nor a connect broadcast can ever draw a y line/label on
    // a chart at rest — that's what mirrored the hovered chart's y label onto
    // every other chart at a meaningless height (TODO item 4). ChartPanel flips
    // show back to "auto" on the ONE chart under the cursor (mouseenter), so
    // hovering still gets the full cross there.
    const opt = buildStackOption({
      unit: "%",
      yTitle: "CPU %",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as {
      tooltip: { axisPointer: { type: string } };
      yAxis: { axisPointer: { show: boolean } };
    };
    expect(opt.tooltip.axisPointer.type).toBe("cross");
    expect(opt.yAxis.axisPointer.show).toBe(false);
  });

  it("attaches event markers to the first series only", () => {
    const events = [
      {
        id: 1,
        label: "point",
        color: "#7c5cff",
        fromMs: 1_200_000,
        toMs: null,
        dash: "dash" as const,
      },
      {
        id: 2,
        label: "span",
        color: "#ff6b6b",
        fromMs: 1_300_000,
        toMs: 1_400_000,
        dash: "dash" as const,
      },
    ];
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0), series(1)],
      window: WINDOW,
      events,
      theme,
      // Named rather than `Record<string, unknown>[]`: the two keys this test
      // is about are now spelled once, in the cast, so a typo below is a
      // compile error instead of a silently-undefined assertion. Same idiom
      // as the tooltip/yAxis cast above.
    }) as { series: { markLine?: unknown; markArea?: unknown }[] };
    expect(opt.series[0].markLine).toBeDefined();
    expect(opt.series[0].markArea).toBeDefined();
    expect(opt.series[1].markLine).toBeUndefined();
    const line = opt.series[0].markLine as { data: { xAxis: number }[] };
    expect(line.data[0].xAxis).toBe(1_200_000);
    const area = opt.series[0].markArea as { data: [{ xAxis: number }, { xAxis: number }][] };
    expect(area.data[0][0].xAxis).toBe(1_300_000);
  });

  it("uses text tokens for axis labels, never series colors", () => {
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as { xAxis: { axisLabel: { color: string } } };
    expect(opt.xAxis.axisLabel.color).toBe(theme.muted);
    expect(SERIES_LIGHT).not.toContain(opt.xAxis.axisLabel.color);
  });

  // Task 11 (Monitor Plan 5c): the wheel used to be captured for zoom (the
  // TODO complaint — it fought the page's own scroll); ECharts' modifier set
  // has no meta key, so Ctrl-drag is the one pan gesture available on every
  // platform (documented in Task 14). `zoomLock: true` (Task 13 review
  // fix) is load-bearing, not cosmetic: without it, ECharts' RoamController
  // installs its mousewheel listener with a HARDCODED zoomOnMouseWheel:true
  // at the roam-controller level regardless of this model's own `false`
  // above, and the wheel never reaches the browser's native scroll (see
  // ChartPanel.tsx/options.ts's comments for the full story) — a real
  // regression the vitest mocked-ECharts suite otherwise can't see at all,
  // so it must at least fail loud here if the flag is ever removed.
  it("frees the wheel for page scroll and pans only via Ctrl-drag", () => {
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as { dataZoom: Record<string, unknown>[] };
    expect(opt.dataZoom[0]).toMatchObject({
      type: "inside",
      filterMode: "none",
      zoomOnMouseWheel: false,
      moveOnMouseMove: "ctrl",
      moveOnMouseWheel: false,
      zoomLock: true,
    });
  });

  // Task 12 (Monitor Plan 5c): brush-based drag zoom-select + sweep-to-mark.
  // outOfBrush.colorAlpha must stay 1 — brushing here SELECTS a range for
  // zoom or marking, it never filters/dims the series it crosses.
  it("configures a lineX-ready brush that never dims the series it crosses", () => {
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0)],
      window: WINDOW,
      events: [],
      theme,
    }) as { brush: { xAxisIndex: number; outOfBrush: { colorAlpha: number } } };
    expect(opt.brush.xAxisIndex).toBe(0);
    expect(opt.brush.outOfBrush.colorAlpha).toBe(1);
  });
});

describe("zoomAbout (Task 11 +/- zoom buttons)", () => {
  it("halves the span about the window's center", () => {
    expect(zoomAbout({ from: 0, to: 10_000 }, 0.5)).toEqual({ from: 2500, to: 7500 });
  });

  it("doubles the span about the window's center", () => {
    expect(zoomAbout({ from: 0, to: 10_000 }, 2)).toEqual({ from: -5000, to: 15_000 });
  });

  it("returns null when the zoomed-in span would fall below the 1000ms floor", () => {
    expect(zoomAbout({ from: 0, to: 1500 }, 0.5)).toBeNull();
  });
});

describe("eventMarkers", () => {
  it("filters to window overlap and converts to ms", () => {
    const rows = [
      { id: 1, timestamp: "1970-01-01T00:20:00Z", label: "in", color: "#111111" },
      { id: 2, timestamp: "1970-01-01T02:00:00Z", label: "out", color: "#222222" },
      {
        id: 3,
        timestamp: "1970-01-01T00:10:00Z",
        end_timestamp: "1970-01-01T00:25:00Z",
        label: "span-straddles",
        color: "#333333",
      },
    ];
    const marks = eventMarkers(rows.map(normalizeEventStyle), WINDOW); // 1_000_000..2_000_000 ms
    expect(marks.map((m) => m.id)).toEqual([1, 3]);
    expect(marks[1].toMs).toBe(1_500_000);
  });

  it("carries the dash style, defaulting to 'dash' when the row omits it", () => {
    const rows = [
      { id: 1, timestamp: "1970-01-01T00:20:00Z", label: "dotted", color: "#111111", dash: "dot" },
      { id: 2, timestamp: "1970-01-01T00:21:00Z", label: "default", color: "#222222" },
    ];
    const marks = eventMarkers(rows.map(normalizeEventStyle), WINDOW);
    expect(marks[0].dash).toBe("dot");
    expect(marks[1].dash).toBe("dash");
  });

  it("assigns negative synthetic ids to id-less rows to avoid collision with real ids", () => {
    // Collision scenario: real row with id=1, and an id-less row that becomes second in output
    // (old fallback would assign out.length=1, colliding with the real id).
    const rows = [
      { id: 1, timestamp: "1970-01-01T00:20:00Z", label: "real-id-1", color: "#111111" },
      { id: null, timestamp: "1970-01-01T00:21:00Z", label: "id-less-second", color: "#222222" },
    ];
    const marks = eventMarkers(rows.map(normalizeEventStyle), WINDOW);
    const ids = marks.map((m) => m.id);
    // All ids must be unique
    expect(new Set(ids).size).toBe(ids.length);
    // Synthetic id must be negative (never collides with real non-negative ids)
    expect(ids).toContain(1); // real id
    expect(ids.some((id) => id < 0)).toBe(true); // synthetic is negative
  });
});

describe("legacy colour is preserved and drawn as the default (#857)", () => {
  it("a JSON import of a named-colour span draws a translucent default fill", () => {
    const { sessions } = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "old",
            start: "1970-01-01T00:00:00Z",
            events: [
              {
                id: 1,
                timestamp: "1970-01-01T00:00:01Z",
                end_timestamp: "1970-01-01T00:00:03Z",
                label: "legacy",
                color: "red",
              },
            ],
          },
        ],
      }),
    );
    const [event] = nth(sessions).events;
    expect(event?.color).toBe(DEFAULT_EVENT_COLOR);
    expect(event?.legacyColor).toBe("red");
    const markers = eventMarkers(nth(sessions).events, LAYOUT.window);
    const { markArea } = eventOverlay(
      markers,
      { muted: "#999", ink: "#eee", surface: "#fff" },
      LAYOUT,
    ) as {
      markArea: { data: [{ itemStyle: { color: string; borderColor: string } }, unknown][] };
    };
    // withAlpha(DEFAULT_EVENT_COLOR, 0.12): the faint fill, not an opaque block.
    const alpha = Math.round(0.12 * 255)
      .toString(16)
      .padStart(2, "0");
    expect(nth(markArea.data)[0].itemStyle.color).toBe(`${DEFAULT_EVENT_COLOR}${alpha}`);
    expect(nth(markArea.data)[0].itemStyle.borderColor).toBe(DEFAULT_EVENT_COLOR);
  });
});

describe("zoomToRange", () => {
  it("maps percentages onto the window", () => {
    expect(zoomToRange(25, 75, WINDOW)).toEqual({ from: 1_250_000, to: 1_750_000 });
  });
});

// Follow-up: two overlapping span events (kitchen-sink.json's "stress run"
// 09:25-09:35 and "log capture" 09:30-09:40) used to draw their markArea
// labels at the SAME default position — ECharts does not lay markArea
// labels out to avoid each other — so the glyphs overlapped into unreadable
// mush, in both themes, on a chart neither this branch nor its predecessor
// touched. `assignLanes` is the pure, directly-testable core of the fix: a
// greedy interval-graph colouring that gives overlapping events distinct
// lanes and leaves the non-overlapping common case alone.
describe("assignLanes", () => {
  it("non-overlapping events all land in lane 0 — the common case is unchanged", () => {
    const events = [
      { fromMs: 0, toMs: 10 },
      { fromMs: 20, toMs: 30 },
      { fromMs: 40, toMs: 50 },
    ];
    expect(assignLanes(events)).toEqual([0, 0, 0]);
  });

  it("two overlapping events get distinct lanes", () => {
    const events = [
      { fromMs: 0, toMs: 10 },
      { fromMs: 5, toMs: 15 },
    ];
    const lanes = assignLanes(events);
    expect(lanes[0]).not.toBe(lanes[1]);
    expect(new Set(lanes).size).toBe(2);
  });

  it("three mutually overlapping events get three distinct lanes", () => {
    const events = [
      { fromMs: 0, toMs: 30 },
      { fromMs: 5, toMs: 25 },
      { fromMs: 10, toMs: 20 },
    ];
    const lanes = assignLanes(events);
    expect(new Set(lanes).size).toBe(3);
  });

  it("half-open intervals: an event starting exactly when another ends does NOT overlap it, and reuses the lane", () => {
    const events = [
      { fromMs: 0, toMs: 10 },
      { fromMs: 10, toMs: 20 }, // starts exactly at the first one's end
    ];
    expect(assignLanes(events)).toEqual([0, 0]);
  });

  it("kitchen-sink.json's own overlap: stress run (09:25-09:35) vs log capture (09:30-09:40)", () => {
    const stressRun = {
      fromMs: Date.parse("2026-07-01T09:25:00Z"),
      toMs: Date.parse("2026-07-01T09:35:00Z"),
    };
    const logCapture = {
      fromMs: Date.parse("2026-07-01T09:30:00Z"),
      toMs: Date.parse("2026-07-01T09:40:00Z"),
    };
    const lanes = assignLanes([stressRun, logCapture]);
    expect(lanes[0]).not.toBe(lanes[1]);
  });

  it("returns lanes in the SAME order as the input, not sorted order", () => {
    // Input is start-descending; assignLanes sorts internally by start but
    // must hand back lanes zipped to the ORIGINAL index order. An
    // overlapping pair (rather than a non-overlapping one, which would
    // trivially land both in lane 0 either way) is what actually detects an
    // index-vs-sort-order swap.
    const events = [
      { fromMs: 50, toMs: 60 }, // index 0: starts SECOND chronologically
      { fromMs: 0, toMs: 55 }, // index 1: starts FIRST, overlaps index 0
    ];
    const lanes = assignLanes(events);
    // index 1 (the earlier start) is the one that claims lane 0 first;
    // index 0 (later start, overlapping) is pushed to a new lane.
    expect(lanes[1]).toBe(0);
    expect(lanes[0]).toBe(1);
  });

  it("empty input returns an empty array", () => {
    expect(assignLanes([])).toEqual([]);
  });

  // #920: the `order` option must leave the default path alone. The expected
  // lanes were taken from `main`'s output for this fixture (ties, a
  // zero-width interval and NaN bounds included) before `order` existed.
  it("withLanes default order is unchanged", () => {
    const fixture = [
      { fromMs: 30, toMs: 70 },
      { fromMs: 0, toMs: 40 },
      { fromMs: 10, toMs: 20 },
      { fromMs: 20, toMs: 50 },
      { fromMs: 40, toMs: 40 },
      { fromMs: 40, toMs: 90 },
      { fromMs: 0, toMs: 40 },
      { fromMs: 55, toMs: 60 },
      { fromMs: 70, toMs: 80 },
      { fromMs: Number.NaN, toMs: 5 },
      { fromMs: 60, toMs: Number.NaN },
      { fromMs: 65, toMs: 66 },
    ];
    expect(assignLanes(fixture)).toEqual([3, 0, 2, 2, 0, 0, 1, 1, 1, 0, 1, 0]);
  });

  it("out-of-start-order non-overlapping intervals share a lane", () => {
    // Placed in `rank` order: [0,10) and [20,30) first, both in lane 0. Then
    // [10,20) fits BETWEEN them. Lane 0's last end (30) is past its start, so
    // a lane-end check would push it to lane 1; only the lane's interval
    // list shows the gap.
    const items = [
      { fromMs: 0, toMs: 10, rank: 0 },
      { fromMs: 10, toMs: 20, rank: 2 },
      { fromMs: 20, toMs: 30, rank: 1 },
    ];
    const laned = withLanes(items, { order: (a, b) => a.rank - b.rank });
    expect(laned.map((l) => l.lane)).toEqual([0, 0, 0]);
    expect(laned.map((l) => l.event)).toEqual(items); // input order, paired
    // A real overlap still takes a new lane under an order.
    const overlapping = [...items, { fromMs: 15, toMs: 25, rank: 3 }];
    expect(withLanes(overlapping, { order: (a, b) => a.rank - b.rank }).map((l) => l.lane)).toEqual(
      [0, 0, 0, 1],
    );
  });
});

describe("eventOverlay markArea lanes", () => {
  const overlayTheme = { muted: "#999", ink: "#eee", surface: "#fff" };

  it("gives two overlapping span events distinct label positions", () => {
    const events = [
      {
        id: 1,
        label: "stress run",
        color: "#ff6b6b",
        fromMs: 1_000,
        toMs: 2_000,
        dash: "dash" as const,
      },
      {
        id: 2,
        label: "log capture",
        color: "#2f9e6e",
        fromMs: 1_500,
        toMs: 2_500,
        dash: "dash" as const,
      },
    ];
    const { markArea } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markArea: { data: [{ label: { position: unknown[] } }, unknown][] };
    };
    const pos0 = markArea.data[0][0].label.position;
    const pos1 = markArea.data[1][0].label.position;
    expect(pos0).not.toEqual(pos1); // stacked, not colliding
  });

  it("non-overlapping span events keep the same (lane-0) label position", () => {
    const events = [
      { id: 1, label: "a", color: "#ff6b6b", fromMs: 1_000, toMs: 2_000, dash: "dash" as const },
      { id: 2, label: "b", color: "#2f9e6e", fromMs: 3_000, toMs: 4_000, dash: "dash" as const },
    ];
    const { markArea } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markArea: { data: [{ label: { position: unknown[] } }, unknown][] };
    };
    const pos0 = markArea.data[0][0].label.position;
    const pos1 = markArea.data[1][0].label.position;
    // Both lane 0 — the common case is unchanged. x is relative to each
    // span's own start, so y (lane 0's top, 4 px) is what pins the lane.
    expect(pos0?.[1]).toBe(4);
    expect(pos1?.[1]).toBe(4);
  });

  it("keeps the label text and draws the fill in the event colour (now alpha-faded)", () => {
    // The lane fix left the label untouched; TODO item 5 later faded the fill
    // by baking alpha into the colour (so the dashed border can stay opaque),
    // so the fill CONTAINS the event colour rather than equalling it.
    const events = [
      {
        id: 1,
        label: "stress run",
        color: "#ff6b6b",
        fromMs: 1_000,
        toMs: 2_000,
        dash: "dash" as const,
      },
      {
        id: 2,
        label: "log capture",
        color: "#2f9e6e",
        fromMs: 1_500,
        toMs: 2_500,
        dash: "dash" as const,
      },
    ];
    const { markArea } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markArea: { data: [{ name: string; itemStyle: { color: string } }, unknown][] };
    };
    expect(markArea.data[0][0].name).toBe("stress run");
    expect(markArea.data[0][0].itemStyle.color).toContain("#ff6b6b");
    expect(markArea.data[1][0].name).toBe("log capture");
    expect(markArea.data[1][0].itemStyle.color).toContain("#2f9e6e");
  });

  it("leaves markLine (instant-event) behaviour untouched", () => {
    const events = [
      {
        id: 1,
        label: "config reload",
        color: "#7c5cff",
        fromMs: 1_000,
        toMs: null,
        dash: "dash" as const,
      },
    ];
    const { markLine } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markLine: { data: { xAxis: number; name: string }[] };
    };
    expect(markLine.data).toHaveLength(1);
    expect(markLine.data[0].xAxis).toBe(1_000);
  });
});

// Task 11 (Monitor Plan 5c) dark-mode regression pin: today's markArea label
// inherits ECharts' default label color regardless of theme — illegible
// against a dark surface. This must fail against the CURRENT eventOverlay
// (no `color` key on the label at all) before the fix lands.
describe("eventOverlay markArea label color (Task 11 dark-mode fix)", () => {
  it("colors the label with theme.ink so it reads against dark surfaces", () => {
    const events = [
      {
        id: 1,
        label: "stress run",
        color: "#ff6b6b",
        fromMs: 1_000,
        toMs: 2_000,
        dash: "dash" as const,
      },
    ];
    const overlayTheme = { muted: "#999", ink: "#f3f4f6", surface: "#fff" };
    const { markArea } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markArea: { data: [{ label: { color: string; fontSize: number } }, unknown][] };
    };
    expect(markArea.data[0][0].label.color).toBe(overlayTheme.ink);
    expect(markArea.data[0][0].label.fontSize).toBe(10);
  });
});

// TODO item 5: the event's dash style round-trips through the editor + API but
// the chart ignored it — markLines were always "dashed" and spans had no
// border at all. Both now honor the dash (spans on all four edges).
describe("eventOverlay dash styling (TODO item 5)", () => {
  const overlayTheme = { muted: "#999", ink: "#f3f4f6", surface: "#fff" };

  it("maps a point event's dash onto the markLine's lineStyle type", () => {
    const events = [
      { id: 1, label: "reload", color: "#7c5cff", fromMs: 1_000, toMs: null, dash: "dot" as const },
    ];
    const { markLine } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markLine: { data: { lineStyle: { type: unknown } }[] };
    };
    expect(markLine.data[0].lineStyle.type).toBe("dotted");
  });

  it("draws the span border in the event's dash type and colour on all four edges", () => {
    const events = [
      {
        id: 1,
        label: "stress",
        color: "#ff6b6b",
        fromMs: 1_000,
        toMs: 2_000,
        dash: "solid" as const,
      },
    ];
    const { markArea } = eventOverlay(events, overlayTheme, LAYOUT) as {
      markArea: {
        data: [
          { itemStyle: { borderType: unknown; borderColor: string; borderWidth: number } },
          unknown,
        ][];
      };
    };
    expect(markArea.data[0][0].itemStyle.borderType).toBe("solid");
    expect(markArea.data[0][0].itemStyle.borderColor).toBe("#ff6b6b");
    expect(markArea.data[0][0].itemStyle.borderWidth).toBeGreaterThan(0);
  });

  it("maps the non-named plotly dash styles to numeric ECharts dash patterns", () => {
    const span = (dash: EventDash) =>
      eventOverlay(
        [{ id: 1, label: "s", color: "#fff", fromMs: 1_000, toMs: 2_000, dash }],
        overlayTheme,
        LAYOUT,
      ).markArea as { data: [{ itemStyle: { borderType: unknown } }, unknown][] };
    expect(span("longdash").data[0][0].itemStyle.borderType).toBeInstanceOf(Array);
    expect(span("dashdot").data[0][0].itemStyle.borderType).toBeInstanceOf(Array);
  });
});

// #863: one annotation layout for every event label — measured widths, edge
// clamping, a bounded lane count and an overflow count. `layoutWith` is the
// pure core; these cases pin its invariants, not its mechanics.
describe("layoutWith (#863)", () => {
  const WIN15 = { from: 0, to: 15 * 60_000 };
  const measure6: MeasureText = (t) => t.length * 6;
  const point = (id: number, fromMs: number, label = `event ${id}`): EventMarker => ({
    id,
    label,
    color: "#7c5cff",
    fromMs,
    toMs: null,
    dash: "dash" as const,
  });
  /** The window instant at pixel `px` of a 1000 px plot. */
  const at = (px: number) => (px / 1000) * WIN15.to;

  /** Invariants 1-3: inside the plot, no same-lane overlap, at most 3 lanes. */
  function expectInvariants(placements: LabelPlacement[], plotWidthPx: number) {
    const visible = placements.filter((p) => !p.hidden);
    for (const p of visible) {
      expect(p.leftPx, `"${p.text}" left edge`).toBeGreaterThanOrEqual(0);
      expect(p.leftPx + p.widthPx, `"${p.text}" right edge`).toBeLessThanOrEqual(plotWidthPx);
      expect(p.lane).toBeGreaterThanOrEqual(0);
      expect(p.lane).toBeLessThan(LABEL_LANES_MAX);
    }
    for (const a of visible) {
      for (const b of visible) {
        if (a === b || a.lane !== b.lane) continue;
        const overlap = a.leftPx < b.leftPx + b.widthPx && b.leftPx < a.leftPx + a.widthPx;
        expect(overlap, `"${a.text}" and "${b.text}" overprint in lane ${a.lane}`).toBe(false);
      }
    }
  }

  it("three points two seconds apart take three lanes", () => {
    const markers = [point(1, 60_000), point(2, 62_000), point(3, 64_000)];
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    expect(new Set(placements.map((p) => p.lane))).toEqual(new Set([0, 1, 2]));
    expect(placements.every((p) => !p.hidden)).toBe(true);
    expect(placements.map((p) => p.id)).toEqual([1, 2, 3]); // input order
  });

  it("five coincident points: three visible, one carries (+2)", () => {
    const markers = [1, 2, 3, 4, 5].map((id) => point(id, 300_000));
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    expect(placements.filter((p) => !p.hidden)).toHaveLength(3);
    expect(placements.filter((p) => p.hidden)).toHaveLength(2);
    const counted = placements.filter((p) => p.text.endsWith(" (+2)"));
    expect(counted).toHaveLength(1);
    expect(nth(counted).lane).toBe(2);
    expect(nth(counted).hidden).toBe(false);
  });

  // #920: which labels stay visible is decided by time, never by text width.
  it("five coincident points keep the chronologically first three visible, regardless of width", () => {
    const t0 = 300_000; // the cluster's own t=0; 4 ms is far below one pixel at 800 px
    const labels = ["test start", "collect logs and more", "test pass", "test fail", "teardown"];
    const markers = labels.map((label, i) => point(i + 1, t0 + i, label));
    const placements = layoutPlacements(markers, WIN15, 800, measure6);
    expectInvariants(placements, 800);
    expect(placements.filter((p) => !p.hidden).map((p) => p.id)).toEqual([1, 2, 3]);
    // Lanes follow time too: the widest label ("collect logs and more") does
    // not take lane 0.
    expect(placements.map((p) => p.lane)).toEqual([0, 1, 2, 3, 4]);
    const host = placements.filter((p) => p.overflowIds.length > 0);
    expect(host).toHaveLength(1);
    expect(nth(host).text.endsWith(" (+2)")).toBe(true);
    expect(nth(host).overflowIds).toEqual([4, 5]);
    // Every other placement counts nothing.
    expect(placements.filter((p) => p !== nth(host)).every((p) => p.overflowIds.length === 0)).toBe(
      true,
    );
  });

  it("the same cluster in shuffled input order lays out the same", () => {
    const t0 = 300_000;
    const labels = ["test start", "collect logs and more", "test pass", "test fail", "teardown"];
    const byId = labels.map((label, i) => point(i + 1, t0 + i, label));
    const markers = [5, 3, 1, 4, 2].map((id) => nth(byId, id - 1));
    const placements = layoutPlacements(markers, WIN15, 800, measure6);
    expectInvariants(placements, 800);
    expect(
      placements
        .filter((p) => !p.hidden)
        .map((p) => p.id)
        .sort((a, b) => a - b),
    ).toEqual([1, 2, 3]);
    const host = placements.filter((p) => p.overflowIds.length > 0);
    expect(host.map((p) => p.id)).toEqual([3]);
    expect(nth(host).overflowIds).toEqual([4, 5]); // chronological, not input order
  });

  it("a hidden label overlapping two lane-2 labels counts on the earliest", () => {
    // Two three-point clusters at x = 300 and x = 400, and one wide label at
    // x = 370 whose extent spans both. It falls after the first cluster in
    // time, so lanes 0-2 are taken there and it is hidden; the second
    // cluster then fills lanes 0-2 beside it. Both lane-2 labels overlap it.
    const late = [21, 22, 23].map((id) => point(id, at(400), "evt"));
    const early = [11, 12, 13].map((id) => point(id, at(300), "evt"));
    const wide = point(30, at(370), "x".repeat(20));
    // The later cluster comes first in input, so input order would pick it.
    const placements = layoutPlacements([...late, wide, ...early], WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    const byId = new Map(placements.map((p) => [p.id, p]));
    expect(byId.get(30)?.hidden).toBe(true);
    expect(byId.get(13)?.lane).toBe(2);
    expect(byId.get(23)?.lane).toBe(2);
    expect(byId.get(13)?.overflowIds).toEqual([30]);
    expect(byId.get(23)?.overflowIds).toEqual([]);
  });

  it("equal times tie-break by id", () => {
    const markers = [
      point(9, 300_000, "same width"),
      point(3, 300_000, "same width"),
      ...[20, 21, 22].map((id, i) => point(id, 300_001 + i, "same width")),
    ];
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    const byId = new Map(placements.map((p) => [p.id, p]));
    expect(byId.get(3)?.lane).toBe(0);
    expect(byId.get(9)?.lane).toBe(1);
    expect(byId.get(20)?.lane).toBe(2);
    expect(byId.get(20)?.overflowIds).toEqual([21, 22]);
  });

  it("id-less markers are listed but not editable", () => {
    const rows = (first: string | null) => [
      // An id-less row first in input, tied in time with a wire-id row.
      // Its label is the wider one, so a width-led layout would put it first.
      {
        id: null,
        timestamp: "1970-01-01T00:05:00Z",
        label: "no id at all",
        color: "#888888",
        dash: "dash" as const,
      },
      {
        id: 7,
        timestamp: "1970-01-01T00:05:00Z",
        label: "wire id",
        color: "#888888",
        dash: "dash" as const,
      },
      ...(first === null
        ? []
        : [
            {
              id: null,
              timestamp: first,
              label: "early no id",
              color: "#888888",
              dash: "dash" as const,
            },
          ]),
    ];
    const tied = eventMarkers(rows(null), WIN15);
    const synthetic = nth(tied.filter((m) => m.id < 0));
    const tiedLayout = layoutPlacements(tied, WIN15, 1000, measure6);
    const lane = (ps: LabelPlacement[], id: number) => ps.find((p) => p.id === id)?.lane;
    expect(lane(tiedLayout, 7)).toBe(0); // the wire id wins the tie
    expect(lane(tiedLayout, synthetic.id)).toBe(1);
    // Earliest in time, an id-less marker goes first: the order is chronological.
    const early = eventMarkers(rows("1970-01-01T00:04:59.999Z"), WIN15);
    const earliest = nth(early.filter((m) => m.label === "early no id"));
    expect(earliest.id).toBeLessThan(0);
    expect(lane(layoutPlacements(early, WIN15, 1000, measure6), earliest.id)).toBe(0);
  });

  it("labels near both edges stay inside the plot", () => {
    const markers = [point(1, WIN15.from, "left edge"), point(2, WIN15.to - 1000, "right edge")];
    const wide = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(wide, 1000);
    expect(wide.every((p) => !p.hidden)).toBe(true);
    // A 30-character label on a 120 px plot, anchored at x = 10.
    const narrow = layoutPlacements(
      [point(3, (10 / 120) * WIN15.to, "x".repeat(30))],
      WIN15,
      120,
      measure6,
    );
    expectInvariants(narrow, 120);
    expect(nth(narrow).hidden).toBe(false);
    expect(nth(narrow).text.endsWith("…")).toBe(true);
  });

  it("centres a label on its anchor when it fits", () => {
    const p = nth(layoutPlacements([point(1, at(500), "deploy")], WIN15, 1000, measure6));
    expect(p.leftPx + p.widthPx / 2).toBe(500);
  });

  it("wide glyphs are measured, not counted", () => {
    const measure14: MeasureText = (t) => t.length * 14;
    const label = "部署完成部署完成部署完成";
    const p = nth(layoutPlacements([point(1, at(500), label)], WIN15, 1000, measure14));
    expectInvariants([p], 1000);
    expect(p.widthPx).toBe(measure14(p.text));
    expect(p.widthPx).toBeLessThanOrEqual(LABEL_MAX_PX);
    expect(p.text.endsWith("…")).toBe(true);
  });

  it("long labels truncate to LABEL_MAX_PX with an ellipsis", () => {
    const p = nth(layoutPlacements([point(1, at(500), "a".repeat(80))], WIN15, 1000, measure6));
    expectInvariants([p], 1000);
    expect(p.text.endsWith("…")).toBe(true);
    expect(p.widthPx).toBe(measure6(p.text));
    expect(p.widthPx).toBeLessThanOrEqual(LABEL_MAX_PX);
    // A short label is left whole.
    const q = nth(layoutPlacements([point(2, at(500), "deploy")], WIN15, 1000, measure6));
    expect(q.text).toBe("deploy");
  });

  it("the overflow suffix is placed with its own width", () => {
    const markers = [1, 2, 3, 4, 5, 6].map((id) =>
      point(id, WIN15.to - 2000, `${"r".repeat(18)}${id}${id}`),
    );
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    const counted = placements.filter((p) => p.text.endsWith(" (+3)"));
    expect(counted).toHaveLength(1);
    expect(nth(counted).widthPx).toBe(measure6(nth(counted).text));
  });

  it("caps the overflow count at +99", () => {
    const markers = Array.from({ length: 110 }, (_, i) => point(i + 1, at(500), "evt"));
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    expect(placements.filter((p) => p.text.endsWith(" (+99)"))).toHaveLength(1);
  });

  it("adjacent overflow clusters count separately", () => {
    const markers = [
      ...[1, 2, 3, 4, 5].map((id) => point(id, at(300), "evt")),
      ...[6, 7, 8, 9, 10].map((id) => point(id, at(500), "evt")),
    ];
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    const counted = placements.filter((p) => p.text.endsWith(" (+2)"));
    expect(counted).toHaveLength(2);
    expect(counted.some((p) => p.id <= 5)).toBe(true);
    expect(counted.some((p) => p.id > 5)).toBe(true);
  });

  it("a plot too narrow for an ellipsis and a count hides every label", () => {
    const placements = layoutPlacements([point(1, at(500), "abc")], WIN15, 30, measure6);
    expect(placements.every((p) => p.hidden)).toBe(true);
  });

  it("empty label takes no lane", () => {
    const markers = [point(1, at(300), ""), point(2, at(300), "real"), point(3, at(300), "too")];
    const placements = layoutPlacements(markers, WIN15, 1000, measure6);
    expectInvariants(placements, 1000);
    expect(nth(placements, 0).hidden).toBe(true);
    expect(
      placements
        .filter((p) => !p.hidden)
        .map((p) => p.lane)
        .sort((a, b) => a - b),
    ).toEqual([0, 1]);
  });

  it("a span and a point whose labels overlap take different lanes", () => {
    const span: EventMarker = {
      id: 1,
      label: "soak",
      color: "#ff6b6b",
      fromMs: at(260),
      toMs: at(340),
      dash: "dash" as const,
    };
    const placements = layoutPlacements(
      [span, point(2, at(300), "checkpoint")],
      WIN15,
      1000,
      measure6,
    );
    expectInvariants(placements, 1000);
    expect(nth(placements, 0).lane).not.toBe(nth(placements, 1).lane);
  });

  it("eventOverlay carries placements", () => {
    const markers = [1, 2, 3, 4].map((id) => point(id, at(300)));
    const layout = { window: WIN15, plotWidthPx: 1000, measure: measure6 };
    const overlay = eventOverlay(markers, theme, layout);
    const data = overlay.markLine.data as { label: { show: boolean } }[];
    expect(data).toHaveLength(4); // a hidden label's line still draws
    expect(data.filter((d) => d.label.show === false)).toHaveLength(1);
    const patch = windowPatch({
      window: WIN15,
      events: markers,
      theme,
      anchorSeriesId: "s0",
      plotWidthPx: 1000,
      measure: measure6,
    }) as { series: EventOverlay[] };
    const built = buildStackOption({
      unit: "%",
      yTitle: "CPU %",
      series: [series(0)],
      window: WIN15,
      events: markers,
      theme,
      plotWidthPx: 1000,
      measure: measure6,
    }) as { series: EventOverlay[] };
    // JSON drops the formatter closures; compare their output separately.
    const texts = (o: EventOverlay) =>
      (o.markLine.data as { label: { formatter: () => string } }[]).map((d) => d.label.formatter());
    expect(JSON.stringify(nth(patch.series).markLine)).toBe(
      JSON.stringify(nth(built.series).markLine),
    );
    expect(JSON.stringify(nth(patch.series).markArea)).toBe(
      JSON.stringify(nth(built.series).markArea),
    );
    expect(texts(nth(patch.series))).toEqual(texts(nth(built.series)));
    expect(texts(nth(built.series)).filter((t) => t.endsWith(" (+1)"))).toHaveLength(1);
  });

  it("markLine and markArea data carry the marker's eventId", () => {
    const span: EventMarker = {
      id: 8,
      label: "soak",
      color: "#f00",
      fromMs: at(100),
      toMs: at(200),
      dash: "dash" as const,
    };
    const layout = { window: WIN15, plotWidthPx: 1000, measure: measure6 };
    const overlay = eventOverlay([point(4, at(500)), span], theme, layout);
    expect((overlay.markLine.data as { eventId: number }[]).map((d) => d.eventId)).toEqual([4]);
    expect(nth(overlay.markArea.data as [{ eventId: number }, unknown][])[0].eventId).toBe(8);
  });

  it("draws a point label at its layout offset, inside the plot top", () => {
    const layout = { window: WIN15, plotWidthPx: 1000, measure: measure6 };
    const p = nth(layoutPlacements([point(1, at(995), "right edge")], WIN15, 1000, measure6));
    const d = nth(
      eventOverlay([point(1, at(995), "right edge")], theme, layout).markLine.data as {
        label: Record<string, unknown>;
      }[],
    );
    expect(d.label).toMatchObject({
      show: true,
      position: "end",
      distance: -(4 + p.lane * 14),
      verticalAlign: "top",
      align: "left",
      offset: [p.leftPx - 995, 0],
      fontSize: LABEL_FONT_SIZE,
      fontFamily: LABEL_FONT_FAMILY,
    });
  });

  it("lays out and draws a multiline label as one line", () => {
    const measured: string[] = [];
    const measure: MeasureText = (t) => {
      measured.push(t);
      return measure6(t);
    };
    const label = "A\nB\r\nC \r \n D";
    const p = nth(layoutPlacements([point(1, at(500), label)], WIN15, 1000, measure));
    expect(p.text).toBe("A B C D");
    expect(measured.some((t) => /[\r\n]/u.test(t))).toBe(false);
    const layout = { window: WIN15, plotWidthPx: 1000, measure: measure6 };
    const line = eventOverlay([point(1, at(500), label)], theme, layout).markLine.data as {
      label: { formatter: () => string };
    }[];
    expect(nth(line).label.formatter()).toBe("A B C D");
    const area = eventOverlay(
      [
        {
          id: 2,
          label: "A\nB\nC",
          color: "#f00",
          fromMs: at(100),
          toMs: at(300),
          dash: "dash" as const,
        },
      ],
      theme,
      layout,
    ).markArea.data as [{ label: { formatter: () => string } }, unknown][];
    expect(nth(area)[0].label.formatter()).toBe("A B C");
  });

  it("truncates a very long label in logarithmically many measurements", () => {
    let calls = 0;
    const counting: MeasureText = (t) => {
      calls += 1;
      return measure6(t);
    };
    const p = nth(layoutPlacements([point(1, at(500), "x".repeat(10_000))], WIN15, 1000, counting));
    expect(calls).toBeLessThanOrEqual(64);
    expect(p.text.endsWith("…")).toBe(true);
    expect(p.widthPx).toBeLessThanOrEqual(LABEL_MAX_PX);
    // The longest prefix that fits: one more character would not.
    const kept = p.text.length - 1;
    expect(measure6(`${"x".repeat(kept + 1)}…`)).toBeGreaterThan(measure6(p.text));
    expect(p.text).toBe(
      nth(layoutPlacements([point(1, at(500), "x".repeat(80))], WIN15, 1000, measure6)).text,
    );
  });

  it("draws a span label relative to its clipped start", () => {
    const span: EventMarker = {
      id: 1,
      label: "soak",
      color: "#f00",
      fromMs: -60_000,
      toMs: at(100),
      dash: "dash" as const,
    };
    const layout = { window: WIN15, plotWidthPx: 1000, measure: measure6 };
    const p = nth(layoutPlacements([span], WIN15, 1000, measure6));
    const area = eventOverlay([span], theme, layout).markArea.data as [
      { label: { position: number[]; align: string; fontFamily: string } },
      unknown,
    ][];
    // Clipped at the window start, x = 0 — ECharts' polygon origin.
    expect(nth(area)[0].label.position).toEqual([p.leftPx, 4 + p.lane * 14]);
    expect(nth(area)[0].label.align).toBe("left");
    expect(nth(area)[0].label.fontFamily).toBe(LABEL_FONT_FAMILY);
  });
});

describe("eventOverlay label halo (#863)", () => {
  it("rings every point and span label in the theme surface colour", () => {
    const dark = chartTheme(true);
    const events: EventMarker[] = [
      {
        id: 1,
        label: "reload",
        color: "#7c5cff",
        fromMs: 1_000,
        toMs: null,
        dash: "dash" as const,
      },
      { id: 2, label: "soak", color: "#ff6b6b", fromMs: 2_000, toMs: 3_000, dash: "dash" as const },
    ];
    const overlay = eventOverlay(events, dark, LAYOUT);
    const line = nth(overlay.markLine.data as { label: Record<string, unknown> }[]);
    const area = nth(overlay.markArea.data as [{ label: Record<string, unknown> }, unknown][]);
    for (const label of [line.label, area[0].label]) {
      expect(label["textBorderColor"]).toBe(dark.surface);
      expect(label["textBorderWidth"]).toBe(3);
    }
  });
});

describe("createTextMeasure (#863)", () => {
  it("under jsdom makes no console.error call and returns approxMeasure widths", () => {
    const spy = vi.spyOn(console, "error");
    const measure = createTextMeasure();
    expect(measure("abcd")).toBe(approxMeasure("abcd"));
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("measures with an injected canvas context at the label font", () => {
    const ctx = { font: "", measureText: (t: string) => ({ width: t.length * 7 }) };
    const measure = createTextMeasure(() => ctx as unknown as CanvasRenderingContext2D);
    expect(measure("abc")).toBe(21);
    expect(ctx.font).toBe(`${LABEL_FONT_SIZE}px ${LABEL_FONT_FAMILY}`);
  });
});
