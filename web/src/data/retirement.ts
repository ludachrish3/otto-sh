// Pure PID-trace retirement policy for chart rendering, re-homed against the
// format:1 models (Task 10) — the fix for the legacy dashboard's #1 known
// bug: one legend/trace per process ever seen accumulated without bound,
// growing chart div height forever. A long ARCHIVE has exactly the same
// problem as a long live run, so this applies in BOTH modes.
//
// Kept dependency-free like the legacy module (no echarts, no DOM), so the
// tricky bits (which ticks count as "latest") stay vitest'able in isolation.
// `SubjectPage.tsx` filters candidate series keys through this before
// building/styling/drawing any chart series — dead PIDs never reach echarts.
import type { MetricRecord } from "../api/export.gen";
import type { SeriesIndex } from "./seriesIndex";

/**
 * A proc/* series retires once its PID hasn't reported in this many of the
 * most recent DISTINCT collection ticks (see `retireStaleSeries`). The
 * index's stored samples (`recs`/`tsMs`) are untouched — this only decides
 * what gets drawn on the chart; a reappearing PID gets its retained history
 * back.
 */
export const RETIRE_AFTER_TICKS = 3;

/** Same rule the legacy dashboard stripped for display — the only thing
 * that distinguishes a per-process metric from any other series. */
export function isProcMetric(label: string): boolean {
  return label.startsWith("proc/");
}

/** The label a series key resolves to, read off its own records (every
 * record under one index key shares the same label — see seriesKey). `null`
 * when the key has no records yet (a brand-new key not yet appended). */
function labelOf(key: string, index: SeriesIndex): string | null {
  const recs: MetricRecord[] | undefined = index.recs.get(key);
  return recs && recs.length > 0 ? recs[0].label : null;
}

function isProcSeries(key: string, index: SeriesIndex): boolean {
  const label = labelOf(key, index);
  return label !== null && isProcMetric(label);
}

/** The last (at most) `k` DISTINCT values in an ascending-sorted array, read
 * off its tail and returned in descending order. `index.tsMs` is always
 * ascending (see seriesIndex.ts), so this never has to look further back
 * than the k-1 duplicates plus k distinct values it's collecting — O(k) in
 * the common case of one sample per tick, never O(the whole series). */
function lastKDistinct(tsMs: number[], k: number): number[] {
  const out: number[] = [];
  const seen = new Set<number>();
  for (let i = tsMs.length - 1; i >= 0 && seen.size < k; i--) {
    const t = tsMs[i];
    if (!seen.has(t)) {
      seen.add(t);
      out.push(t);
    }
  }
  return out;
}

/**
 * Drops proc/* series keys whose most recent sample isn't among the latest
 * `opts.ticks` DISTINCT tick timestamps observed across this chart's proc/*
 * candidates. Non-proc keys always pass through untouched — they never
 * retire.
 *
 * A proc key with no data at all yet (not present in `index` — e.g. it
 * hasn't been appended) also passes through: there's nothing to judge it
 * stale by.
 *
 * Recomputed fresh from `index` on every call, with no retained state of its
 * own: the index keeps every sample forever (export needs it), so "the
 * latest k ticks" is always derivable from what's already there.
 *
 * O(procKeys x k), not O(total proc samples): the global top-k distinct
 * ticks can only be composed of values that are each, individually, among
 * SOME series' own latest-k distinct values. Proof: let t be one of the
 * global top-k distinct tick values, and let S be a series that reports at
 * t. At most k-1 OTHER distinct tick values (global) exceed t (t is
 * rank <= k), so S can hold at most k-1 samples newer than t — meaning t's
 * rank *within S's own timestamps* is also <= k. So instead of unioning
 * every sample of every proc series (old cost: O(total proc samples) per
 * call, walked from every chart on every live tick — the very thing this
 * retirement policy exists to keep bounded), each series only needs to
 * contribute its own tail of at most k distinct values to the candidate
 * pool the global top-k is drawn from, and membership only needs to check
 * that same small tail back against it.
 */
export function retireStaleSeries(
  keys: string[],
  index: SeriesIndex,
  opts: { ticks?: number } = {},
): string[] {
  const k = opts.ticks ?? RETIRE_AFTER_TICKS;
  const procKeys = keys.filter((key) => isProcSeries(key, index));
  if (procKeys.length === 0) return keys;

  const tails = new Map<string, number[]>();
  const candidates = new Set<number>();
  for (const key of procKeys) {
    const tail = lastKDistinct(index.tsMs.get(key) ?? [], k);
    tails.set(key, tail);
    for (const t of tail) candidates.add(t);
  }
  const latestTicks = new Set([...candidates].sort((a, b) => b - a).slice(0, k));

  return keys.filter((key) => {
    if (!isProcSeries(key, index)) return true;
    const tail = tails.get(key) ?? [];
    return tail.length === 0 || tail.some((t) => latestTicks.has(t));
  });
}
