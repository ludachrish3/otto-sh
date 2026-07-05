// One Plotly chart group: a `.section-divider` heading (+ `.expand-btn`)
// and a `.metric-plot` div. Mirrors dashboard.js's `addMetricPlotToContainer`
// (DOM structure) + `initTabPlots` (lazy `Plotly.newPlot`, once, on first
// tab activation) + `toggleExpand`/`collapseExpanded` (expand/collapse — via
// React state here rather than raw `classList`/`textContent` mutation, but
// producing the same classes/body state/Escape behavior; see `ChartGrid`,
// which owns the "only one chart expanded at a time" coordination).
import { useEffect, useLayoutEffect, useRef } from "react";

import type { MonitorEvent, Point } from "../api/client";
import type { ChartGroup } from "../grouping";
import { buildPanelRender, CONSTANT_CHART_HEIGHT_PX, plotly } from "../plotly";

/** Task 11: floor for the "expanded" effect's viewport-derived height below —
 * see its comment for why an explicit minimum beats an unclamped `availH`
 * that could reach zero or negative on a very short viewport. Matches the
 * baseline constant so an expanded chart is never SHORTER than a collapsed
 * one, whatever the viewport. */
const MIN_EXPANDED_HEIGHT_PX = CONSTANT_CHART_HEIGHT_PX;

interface ChartPanelProps {
  group: ChartGroup;
  /** `group.tabId === activeTab` — gates the one-shot lazy `newPlot`. */
  active: boolean;
  series: Record<string, Point[]>;
  selectedHost: string | null;
  events: MonitorEvent[];
  /** Bumped by `ChartGrid` on theme toggle / pause-resume / host switch /
   * expand-to-none — dashboard.js's `refreshPlot()` callers. */
  refreshEpoch: number;
  expanded: boolean;
  onToggleExpand: () => void;
  onInitialized: (id: string) => void;
  registerDiv: (id: string, div: HTMLDivElement | null) => void;
  /** dashboard.js's `mp.div.on('plotly_clickannotation', ...)` handler body — opens the event popover (`ChartGrid` wires this to the store's `openPopover` action). */
  onAnnotationClick: (eventId: number, x: number, y: number) => void;
}

function ChartPanel({
  group,
  active,
  series,
  selectedHost,
  events,
  refreshEpoch,
  expanded,
  onToggleExpand,
  onInitialized,
  registerDiv,
  onAnnotationClick,
}: ChartPanelProps) {
  const divRef = useRef<HTMLDivElement | null>(null);
  const dividerRef = useRef<HTMLDivElement | null>(null);
  const mountedRef = useRef(false);
  const skipMetricsEffect = useRef(true);
  const skipRefreshEffect = useRef(true);
  // "Latest ref" for the annotation-click handler below, which is wired
  // exactly once (inside the one-shot mount effect) but must always resolve
  // `data.index` against the CURRENT events array, not the one captured at
  // mount time.
  const eventsRef = useRef(events);
  eventsRef.current = events;

  useEffect(() => {
    registerDiv(group.id, divRef.current);
    return () => registerDiv(group.id, null);
  }, [group.id, registerDiv]);

  // dashboard.js's `initTabPlots()`: deferred `Plotly.newPlot`, once, the
  // first time this group's tab becomes active. Deliberately keyed on
  // `active` alone — this is a one-shot (guarded by `mountedRef`) that draws
  // whatever `series`/`events` are current *at the moment of activation*,
  // exactly like dashboard.js's one-shot deferred init; it is not meant to
  // re-fire on later data (that's the live-append / refresh effects below).
  // biome-ignore lint/correctness/useExhaustiveDependencies: one-shot init keyed on `active` alone; series/events/group are drawn from whatever is current at activation and must NOT re-fire on their later changes (see comment above).
  useEffect(() => {
    if (!active || mountedRef.current || !divRef.current) return;
    mountedRef.current = true;
    const div = divRef.current;
    const { traces, layout } = buildPanelRender(
      group.metrics,
      series,
      selectedHost,
      events,
      group.metrics[0].y_title,
    );
    void plotly.newPlot(div, traces, layout).then(() => {
      onInitialized(group.id);
      // dashboard.js's `initTabPlots()`: wires the annotation-click ->
      // popover-open right after the initial draw.
      plotly.onClickAnnotation(div, (data) => {
        const ev = eventsRef.current[data.index];
        if (!ev) return;
        onAnnotationClick(ev.id, data.event.clientX, data.event.clientY);
      });
    });
    // Deps deliberately limited to `active` — see the comment above.
  }, [active]);

  // dashboard.js's `appendMetricPoint()` "changed" branches (placeholder
  // replacement / a new series joining this group): the metrics array
  // itself changed shape, so the fast `extendTraces` path doesn't apply —
  // do the full `Plotly.react()` rebuild a new trace count needs. No-ops
  // before this panel is ever initialized (mirrors legacy's
  // `if (targetMp.initialized)` guard around that rebuild).
  // biome-ignore lint/correctness/useExhaustiveDependencies: rebuild is keyed on the metrics *shape* changing (`group.metrics`); series/events/selectedHost are read fresh inside, by design (see comment above).
  useEffect(() => {
    if (skipMetricsEffect.current) {
      skipMetricsEffect.current = false;
      return;
    }
    if (!mountedRef.current || !divRef.current) return;
    const { traces, layout } = buildPanelRender(
      group.metrics,
      series,
      selectedHost,
      events,
      group.metrics[0].y_title,
    );
    void plotly.react(divRef.current, traces, layout);
    // Deps deliberately limited to `group.metrics` — rebuild is keyed on the metrics *shape* changing.
  }, [group.metrics]);

  // dashboard.js's `refreshPlot()` callers: theme toggle, pause resume,
  // host switch, and expand-collapse's restore-natural-height pass (all
  // funneled through `ChartGrid`'s `refreshEpoch`).
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the `refreshEpoch` trigger; current props are read fresh at fire time, mirroring dashboard.js's refreshPlot() (see comment above).
  useEffect(() => {
    if (skipRefreshEffect.current) {
      skipRefreshEffect.current = false;
      return;
    }
    if (!mountedRef.current || !divRef.current) return;
    const { traces, layout } = buildPanelRender(
      group.metrics,
      series,
      selectedHost,
      events,
      group.metrics[0].y_title,
    );
    void plotly.react(divRef.current, traces, layout);
    // Deps deliberately limited to `refreshEpoch` — the trigger; current props are read fresh at fire time.
  }, [refreshEpoch]);

  // dashboard.js's `toggleExpand()`: relayout to the available viewport
  // height once expanded (collapse's height restoration is the
  // `refreshEpoch` effect above, matching legacy's
  // `collapseExpanded() -> refreshPlot()`).
  //
  // Task 11 addendum: under `PLOT_CONFIG.responsive: true`, Plotly stops
  // imposing a pixel height on this div itself — internally it sizes its
  // `.svg-container` to `height: 100%` of THIS div rather than writing an
  // explicit pixel value, so `relayout({height})` alone no longer changes
  // the div's own visible box (it only affects the plot drawn *inside*
  // whatever box the div already has). This div's own CSS height is now the
  // source of truth (baseline: the inline `style` below, a constant;
  // expanded: this effect, imperatively) — set it explicitly here, and let
  // the ResizeObserver effect (which now owns telling Plotly to actually
  // redraw) pick up the resulting box-size change. `useLayoutEffect` (not
  // `useEffect`): the SAME render that flips `expanded` true also clears the
  // baseline inline `style` (see the JSX below), so a plain `useEffect`
  // would let the browser paint one frame with no explicit height at all
  // (auto/collapsed, clipped by `.metric-plot`'s `overflow: hidden`) before
  // this ever runs — `useLayoutEffect` sets it synchronously before that
  // paint. `Math.max(..., MIN_EXPANDED_HEIGHT_PX)`: `availH` can go to zero
  // or negative on a very short viewport (mobile Safari's collapsing
  // toolbar, an unusually squat window); an explicit floor beats silently
  // producing an invalid CSS value (which the browser just ignores,
  // reverting to "auto" — the exact collapse this effect exists to prevent).
  useLayoutEffect(() => {
    if (!expanded || !mountedRef.current || !divRef.current) return;
    const header = document.querySelector("header");
    const eventBar = document.getElementById("event-bar");
    const availH = Math.max(
      MIN_EXPANDED_HEIGHT_PX,
      window.innerHeight -
        (header instanceof HTMLElement ? header.offsetHeight : 0) -
        (eventBar instanceof HTMLElement ? eventBar.offsetHeight : 0) -
        (dividerRef.current?.offsetHeight ?? 0),
    );
    divRef.current.style.height = `${availH}px`;
    void plotly.relayout(divRef.current, { height: availH });
  }, [expanded]);

  // Task 11's window-resize fix: legacy's #2 known bug was that plots never
  // resized with the window (`PLOT_CONFIG.responsive` was `false`, see
  // plotly.ts). A ResizeObserver on this panel's own div is more reliable
  // than depending solely on Plotly's internal `responsive`-driven `window`
  // resize listener — it also fires for ANY container-size change, not just
  // a `window` resize (this tab becoming active, the expand/collapse height
  // changes the effect above makes) — and gives a deterministic trigger the
  // resize pin can wait on. `plotly.resize` on a div Plotly hasn't drawn
  // into yet is a verified no-op (not a throw), so the `mountedRef` guard
  // below is a cheap skip of known-useless calls, not a correctness
  // requirement. `Plots.resize` re-fits BOTH axes to the div's current box —
  // width in practice always comes from CSS/flex layout; height comes from
  // whichever of the effects above last set this div's own CSS height, so
  // there's no fight over who "owns" height, just who last wrote it.
  useEffect(() => {
    const div = divRef.current;
    if (!div) return;
    const observer = new ResizeObserver(() => {
      if (!mountedRef.current || !divRef.current) return;
      void plotly.resize(divRef.current);
    });
    observer.observe(div);
    return () => observer.disconnect();
  }, []);

  return (
    <>
      <div
        ref={dividerRef}
        className={expanded ? "section-divider expanded-title" : "section-divider"}
      >
        {group.chartKey}
        <button type="button" className="expand-btn" onClick={onToggleExpand}>
          {expanded ? "Collapse" : "Expand"}
        </button>
      </div>
      <div
        ref={divRef}
        className={expanded ? "metric-plot expanded-plot" : "metric-plot"}
        // Task 11: this div's own CSS height is now the source of truth for
        // Plotly's internal `.svg-container` (`height: 100%` under
        // `responsive: true` — see the comments above). Baseline height is
        // this constant, applied declaratively; `expanded` clears it so the
        // "expanded" effect's imperative `style.height = availH` (not
        // React-tracked) isn't immediately overwritten back to the constant
        // on the SAME render that flips `expanded` true, and is restored the
        // instant `expanded` flips back to `false`.
        style={expanded ? undefined : { height: CONSTANT_CHART_HEIGHT_PX }}
      />
    </>
  );
}

export default ChartPanel;
