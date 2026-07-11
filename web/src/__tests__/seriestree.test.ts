import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree, collectSeriesPoints, filterTree, sourcesIn } from "../data/seriesTree";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

describe("buildSeriesTree — host subject", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");

  it("groups by chart with spec metadata", () => {
    const cpu = tree.find((c) => c.chartKey === "cpu");
    expect(cpu).toBeDefined();
    expect(cpu?.unit).toBe("%");
    expect(cpu?.series).toHaveLength(1);
    expect(cpu?.series[0].key).toBe("CPU %");
  });

  it("marks mgmt-sourced series with their source", () => {
    const psu = tree.find((c) => c.chartKey === "psu-temp");
    expect(psu?.series[0].source).toBe("mgmt-01");
  });

  it("assigns stable slots from the full tree", () => {
    for (const chart of tree) {
      for (let i = 0; i < chart.series.length; i++) {
        expect(chart.series[i].slot).toBe(i);
      }
    }
  });
});

describe("buildSeriesTree — element subject", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a");

  it("includes the element-targeted series", () => {
    const ambient = tree.find((c) => c.chartKey === "ambient");
    expect(ambient?.series.some((s) => s.host === "chassis-a")).toBe(true);
  });

  it("includes member-host series named by host", () => {
    const cpu = tree.find((c) => c.chartKey === "cpu");
    expect(cpu?.series.map((s) => s.host)).toEqual([
      "chassis-a_lc1",
      "chassis-a_lc2",
      "chassis-a_sup",
    ]);
    expect(cpu?.series[0].key).toBe("chassis-a_lc1/CPU %");
  });
});

describe("filterTree + sourcesIn", () => {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");

  it("search prunes by series and chart label, case-insensitive", () => {
    const hit = filterTree(tree, { search: "psu", chips: null, source: null });
    expect(hit.map((c) => c.chartKey)).toEqual(["psu-temp"]);
    expect(filterTree(tree, { search: "zzz", chips: null, source: null })).toEqual([]);
  });

  it("chips restrict to whole chart groups", () => {
    const hit = filterTree(tree, { search: "", chips: new Set(["cpu"]), source: null });
    expect(hit.map((c) => c.chartKey)).toEqual(["cpu"]);
  });

  it("source filter keeps only externally-sourced series", () => {
    const hit = filterTree(tree, { search: "", chips: null, source: "mgmt-01" });
    expect(hit.every((c) => c.series.every((s) => s.source === "mgmt-01"))).toBe(true);
    expect(hit.length).toBeGreaterThan(0);
  });

  it("filtering preserves original slots (no repaint)", () => {
    const psu = filterTree(tree, { search: "psu", chips: null, source: null })[0];
    const original = tree.find((c) => c.chartKey === "psu-temp");
    expect(psu.series[0].slot).toBe(original?.series[0].slot);
  });

  it("sourcesIn lists distinct external sources", () => {
    expect(sourcesIn(tree)).toEqual(["mgmt-01"]);
  });
});

describe("collectSeriesPoints", () => {
  it("returns in-range [ms, value] pairs for checked keys only", () => {
    const tree = buildSeriesTree(kitchen, "chassis-a_lc1");
    const range = { from: kitchen.startMs, to: kitchen.startMs + 10 * 60_000 };
    const points = collectSeriesPoints(kitchen, tree, new Set(["CPU %"]), range);
    expect([...points.keys()]).toEqual(["CPU %"]);
    const cpu = points.get("CPU %") ?? [];
    expect(cpu.length).toBeGreaterThan(10);
    expect(cpu.every(([ts]) => ts >= range.from && ts <= range.to)).toBe(true);
    expect(cpu).toEqual([...cpu].sort((a, b) => a[0] - b[0]));
  });
});
