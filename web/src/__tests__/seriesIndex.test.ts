import { describe, expect, it } from "vitest";
import { appendToIndex, buildIndex, sliceSeries } from "../data/seriesIndex";
import type { MetricRecord } from "../api/export.gen";

const rec = (host: string, label: string, iso: string, value: number): MetricRecord =>
  ({ host, label, timestamp: iso, value }) as MetricRecord;

describe("buildIndex", () => {
  it("groups by host/label and keeps times ascending", () => {
    const idx = buildIndex([
      rec("a", "cpu", "2026-07-12T10:00:00Z", 1),
      rec("a", "cpu", "2026-07-12T10:00:05Z", 2),
      rec("a", "mem", "2026-07-12T10:00:05Z", 3),
      rec("b", "cpu", "2026-07-12T10:00:10Z", 4),
    ]);
    expect([...idx.recs.keys()].sort()).toEqual(["a/cpu", "a/mem", "b/cpu"]);
    expect(idx.keysByHost.get("a")?.sort()).toEqual(["a/cpu", "a/mem"]);
    expect(idx.tsMs.get("a/cpu")).toEqual([
      Date.parse("2026-07-12T10:00:00Z"),
      Date.parse("2026-07-12T10:00:05Z"),
    ]);
  });
});

describe("appendToIndex", () => {
  it("appends in place and bumps only the touched series' revision", () => {
    const idx = buildIndex([
      rec("a", "cpu", "2026-07-12T10:00:00Z", 1),
      rec("a", "mem", "2026-07-12T10:00:00Z", 9),
    ]);
    const cpuArrayBefore = idx.recs.get("a/cpu");
    const memRevBefore = idx.rev.get("a/mem");

    appendToIndex(idx, [rec("a", "cpu", "2026-07-12T10:00:05Z", 2)]);

    // Pushed IN PLACE — no array copying, so append stays O(batch) not O(all).
    expect(idx.recs.get("a/cpu")).toBe(cpuArrayBefore);
    expect(idx.recs.get("a/cpu")?.length).toBe(2);
    // Only the touched series' revision moves; untouched charts keep their memo.
    expect(idx.rev.get("a/cpu")).toBe(1);
    expect(idx.rev.get("a/mem")).toBe(memRevBefore);
  });

  it("registers a brand-new series and host", () => {
    const idx = buildIndex([]);
    appendToIndex(idx, [rec("z", "cpu", "2026-07-12T10:00:00Z", 1)]);
    expect(idx.keysByHost.get("z")).toEqual(["z/cpu"]);
    expect(idx.recs.get("z/cpu")?.length).toBe(1);
  });
});

describe("sliceSeries", () => {
  const idx = buildIndex([
    rec("a", "cpu", "2026-07-12T10:00:00Z", 1),
    rec("a", "cpu", "2026-07-12T10:00:05Z", 2),
    rec("a", "cpu", "2026-07-12T10:00:10Z", 3),
  ]);

  it("returns everything when the range is null", () => {
    expect(sliceSeries(idx, "a/cpu", null).map((m) => m.value)).toEqual([1, 2, 3]);
  });

  it("returns only the in-range samples", () => {
    const range = {
      from: Date.parse("2026-07-12T10:00:04Z"),
      to: Date.parse("2026-07-12T10:00:06Z"),
    };
    expect(sliceSeries(idx, "a/cpu", range).map((m) => m.value)).toEqual([2]);
  });

  it("returns [] for an unknown key rather than throwing", () => {
    expect(sliceSeries(idx, "nope/cpu", null)).toEqual([]);
  });
});
