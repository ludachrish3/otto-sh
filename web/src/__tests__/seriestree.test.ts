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
    // Anti-vacuity: every host-subject chart here is single-series, so the
    // loop above alone only ever checks slot 0 === 0. The element tree's cpu
    // chart is the multi-series case that gives slots real teeth.
    const cpu = buildSeriesTree(kitchen, "chassis-a").find((c) => c.chartKey === "cpu");
    expect(cpu?.series.map((s) => s.slot)).toEqual([0, 1, 2]);
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
    // A filter that repaints slots from 0 would still pass a single-series
    // comparison (0 === 0, the old psu-temp form) — proving no-repaint needs
    // a surviving series whose original slot is NONZERO. In the element
    // tree's 3-series cpu chart, "sup" keeps only chassis-a_sup, slot 2.
    const elementTree = buildSeriesTree(kitchen, "chassis-a");
    const hit = filterTree(elementTree, { search: "sup", chips: null, source: null });
    const cpu = hit.find((c) => c.chartKey === "cpu");
    expect(cpu?.series.map((s) => [s.host, s.slot])).toEqual([["chassis-a_sup", 2]]);
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
