// Per-subject view (UX spec §9): left = SeriesPanel (search/chips/tree),
// right = charts stacked on a shared time axis with one synced crosshair
// (echarts group connect) and brush/wheel zoom driving the SAME range the
// review bar owns. Events overlay every chart (markLine/markArea). Table
// tabs render log-event tables below the stack. Review is display-only;
// marking/editing arrives with the live hookup.
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "wouter";

import { ChartPanel } from "../charts/ChartPanel";
import { buildStackOption, chartTheme, eventMarkers, type SeriesInput } from "../charts/options";
import { MAX_SERIES_PER_CHART } from "../charts/palette";
import { useIsDark } from "../charts/useIsDark";
import { clampRange, metricsForSubject, sessionBounds, subjectKind } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { buildSeriesTree, collectSeriesPoints, filterTree } from "../data/seriesTree";
import { groupRowsFromData, logKey, visibleRows } from "../logevents";
import { SeriesPanel } from "./SeriesPanel";

export function SubjectPage() {
  const params = useParams<{ id: string }>();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const setRange = useReviewStore((s) => s.actions.setRange);
  const dark = useIsDark();

  const id = params.id;
  const [search, setSearch] = useState("");
  const [chips, setChips] = useState<Set<string> | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());

  const tree = useMemo(() => (session ? buildSeriesTree(session, id) : []), [session, id]);

  // (Re)select everything whenever the subject or session changes.
  const treeKey = `${session?.id ?? ""}:${id}`;
  // biome-ignore lint/correctness/useExhaustiveDependencies: treeKey is the session+subject identity; tree derives from it
  useEffect(() => {
    setChecked(new Set(tree.flatMap((c) => c.series.map((s) => s.key))));
    setSearch("");
    setChips(null);
    setSource(null);
  }, [treeKey]);

  if (!session) return null;
  const kind = subjectKind(session, id);
  if (kind === null) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown subject "{id}" in this session. <Link href="/">Back to overview</Link>
      </main>
    );
  }

  const bounds = sessionBounds(session);
  const window_ = range ?? bounds;
  const theme = chartTheme(dark);
  const filtered = filterTree(tree, { search, chips, source });
  const points = collectSeriesPoints(session, tree, checked, range);
  const markers = eventMarkers(session.events, window_);

  const host = session.lab.hosts.find((h) => h.id === id);
  const metrics = metricsForSubject(session, id, range);
  const labels = [...new Set(metrics.map((m) => m.label))].sort();

  const toggle = (key: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Log tables: one per table tab, per row-holding host (the subject, or
  // members for an element subject).
  const tableHosts =
    kind === "element" ? (session.elements.find((e) => e.id === id)?.hostIds ?? []) : [id];
  const grouped = groupRowsFromData(
    session.logEvents.map((r) => ({
      timestamp: r.timestamp,
      host: r.host ?? "",
      tab: r.tab ?? "",
      fields: r.fields ?? {},
    })),
  );
  const tableTabs = session.meta.tabs.filter((t) => t.kind === "table");

  return (
    <main data-testid="subject-page" className="flex flex-col gap-4 p-4">
      <nav className="text-sm text-gray-400">
        <Link href="/">Fleet</Link> / {id}
      </nav>
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-gray-400">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
      </h1>
      <p data-testid="series-summary" className="text-sm text-gray-500 dark:text-gray-400">
        {labels.length} series · {metrics.length} samples in range
      </p>
      <div className="flex gap-4">
        <SeriesPanel
          tree={filterTree(tree, { search, chips, source })}
          checked={checked}
          onToggle={toggle}
          search={search}
          onSearch={setSearch}
          chips={chips}
          onChips={setChips}
          source={source}
          onSource={setSource}
        />
        <div data-testid="chart-stack" className="flex min-w-0 grow flex-col gap-4">
          {filtered.map((chart) => {
            const active = chart.series.filter((s) => checked.has(s.key));
            if (active.length === 0) return null;
            const shown = active.slice(0, MAX_SERIES_PER_CHART);
            const series: SeriesInput[] = shown
              .map((s) => ({
                key: s.key,
                name: s.key === s.label ? s.label : s.host,
                slot: s.slot,
                points: points.get(s.key) ?? [],
              }))
              .filter((s) => s.points.length > 0);
            if (series.length === 0) return null;
            return (
              <section key={chart.chartKey}>
                <h2 className="mb-1 text-sm font-medium text-gray-600 dark:text-gray-300">
                  {chart.chartLabel}
                </h2>
                <ChartPanel
                  option={buildStackOption({
                    unit: chart.unit,
                    yTitle: chart.yTitle,
                    series,
                    window: window_,
                    events: markers,
                    theme,
                  })}
                  groupId={`subject-${id}`}
                  window={window_}
                  onZoom={(r) => setRange(clampRange(r, bounds))}
                  testId={`chart-panel-${chart.chartKey}`}
                />
                {active.length > MAX_SERIES_PER_CHART && (
                  <p
                    data-testid={`series-overflow-${chart.chartKey}`}
                    className="mt-1 text-xs text-gray-400"
                  >
                    showing {MAX_SERIES_PER_CHART} of {active.length} — narrow the selection
                  </p>
                )}
              </section>
            );
          })}
          {filtered.length === 0 && (
            <p className="text-sm text-gray-400">No series match the current filters.</p>
          )}
        </div>
      </div>
      {tableTabs.map((tab) =>
        tableHosts.map((tableHost) => (
          <LogTable
            key={`${tab.id}:${tableHost}`}
            tabId={tab.id ?? ""}
            label={tab.label ?? tab.id ?? ""}
            hostLabel={kind === "element" ? tableHost : null}
            columns={tab.columns ?? []}
            rows={grouped[logKey(tableHost, tab.id ?? "")] ?? []}
          />
        )),
      )}
    </main>
  );
}

function LogTable(props: {
  tabId: string;
  label: string;
  hostLabel: string | null;
  columns: string[];
  rows: ReturnType<typeof groupRowsFromData>[string];
}) {
  const { tabId, label, hostLabel, columns, rows } = props;
  const [filter, setFilter] = useState("");
  if (rows.length === 0) return null;
  const visible = visibleRows(rows, filter);
  return (
    <section data-testid={`log-table-${tabId}`} className="max-w-3xl">
      <div className="mb-1 flex items-center gap-3">
        <h2 className="text-sm font-medium text-gray-600 dark:text-gray-300">
          {label}
          {hostLabel ? ` — ${hostLabel}` : ""}
        </h2>
        <span data-testid={`log-filter-${tabId}`}>
          <input
            type="text"
            placeholder="filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="rounded border border-gray-200 px-2 py-0.5 text-xs dark:border-gray-700
              dark:bg-gray-900"
          />
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-gray-400">
              <th className="py-1 pr-3 font-medium">time</th>
              {columns.map((c) => (
                <th key={c} className="py-1 pr-3 font-medium">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are static snapshots
              <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                <td className="py-1 pr-3 text-gray-400">
                  {new Date(row.timestamp).toLocaleTimeString()}
                </td>
                {columns.map((c) => (
                  <td key={c} className="py-1 pr-3">
                    {row.fields[c] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
