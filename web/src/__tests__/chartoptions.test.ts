import { describe, expect, it } from "vitest";
import { buildStackOption, chartTheme, eventMarkers, zoomToRange } from "../charts/options";
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
