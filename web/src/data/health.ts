// Derived health (contract spec 2026-07-10 §6): a pure function of
// (samples, selected range, cadences). Nothing is stored — "last known
// status" means last known WITHIN THE SELECTED RANGE, so narrowing the
// range re-evaluates it. The same functions also drive live mode's
// unreachable-dimming (Plan 5b): OverviewPage passes a live `nowMs` from
// data/clock.ts instead of leaving it to default to the session's endMs.
import type { DerivedElement, NormalizedSession, TimeRange } from "./exportDoc";

/** Down when the gap past the last sample exceeds K x the host's cadence. */
export const HEALTH_K = 3;

type HealthStatus = "ok" | "down" | "no-data" | "unknown";

export interface SubjectHealth {
  status: HealthStatus;
  lastSeenMs: number | null;
  outageMs: number;
}

export interface Headline {
  text: string;
  chartKey: string;
}

/** Fastest collection cadence (ms) among the charts this host reports,
 * via label -> chartMap -> spec.interval, falling back to the global
 * interval. null = unresolvable (no intervals anywhere). */
function cadenceMs(session: NormalizedSession, labels: Set<string>): number | null {
  const chartLabels = new Set([...labels].map((l) => session.chartMap[l] ?? l));
  const intervals = session.meta.charts
    .filter((c) => chartLabels.has(c.label))
    .map((c) => c.interval ?? session.meta.interval)
    .filter((v): v is number => v != null);
  if (intervals.length > 0) return Math.min(...intervals) * 1000;
  return session.meta.interval != null ? session.meta.interval * 1000 : null;
}

/** The latest sample at or before `t` — its timestamp and its position in the
 * series — or null when every sample is after `t`. Binary search: O(log n)
 * per series, never a scan of the series itself.
 *
 * Returns the TIMESTAMP rather than the bare index the callers used to read
 * back out of `tsMs` themselves. "-1 means nothing found" is a convention
 * every caller had to remember and re-check; `null` is one the type enforces.
 * `index` comes along only for the one caller that must reach the aligned
 * record array. */
function lastSampleAtOrBefore(tsMs: number[], t: number): { index: number; ts: number } | null {
  let lo = 0;
  let hi = tsMs.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const at = tsMs[mid];
    // `at !== undefined &&` is exactly the old `tsMs[mid] <= t`: a missing
    // slot compared false and took the `else` too.
    if (at !== undefined && at <= t) lo = mid + 1;
    else hi = mid;
  }
  const index = lo - 1;
  const ts = tsMs[index];
  return ts === undefined ? null : { index, ts };
}

/** Health of a single host (spec §6, same rule as healthForHosts — this IS
 * the rule, not a parallel copy of it). healthForHosts is a loop over this. */
export function healthForHost(
  session: NormalizedSession,
  hostId: string,
  range: TimeRange | null,
  /** Wall clock for live mode. Defaults to the session's end — which is what an
   * ARCHIVE means by "now". A live session's end is open, so if we defaulted there
   * the gap would always be zero and a dead host would never go amber. */
  nowMs?: number,
): SubjectHealth {
  const evalFrom = range?.from ?? session.startMs;
  const evalTo = nowMs ?? Math.min(range?.to ?? session.endMs, session.endMs);

  // The series this host reports, anywhere in the session — cadence must
  // not depend on the range, so this comes from the index, not a scan.
  const keys = session.index.keysByHost.get(hostId);
  if (!keys || keys.length === 0) {
    // No metric series at all in this session: log-only or silent.
    // No health claim either way (spec: absence of logs proves nothing).
    return { status: "unknown", lastSeenMs: null, outageMs: 0 };
  }

  // Last sample WITHIN [evalFrom, evalTo], per series via binary search,
  // then the max across the host's series — same universe of samples the
  // old full scan visited, just reached by index lookup instead of a scan.
  let last: number | null = null;
  const labels = new Set<string>();
  for (const key of keys) {
    labels.add(key.slice(hostId.length + 1));
    // biome-ignore lint/style/noNonNullAssertion: tsMs always has an entry for every key in keysByHost
    const tsMs = session.index.tsMs.get(key)!;
    const found = lastSampleAtOrBefore(tsMs, evalTo);
    if (found === null || found.ts < evalFrom) continue;
    if (last === null || found.ts > last) last = found.ts;
  }

  if (last === null) {
    return { status: "no-data", lastSeenMs: null, outageMs: 0 };
  }
  // Live sessions know their cadence directly (meta.interval, set from the
  // collector). Archives that lack a global interval fall back to the
  // per-chart scan.
  const cadence =
    session.meta.interval !== null ? session.meta.interval * 1000 : cadenceMs(session, labels);
  if (cadence === null) {
    return { status: "unknown", lastSeenMs: last, outageMs: 0 };
  }
  const gap = evalTo - last;
  const down = gap > HEALTH_K * cadence;
  return {
    status: down ? "down" : "ok",
    lastSeenMs: last,
    outageMs: down ? gap : 0,
  };
}

export function healthForHosts(
  session: NormalizedSession,
  range: TimeRange | null,
  /** Wall clock for live mode. Defaults to the session's end — which is what an
   * ARCHIVE means by "now". A live session's end is open, so if we defaulted there
   * the gap would always be zero and a dead host would never go amber. */
  nowMs?: number,
): Map<string, SubjectHealth> {
  const out = new Map<string, SubjectHealth>();
  for (const host of session.lab.hosts) {
    out.set(host.id, healthForHost(session, host.id, range, nowMs));
  }
  return out;
}

/** Member healths in slot-then-id order — the segmented rollup bar's input
 * and the element's health indicator everywhere (UX spec §8). */
export function elementRollup(
  element: DerivedElement,
  healths: Map<string, SubjectHealth>,
  session?: NormalizedSession,
): SubjectHealth[] {
  const bySlot = (id: string): number => {
    const host = session?.lab.hosts.find((h) => h.id === id);
    return host?.slot ?? Number.POSITIVE_INFINITY;
  };
  return [...element.hostIds]
    .sort((a, b) => bySlot(a) - bySlot(b) || a.localeCompare(b))
    .map((id) => healths.get(id) ?? { status: "unknown", lastSeenMs: null, outageMs: 0 });
}

function formatValue(value: number): string {
  return value >= 10 ? String(Math.round(value)) : String(Math.round(value * 10) / 10);
}

/** What makes a "%" chart the CPU one, for `headlineFor`'s preference sort.
 * Module-level because it is consulted twice per comparison inside a sort
 * comparator, i.e. O(n log n) times per call. */
const CPU_LABEL = /cpu/i;

/** Labeled headline metric for a host tile (UX spec §8): CPU-preferred,
 * else the first meta-order chart with in-range samples. The LABEL matters
 * because of the fallback — "34% cpu", "7212 rpm fan".
 *
 * OverviewPage is the fleet grid — the page the liveness clock re-renders on
 * every collection tick — so this must not scan `session.metrics` (that was
 * O(all points) x hosts, per tick, once this task's caller went live). Reads
 * through the index instead, the same way metricsForSubject and
 * healthForHosts do: only this host's own series, and a binary search per
 * series rather than a linear scan of it. */
export function headlineFor(
  session: NormalizedSession,
  hostId: string,
  range: TimeRange | null,
): Headline | null {
  const evalFrom = range?.from ?? session.startMs;
  const evalTo = Math.min(range?.to ?? session.endMs, session.endMs);
  const specs = [...session.meta.charts].sort((a, b) => {
    const cpuA = a.unit === "%" && CPU_LABEL.test(a.label) ? 0 : 1;
    const cpuB = b.unit === "%" && CPU_LABEL.test(b.label) ? 0 : 1;
    return cpuA - cpuB;
  });

  const keys = session.index.keysByHost.get(hostId) ?? [];

  for (const spec of specs) {
    let best: { ts: number; value: number } | null = null;
    for (const key of keys) {
      // seriesKey is `${host}/${label}` (seriesIndex.ts) — same slice
      // healthForHosts already uses to recover the raw label from a key.
      const rawLabel = key.slice(hostId.length + 1);
      if ((session.chartMap[rawLabel] ?? rawLabel) !== spec.label) continue;
      // biome-ignore lint/style/noNonNullAssertion: tsMs/recs always have an entry for every key in keysByHost
      const tsMs = session.index.tsMs.get(key)!;
      const found = lastSampleAtOrBefore(tsMs, evalTo);
      if (found === null || found.ts < evalFrom) continue;
      // seriesIndex.ts builds `tsMs` and `recs` together, one entry per
      // sample, so a position found in one indexes the other. Stating that
      // here — and naming it if it is ever untrue — replaces a `!` that
      // asserted the same thing silently and would have surfaced as a bare
      // "cannot read properties of undefined" three frames away.
      const recs = session.index.recs.get(key);
      const rec = recs?.[found.index];
      if (rec === undefined) {
        throw new Error(
          `headlineFor: series "${key}" has ${recs?.length ?? 0} record(s) for ` +
            `${tsMs.length} timestamp(s) — seriesIndex must keep them aligned`,
        );
      }
      if (!best || found.ts > best.ts) best = { ts: found.ts, value: rec.value };
    }
    if (best) {
      const unit = spec.unit === "%" ? "%" : ` ${spec.unit}`;
      return { text: `${formatValue(best.value)}${unit} ${spec.chart}`, chartKey: spec.chart };
    }
  }
  return null;
}
