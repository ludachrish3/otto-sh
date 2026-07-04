// `#tab-bar` + one `.tab-btn[data-tab]` per tab that resolves to at least one
// known metric. Chart panels (`.tab-panel`/`#tab-<id>`) are ChartGrid's
// concern (Task 6); this component only owns tab selection.
import type { ChartSpec, TabSpec } from "../api/types.gen";
import { useMonitorActions, useMonitorStore } from "../store";

// Mirrors dashboard.js's resolvedTabMetrics()/initTabCharts(): a tab whose
// configured metric labels don't resolve against meta.metrics never gets a
// button at all.
function hasResolvedMetrics(tab: TabSpec, metrics: ChartSpec[]): boolean {
  const labels = new Set(metrics.map((m) => m.label));
  return tab.metrics.some((label) => labels.has(label));
}

// Module-level stable references — see Header.tsx's EMPTY_HOSTS comment: a
// fresh `[]` fallback literal inside a zustand selector defeats
// useSyncExternalStore's `Object.is` snapshot check while `meta` is null,
// which crashes the app with React error #185 (infinite update loop).
const EMPTY_TABS: TabSpec[] = [];
const EMPTY_METRICS: ChartSpec[] = [];

function TabBar() {
  const tabs = useMonitorStore((s) => s.meta?.tabs ?? EMPTY_TABS);
  const metrics = useMonitorStore((s) => s.meta?.metrics ?? EMPTY_METRICS);
  const activeTab = useMonitorStore((s) => s.activeTab);
  const { selectTab } = useMonitorActions();

  const visibleTabs = tabs.filter(
    (tab) => tab.kind === "table" || hasResolvedMetrics(tab, metrics),
  );

  return (
    <nav id="tab-bar">
      {visibleTabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={activeTab === tab.id ? "tab-btn active" : "tab-btn"}
          data-tab={tab.id}
          onClick={() => {
            selectTab(tab.id);
          }}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}

export default TabBar;
