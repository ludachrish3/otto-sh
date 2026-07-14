// Pins src/data/retirement.ts — the Task 10 re-homing of the legacy
// dashboard's #1 known-bug fix ("chart divs grew forever because one
// legend/trace per PID ever seen accumulated without bound") against the
// format:1 models. Named `retirement.data.test.ts` (not `retirement.test.ts`)
// because the pre-existing `src/retirement.ts` + `src/__tests__/retirement.test.ts`
// pair stays put until Task 12's deletion sweep — two copies coexist for
// this task by design (see the Task 10 brief), and both source modules
// happen to share the basename "retirement".
//
// A proc/* series drops out of the CHART once its PID is absent from the
// latest K consecutive collection ticks (the index's `recs`/`tsMs` are
// untouched — this only decides which keys are still worth drawing).
import { describe, expect, it } from "vitest";

import type { MetricRecord } from "../api/export.gen";
import { isProcMetric, RETIRE_AFTER_TICKS, retireStaleSeries } from "../data/retirement";
import { buildIndex, type SeriesIndex, seriesKey } from "../data/seriesIndex";

const HOST = "hostA";

/** Distinct, chronologically ordered collection ticks — `parseTs` needs real
 * ISO timestamps (unlike the legacy suite's bare "t1"/"t2" strings, which
 * only had to sort as strings). */
function tick(n: number): string {
  return `2026-07-13T00:00:${String(n).padStart(2, "0")}.000Z`;
}

function rec(label: string, n: number, value = 1): MetricRecord {
  return { host: HOST, label, timestamp: tick(n), value };
}

function key(label: string): string {
  return seriesKey(HOST, label);
}

function indexOf(records: MetricRecord[]): SeriesIndex {
  return buildIndex(records);
}

describe("isProcMetric", () => {
  it("is true only for proc/* labels", () => {
    expect(isProcMetric("proc/101")).toBe(true);
    expect(isProcMetric("Overall CPU")).toBe(false);
  });
});

describe("retireStaleSeries — transitions", () => {
  it("appear: a brand-new PID with a point at the latest tick is kept", () => {
    const index = indexOf([rec("proc/1", 3)]);
    const out = retireStaleSeries([key("proc/1")], index);
    expect(out).toEqual([key("proc/1")]);
  });

  it("persist: a PID reporting across several ticks, still within the latest K, is kept", () => {
    // Five ticks total; proc/1 reports every tick, proc/2 only at the latest three.
    const index = indexOf([
      rec("proc/1", 1),
      rec("proc/1", 2),
      rec("proc/1", 3),
      rec("proc/1", 4),
      rec("proc/1", 5),
      rec("proc/2", 3),
      rec("proc/2", 4),
      rec("proc/2", 5),
    ]);
    const out = retireStaleSeries([key("proc/1"), key("proc/2")], index, { ticks: 3 });
    expect([...out].sort()).toEqual([key("proc/1"), key("proc/2")].sort());
  });

  it("retire: a PID whose latest point falls outside the latest K ticks is dropped", () => {
    // Distinct ticks t1..t5; proc/2's last point (t2) is not among the latest 3 (t3,t4,t5).
    const index = indexOf([
      rec("proc/1", 1),
      rec("proc/1", 2),
      rec("proc/1", 3),
      rec("proc/1", 4),
      rec("proc/1", 5),
      rec("proc/2", 1),
      rec("proc/2", 2),
    ]);
    const out = retireStaleSeries([key("proc/1"), key("proc/2")], index, { ticks: 3 });
    expect(out).toEqual([key("proc/1")]);
  });

  it("reappear: a retired PID with a fresh point at the newest tick is kept again", () => {
    const index = indexOf([
      rec("proc/1", 1),
      rec("proc/1", 2),
      rec("proc/1", 3),
      rec("proc/1", 4),
      rec("proc/1", 5),
      rec("proc/1", 6),
      // proc/2 was retired after t2 (outside latest-3 at t4/t5/t6) but now
      // reports again at t6 — its earlier (t1, t2) history is still there.
      rec("proc/2", 1),
      rec("proc/2", 2),
      rec("proc/2", 6),
    ]);
    const out = retireStaleSeries([key("proc/1"), key("proc/2")], index, { ticks: 3 });
    expect([...out].sort()).toEqual([key("proc/1"), key("proc/2")].sort());
    // The retained history travels with it — retireStaleSeries only filters
    // which keys pass through, never mutates the index.
    expect(index.recs.get(key("proc/2"))).toHaveLength(3);
  });

  it("uses RETIRE_AFTER_TICKS (3) as the default window", () => {
    const index = indexOf([rec("proc/1", 1), rec("proc/2", 2), rec("proc/3", 3), rec("proc/4", 4)]);
    const keys = [key("proc/1"), key("proc/2"), key("proc/3"), key("proc/4")];
    const out = retireStaleSeries(keys, index); // no ticks passed -> default RETIRE_AFTER_TICKS
    expect(RETIRE_AFTER_TICKS).toBe(3);
    expect(out).toEqual([key("proc/2"), key("proc/3"), key("proc/4")]);
  });

  it("never retires non-proc series, regardless of tick recency", () => {
    const index = indexOf([
      rec("Overall CPU", 0), // ancient, single point
      rec("proc/1", 5),
      rec("proc/1", 6),
      rec("proc/1", 7), // recent ticks define the window
    ]);
    const out = retireStaleSeries([key("Overall CPU"), key("proc/1")], index, { ticks: 2 });
    expect([...out].sort()).toEqual([key("Overall CPU"), key("proc/1")].sort());
  });

  it("is a no-op (identity) when there are no proc/* keys in the group", () => {
    const index = indexOf([rec("Overall CPU", 1), rec("Memory Usage", 2)]);
    const keys = [key("Overall CPU"), key("Memory Usage")];
    const out = retireStaleSeries(keys, index);
    expect(out).toBe(keys);
  });

  it("keeps a proc key with no data at all yet (nothing to judge it stale by)", () => {
    const index = indexOf([rec("proc/1", 1), rec("proc/1", 2), rec("proc/1", 3)]);
    // proc/2 has never reported — it isn't in the index at all.
    const out = retireStaleSeries([key("proc/1"), key("proc/2")], index, { ticks: 3 });
    expect([...out].sort()).toEqual([key("proc/1"), key("proc/2")].sort());
  });
});
