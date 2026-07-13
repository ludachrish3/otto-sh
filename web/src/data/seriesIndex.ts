// The per-series index: what makes live mode's costs flat in run length.
//
// Before this, `metricsForSubject` filtered the whole flat `metrics` array on every
// render (O(total points), per subject) and `healthForHosts` scanned every sample.
// Both are already slow for a long ARCHIVE; a live run just makes them fatal — the
// clock re-runs health even when no data arrives, so an O(all-points) health check
// would burn the main thread forever on a run that has gone quiet.
//
// Appends push IN PLACE (O(1)) and bump that series' `rev`. Chart memos key on the
// revision, so only charts whose series actually changed re-memo. Copying arrays to
// manufacture new identities would restore the O(all) cost we are removing.
import type { MetricRecord } from "../api/export.gen";
import type { TimeRange } from "./exportDoc";
import { parseTs } from "./time";

export interface SeriesIndex {
  /** `host/label` -> ascending sample times (ms), index-aligned with `recs`. */
  tsMs: Map<string, number[]>;
  /** `host/label` -> the records themselves. */
  recs: Map<string, MetricRecord[]>;
  /** host -> the series keys it reports. */
  keysByHost: Map<string, string[]>;
  /** `host/label` -> bumped on every append. The memo key for that series' chart. */
  rev: Map<string, number>;
}

export const seriesKey = (host: string, label: string): string => `${host}/${label}`;

function emptyIndex(): SeriesIndex {
  return {
    tsMs: new Map(),
    recs: new Map(),
    keysByHost: new Map(),
    rev: new Map(),
  };
}

export function buildIndex(metrics: MetricRecord[]): SeriesIndex {
  const index = emptyIndex();
  appendToIndex(index, metrics);
  // A fresh index starts every series at revision 0 — buildIndex is not an "append"
  // from the memo's point of view, it is the baseline.
  for (const key of index.rev.keys()) index.rev.set(key, 0);
  return index;
}

export function appendToIndex(index: SeriesIndex, metrics: MetricRecord[]): void {
  const touched = new Set<string>();
  for (const m of metrics) {
    // A row without a host can't be attributed to any subject (the same rule
    // the pre-index scans applied implicitly: `m.host !== subjectId` and
    // `hostIds.has(m.host ?? "")` both always excluded it) — so it never
    // enters the index at all.
    if (m.host == null) continue;
    const host = m.host;
    const key = seriesKey(host, m.label);
    const ts = parseTs(m.timestamp);

    let recs = index.recs.get(key);
    if (recs === undefined) {
      recs = [];
      index.recs.set(key, recs);
      index.tsMs.set(key, []);
      index.rev.set(key, 0);
      const keys = index.keysByHost.get(host);
      if (keys === undefined) index.keysByHost.set(host, [key]);
      else keys.push(key);
    }
    recs.push(m);
    // biome-ignore lint/style/noNonNullAssertion: just set above when recs was undefined
    index.tsMs.get(key)!.push(ts);
    touched.add(key);
  }
  for (const key of touched) index.rev.set(key, (index.rev.get(key) ?? 0) + 1);
}

/** Lower bound: first index whose time is >= t. Assumes `tsMs` ascending. */
function lowerBound(tsMs: number[], t: number): number {
  let lo = 0;
  let hi = tsMs.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (tsMs[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export function sliceSeries(
  index: SeriesIndex,
  key: string,
  range: TimeRange | null,
): MetricRecord[] {
  const recs = index.recs.get(key);
  if (recs === undefined) return [];
  if (range === null) return recs;
  // biome-ignore lint/style/noNonNullAssertion: tsMs and recs are always set together
  const tsMs = index.tsMs.get(key)!;
  const from = lowerBound(tsMs, range.from);
  const to = lowerBound(tsMs, range.to + 1);
  return recs.slice(from, to);
}
