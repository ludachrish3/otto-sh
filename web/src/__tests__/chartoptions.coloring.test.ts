import { describe, expect, it } from "vitest";

import { buildStackOption, chartTheme, type SeriesInput } from "../charts/options";

function lineFor(s: SeriesInput) {
  const built = buildStackOption({
    unit: "%",
    yTitle: "Usage %",
    series: [s],
    window: { from: 0, to: 1000 },
    events: [],
    theme: chartTheme(false),
  });
  return (built.series as Array<{ itemStyle: { color: string }; lineStyle: { width: number } }>)[0];
}

describe("hybrid CPU coloring", () => {
  it("muted series use the muted color at reduced width", () => {
    const theme = chartTheme(false);
    const line = lineFor({ key: "core 0", name: "core 0", slot: 3, muted: true, points: [] });
    expect(line.itemStyle.color).toBe(theme.mutedSeries);
    expect(line.lineStyle.width).toBeLessThan(2);
  });

  it("non-muted series use the palette slot color at full width", () => {
    const theme = chartTheme(false);
    const line = lineFor({ key: "Overall CPU", name: "Overall CPU", slot: 0, points: [] });
    expect(line.itemStyle.color).toBe(theme.series[0]);
    expect(line.lineStyle.width).toBe(2);
  });
});
