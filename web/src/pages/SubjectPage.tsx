// Per-subject view (UX spec §9): left = SeriesPanel (search/chips/tree),
// right = charts stacked on a shared time axis with one synced crosshair
// (echarts group connect) and brush/wheel zoom driving the SAME range the
// review bar owns. Events overlay every chart (markLine/markArea). Table
// tabs render log-event tables below the stack. Review is display-only;
// marking/editing arrives with the live hookup.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "wouter";

import { Table } from "@/components/application/table/table";
import { Tabs } from "@/components/application/tabs/tabs";
import { ButtonGroup, ButtonGroupItem } from "@/components/base/button-group/button-group";
import { Input } from "@/components/base/input/input";
import { ChartPanel } from "../charts/ChartPanel";
import {
  buildStackOption,
  chartTheme,
  type EventMarker,
  eventMarkers,
  type SeriesInput,
} from "../charts/options";
import { MAX_SERIES_PER_CHART } from "../charts/palette";
import { useIsDark } from "../charts/useIsDark";
import {
  clampRange,
  metricsForSubject,
  type NormalizedSession,
  sessionBounds,
  subjectKind,
  type TimeRange,
} from "../data/exportDoc";
import { groupRowsFromData, type LogEventRow, logKey, visibleRows } from "../data/logevents";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { seriesKey } from "../data/seriesIndex";
import { buildSeriesTree, collectSeriesPoints, filterTree } from "../data/seriesTree";
import { liveRange } from "../data/time";
import { EventsPanel } from "../shell/EventsPanel";
import { SubjectHealthBanner } from "../shell/SubjectHealthBanner";
import { LIVE_WINDOW_PRESETS } from "../ui/commands";
import { SeriesPanel } from "./SeriesPanel";

// The selected live-window preset is DERIVED from `windowMs`, never stored
// separately (Task 6/7: same "derive, don't store" lesson as
// reviewStore's `useIsPaused` — a stored copy of a derived value drifts).
function selectedWindowId(windowMs: number): string {
  return LIVE_WINDOW_PRESETS.find((p) => p.ms === windowMs)?.id ?? "15m";
}

export function SubjectPage() {
  const params = useParams<{ id: string }>();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  const windowMs = useReviewStore((s) => s.windowMs);
  const setRange = useReviewStore((s) => s.actions.setRange);
  const setWindow = useReviewStore((s) => s.actions.setWindow);
  const dark = useIsDark();

  const id = params.id;
  const [search, setSearch] = useState("");
  const [chips, setChips] = useState<Set<string> | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [eventsOpen, setEventsOpen] = useState(false);

  const tree = useMemo(() => (session ? buildSeriesTree(session, id) : []), [session, id]);
  const allKeys = tree.flatMap((c) => c.series.map((s) => s.key));

  // Log tables (Plan 5b final review, Finding I6): session.logEvents.map(...)
  // + groupRowsFromData re-map EVERY log row this session has ever held into
  // fresh objects, on every SubjectPage render — the 500-row-per-tab cap
  // (data/logevents.ts) only applies AFTER that full pass. A live tick
  // re-renders this page whenever ANY fragment lands (even a metrics-only
  // one that never touches logEvents at all), so a chatty log tab paid this
  // cost on every tick regardless of whether it grew. Memoized on the
  // session's identity + logEvents.length: fragment.ts pushes new rows onto
  // `session.logEvents` IN PLACE (same array reference for the session's
  // whole lifetime — see the "mutated in place" note on session.metrics
  // elsewhere in this file), so the reference itself can never be a useMemo
  // dependency; `.length` is the cheap, correct proxy for "rows were
  // actually appended" (an append-only array, same reasoning as `allKeys`
  // above).
  const logEventsLength = session?.logEvents.length ?? 0;
  // biome-ignore lint/correctness/useExhaustiveDependencies: session is read inside for its (in-place-mutated, reference-stable) logEvents array — session?.id + logEventsLength are the stable proxies for "rows changed" (see comment above)
  const grouped = useMemo(() => {
    if (!session) return {};
    return groupRowsFromData(
      session.logEvents.map((r) => ({
        timestamp: r.timestamp,
        host: r.host ?? "",
        tab: r.tab ?? "",
        fields: r.fields ?? {},
      })),
    );
  }, [session?.id, logEventsLength]);

  // (Re)select everything whenever the subject or session changes, AND
  // auto-select any series that starts reporting for the FIRST time while
  // this same page stays open. `treeKey` alone (session+subject identity)
  // under-fires for a live page: it never changes for as long as the page
  // stays mounted, but `tree` keeps growing as new series report — so a
  // host with zero data at the moment its page was opened stayed
  // `checked = ∅` forever, however much data later streamed in, until the
  // user navigated away and back (a fresh mount re-seeds `checked` from
  // whatever `tree` looks like AT THAT POINT). Plan 5b Task 13's replay
  // soak caught this live end-to-end: open a fresh host's page before its
  // first tick lands, then stream ~180k points at it — SeriesPanel's
  // checkboxes appear (tree keeps growing, that part was always live), but
  // every one stays silently unchecked and no chart ever renders.
  // `allKeys.length` is a cheap, CORRECT growth signal specifically because
  // a session's series set only ever accumulates (buildSeriesTree walks
  // session.index.keysByHost, which never shrinks): it cannot go back down,
  // so "the count grew" and "a new key appeared" always coincide.
  const treeKey = `${session?.id ?? ""}:${id}`;
  const knownKeysRef = useRef<Set<string>>(new Set());
  const lastTreeKeyRef = useRef<string | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: allKeys is a fresh array every render; allKeys.length is the stable stand-in for "the tree grew" (see comment above)
  useEffect(() => {
    const subjectChanged = lastTreeKeyRef.current !== treeKey;
    lastTreeKeyRef.current = treeKey;
    if (subjectChanged) {
      knownKeysRef.current = new Set(allKeys);
      setChecked(new Set(allKeys));
      setSearch("");
      setChips(null);
      setSource(null);
      return;
    }
    const newKeys = allKeys.filter((k) => !knownKeysRef.current.has(k));
    if (newKeys.length === 0) return;
    knownKeysRef.current = new Set(allKeys);
    setChecked((prev) => new Set([...prev, ...newKeys]));
  }, [treeKey, allKeys.length]);

  if (!session) return null;
  const kind = subjectKind(session, id);
  if (kind === null) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-tertiary">
        Unknown subject "{id}" in this session. <Link href="/hosts">Back to hosts</Link>
      </main>
    );
  }

  const bounds = sessionBounds(session);
  // range === null in live mode means "follow the tail": the window is
  // derived from the session's latest ingested sample (endMs), not a raw
  // wall clock — every appended fragment moves endMs, and that's what
  // re-renders this page, so the window advances exactly when new data
  // arrives without this component ever subscribing to the liveness clock
  // (which stays reserved for health/dimming — see data/clock.ts). Pausing
  // (reviewStore's togglePause) snapshots a `range`, so this falls through
  // to the frozen branch automatically; review mode is unaffected (mode
  // !== "live" always takes the existing bounds fallback).
  const window_ = range ?? (mode === "live" ? liveRange(session.endMs, windowMs) : bounds);
  const theme = chartTheme(dark);
  const filtered = filterTree(tree, { search, chips, source });
  // Pass the DERIVED window, not the raw (possibly-null) `range` — while
  // following in live mode, `range` is null but the chart still only shows
  // `window_` (liveRange's rolling slice). Passing `range` here scanned all
  // of `session.metrics` and handed ECharts every point since session start
  // on every render (unbounded growth on a long-running session), and made
  // `data-point-count` report total-ever rather than points-in-window (Task
  // 13's browser lane asserts chart growth/freezing through that attribute).
  const points = collectSeriesPoints(session, tree, checked, window_);
  const markers = eventMarkers(session.events, window_);

  const host = session.lab.hosts.find((h) => h.id === id);
  // Pass the DERIVED window, not the raw (possibly-null) `range` — same fix
  // as `points` above (Task 9 applied it to collectSeriesPoints; this call
  // site was missed — Plan 5b final review, Finding I6). Passing `range`
  // scanned every sample this subject has EVER reported on every live tick
  // while following (range is null then), and made the "N series · M
  // samples in range" summary below report total-ever instead of the
  // window it claims to describe.
  const metrics = metricsForSubject(session, id, window_);
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
  // members for an element subject). `grouped` is computed once above
  // (memoized — see the useMemo near `tree`), not re-derived here.
  const tableHosts =
    kind === "element" ? (session.elements.find((e) => e.id === id)?.hostIds ?? []) : [id];
  const tableTabs = session.meta.tabs.filter((t) => t.kind === "table");

  return (
    <main data-testid="subject-page" className="flex flex-col gap-4 p-4">
      <nav className="text-sm text-quaternary">
        <Link href="/hosts">Fleet</Link> / {id}
      </nav>
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-quaternary">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
        <span className="ml-auto flex items-center gap-2 text-sm font-normal">
          {mode === "live" && (
            <ButtonGroup
              aria-label="Live window"
              data-testid="live-window"
              size="sm"
              selectedKeys={new Set([selectedWindowId(windowMs)])}
              disallowEmptySelection
              onSelectionChange={(keys) => {
                const selected = [...keys][0];
                const preset = LIVE_WINDOW_PRESETS.find((p) => p.id === selected);
                if (preset) setWindow(preset.ms);
              }}
            >
              {LIVE_WINDOW_PRESETS.map((p) => (
                <ButtonGroupItem key={p.id} id={p.id} data-testid={`live-window-${p.id}`}>
                  {p.label}
                </ButtonGroupItem>
              ))}
            </ButtonGroup>
          )}
          {session.events.length > 0 && (
            <button
              type="button"
              data-testid="events-button"
              onClick={() => setEventsOpen(true)}
              className="cursor-pointer rounded-md px-2 py-1 text-sm text-tertiary
                hover:bg-primary_hover"
            >
              Events{" "}
              <span data-testid="events-count" className="rounded-full bg-tertiary px-1.5 text-xs">
                {session.events.length}
              </span>
            </button>
          )}
        </span>
      </h1>
      <EventsPanel isOpen={eventsOpen} onClose={() => setEventsOpen(false)} />
      <p data-testid="series-summary" className="text-sm text-tertiary">
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
        <SubjectHealthBanner subjectId={id}>
          <div data-testid="chart-stack" className="flex min-w-0 grow flex-col gap-4">
            {filtered.map((chart) => {
              const active = chart.series.filter((s) => checked.has(s.key));
              if (active.length === 0) return null;
              const cap = chart.maxSeries; // number = cap, null = uncapped
              const shown = cap == null ? active : active.slice(0, cap);
              const muteCores = chart.chartKey === "CPU" && shown.length > MAX_SERIES_PER_CHART;
              // Slot 0 is only forced for a single "Overall CPU" series: a
              // multi-host element view can show one "Overall CPU" per host,
              // and collapsing all of them onto slot 0 would render N bold
              // identical-color lines instead of each host keeping its own
              // entity color.
              const overallCount = shown.filter((s) => s.label === "Overall CPU").length;
              // Pair each SeriesInput with its seriesIndex key (`seriesKey(host,
              // label)`) WHILE host/label are both still in scope: a host subject's
              // own series has a bare-label SeriesNode.key ("m0"), which is NOT the
              // "h0/m0" shape session.index.rev is keyed on — only an element
              // subject's member series ("host/label") happens to already match.
              // Losing host/label by flattening into SeriesInput first (as before)
              // made the revision lookup silently always miss.
              const entries = shown
                .map((s) => ({
                  input: {
                    key: s.key,
                    name: s.key === s.label ? s.label : s.host,
                    slot: muteCores && overallCount === 1 && s.label === "Overall CPU" ? 0 : s.slot,
                    muted: muteCores && s.label.startsWith("core "),
                    points: points.get(s.key) ?? [],
                  } satisfies SeriesInput,
                  idxKey: seriesKey(s.host, s.label),
                }))
                .filter((e) => e.input.points.length > 0);
              if (entries.length === 0) return null;
              const series = entries.map((e) => e.input);
              const revKey = entries
                .map((e) => `${e.idxKey}:${session.index.rev.get(e.idxKey) ?? 0}`)
                .join(",");
              return (
                <ChartSection
                  key={chart.chartKey}
                  chartKey={chart.chartKey}
                  chartLabel={chart.chartLabel}
                  unit={chart.unit}
                  yTitle={chart.yTitle}
                  series={series}
                  window_={window_}
                  windowMs={windowMs}
                  markers={markers}
                  theme={theme}
                  dark={dark}
                  range={range}
                  sessionEvents={session.events}
                  revKey={revKey}
                  checked={checked}
                  groupId={`subject-${id}`}
                  onZoom={(r) => setRange(clampRange(r, bounds))}
                  overflowCount={cap != null && active.length > cap ? active.length : null}
                />
              );
            })}
            {filtered.length === 0 && (
              <p className="text-sm text-quaternary">No series match the current filters.</p>
            )}
          </div>
        </SubjectHealthBanner>
      </div>
      {tableTabs.length > 0 && (
        <Tabs>
          <Tabs.List aria-label="Log tables" type="button-border" size="sm">
            {tableTabs.map((tab) => (
              <Tabs.Item key={tab.id} id={tab.id ?? ""}>
                {tab.label ?? tab.id ?? ""}
              </Tabs.Item>
            ))}
          </Tabs.List>
          {tableTabs.map((tab) => (
            <Tabs.Panel key={tab.id} id={tab.id ?? ""} className="flex flex-col gap-4 pt-3">
              {tableHosts.map((tableHost) => (
                <LogTable
                  key={`${tab.id}:${tableHost}`}
                  tabId={tab.id ?? ""}
                  label={tab.label ?? tab.id ?? ""}
                  hostLabel={kind === "element" ? tableHost : null}
                  columns={tab.columns ?? []}
                  rows={grouped[logKey(tableHost, tab.id ?? "")] ?? []}
                />
              ))}
            </Tabs.Panel>
          ))}
        </Tabs>
      )}
    </main>
  );
}

// Owns the one useMemo that gates buildStackOption — pulled out of
// SubjectPage's `.map()` because a hook can't be called a variable number
// of times per render (the chart list's length changes with search and
// checkbox state). Memoizing here, once per chart, is also what makes the
// memo's SKIP actually pay off: buildStackOption builds the full ECharts
// option (series styling, markLine/markArea, axis config) — the expensive
// part this task exists to avoid redoing for a chart that didn't change.
function ChartSection(props: {
  chartKey: string;
  chartLabel: string;
  unit: string;
  yTitle: string;
  series: SeriesInput[];
  window_: TimeRange;
  /** The follow window's WIDTH (reviewStore's `windowMs`), not its bounds —
   * see the comment on the memo below for why this, and specifically not
   * `window_.from`/`window_.to`, is the dep that belongs here. */
  windowMs: number;
  markers: EventMarker[];
  theme: ReturnType<typeof chartTheme>;
  dark: boolean;
  range: TimeRange | null;
  sessionEvents: NormalizedSession["events"];
  /** `seriesKey(host,label):rev` joined per drawn series (session.index.rev,
   * seriesIndex.ts) — built by the caller, where host/label are still in
   * scope (see the comment at the call site on why SeriesInput.key alone
   * isn't enough). Bumped ONLY for a series that actually got new points, so
   * a live tick rebuilds just the charts that moved. */
  revKey: string;
  /** The SubjectPage-local checkbox selection. A live tick never touches this
   * (it's UI state, untouched by appendFragment), so it costs the tick
   * nothing — but a checkbox/search/chip interaction is a full-page,
   * infrequent, user-initiated re-layout, and is deliberately still allowed
   * to bust every visible chart's memo, same as before this task (see
   * subjectpage.test.tsx's "unchecking a series..." test: unchecking a
   * chart's only series drops that ChartSection entirely, so the *other*
   * charts are what the test observes re-applying an option). */
  checked: Set<string>;
  groupId: string;
  onZoom: (range: TimeRange) => void;
  overflowCount: number | null;
}) {
  const {
    chartKey,
    chartLabel,
    unit,
    yTitle,
    series,
    window_,
    windowMs,
    markers,
    theme,
    dark,
    range,
    sessionEvents,
    revKey,
    checked,
    groupId,
    onZoom,
    overflowCount,
  } = props;
  // Keying on `session` identity instead would bust every chart's memo on
  // every tick — applyFragment returns a NEW session object for any
  // non-empty fragment, regardless of which host it touched — which is the
  // identity trap this whole design exists to avoid. `range` stands in for
  // "the user moved the window" (pan/zoom/pause); `dark` stands in for
  // `theme` (chartTheme(dark) is a fresh object every render, so depending
  // on `theme` itself would never skip); `sessionEvents` is session.events,
  // whose reference is preserved by applyFragment whenever a fragment
  // doesn't touch events — unlike `markers`, which is recomputed (new array)
  // on every SubjectPage render regardless. unit/yTitle/series/window_/markers/theme
  // are intentionally left OUT of the deps below — they are fresh objects/arrays
  // every SubjectPage render, and depending on them directly would defeat this
  // memo on every tick; revKey/range/dark/sessionEvents/checked/windowMs are the
  // stable proxies that actually capture "did this chart's own inputs change."
  //
  // window_/markers are consumed here too (buildStackOption bakes the window
  // at whatever instant this memo happens to recompute), but NOT tracked as
  // deps — in live-follow mode `window_` is derived from session.endMs,
  // which is global (fragment.ts extends it from ANY host's metric), so a
  // chart whose own series never ticks would otherwise keep a stale x-axis
  // forever. That's handled OUTSIDE this memo: ChartPanel gets `window_`/
  // `markers` as separate props and re-applies just the axis bounds + event
  // markLine/markArea as a cheap merge patch whenever the window moves,
  // independent of this (expensive, revKey-gated) full rebuild. See
  // ChartPanel.tsx and options.ts's windowPatch.
  //
  // `windowMs` IS tracked, though, and deliberately NOT via `window_.from`/
  // `window_.to` (Task 6 follow-up's bug + fix). Two different things move
  // the derived `window_` while following: (a) session.endMs advancing on
  // every live tick — a pure SLIDE, harmless to skip here, because points
  // that age out of view were already excluded and new ones for THIS
  // chart's own series arrive via `revKey`; and (b) the user picking a
  // wider/narrower preset (the presets ButtonGroup in this page's title
  // row -> reviewStore's `setWindow`) — a WIDTH change, which pulls previously-excluded points
  // back into `series` (collectSeriesPoints re-slices against the new
  // window in SubjectPage's render body) that this memo must actually bake
  // in, not just widen the axis around. `revKey` doesn't move for that
  // (the series' own data didn't change, only which slice of it is in
  // range), and neither does `range` (still null — the view is still
  // following, by design; see reviewStore's setWindow doc comment). Nothing
  // else in this dep list was going to fire a rebuild, so the points would
  // silently stay stale until this chart's series next ticked on its own —
  // exactly the bug the browser lane's data-echarts-point-count assertion
  // pins (test_live_shell.py). `windowMs` is the right proxy for (b)
  // because it changes ONLY on that rare, user-initiated action; `window_.to`
  // would also satisfy (b) but reintroduces (a) — it advances on every
  // single live tick (any host's, via the shared session.endMs) — which
  // would defeat the whole point of gating the expensive rebuild on
  // `revKey` in the first place. See chart_memo.test.tsx for both directions
  // pinned as mutation-proof tests.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above — deliberately partial
  const option = useMemo(
    () => buildStackOption({ unit, yTitle, series, window: window_, events: markers, theme }),
    [revKey, range, dark, sessionEvents, checked, windowMs],
  );
  // data-point-count/data-window-to: the browser lane (Task 13) asserts
  // growth/freezing off these instead of reading canvas pixels — a live tick
  // or a pause must be provable without decoding what ECharts actually
  // painted. Deliberately NOT part of the memo above: they must always
  // reflect the current render, even when `option` itself was reused.
  const pointCount = series.reduce((n, s) => n + s.points.length, 0);
  return (
    <section
      data-testid={`chart-${chartKey}`}
      data-point-count={pointCount}
      data-window-to={window_.to}
    >
      <h2 className="mb-1 text-sm font-medium text-secondary">{chartLabel}</h2>
      <ChartPanel
        option={option}
        groupId={groupId}
        window={window_}
        markers={markers}
        theme={theme}
        anchorSeriesId={series[0]?.key ?? null}
        onZoom={onZoom}
        testId={`chart-panel-${chartKey}`}
      />
      {overflowCount !== null && (
        <p data-testid={`series-overflow-${chartKey}`} className="mt-1 text-xs text-quaternary">
          {/* Every capped chart in practice uses the default cap; non-default caps wouldn't be reflected here. */}
          showing {MAX_SERIES_PER_CHART} of {overflowCount} — narrow the selection
        </p>
      )}
    </section>
  );
}

// A column descriptor for the vendored `Table`'s dynamic-collection API
// (react-aria-components' Table needs each column/row item keyed by a
// unique `id`, unlike a hand-rolled `<table>`'s free-form `.map()`). "time"
// is always first and synthetic — the wire's `columns` never lists it.
interface LogColumn {
  id: string;
  label: string;
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
  const tableColumns: LogColumn[] = [
    { id: "time", label: "time" },
    ...columns.map((c) => ({ id: c, label: c })),
  ];
  // `id: i` (the row's position in `visible`) is the same "static snapshot"
  // key the pre-migration hand-rolled `<tr key={i}>` used — visible is
  // recomputed fresh from `rows`/`filter` every render, never reconciled
  // against a previous list, so there is no reorder/insert for an
  // index-based id to get wrong.
  const items = visible.map((row, i) => ({ id: i, row }));
  return (
    <section data-testid={`log-table-${tabId}`} className="max-w-3xl">
      <div className="mb-1 flex items-center gap-3">
        <h2 className="text-sm font-medium text-secondary">
          {label}
          {hostLabel ? ` — ${hostLabel}` : ""}
        </h2>
        {/* Untitled UI's `Input` doesn't forward a `data-testid` it's given
            onto the `<input>` it renders internally (see ui/TextInput.tsx's
            module comment for the same gap on `InputBase`) — this control's
            testid contract is on the WRAPPING element instead (the e2e
            suite selects `[data-testid="log-filter-<tab>"] input`), so the
            outer span carries it and `Input` doesn't need its own. */}
        <span data-testid={`log-filter-${tabId}`}>
          <Input
            aria-label={`Filter ${label}`}
            size="sm"
            placeholder="filter…"
            value={filter}
            onChange={setFilter}
          />
        </span>
      </div>
      <Table aria-label={`${label} log`} size="sm">
        <Table.Header columns={tableColumns}>
          {(column: LogColumn) => (
            // react-aria-components' Table requires exactly one row-header
            // column for its grid a11y semantics (it throws otherwise) —
            // "time" is the natural row identifier here, same role the
            // hand-rolled table's leftmost <td> played implicitly.
            <Table.Head id={column.id} isRowHeader={column.id === "time"}>
              {column.label}
            </Table.Head>
          )}
        </Table.Header>
        <Table.Body items={items}>
          {(item: { id: number; row: LogEventRow }) => (
            <Table.Row id={item.id} columns={tableColumns}>
              {(column: LogColumn) => (
                <Table.Cell>
                  {column.id === "time"
                    ? new Date(item.row.timestamp).toLocaleTimeString()
                    : (item.row.fields[column.id] ?? "")}
                </Table.Cell>
              )}
            </Table.Row>
          )}
        </Table.Body>
      </Table>
    </section>
  );
}
