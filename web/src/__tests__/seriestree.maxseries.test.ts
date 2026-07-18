import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { MAX_SERIES_PER_CHART } from "../charts/palette";
import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree } from "../data/seriesTree";

const HERE = dirname(fileURLToPath(import.meta.url));
function freshKitchen() {
  return parseExportDocument(readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"))
    .sessions[0];
}

describe("buildSeriesTree maxSeries", () => {
  it("defaults a spec with no max_series to the default cap", () => {
    const tree = buildSeriesTree(freshKitchen(), "chassis-a_lc1");
    expect(tree.length).toBeGreaterThan(0);
    for (const chart of tree) expect(chart.maxSeries).toBe(MAX_SERIES_PER_CHART);
  });

  it("passes through null (uncapped) and an explicit numeric cap", () => {
    const session = freshKitchen();
    const cpu = session.meta.charts.find((c) => c.chart === "cpu");
    const psu = session.meta.charts.find((c) => c.chart === "psu-temp");
    expect(cpu).toBeDefined();
    expect(psu).toBeDefined();
    if (cpu) cpu.max_series = null; // uncapped
    if (psu) psu.max_series = 3; // explicit cap
    const tree = buildSeriesTree(session, "chassis-a_lc1");
    const byKey = Object.fromEntries(tree.map((c) => [c.chartKey, c.maxSeries]));
    expect(byKey.cpu).toBeNull();
    expect(byKey["psu-temp"]).toBe(3);
  });
});
