// The per-subject series model (UX spec §9): metric-first tree grouped by
// chart, with the subject x source axes as node metadata. Slots are
// assigned HERE, once, from the unfiltered tree — the palette follows the
// entity, so search/chips/checkbox filtering never recolors survivors.
import type { NormalizedSession, TimeRange } from "./exportDoc";
import { parseTs } from "./time";

export interface SeriesNode {
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

  // Distinct (host, label, source) triples relevant to this subject.
  const raw = new Map<string, RawNode>();
  for (const m of session.metrics) {
    const host = m.host ?? "";
    let node: RawNode | null = null;
    if (host === subjectId) {
      node = {
        key: m.label,
        label: m.label,
        host,
        source: m.source ?? null,
        elementTarget: isElement,
      };
    } else if (members.has(host)) {
      node = {
        key: `${host}/${m.label}`,
        label: m.label,
        host,
        source: m.source ?? null,
        elementTarget: false,
      };
    }
    if (node && !raw.has(node.key)) raw.set(node.key, node);
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

/** In-range [ms, value] point arrays for the checked series keys, one pass
 * over the session's metrics, time-sorted (fixture data is generated
 * sorted; the sort is a cheap invariant guard). */
export function collectSeriesPoints(
  session: NormalizedSession,
  tree: ChartNode[],
  checked: Set<string>,
  range: TimeRange | null,
): Map<string, [number, number][]> {
  const keyOf = new Map<string, string>(); // "host|label" -> node key
  for (const chart of tree) {
    for (const s of chart.series) {
      if (checked.has(s.key)) keyOf.set(`${s.host}|${s.label}`, s.key);
    }
  }
  const out = new Map<string, [number, number][]>();
  for (const m of session.metrics) {
    const key = keyOf.get(`${m.host ?? ""}|${m.label}`);
    if (key === undefined) continue;
    const ts = parseTs(m.timestamp);
    if (range && (ts < range.from || ts > range.to)) continue;
    const arr = out.get(key);
    if (arr) arr.push([ts, m.value]);
    else out.set(key, [[ts, m.value]]);
  }
  for (const arr of out.values()) arr.sort((a, b) => a[0] - b[0]);
  return out;
}
