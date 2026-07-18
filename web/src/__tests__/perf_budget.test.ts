// Tier-1 scaling budget (spec §Proving it). These assert the SHAPE — cost flat in
// run length — not a stopwatch: wall-clock thresholds on a shared CI runner are
// noise, and the thing that kills us is an O(total-run) term hiding in a per-tick
// path. Only a ratio test catches that.
import { describe, expect, it } from "vitest";
import type { MetricRecord } from "../api/export.gen";
import { healthForHosts } from "../data/health";
import { appendToIndex, buildIndex } from "../data/seriesIndex";
import { synthSession } from "./_synth";

const HOSTS = 7;
const SERIES_PER_HOST = 13; // ~90 series total, the live bed's shape
const INTERVAL_S = 5;

// healthForHosts iterates session.lab.hosts, so this fixture MUST populate real
// hosts (_synth.ts does: h0..h{hosts-1}, matching the synthesized metrics'
// `host` field) — with lab.hosts: [] the per-host loop body never runs and the
// guard below times an empty loop.
//
// This scale is deliberately much bigger than "the live bed's shape" above.
// At that realistic size (~90 series) the correctly-indexed healthForHosts is
// SO fast (a handful of O(log n) binary searches) that both tShort and tLong
// land in the sub-0.1ms range — under the timer's own noise floor and under
// the budget's fixed 2ms floor, which means the ratio assertion can't tell a
// correct run from a reintroduced O(all-points) scan OR a per-series linear
// scan: both mutations were verified to also stay under 2ms at that scale.
// Ten thousand series (200 hosts x 50 series) pushes the CORRECT
// implementation's wall time up past ~0.7ms — comfortably clear of both
// floors — while keeping tick counts (and so the fixture's build cost) small,
// since it's series COUNT that drives this loop's cost, not run length.
// short/long keep the same 12x run-length ratio as before.
const HEALTH_HOSTS = 200;
const HEALTH_SERIES_PER_HOST = 50; // 10,000 series — see comment above
const HEALTH_SHORT_TICKS = 50;
const HEALTH_LONG_TICKS = 600; // 12x HEALTH_SHORT_TICKS, same growth as before

function timeIt(fn: () => void, reps: number): number {
  const t0 = performance.now();
  for (let i = 0; i < reps; i++) fn();
  return (performance.now() - t0) / reps;
}

describe("tier-1 scaling budget: cost must be flat in run length", () => {
  it("healthForHosts does not get slower as the run gets longer", () => {
    const short = synthSession({
      hosts: HEALTH_HOSTS,
      seriesPerHost: HEALTH_SERIES_PER_HOST,
      ticks: HEALTH_SHORT_TICKS,
      intervalS: INTERVAL_S,
    });
    const long = synthSession({
      hosts: HEALTH_HOSTS,
      seriesPerHost: HEALTH_SERIES_PER_HOST,
      ticks: HEALTH_LONG_TICKS,
      intervalS: INTERVAL_S,
    });
    expect(long.metrics.length).toBeGreaterThan(5_000_000);

    const now = long.endMs;
    // Warm up the JIT before the timed reps so the first (compiling) call
    // doesn't pollute either average.
    void healthForHosts(short, null, now);
    void healthForHosts(long, null, now);
    const tShort = timeIt(() => void healthForHosts(short, null, now), 20);
    const tLong = timeIt(() => void healthForHosts(long, null, now), 20);

    // 12x the data must NOT cost meaningfully more. Generous ratio so CI noise
    // cannot flake it, but an O(all-points) regression is ~12x and blows through.
    expect(tLong).toBeLessThan(Math.max(tShort * 4, 2));
  }, 20_000);

  it("appendToIndex does not get slower as the run gets longer", () => {
    const batch = (): MetricRecord[] =>
      Array.from({ length: HOSTS * SERIES_PER_HOST }, (_, i) => ({
        host: `h${i % HOSTS}`,
        label: `m${i % SERIES_PER_HOST}`,
        timestamp: new Date(Date.now() + i).toISOString(),
        value: i,
      })) as MetricRecord[];

    const small = buildIndex(
      synthSession({
        hosts: HOSTS,
        seriesPerHost: SERIES_PER_HOST,
        ticks: 720,
        intervalS: INTERVAL_S,
      }).metrics,
    );
    const big = buildIndex(
      synthSession({
        hosts: HOSTS,
        seriesPerHost: SERIES_PER_HOST,
        ticks: 8640,
        intervalS: INTERVAL_S,
      }).metrics,
    );

    const tSmall = timeIt(() => appendToIndex(small, batch()), 100);
    const tBig = timeIt(() => appendToIndex(big, batch()), 100);

    expect(tBig).toBeLessThan(Math.max(tSmall * 4, 2));
  });
});
