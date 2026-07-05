// Tabs → `#tab-<id>` panels + `.tab-charts` containers, built from
// `meta.tabs`/`meta.metrics` (dashboard.js's `initTabCharts()`), plus the
// live-append wiring (dashboard.js's `appendMetricPoint()`) and the
// theme/pause-resume/host-switch/expand-collapse full-refresh triggers
// (`refreshPlot()`'s callers). `grouping.ts` owns the pure chart-group
// bookkeeping; this component owns the DOM/Plotly side effects and store
// subscriptions.
import { useCallback, useEffect, useRef, useState } from "react";

import {
  appendMetricToGroups,
  buildInitialChartGroups,
  type ChartGroup,
  initSeriesFromData,
} from "../grouping";
import { buildExtendUpdate, buildPanelRender, plotly } from "../plotly";
import { isProcMetric } from "../retirement";
import { useMonitorStore } from "../store";
import { onThemeChange } from "../theme";
import ChartPanel from "./ChartPanel";
import EventTable from "./EventTable";

function ChartGrid() {
  const meta = useMonitorStore((s) => s.meta);
  const series = useMonitorStore((s) => s.series);
  const chartMap = useMonitorStore((s) => s.chartMap);
  const events = useMonitorStore((s) => s.events);
  const activeTab = useMonitorStore((s) => s.activeTab);
  const selectedHost = useMonitorStore((s) => s.selectedHost);
  const paused = useMonitorStore((s) => s.paused);
  const lastMetric = useMonitorStore((s) => s.lastMetric);
  const selectTab = useMonitorStore((s) => s.actions.selectTab);
  const chartsReady = useMonitorStore((s) => s.actions.chartsReady);
  const openPopover = useMonitorStore((s) => s.actions.openPopover);

  const [groups, setGroups] = useState<ChartGroup[]>([]);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [expandedGroupId, setExpandedGroupId] = useState<string | null>(null);

  // dashboard.js's `state.metricPlots.length === 0` check — true until the
  // chart-group structure has been built for the first time.
  const builtRef = useRef(false);
  const prevHostRef = useRef(selectedHost);
  const prevPausedRef = useRef(paused);
  const prevEventsRef = useRef(events);

  // "Latest ref" pattern: the live-append effect below fires exactly once
  // per `lastMetric` (its only dependency), so it reads everything else
  // through this ref to avoid stale closures without re-running on every
  // unrelated render.
  const latest = useRef({ groups, paused, selectedHost, meta });
  latest.current = { groups, paused, selectedHost, meta };

  const divs = useRef(new Map<string, HTMLDivElement>());
  const registerDiv = useCallback((id: string, div: HTMLDivElement | null) => {
    if (div) divs.current.set(id, div);
    else divs.current.delete(id);
  }, []);
  const markInitialized = useCallback((id: string) => {
    setGroups((gs) => gs.map((g) => (g.id === id ? { ...g, initialized: true } : g)));
  }, []);

  // dashboard.js's `initTabCharts()`: build the chart-group structure once —
  // immediately for historical (`meta.hosts.length === 0`), or on the first
  // host pick in live mode (mirrors `populateHostSelect`'s change handler:
  // `if (state.metricPlots.length === 0) initTabCharts();`).
  useEffect(() => {
    if (!meta || builtRef.current) return;
    const historical = meta.hosts.length === 0;
    if (!historical && selectedHost === null) return;
    const { groups: initial, firstTabId } = buildInitialChartGroups(meta);
    setGroups(initSeriesFromData(initial, chartMap, series, meta.metrics));
    builtRef.current = true;
    // dashboard.js's `initTabCharts()`'s unconditional
    // `clear-events-btn.disabled = false` — fires whether or not there
    // turned out to be a first tab to activate.
    chartsReady();
    const initialTab = firstTabId ?? meta.tabs.find((t) => t.kind === "table")?.id;
    if (initialTab) selectTab(initialTab);
  }, [meta, selectedHost, chartMap, series, selectTab, chartsReady]);

  // dashboard.js's host-select change handler's `else refreshPlot();`
  // branch: once charts already exist, switching hosts re-renders in place
  // rather than rebuilding the tab/chart structure.
  useEffect(() => {
    if (builtRef.current && prevHostRef.current !== selectedHost) {
      setRefreshEpoch((e) => e + 1);
    }
    prevHostRef.current = selectedHost;
  }, [selectedHost]);

  // dashboard.js's pause-btn handler: resuming triggers `refreshPlot()`
  // ("a full refresh from state" — the frozen SSE points already landed in
  // the store while paused; this is what draws them).
  useEffect(() => {
    if (builtRef.current && prevPausedRef.current && !paused) {
      setRefreshEpoch((e) => e + 1);
    }
    prevPausedRef.current = paused;
  }, [paused]);

  // dashboard.js's `addEventToPlot()` / the SSE `event_updated`/`event_deleted`
  // branches: a new/changed/removed event redraws every initialized panel's
  // shapes+annotations (and the topMargin/height they drive). Legacy gates
  // ONLY the add case on `state.paused` (`addEventToPlot()`'s
  // `if (state.paused) return;` early-return, BEFORE its `refreshPlot()`
  // call) — the `event_updated`/`event_deleted` branches in `src.onmessage`
  // call `refreshPlot()` unconditionally, with no pause check at all. This
  // is byte-faithful to that asymmetry: an add is detected as a length
  // increase over the previous `events` array (the store's `eventMsg`
  // reducer only ever pushes, so a longer array means a new event arrived,
  // never an update/delete), and only that case is skipped while paused.
  useEffect(() => {
    if (builtRef.current && prevEventsRef.current !== events) {
      const isAdd = events.length > prevEventsRef.current.length;
      if (!(isAdd && paused)) {
        setRefreshEpoch((e) => e + 1);
      }
    }
    prevEventsRef.current = events;
  }, [events, paused]);

  // dashboard.js's theme-btn handler's trailing `refreshPlot()` call.
  useEffect(
    () =>
      onThemeChange(() => {
        setRefreshEpoch((e) => e + 1);
      }),
    [],
  );

  // dashboard.js's `collapseExpanded()`'s trailing `refreshPlot()` call —
  // restores natural (unexpanded) layout heights once nothing is expanded.
  // (Switching directly from one expanded chart to another skips this
  // interim refresh — a harmless simplification versus legacy's
  // collapse-then-reexpand, with no pin coverage on the transient state.)
  useEffect(() => {
    if (expandedGroupId === null && builtRef.current) setRefreshEpoch((e) => e + 1);
  }, [expandedGroupId]);

  useEffect(() => {
    document.body.classList.toggle("plot-expanded", expandedGroupId !== null);
  }, [expandedGroupId]);

  // dashboard.js's top-level Escape keydown handler.
  useEffect(() => {
    function onKeydown(e: KeyboardEvent): void {
      if (e.key === "Escape" && expandedGroupId !== null) setExpandedGroupId(null);
    }
    document.addEventListener("keydown", onKeydown);
    return () => document.removeEventListener("keydown", onKeydown);
  }, [expandedGroupId]);

  // dashboard.js's `appendMetricPoint()`: the store's `metricMsg` reducer
  // always appends the point to `series` regardless of pause/host — this
  // effect is the `if (state.paused) return;` / `if (host === state.
  // selectedHost)` gate, plus the metricPlots bookkeeping (`grouping.ts`)
  // and the resulting Plotly call (extend fast path, or a `setGroups` that
  // lets `ChartPanel` do the full rebuild for a structural change).
  // biome-ignore lint/correctness/useExhaustiveDependencies: fires on [lastMetric] only; sibling state (groups/paused/host/meta) is read fresh via latest.current, mirroring dashboard.js's appendMetricPoint gate.
  useEffect(() => {
    if (!lastMetric) return;
    const {
      groups: current,
      paused: isPaused,
      selectedHost: host,
      meta: currentMeta,
    } = latest.current;
    if (isPaused || lastMetric.host !== host) return;
    const outcome = appendMetricToGroups(current, lastMetric, currentMeta?.tabs ?? []);
    if (outcome.kind === "changed") {
      setGroups(outcome.groups);
    } else if (outcome.kind === "extend") {
      const div = divs.current.get(outcome.groupId);
      if (!div) return;
      const targetGroup = current.find((g) => g.id === outcome.groupId);
      // Task 10's retirement policy can reshuffle which traces are actually
      // drawn on any new proc/* tick — a fresh point can push an older PID
      // out of the latest-K window, or bring a retired one back — so the
      // raw index-based extend fast path (traceIndex into the group's FULL,
      // never-trimmed metrics list) is only safe for groups retirement never
      // touches at all (no proc/* metrics). A group with at least one
      // proc/* metric always gets a full rebuild instead, via the same
      // buildPanelRender ChartPanel's own effects use, so it re-derives the
      // current retired-set + legend cap fresh rather than trusting a stale
      // index.
      if (targetGroup?.metrics.some((m) => isProcMetric(m.label))) {
        const { traces, layout } = buildPanelRender(
          targetGroup.metrics,
          series,
          host,
          events,
          targetGroup.metrics[0].y_title,
        );
        void plotly.react(div, traces, layout);
      } else {
        plotly.extendTraces(div, buildExtendUpdate(lastMetric), [outcome.traceIndex]);
      }
    }
  }, [lastMetric]);

  if (!meta) return null;

  const tabIds = new Set(groups.map((g) => g.tabId));
  const visibleTabs = meta.tabs.filter((t) => tabIds.has(t.id));

  return (
    <>
      {visibleTabs.map((tab) => {
        const tabGroups = groups.filter((g) => g.tabId === tab.id);
        const isActive = activeTab === tab.id;
        const sectionExpanded = tabGroups.some((g) => g.id === expandedGroupId);
        return (
          <div
            key={tab.id}
            id={`tab-${tab.id}`}
            className={isActive ? "tab-panel active" : "tab-panel"}
          >
            <div
              id={`charts-${tab.id}`}
              className={sectionExpanded ? "tab-charts expanded-section" : "tab-charts"}
            >
              {tabGroups.map((group) => (
                <ChartPanel
                  key={group.id}
                  group={group}
                  active={isActive}
                  series={series}
                  selectedHost={selectedHost}
                  events={events}
                  refreshEpoch={refreshEpoch}
                  expanded={group.id === expandedGroupId}
                  onToggleExpand={() => {
                    setExpandedGroupId((cur) => (cur === group.id ? null : group.id));
                  }}
                  onInitialized={markInitialized}
                  registerDiv={registerDiv}
                  onAnnotationClick={openPopover}
                />
              ))}
            </div>
          </div>
        );
      })}
      {meta.tabs
        .filter((t) => t.kind === "table")
        .map((tab) => (
          <div
            key={tab.id}
            id={`tab-${tab.id}`}
            className={activeTab === tab.id ? "tab-panel active" : "tab-panel"}
          >
            <EventTable tab={tab} />
          </div>
        ))}
    </>
  );
}

export default ChartGrid;
