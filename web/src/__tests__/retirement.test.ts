// Pins retirement.ts — the Task 10 fix for the legacy dashboard's #1 known
// bug ("chart divs grew forever because one legend/trace per PID ever seen
// accumulated without bound"). Two independent policies:
//   - retireStaleMetrics: a proc/* metric drops out of the CHART once its
//     PID is absent from the latest K consecutive collection ticks (store
//     data is untouched — this only decides what gets drawn).
//   - selectLegendEntries: once more traces are active than the legend
//     budget allows, only the top entries by latest value keep a legend row.
import { describe, expect, it } from "vitest";

import type { Point } from "../api/client";
import type { Metric } from "../grouping";
import { isProcMetric, type LegendCandidate, RETIREMENT_K, retireStaleMetrics, selectLegendEntries } from "../retirement";

function pt(ts: string, value = 1): Point {
  return { ts, value, meta: null };
}

function metric(label: string, overrides: Partial<Metric> = {}): Metric {
  return { label, chart: "cpu", y_title: "%", unit: "%", ...overrides };
}

describe("isProcMetric", () => {
  it("is true only for proc/* labels", () => {
    expect(isProcMetric("proc/101")).toBe(true);
    expect(isProcMetric("Overall CPU")).toBe(false);
  });
});

describe("retireStaleMetrics — transitions", () => {
  it("appear: a brand-new PID with a point at the latest tick is kept", () => {
    const metrics = [metric("proc/1")];
    const series = { "proc/1": [pt("t3")] };
    const out = retireStaleMetrics(metrics, series);
    expect(out.map((m) => m.label)).toEqual(["proc/1"]);
  });

  it("persist: a PID reporting across several ticks, still within the latest K, is kept", () => {
    const metrics = [metric("proc/1"), metric("proc/2")];
    // Five ticks total; proc/1 reports every tick, proc/2 only at the latest three.
    const series = {
      "proc/1": [pt("t1"), pt("t2"), pt("t3"), pt("t4"), pt("t5")],
      "proc/2": [pt("t3"), pt("t4"), pt("t5")],
    };
    const out = retireStaleMetrics(metrics, series, 3);
    expect(out.map((m) => m.label).sort()).toEqual(["proc/1", "proc/2"]);
  });

  it("retire: a PID whose latest point falls outside the latest K ticks is dropped", () => {
    const metrics = [metric("proc/1"), metric("proc/2")];
    // Distinct ticks t1..t5; proc/2's last point (t2) is not among the latest 3 (t3,t4,t5).
    const series = {
      "proc/1": [pt("t1"), pt("t2"), pt("t3"), pt("t4"), pt("t5")],
      "proc/2": [pt("t1"), pt("t2")],
    };
    const out = retireStaleMetrics(metrics, series, 3);
    expect(out.map((m) => m.label)).toEqual(["proc/1"]);
  });

  it("reappear: a retired PID with a fresh point at the newest tick is kept again", () => {
    const metrics = [metric("proc/1"), metric("proc/2")];
    const series = {
      "proc/1": [pt("t1"), pt("t2"), pt("t3"), pt("t4"), pt("t5"), pt("t6")],
      // proc/2 was retired after t2 (outside latest-3 at t4/t5/t6) but now
      // reports again at t6 — its earlier (t1, t2) history is still there.
      "proc/2": [pt("t1"), pt("t2"), pt("t6")],
    };
    const out = retireStaleMetrics(metrics, series, 3);
    expect(out.map((m) => m.label).sort()).toEqual(["proc/1", "proc/2"]);
    // The retained history travels with it — retireStaleMetrics only
    // filters which Metric entries pass through, never mutates series.
    expect(series["proc/2"]).toHaveLength(3);
  });

  it("uses RETIREMENT_K (3) as the default window", () => {
    const metrics = [metric("proc/1"), metric("proc/2"), metric("proc/3"), metric("proc/4")];
    const series = {
      "proc/1": [pt("t1")],
      "proc/2": [pt("t2")],
      "proc/3": [pt("t3")],
      "proc/4": [pt("t4")],
    };
    const out = retireStaleMetrics(metrics, series); // no k passed -> default RETIREMENT_K
    expect(RETIREMENT_K).toBe(3);
    expect(out.map((m) => m.label)).toEqual(["proc/2", "proc/3", "proc/4"]);
  });

  it("never retires non-proc series, regardless of tick recency", () => {
    const metrics = [metric("Overall CPU", { chart: "cpu" }), metric("proc/1")];
    const series = {
      "Overall CPU": [pt("t0")], // ancient, single point
      "proc/1": [pt("t5"), pt("t6"), pt("t7")], // recent ticks define the window
    };
    const out = retireStaleMetrics(metrics, series, 2);
    expect(out.map((m) => m.label).sort()).toEqual(["Overall CPU", "proc/1"]);
  });

  it("is a no-op (identity) when there are no proc/* metrics in the group", () => {
    const metrics = [metric("Overall CPU"), metric("Memory Usage", { chart: "memory" })];
    const out = retireStaleMetrics(metrics, {});
    expect(out).toBe(metrics);
  });

  it("keeps a proc metric with no data at all yet (nothing to judge it stale by)", () => {
    const metrics = [metric("proc/1"), metric("proc/2")];
    const series = { "proc/1": [pt("t1"), pt("t2"), pt("t3")] };
    const out = retireStaleMetrics(metrics, series, 3);
    // proc/2 has never reported; only actual stale (reported-then-stopped) PIDs retire.
    expect(out.map((m) => m.label).sort()).toEqual(["proc/1", "proc/2"]);
  });
});

describe("selectLegendEntries — cap ordering", () => {
  function candidate(label: string, value: number): LegendCandidate {
    return { label, value };
  }

  it("keeps everything when under the cap", () => {
    const candidates = [candidate("a", 1), candidate("b", 2)];
    expect(selectLegendEntries(candidates, 5)).toEqual(new Set(["a", "b"]));
  });

  it("keeps exactly the cap count when equal to the cap", () => {
    const candidates = [candidate("a", 1), candidate("b", 2)];
    expect(selectLegendEntries(candidates, 2)).toEqual(new Set(["a", "b"]));
  });

  it("over budget: keeps only the top entries by latest value, dropping the rest", () => {
    const candidates = [candidate("low", 1), candidate("high", 10), candidate("mid", 5)];
    expect(selectLegendEntries(candidates, 2)).toEqual(new Set(["high", "mid"]));
  });

  it("ranks strictly by value, independent of input order", () => {
    const candidates = [candidate("a", 3), candidate("b", 9), candidate("c", 1), candidate("d", 7)];
    expect(selectLegendEntries(candidates, 2)).toEqual(new Set(["b", "d"]));
  });

  it("cap of 0 keeps nothing", () => {
    const candidates = [candidate("a", 1)];
    expect(selectLegendEntries(candidates, 0)).toEqual(new Set());
  });
});
