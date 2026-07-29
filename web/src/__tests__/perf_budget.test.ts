// Tier-1 scaling budget (spec §Proving it): the live-mode hot paths must cost
// the same on a long run as on a short one. These guards COUNT WORK. They used
// to time it, and that was wrong twice over.
//
//  1. A stopwatch cannot be deterministic. `tLong < max(tShort * 4, 2)` divides
//     one noisy measurement by another, so it measures the machine as much as
//     the code. Measured over 15 shuffled full-suite runs (2026-07-29): tShort
//     ranged 0.88-1.34ms and tLong 1.72-2.58ms, which left the assertion as
//     little as 1.36x of headroom — on a guard whose stated tolerance is 4x.
//     One GC pause or one scheduler preemption inside the timed loop closes
//     that gap on code nobody touched, and did: 44.79ms against a 4.18ms bound.
//  2. Timing FORCED the fixture to be enormous. At the live bed's real shape
//     (~90 series) a correctly-indexed healthForHosts finishes below the
//     timer's own noise floor, so this file had scaled up to 200 hosts x 50
//     series x 600 ticks — 6 million records. Measured peak RSS for this ONE
//     test file: 1.21 GB, against a 185 MB vitest baseline. On a 4-CPU /
//     7.6 GB dev VM already 2.3 GB into swap, running three vitest workers,
//     that allocation IS the stall that broke assertion 1. The guard was
//     manufacturing the noise it then tripped over.
//
// Counting sample reads fixes both at once. A read is a read whether the
// machine is idle or swapping, so the counts are bit-for-bit reproducible; and
// counting has no noise floor, so the fixture shrinks back to the shape the
// dashboard actually sees. Same 12x growth in run length, 54,600 records
// instead of 6,000,000. Measured, this file alone: peak RSS 1.21 GB -> 215 MB
// and wall clock 4.45s -> 1.32s, i.e. its own footprint above the vitest
// baseline falls from ~1.03 GB to ~30 MB.
//
// Every number quoted below was measured, and each regression was re-proven by
// mutating the source and watching the guard redden.
import { describe, expect, it } from "vitest";
import type { MetricRecord } from "../api/export.gen";
import type { NormalizedSession } from "../data/exportDoc";
import { healthForHosts } from "../data/health";
import { appendToIndex, buildIndex, type SeriesIndex, seriesKey } from "../data/seriesIndex";
import { synthSession } from "./_synth";

const HOSTS = 7;
const SERIES_PER_HOST = 13; // 91 series, the live bed's shape
const INTERVAL_S = 5;

// 12x growth in run length — the same ratio the timed version used, and what
// the bounds below are calibrated against.
const SHORT_TICKS = 50;
const LONG_TICKS = 600;

// Fixed epoch for synthesized batches. The timed version seeded these from
// `Date.now()`, which made the input to a supposedly deterministic guard depend
// on the moment it ran.
const T0 = Date.parse("2026-07-12T00:00:00Z");

// A numeric-index property key ("0", "17") — i.e. a read of a SAMPLE, as
// opposed to "length"/"push"/Symbol.iterator. Module level because the Proxy
// trap consults it on every property access (biome performance/useTopLevelRegex).
const INDEX_KEY = /^\d+$/;

/** `arr`, with every numeric-index read reported to `onRead`. */
function counting<T>(arr: T[], onRead: () => void): T[] {
  return new Proxy(arr, {
    get(target, prop, receiver) {
      if (typeof prop === "string" && INDEX_KEY.test(prop)) onRead();
      return Reflect.get(target, prop, receiver);
    },
  });
}

/** Swap every per-sample array in `index` for a counting proxy.
 *
 * `keysByHost` is deliberately NOT wrapped: it holds one entry per SERIES, and
 * both fixtures have the same series count, so counting it would add an equal
 * constant to both sides and do nothing but dilute the ratio. Replacing the
 * value of a key that already exists does not disturb a Map's iteration. */
function countIndexReads(index: SeriesIndex, onRead: () => void): void {
  for (const [key, arr] of index.tsMs) index.tsMs.set(key, counting(arr, onRead));
  for (const [key, arr] of index.recs) index.recs.set(key, counting(arr, onRead));
}

/** Total sample reads through `session`, by any route. */
function countSessionReads(session: NormalizedSession): () => number {
  let reads = 0;
  const onRead = (): void => {
    reads += 1;
  };
  // The flat array first: an O(all-points) regression reads THIS, and no amount
  // of instrumenting the index would reveal it.
  session.metrics = counting(session.metrics, onRead);
  countIndexReads(session.index, onRead);
  return () => reads;
}

describe("tier-1 scaling budget: cost must be flat in run length", () => {
  it("healthForHosts does not read more samples as the run gets longer", () => {
    const shape = { hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, intervalS: INTERVAL_S };
    const short = synthSession({ ...shape, ticks: SHORT_TICKS });
    const long = synthSession({ ...shape, ticks: LONG_TICKS });
    const shortReads = countSessionReads(short);
    const longReads = countSessionReads(long);

    // One `now` for both, as the timed version used: the long run's end.
    const now = long.endMs;
    healthForHosts(short, null, now);
    healthForHosts(long, null, now);

    // Liveness. A healthForHosts that returned early — no hosts, an empty
    // index — would read nothing and sail through a pure ratio assertion.
    // Measured: 546 (91 series x a 6-step binary search over 50 samples).
    expect(shortReads()).toBeGreaterThan(0);

    // 12x the data must cost only the ~log2 more reads a binary search needs.
    // Measured 546 -> 910 (1.667x) against a bound of 2184; the count is
    // identical on every run. Mutation-verified red: replacing the binary
    // search with a per-series linear scan reads 4,641 -> 54,691 (11.8x), and
    // scanning the flat `session.metrics` instead of the index reads
    // 31,850 -> 382,200 (12.0x).
    expect(longReads()).toBeLessThan(shortReads() * 4);
  });

  it("appendToIndex does not read more of the index as the index grows", () => {
    const batch = (): MetricRecord[] =>
      Array.from({ length: HOSTS * SERIES_PER_HOST }, (_, i) => ({
        host: `h${i % HOSTS}`,
        label: `m${i % SERIES_PER_HOST}`,
        timestamp: new Date(T0 + i).toISOString(),
        value: i,
      })) as MetricRecord[];

    const shape = { hosts: HOSTS, seriesPerHost: SERIES_PER_HOST, intervalS: INTERVAL_S };
    const small = buildIndex(synthSession({ ...shape, ticks: SHORT_TICKS }).metrics);
    const big = buildIndex(synthSession({ ...shape, ticks: LONG_TICKS }).metrics);

    let smallReads = 0;
    let bigReads = 0;
    countIndexReads(small, () => {
      smallReads += 1;
    });
    countIndexReads(big, () => {
      bigReads += 1;
    });

    const probe = seriesKey("h0", "m0");
    const before = big.recs.get(probe)?.length ?? 0;
    appendToIndex(small, batch());
    appendToIndex(big, batch());

    // Liveness, and it carries more weight here than above: a correct in-place
    // append reads ZERO existing elements, and zero is also what a no-op
    // appendToIndex would report. This is what tells the two apart. A batch
    // carries exactly one record per series, so every series grows by one.
    expect(big.recs.get(probe)?.length).toBe(before + 1);

    // Appending must not depend on what is already indexed. Measured 0 -> 0.
    // Mutation-verified red: rebuilding the arrays instead of pushing in place
    // (`index.tsMs.set(key, [...old, ts])` — the copy that would restore the
    // O(all) cost the index exists to remove) reads 9,100 -> 109,200 (12.0x).
    // The `, 1` floor is what lets the honest 0 -> 0 through.
    expect(bigReads).toBeLessThan(Math.max(smallReads * 4, 1));
  });
});
