// Pure PID-trace retirement policy for chart rendering — the Task 10 fix for
// the legacy dashboard's #1 known bug: one legend/trace per process ever
// seen accumulated without bound, growing chart div height forever. Kept
// dependency-free (no Plotly, no DOM) like grouping.ts, so the tricky bits
// (which ticks count as "latest", cap ordering) are vitest'able in
// isolation; `plotly.ts`'s `buildPanelRender` applies this policy before
// building traces/layout — `ChartPanel` never calls it directly.
import type { Point } from "./api/client";
import type { Metric } from "./grouping";

/**
 * A proc/* trace retires once its PID hasn't reported in this many of the
 * most recent DISTINCT collection ticks (see `retireStaleMetrics`). Store
 * data (`series`) is untouched by retirement — this only decides what gets
 * drawn on the chart; a reappearing PID gets its retained history back.
 */
export const RETIREMENT_K = 3;

/**
 * Legend budget, expressed in rows per the design spec. The row -> entry
 * count conversion (`* ITEMS_PER_ROW`) happens in plotly.ts, which already
 * owns that constant — keeping it out of this module avoids pulling the
 * Plotly-bundle import in just for a number.
 */
export const LEGEND_CAP_ROWS = 2;

/** Same rule `buildMetricTraces` strips for display — the only thing that
 * distinguishes a per-process metric from any other series. */
export function isProcMetric(label: string): boolean {
  return label.startsWith("proc/");
}

/**
 * Drops proc/* metrics whose most recent data point isn't among the latest
 * `k` DISTINCT tick timestamps observed across this chart's proc/* series
 * (ISO-8601 timestamps sort chronologically as strings, so a plain string
 * sort suffices). Non-proc metrics always pass through untouched — they
 * never retire.
 *
 * A proc metric with no data at all yet also passes through: there's
 * nothing to judge it stale by. This shouldn't occur via the live SSE path
 * (a metric only ever joins a chart group together with its first point),
 * but the function stays total rather than surprising a caller that hits
 * it with the earlier-than-expected empty case.
 *
 * Recomputed fresh from `seriesByLabel` on every call, with no retained
 * state of its own: `series` keeps every point forever (export needs it),
 * so "the latest k ticks" is always derivable from what's already there.
 */
export function retireStaleMetrics(
  metrics: Metric[],
  seriesByLabel: Record<string, Point[]>,
  k: number = RETIREMENT_K,
): Metric[] {
  const procMetrics = metrics.filter((m) => isProcMetric(m.label));
  if (procMetrics.length === 0) return metrics;

  const ticks = new Set<string>();
  for (const m of procMetrics) {
    for (const p of seriesByLabel[m.label] ?? []) ticks.add(p.ts);
  }
  const latestTicks = new Set([...ticks].sort().slice(-k));

  return metrics.filter((m) => {
    if (!isProcMetric(m.label)) return true;
    const pts = seriesByLabel[m.label] ?? [];
    return pts.length === 0 || pts.some((p) => latestTicks.has(p.ts));
  });
}

/** One legend candidate: a trace's identifying label plus its latest value
 * (the ranking key `selectLegendEntries` sorts by). */
export interface LegendCandidate {
  label: string;
  value: number;
}

/**
 * "Over legend budget -> keep top entries by latest value": when there are
 * more candidates than `cap`, keeps only the top `cap` ranked by `value`
 * descending; ties keep their relative input order (`Array.sort` is
 * stable). Never changes which traces are DRAWN — callers use the returned
 * set only to decide each trace's `showlegend`.
 */
export function selectLegendEntries(candidates: LegendCandidate[], cap: number): Set<string> {
  if (candidates.length <= cap) return new Set(candidates.map((c) => c.label));
  return new Set(
    [...candidates]
      .sort((a, b) => b.value - a.value)
      .slice(0, cap)
      .map((c) => c.label),
  );
}
