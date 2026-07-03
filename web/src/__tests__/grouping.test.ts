// Pins grouping.ts against the dashboard.js behaviors it ports:
// `resolvedTabMetrics`/`initTabCharts`'s chart-group Map, `initSeriesFromData`,
// and `appendMetricPoint`'s metricPlots bookkeeping — especially the tricky
// branches called out in the Phase 2 plan: placeholder replacement, a
// brand-new chart group, and a second series joining an already-plotted
// group (e.g. "Load (5m)" arriving after "Load (1m)").
import { describe, expect, it } from "vitest";

import type { ChartSpec, TabSpec } from "../api/types.gen";
import {
  appendMetricToGroups,
  buildInitialChartGroups,
  type ChartGroup,
  groupByChart,
  initSeriesFromData,
  type Metric,
  resolvedTabMetrics,
} from "../grouping";

function metric(overrides: Partial<ChartSpec> = {}): ChartSpec {
  return {
    label: "Overall CPU",
    y_title: "%",
    unit: "%",
    command: "cpu",
    chart: "cpu",
    ...overrides,
  };
}

function tab(overrides: Partial<TabSpec> = {}): TabSpec {
  return { id: "cpu", label: "CPU", metrics: ["Overall CPU"], ...overrides };
}

describe("resolvedTabMetrics (dashboard.js resolvedTabMetrics)", () => {
  it("resolves labels against meta.metrics, preserving tab.metrics order", () => {
    const metrics = [metric({ label: "b", chart: "b" }), metric({ label: "a", chart: "a" })];
    const t = tab({ metrics: ["a", "b"] });
    expect(resolvedTabMetrics(t, metrics).map((m) => m.label)).toEqual(["a", "b"]);
  });

  it("drops labels that don't resolve to a known metric", () => {
    const metrics = [metric({ label: "a", chart: "a" })];
    const t = tab({ metrics: ["a", "ghost"] });
    expect(resolvedTabMetrics(t, metrics)).toEqual([metric({ label: "a", chart: "a" })]);
  });
});

describe("groupByChart (dashboard.js initTabCharts' chartGroups Map)", () => {
  it("groups metrics sharing a chart key, preserving first-seen order", () => {
    const metrics: Metric[] = [
      { label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" },
      { label: "Load (1m)", chart: "load", y_title: "", unit: "" },
      { label: "proc/init", chart: "cpu", y_title: "%", unit: "%" },
    ];
    const groups = groupByChart(metrics);
    expect(groups.map((g) => g.chartKey)).toEqual(["cpu", "load"]);
    expect(groups[0].metrics.map((m) => m.label)).toEqual(["Overall CPU", "proc/init"]);
    expect(groups[1].metrics.map((m) => m.label)).toEqual(["Load (1m)"]);
  });
});

describe("buildInitialChartGroups (dashboard.js initTabCharts)", () => {
  it("builds one group per chart key per tab and returns the first tab id", () => {
    const meta = {
      metrics: [metric({ label: "Overall CPU", chart: "cpu" }), metric({ label: "Memory Usage", chart: "mem" })],
      tabs: [tab({ id: "cpu", metrics: ["Overall CPU"] }), tab({ id: "memory", label: "Memory", metrics: ["Memory Usage"] })],
    };
    const { groups, firstTabId } = buildInitialChartGroups(meta);
    expect(firstTabId).toBe("cpu");
    expect(groups).toEqual([
      { id: "cpu::cpu", tabId: "cpu", chartKey: "cpu", metrics: [metric({ label: "Overall CPU", chart: "cpu" })], initialized: false },
      {
        id: "memory::mem",
        tabId: "memory",
        chartKey: "mem",
        metrics: [metric({ label: "Memory Usage", chart: "mem" })],
        initialized: false,
      },
    ]);
  });

  it("skips a tab whose metrics don't resolve to anything (never gets a panel)", () => {
    const meta = {
      metrics: [metric({ label: "Overall CPU", chart: "cpu" })],
      tabs: [tab({ id: "ghost", metrics: ["nonexistent"] }), tab({ id: "cpu", metrics: ["Overall CPU"] })],
    };
    const { groups, firstTabId } = buildInitialChartGroups(meta);
    expect(groups.map((g) => g.tabId)).toEqual(["cpu"]);
    expect(firstTabId).toBe("cpu");
  });
});

describe("initSeriesFromData (dashboard.js initSeriesFromData)", () => {
  const chartMap = { "Load (1m)": "load", "Load (5m)": "load" };
  const metaMetrics = [metric({ label: "load", chart: "load", y_title: "load avg", unit: "" })];

  function placeholderGroup(): ChartGroup {
    return {
      id: "cpu::load",
      tabId: "cpu",
      chartKey: "load",
      metrics: [{ label: "load", chart: "load", y_title: "load avg", unit: "" }],
      initialized: false,
    };
  }

  it("placeholder replacement: a chart-key placeholder metric is dropped once real series arrive", () => {
    const groups = [placeholderGroup()];
    const series = { "Load (1m)": [{ ts: "t1", value: 1, meta: null }] };
    const out = initSeriesFromData(groups, chartMap, series, metaMetrics);
    expect(out[0].metrics.map((m) => m.label)).toEqual(["Load (1m)"]);
  });

  it("registers BOTH real labels for a chart, sourcing y_title/unit from meta.metrics", () => {
    const groups = [placeholderGroup()];
    const series = {
      "Load (1m)": [{ ts: "t1", value: 1, meta: null }],
      "Load (5m)": [{ ts: "t1", value: 2, meta: null }],
    };
    const out = initSeriesFromData(groups, chartMap, series, metaMetrics);
    expect(out[0].metrics).toEqual([
      { label: "Load (1m)", chart: "load", y_title: "load avg", unit: "" },
      { label: "Load (5m)", chart: "load", y_title: "load avg", unit: "" },
    ]);
  });

  it("host-prefixed series keys resolve to their bare label before chartMap lookup", () => {
    const groups = [placeholderGroup()];
    const series = { "host1/Load (1m)": [{ ts: "t1", value: 1, meta: null }] };
    const out = initSeriesFromData(groups, chartMap, series, metaMetrics);
    expect(out[0].metrics.map((m) => m.label)).toEqual(["Load (1m)"]);
  });

  it("ignores empty series arrays and series with no chartMap entry", () => {
    const groups = [placeholderGroup()];
    const series = { "Load (1m)": [], "Unrelated Metric": [{ ts: "t1", value: 1, meta: null }] };
    const out = initSeriesFromData(groups, chartMap, series, metaMetrics);
    expect(out).toBe(groups); // no group matched -> same reference, nothing changed
  });

  it("leaves unrelated groups untouched (same array reference for that entry)", () => {
    const other: ChartGroup = {
      id: "cpu::cpu",
      tabId: "cpu",
      chartKey: "cpu",
      metrics: [{ label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" }],
      initialized: false,
    };
    const groups = [other, placeholderGroup()];
    const series = { "Load (1m)": [{ ts: "t1", value: 1, meta: null }] };
    const out = initSeriesFromData(groups, chartMap, series, metaMetrics);
    expect(out[0]).toBe(other);
    expect(out[1]).not.toBe(groups[1]);
  });
});

describe("appendMetricToGroups (dashboard.js appendMetricPoint's metricPlots bookkeeping)", () => {
  const tabs: TabSpec[] = [tab({ id: "cpu", metrics: ["Overall CPU"] }), tab({ id: "memory", label: "Memory", metrics: ["Memory Usage"] })];

  it("extends an existing, initialized group when the label already belongs to it", () => {
    const groups: ChartGroup[] = [
      {
        id: "cpu::cpu",
        tabId: "cpu",
        chartKey: "cpu",
        metrics: [
          { label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" },
          { label: "proc/init", chart: "cpu", y_title: "%", unit: "%" },
        ],
        initialized: true,
      },
    ];
    const outcome = appendMetricToGroups(groups, { label: "proc/init", chart: "cpu", y_title: "%", unit: "%" }, tabs);
    expect(outcome).toEqual({ kind: "extend", groupId: "cpu::cpu", traceIndex: 1 });
  });

  it("is a no-op when the label's group exists but was never initialized (tab never visited)", () => {
    const groups: ChartGroup[] = [
      {
        id: "cpu::cpu",
        tabId: "cpu",
        chartKey: "cpu",
        metrics: [{ label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" }],
        initialized: false,
      },
    ];
    const outcome = appendMetricToGroups(groups, { label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" }, tabs);
    expect(outcome).toEqual({ kind: "noop" });
  });

  it("placeholder replacement: a new label whose group's sole metric is the chart-key placeholder swaps it in place", () => {
    const groups: ChartGroup[] = [
      {
        id: "cpu::load",
        tabId: "cpu",
        chartKey: "load",
        metrics: [{ label: "load", chart: "load", y_title: "", unit: "" }],
        initialized: true,
      },
    ];
    const outcome = appendMetricToGroups(groups, { label: "Load (1m)", chart: "load", y_title: "load avg", unit: "" }, tabs);
    expect(outcome.kind).toBe("changed");
    if (outcome.kind !== "changed") throw new Error("unreachable");
    expect(outcome.groups[0].metrics).toEqual([{ label: "Load (1m)", chart: "load", y_title: "load avg", unit: "" }]);
  });

  it("Load-series-joins-existing-chart: a second real label is appended alongside the first, not swapped", () => {
    const groups: ChartGroup[] = [
      {
        id: "cpu::load",
        tabId: "cpu",
        chartKey: "load",
        metrics: [{ label: "Load (1m)", chart: "load", y_title: "load avg", unit: "" }],
        initialized: true,
      },
    ];
    const outcome = appendMetricToGroups(groups, { label: "Load (5m)", chart: "load", y_title: "load avg", unit: "" }, tabs);
    expect(outcome.kind).toBe("changed");
    if (outcome.kind !== "changed") throw new Error("unreachable");
    expect(outcome.groups[0].metrics.map((m) => m.label)).toEqual(["Load (1m)", "Load (5m)"]);
  });

  it("new-chart-group creation: an unseen chart lands under the tab that configures its label", () => {
    const outcome = appendMetricToGroups([], { label: "Memory Usage", chart: "mem", y_title: "%", unit: "%" }, tabs);
    expect(outcome.kind).toBe("changed");
    if (outcome.kind !== "changed") throw new Error("unreachable");
    expect(outcome.groups).toEqual([
      {
        id: "memory::mem",
        tabId: "memory",
        chartKey: "mem",
        metrics: [{ label: "Memory Usage", chart: "mem", y_title: "%", unit: "%" }],
        initialized: false,
      },
    ]);
  });

  it("new-chart-group creation falls back to the first tab when no tab lists the label", () => {
    const outcome = appendMetricToGroups([], { label: "proc/newpid", chart: "cpu", y_title: "%", unit: "%" }, tabs);
    expect(outcome.kind).toBe("changed");
    if (outcome.kind !== "changed") throw new Error("unreachable");
    expect(outcome.groups[0].tabId).toBe("cpu"); // tabs[0], since no tab.metrics includes "proc/newpid"
  });

  it("is a no-op when there are no tabs at all to host a brand-new group", () => {
    const outcome = appendMetricToGroups([], { label: "Overall CPU", chart: "cpu", y_title: "%", unit: "%" }, []);
    expect(outcome).toEqual({ kind: "noop" });
  });
});
