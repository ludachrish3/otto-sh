import { describe, expect, it } from "vitest";
import {
  assignLanes,
  buildStackOption,
  chartTheme,
  eventMarkers,
  eventOverlay,
  zoomAbout,
  zoomToRange,
} from "../charts/options";
import { MAX_SERIES_PER_CHART, SERIES_DARK, SERIES_LIGHT } from "../charts/palette";

const WINDOW = { from: 1_000_000, to: 2_000_000 };
const theme = chartTheme(false);

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

  it("attaches event markers to the first series only", () => {
    const events = [
      { id: 1, label: "point", color: "#7c5cff", fromMs: 1_200_000, toMs: null },
      { id: 2, label: "span", color: "#ff6b6b", fromMs: 1_300_000, toMs: 1_400_000 },
    ];
    const opt = buildStackOption({
      unit: "",
      yTitle: "y",
      series: [series(0), series(1)],
      window: WINDOW,
      events,
      theme,
    }) as { series: Record<string, unknown>[] };
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
    const marks = eventMarkers(rows, WINDOW); // 1_000_000..2_000_000 ms
    expect(marks.map((m) => m.id)).toEqual([1, 3]);
    expect(marks[1].toMs).toBe(1_500_000);
  });

  it("assigns negative synthetic ids to id-less rows to avoid collision with real ids", () => {
    // Collision scenario: real row with id=1, and an id-less row that becomes second in output
    // (old fallback would assign out.length=1, colliding with the real id).
    const rows = [
      { id: 1, timestamp: "1970-01-01T00:20:00Z", label: "real-id-1", color: "#111111" },
      { id: null, timestamp: "1970-01-01T00:21:00Z", label: "id-less-second", color: "#222222" },
    ];
    const marks = eventMarkers(rows, WINDOW);
    const ids = marks.map((m) => m.id);
    // All ids must be unique
    expect(new Set(ids).size).toBe(ids.length);
    // Synthetic id must be negative (never collides with real non-negative ids)
    expect(ids).toContain(1); // real id
    expect(ids.some((id) => id < 0)).toBe(true); // synthetic is negative
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
});

describe("eventOverlay markArea lanes", () => {
  const theme = { muted: "#999", ink: "#eee" };

  it("gives two overlapping span events distinct label positions", () => {
    const events = [
      { id: 1, label: "stress run", color: "#ff6b6b", fromMs: 1_000, toMs: 2_000 },
      { id: 2, label: "log capture", color: "#2f9e6e", fromMs: 1_500, toMs: 2_500 },
    ];
    const { markArea } = eventOverlay(events, theme) as {
      markArea: { data: [{ label: { position: unknown[] } }, unknown][] };
    };
    const pos0 = markArea.data[0][0].label.position;
    const pos1 = markArea.data[1][0].label.position;
    expect(pos0).not.toEqual(pos1); // stacked, not colliding
  });

  it("non-overlapping span events keep the same (lane-0) label position", () => {
    const events = [
      { id: 1, label: "a", color: "#ff6b6b", fromMs: 1_000, toMs: 2_000 },
      { id: 2, label: "b", color: "#2f9e6e", fromMs: 3_000, toMs: 4_000 },
    ];
    const { markArea } = eventOverlay(events, theme) as {
      markArea: { data: [{ label: { position: unknown[] } }, unknown][] };
    };
    const pos0 = markArea.data[0][0].label.position;
    const pos1 = markArea.data[1][0].label.position;
    expect(pos0).toEqual(pos1); // both lane 0 — the common case is unchanged
  });

  it("leaves label text and colour untouched — a layout fix, not a restyle", () => {
    const events = [
      { id: 1, label: "stress run", color: "#ff6b6b", fromMs: 1_000, toMs: 2_000 },
      { id: 2, label: "log capture", color: "#2f9e6e", fromMs: 1_500, toMs: 2_500 },
    ];
    const { markArea } = eventOverlay(events, theme) as {
      markArea: { data: [{ name: string; itemStyle: { color: string } }, unknown][] };
    };
    expect(markArea.data[0][0].name).toBe("stress run");
    expect(markArea.data[0][0].itemStyle.color).toBe("#ff6b6b");
    expect(markArea.data[1][0].name).toBe("log capture");
    expect(markArea.data[1][0].itemStyle.color).toBe("#2f9e6e");
  });

  it("leaves markLine (instant-event) behaviour untouched", () => {
    const events = [{ id: 1, label: "config reload", color: "#7c5cff", fromMs: 1_000, toMs: null }];
    const { markLine } = eventOverlay(events, theme) as {
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
    const events = [{ id: 1, label: "stress run", color: "#ff6b6b", fromMs: 1_000, toMs: 2_000 }];
    const theme = { muted: "#999", ink: "#f3f4f6" };
    const { markArea } = eventOverlay(events, theme) as {
      markArea: { data: [{ label: { color: string; fontSize: number } }, unknown][] };
    };
    expect(markArea.data[0][0].label.color).toBe(theme.ink);
    expect(markArea.data[0][0].label.fontSize).toBe(10);
  });
});
