// The per-subject series model (UX spec §9): metric-first tree grouped by
// chart, with the subject x source axes as node metadata. Slots are
// assigned HERE, once, from the unfiltered tree — the palette follows the
// entity, so search/chips/checkbox filtering never recolors survivors.

import { MAX_SERIES_PER_CHART } from "../charts/palette";
import type { NormalizedSession, TimeRange } from "./exportDoc";
import { seriesKey, sliceSeries } from "./seriesIndex";
import { parseTs } from "./time";

// Not exported: reachable through ChartNode.series; no external importer
// exists, and knip enforces that (same rule as EVENT_COLOR_SWATCHES).
interface SeriesNode {
  key: string;
  label: string;
  host: string;
  source: string | null;
  slot: number;
}

export interface ChartNode {
  chartKey: string;
  chartLabel: string;
  unit: string;
  yTitle: string;
  maxSeries: number | null;
  series: SeriesNode[];
}

interface RawNode {
  key: string;
  label: string;
  host: string;
  source: string | null;
  elementTarget: boolean;
}

export function buildSeriesTree(session: NormalizedSession, subjectId: string): ChartNode[] {
  const isElement = session.elementIds.has(subjectId) && !session.hostIds.has(subjectId);
  const element = session.elements.find((e) => e.id === subjectId);
  const members = new Set(isElement ? (element?.hostIds ?? []) : []);

  // Distinct (host, label, source) triples relevant to this subject — walked
  // off the per-series index (O(this subject's own + its members' series)),
  // NOT a scan of session.metrics. This runs on every SubjectPage render —
  // i.e. every live tick, for however long the page stays open — so an
  // O(total session points) scan here is the exact same fatal cliff
  // metricsForSubject/healthForHosts were switched to the index to avoid
  // (see seriesIndex.ts's header); a sustained live stream (Plan 5b Task 13's
  // replay soak) reliably found the main thread falling permanently behind
  // once session.metrics reached ~10^5 points, well before any real run
  // would generate that much.
  // subjectId itself is always scanned: an element can carry its OWN
  // directly-attached series (metrics recorded with `host` == the element's
  // own id, e.g. a chassis-level "ambient" sensor with no member host of
  // its own) on top of whatever its member hosts report.
  const relevantHosts = isElement ? [subjectId, ...members] : [subjectId];
  const raw = new Map<string, RawNode>();
  for (const host of relevantHosts) {
    for (const key of session.index.keysByHost.get(host) ?? []) {
      // Every record under one index key shares the same label; the FIRST
      // one determines `.source` here too, exactly as the pre-index scan's
      // "first metric seen for this (host,label) wins" `!raw.has(...)` guard
      // did (iteration order differs — index-of-this-host vs global metric
      // arrival order — but insertion order into `raw` never affects the
      // output, which is re-sorted below regardless).
      const rec = session.index.recs.get(key)?.[0];
      if (rec === undefined) continue;
      const node: RawNode =
        host === subjectId
          ? {
              key: rec.label,
              label: rec.label,
              host,
              source: rec.source ?? null,
              elementTarget: isElement,
            }
          : {
              key: `${host}/${rec.label}`,
              label: rec.label,
              host,
              source: rec.source ?? null,
              elementTarget: false,
            };
      if (!raw.has(node.key)) raw.set(node.key, node);
    }
  }

  // Group by chart label -> spec.
  const groups = new Map<string, RawNode[]>();
  for (const node of raw.values()) {
    const chartLabel = session.chartMap[node.label] ?? node.label;
    const list = groups.get(chartLabel);
    if (list) list.push(node);
    else groups.set(chartLabel, [node]);
  }

  const out: ChartNode[] = [];
  for (const [chartLabel, nodes] of groups) {
    const spec = session.meta.charts.find((c) => c.label === chartLabel);
    nodes.sort((a, b) => {
      if (a.elementTarget !== b.elementTarget) return a.elementTarget ? -1 : 1;
      return a.host.localeCompare(b.host) || a.label.localeCompare(b.label);
    });
    out.push({
      chartKey: spec?.chart ?? chartLabel,
      chartLabel,
      unit: spec?.unit ?? "",
      yTitle: spec?.y_title ?? chartLabel,
      maxSeries: spec?.max_series === undefined ? MAX_SERIES_PER_CHART : spec.max_series,
      series: nodes.map((n, i) => ({
        key: n.key,
        label: n.label,
        host: n.host,
        source: n.source,
        slot: i,
      })),
    });
  }
  // Chart order: meta.charts order first, unknown charts after, by label.
  const orderOf = (key: string): number => {
    const idx = session.meta.charts.findIndex((c) => c.chart === key);
    return idx === -1 ? Number.POSITIVE_INFINITY : idx;
  };
  return out.sort(
    (a, b) => orderOf(a.chartKey) - orderOf(b.chartKey) || a.chartLabel.localeCompare(b.chartLabel),
  );
}

export function filterTree(
  tree: ChartNode[],
  opts: { search: string; chips: Set<string> | null; source: string | null },
): ChartNode[] {
  const needle = opts.search.trim().toLowerCase();
  const out: ChartNode[] = [];
  for (const chart of tree) {
    if (opts.chips && !opts.chips.has(chart.chartKey)) continue;
    const chartHit = needle === "" || chart.chartLabel.toLowerCase().includes(needle);
    const series = chart.series.filter((s) => {
      if (opts.source !== null && s.source !== opts.source) return false;
      if (chartHit) return true;
      return s.label.toLowerCase().includes(needle) || s.host.toLowerCase().includes(needle);
    });
    if (series.length) out.push({ ...chart, series });
  }
  return out;
}

export function sourcesIn(tree: ChartNode[]): string[] {
  const set = new Set<string>();
  for (const chart of tree) {
    for (const s of chart.series) if (s.source !== null) set.add(s.source);
  }
  return [...set].sort();
}

/** In-range [ms, value] point arrays for the checked series keys — sliced
 * directly off the per-series index (`sliceSeries`, a binary search per
 * key), not a pass over the session's whole metrics array. Same rationale
 * as `buildSeriesTree` above: this is called straight from SubjectPage's
 * render body (no memo), so on a long live run an O(total points) scan here
 * runs on literally every tick. `sliceSeries`'s binary search relies on
 * each series' samples being time-ascending in the index, same invariant
 * every other index reader (health.ts, seriesIndex.ts itself) already
 * trusts rather than re-verifying. */
export function collectSeriesPoints(
  session: NormalizedSession,
  tree: ChartNode[],
  checked: Set<string>,
  range: TimeRange | null,
): Map<string, [number, number][]> {
  const out = new Map<string, [number, number][]>();
  for (const chart of tree) {
    for (const s of chart.series) {
      if (!checked.has(s.key)) continue;
      const recs = sliceSeries(session.index, seriesKey(s.host, s.label), range);
      out.set(
        s.key,
        recs.map((r): [number, number] => [parseTs(r.timestamp), r.value]),
      );
    }
  }
  return out;
}
