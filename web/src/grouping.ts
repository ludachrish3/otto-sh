// Pure chart-group assembly + live-append bookkeeping, ported from
// src/otto/monitor/static/dashboard.js's `initTabCharts()` /
// `initSeriesFromData()` / `appendMetricPoint()` (the `state.metricPlots`
// management half of each — trace/layout rendering lives in `plotly.ts`,
// and the imperative Plotly calls + DOM refs live in `ChartGrid.tsx`/
// `ChartPanel.tsx`). Kept dependency-free (no DOM, no Plotly, no zustand) so
// the tricky branches — placeholder replacement, brand-new chart-group
// creation, and a second series joining an already-plotted group (e.g.
// "Load (5m)" arriving after "Load (1m)") — are vitest'able in isolation.
import type { Point } from "./api/client";
import type { ChartSpec, MonitorDashboardApiMetaPayload, TabSpec } from "./api/types.gen";

/**
 * The subset of a `ChartSpec` a chart group needs to render a trace.
 * Live-appended metrics (dashboard.js's inline `{label, chart, y_title,
 * unit}` object literals inside `appendMetricPoint`) never carry
 * `command`/`interval`, so this is deliberately narrower than `ChartSpec`
 * rather than reusing it wholesale.
 */
export interface Metric {
  label: string;
  chart: string;
  y_title: string;
  unit: string;
}

/**
 * One Plotly chart's worth of state — dashboard.js's `state.metricPlots[i]`
 * minus the DOM node (owned by `ChartPanel`) and plus a stable `id` (legacy
 * has no equivalent; it relied on DOM node identity, which React can't key
 * on before the node exists).
 */
export interface ChartGroup {
  id: string;
  tabId: string;
  chartKey: string;
  /** Invariant: never empty — every group is created with (and only ever
   * grows or swaps within) at least one metric. */
  metrics: Metric[];
  initialized: boolean;
}

function makeGroupId(tabId: string, chartKey: string): string {
  return `${tabId}::${chartKey}`;
}

/**
 * dashboard.js's `resolvedTabMetrics()`: `tab.metrics` (label strings)
 * resolved against `meta.metrics`, preserving order, dropping labels that
 * don't resolve to a known metric.
 */
export function resolvedTabMetrics(tab: TabSpec, metrics: ChartSpec[]): Metric[] {
  const byLabel = new Map(metrics.map((m) => [m.label, m]));
  const out: Metric[] = [];
  for (const label of tab.metrics) {
    const m = byLabel.get(label);
    if (m) out.push(m);
  }
  return out;
}

/**
 * dashboard.js's `chartGroups` Map built inside `initTabCharts()`: groups a
 * tab's resolved metrics by `chart` key, preserving first-seen order.
 */
export function groupByChart(metrics: Metric[]): { chartKey: string; metrics: Metric[] }[] {
  const order: string[] = [];
  const byChart = new Map<string, Metric[]>();
  for (const metric of metrics) {
    let bucket = byChart.get(metric.chart);
    if (!bucket) {
      bucket = [];
      byChart.set(metric.chart, bucket);
      order.push(metric.chart);
    }
    bucket.push(metric);
  }
  return order.map((chartKey) => ({ chartKey, metrics: byChart.get(chartKey) as Metric[] }));
}

/**
 * dashboard.js's `initTabCharts()` tab loop (minus DOM creation): one
 * `ChartGroup` per chart key per tab that resolves at least one metric,
 * tabs in `meta.tabs` order. Also returns the first tab id — legacy
 * auto-activates it once the chart groups are built
 * (`if (firstTabId) activateTab(firstTabId);`).
 */
export function buildInitialChartGroups(
  meta: Pick<MonitorDashboardApiMetaPayload, "tabs" | "metrics">,
): { groups: ChartGroup[]; firstTabId: string | null } {
  const groups: ChartGroup[] = [];
  let firstTabId: string | null = null;
  for (const tab of meta.tabs) {
    const tabMetrics = resolvedTabMetrics(tab, meta.metrics);
    if (tabMetrics.length === 0) continue;
    for (const { chartKey, metrics } of groupByChart(tabMetrics)) {
      groups.push({ id: makeGroupId(tab.id, chartKey), tabId: tab.id, chartKey, metrics, initialized: false });
    }
    if (firstTabId === null) firstTabId = tab.id;
  }
  return { groups, firstTabId };
}

/**
 * dashboard.js's `initSeriesFromData()`: scans already-loaded series keys
 * (historical preload, or `/api/data`'s snapshot) and registers any series
 * label not yet represented in its chart group. This is how "extra"
 * per-process (`proc/*`) and multi-label (`Load (1m)`/`Load (5m)`) series
 * show up even though `meta.metrics`/`resolvedTabMetrics` only seeds one
 * placeholder entry per chart. Returns a NEW groups array; a group is only
 * replaced (new object identity) when its metrics actually changed, so
 * unaffected panels don't needlessly re-render.
 */
export function initSeriesFromData(
  groups: ChartGroup[],
  chartMap: Record<string, string>,
  series: Record<string, Point[]>,
  metaMetrics: ChartSpec[],
): ChartGroup[] {
  const order: string[] = [];
  const byChart = new Map<string, string[]>();
  for (const key of Object.keys(series)) {
    if (series[key].length === 0) continue;
    const slash = key.indexOf("/");
    const label = slash >= 0 ? key.slice(slash + 1) : key;
    const chart = chartMap[label];
    if (!chart) continue;
    let labels = byChart.get(chart);
    if (!labels) {
      labels = [];
      byChart.set(chart, labels);
      order.push(chart);
    }
    if (!labels.includes(label)) labels.push(label);
  }

  let next = groups;
  for (const chart of order) {
    const labels = byChart.get(chart) as string[];
    const idx = next.findIndex((g) => g.metrics.some((m) => m.chart === chart));
    if (idx < 0) continue;
    const metaMeta = metaMetrics.find((m) => m.chart === chart);
    let metrics = next[idx].metrics;

    // Remove a placeholder: a metric whose label IS the chart key but isn't
    // itself a real series in chartMap (a stand-in seeded from meta.metrics
    // before any data arrived for the real, differently-labeled series).
    const placeholderIdx = metrics.findIndex(
      (m) => m.label === chart && !Object.prototype.hasOwnProperty.call(chartMap, m.label),
    );
    if (placeholderIdx >= 0) {
      metrics = metrics.filter((_, i) => i !== placeholderIdx);
    }

    for (const label of labels) {
      if (!metrics.some((m) => m.label === label)) {
        metrics = [...metrics, { label, chart, y_title: metaMeta?.y_title ?? "", unit: metaMeta?.unit ?? "" }];
      }
    }

    if (metrics !== next[idx].metrics) {
      const updated: ChartGroup = { ...next[idx], metrics };
      next = next.map((g, i) => (i === idx ? updated : g));
    }
  }
  return next;
}

export type AppendOutcome =
  | { kind: "extend"; groupId: string; traceIndex: number }
  | { kind: "changed"; groups: ChartGroup[] }
  | { kind: "noop" };

/**
 * The chart-group-management half of dashboard.js's `appendMetricPoint()`
 * (everything except the `state.paused`/`host === state.selectedHost` gates
 * and the actual `Plotly.extendTraces`/`Plotly.react` calls, which are
 * `ChartGrid`'s job since they're side effects on real DOM nodes this
 * module never touches). Three legacy branches, three outcomes:
 *
 *  - the label already belongs to a group: `"extend"` if that group is
 *    already plotted (fast path — a single new point on an existing
 *    trace), else `"noop"` (the point already lives in the store; the
 *    eventual lazy `initTabPlots`-equivalent will draw it in full once the
 *    tab is visited).
 *  - another metric sharing the same `chart` already has a group (e.g.
 *    "Load (5m)" arriving after "Load (1m)"): `"changed"`, with the new
 *    metric appended, or swapped into a placeholder slot if one is present.
 *  - neither: a brand-new chart group is created under the first tab that
 *    lists this label, falling back to the first tab at all (mirrors
 *    dashboard.js's `tabs.find(...) || state.meta.tabs?.[0]`), or `"noop"`
 *    if there are no tabs to host it.
 *
 * Deliberately does NOT replicate dashboard.js's redundant trailing
 * `Plotly.extendTraces` call that fires (in the legacy source) after the
 * "changed" branches' full `Plotly.react` rebuild — that rebuild already
 * draws the just-appended point (it reads the same, already-updated,
 * `state.series`), so the trailing extend duplicates the newest point on
 * that trace. No pin exercises this path; it reads as an unintentional
 * double-apply rather than a deliberate behavior, so `ChartGrid`'s
 * "changed" handling (a single full `react()` rebuild) is the only Plotly
 * call issued for those two branches.
 */
export function appendMetricToGroups(groups: ChartGroup[], msg: Metric, tabs: TabSpec[]): AppendOutcome {
  const { label, chart, y_title, unit } = msg;

  const existing = groups.find((g) => g.metrics.some((m) => m.label === label));
  if (existing) {
    if (!existing.initialized) return { kind: "noop" };
    const traceIndex = existing.metrics.findIndex((m) => m.label === label);
    return { kind: "extend", groupId: existing.id, traceIndex };
  }

  const chartGroupIdx = groups.findIndex((g) => g.metrics[0]?.chart === chart);
  if (chartGroupIdx >= 0) {
    const target = groups[chartGroupIdx];
    const metric: Metric = { label, chart, y_title, unit };
    const placeholderIdx = target.metrics.findIndex((m) => m.label === chart && label !== chart);
    const metrics =
      placeholderIdx >= 0
        ? target.metrics.map((m, i) => (i === placeholderIdx ? metric : m))
        : [...target.metrics, metric];
    const groupsOut = groups.map((g, i) => (i === chartGroupIdx ? { ...g, metrics } : g));
    return { kind: "changed", groups: groupsOut };
  }

  const tab = tabs.find((t) => t.metrics.includes(label)) ?? tabs[0];
  if (!tab) return { kind: "noop" };
  const newGroup: ChartGroup = {
    id: makeGroupId(tab.id, chart),
    tabId: tab.id,
    chartKey: chart,
    metrics: [{ label, chart, y_title, unit }],
    initialized: false,
  };
  return { kind: "changed", groups: [...groups, newGroup] };
}
