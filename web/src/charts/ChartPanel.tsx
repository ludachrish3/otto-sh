// Direct ECharts instance management (UX spec §5): init/setOption/resize/
// dispose against a ref'd div. Instances join `groupId` so echarts.connect
// syncs the axisPointer crosshair across the whole stack. Zoom gestures
// (inside dataZoom) are debounced and surfaced as an absolute TimeRange —
// the review store's range is the single source of truth, so the zoomed
// window round-trips through the store and every chart (and the review
// bar's inputs) follows.
import { useEffect, useRef } from "react";

import type { TimeRange } from "../data/exportDoc";
import { echarts } from "./echarts";
import { type ChartTheme, type EventMarker, windowPatch, zoomToRange } from "./options";

const HEIGHT_PX = 280;
const ZOOM_DEBOUNCE_MS = 200;
const MIN_ZOOM_DELTA_MS = 1000;

// Task 12 (Monitor Plan 5c): arms a single-shot lineX brush select. Dispatched
// once after init AND after every notMerge setOption — arming is instance-
// level (echarts' `takeGlobalCursor`, not part of the option object), so a
// whole-model rebuild silently drops it unless re-issued (see the option
// effect below).
const BRUSH_ARM_ACTION = {
  type: "takeGlobalCursor",
  key: "brush",
  brushOption: { brushType: "lineX", brushMode: "single" },
} as const;

interface EChartsLike {
  group: string;
  setOption: (option: Record<string, unknown>, opts?: Record<string, unknown>) => void;
  on: (event: string, handler: (e: unknown) => void) => void;
  dispatchAction: (payload: Record<string, unknown>) => void;
  resize: () => void;
  dispose: () => void;
}

export function ChartPanel(props: {
  option: Record<string, unknown>;
  groupId: string;
  window: TimeRange;
  /** Present only when the caller wants the cheap incremental axis/marker
   * patch (SubjectPage's live charts). Omitted by tests exercising the bare
   * option-replace lifecycle — see chartpanel.test.tsx. */
  markers?: EventMarker[];
  theme?: Pick<ChartTheme, "muted" | "ink">;
  /** id (SeriesInput.key) of the chart's index-0 series — the one
   * buildStackOption/eventOverlay attach markLine/markArea to. */
  anchorSeriesId?: string | null;
  onZoom?: (range: TimeRange) => void;
  /** True while "Sweep span on chart" (uiStore's sweepArmed) is armed — a
   * brush drag opens the event editor via `onSweep` instead of zooming. */
  sweepArmed?: boolean;
  onSweep?: (range: TimeRange) => void;
  testId?: string;
}) {
  const {
    option,
    groupId,
    window: win,
    markers,
    theme,
    anchorSeriesId,
    onZoom,
    sweepArmed,
    onSweep,
    testId,
  } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsLike | null>(null);
  const latest = useRef({ win, onZoom, sweepArmed, onSweep });
  latest.current = { win, onZoom, sweepArmed, onSweep };

  useEffect(() => {
    if (!el.current) return;
    const instance = echarts.init(el.current) as unknown as EChartsLike;
    instance.group = groupId;
    echarts.connect(groupId);
    let timer: ReturnType<typeof setTimeout> | undefined;
    instance.on("datazoom", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const evt = e as { start?: number; end?: number; batch?: { start: number; end: number }[] };
        const start = evt.batch?.[0]?.start ?? evt.start;
        const end = evt.batch?.[0]?.end ?? evt.end;
        if (start === undefined || end === undefined) return;
        const range = zoomToRange(start, end, latest.current.win);
        const noop =
          Math.abs(range.from - latest.current.win.from) < MIN_ZOOM_DELTA_MS &&
          Math.abs(range.to - latest.current.win.to) < MIN_ZOOM_DELTA_MS;
        if (!noop) latest.current.onZoom?.(range);
      }, ZOOM_DEBOUNCE_MS);
    });
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(el.current);
    chart.current = instance;
    instance.dispatchAction(BRUSH_ARM_ACTION);
    instance.on("brushEnd", (e) => {
      const evt = e as { areas?: { coordRange?: [number, number] }[] };
      const coordRange = evt.areas?.[0]?.coordRange;
      // Always clear the ghost — a committed zoom re-renders the window, and
      // a sweep opens the editor; a lingering brush rect over either is noise.
      instance.dispatchAction({ type: "brush", areas: [] });
      if (!coordRange) return;
      const range = { from: Math.round(coordRange[0]), to: Math.round(coordRange[1]) };
      if (range.to - range.from < MIN_ZOOM_DELTA_MS) return;
      if (latest.current.sweepArmed) latest.current.onSweep?.(range);
      else latest.current.onZoom?.(range);
    });
    // Ctrl-drag pan (Task 13 no-op risk #2) is implemented by hand here,
    // bypassing both the brush and ECharts' own dataZoom entirely — two
    // independent defects, both found only by driving a real browser (the
    // vitest suite mocks ECharts and can't see either):
    //
    // (1) An armed global brush cursor (takeGlobalCursor, Task 12) captures
    //     every plain drag on this chart, including a Ctrl-held one — a
    //     probe confirmed a Ctrl-drag came back as a ZOOM without any
    //     mitigation, proof the brush was handling it, not dataZoom. The
    //     brief's documented mitigation (toggling the brush cursor off via
    //     `takeGlobalCursor`/`brushType:false` on Ctrl keydown/up) DID stop
    //     that in isolation, but is not what ships here: the capture-phase
    //     `stopPropagation()` below (needed anyway for defect (2)) already
    //     keeps every Ctrl-held mousedown from ever reaching the brush's own
    //     zrender listener, which makes toggling the brush's armed state
    //     redundant — shipping both would be two mechanisms solving the same
    //     half of the problem. Only this one is in the code.
    // (2) With the brush out of the way, dataZoom's OWN "inside" pan is
    //     STILL a no-op: `xAxis.min`/`max` here are always set to the
    //     currently-shown window (buildStackOption), so dataZoom's percent
    //     range is permanently `[0, 100]` — its own full extent already
    //     equals what's displayed. Probed directly against ECharts'
    //     `sliderMove` (component/helper/sliderMove.js): a real, nonzero
    //     per-drag delta goes in, but with the handled span already
    //     spanning the whole `[0, 100]` extent there is no room to shift it
    //     within that extent, so the clamped result comes back exactly
    //     equal to the range it started from, every time — "moveOnMouseMove:
    //     ctrl" can never fire a real `datazoom` pan event under this app's
    //     "axis == current window" design, independent of the brush.
    //
    // The fix for both: own the gesture completely. Native listeners on
    // this component's own container, in the CAPTURE phase (so they run
    // before the event ever reaches the canvas zrender listens on) and
    // `stopPropagation()`-ed whenever Ctrl is held with the primary button
    // down, so neither the brush nor dataZoom's roam controller ever sees a
    // Ctrl-drag at all; the pan itself is a plain pixel-delta-to-time-delta
    // conversion against the window captured at drag start, debounced the
    // same way the brush's own zoom-select is (`ZOOM_DEBOUNCE_MS`) so a fast
    // drag doesn't force a full chart rebuild on every intermediate
    // mousemove.
    const container = el.current;
    let panFrom: { clientX: number; win: TimeRange } | null = null;
    let panTimer: ReturnType<typeof setTimeout> | undefined;
    // Left button only (button === 0) -- a Ctrl+right-drag (context menu) or
    // Ctrl+middle-drag (autoscroll on some platforms) must not arm the pan.
    const onPanDown = (e: MouseEvent) => {
      if (!e.ctrlKey || e.button !== 0) return;
      panFrom = { clientX: e.clientX, win: latest.current.win };
      e.stopPropagation();
    };
    const onPanMove = (e: MouseEvent) => {
      if (panFrom === null) return;
      // `e.buttons` (the CURRENT held-button bitmask), not `e.ctrlKey` alone,
      // is what proves the drag is still live. Without this check: Ctrl-drag,
      // move off the chart, release the button OUTSIDE it (this container's
      // own mouseup never fires), move back over the chart with Ctrl still
      // held -- onPanMove would keep "panning" against the stale `panFrom`
      // snapshot with no button down at all, and the only thing that could
      // ever clear it was another Ctrl+mousedown. Bail AND clear the stale
      // state the moment the primary button isn't down, regardless of Ctrl.
      if ((e.buttons & 1) === 0) {
        panFrom = null;
        return;
      }
      if (!e.ctrlKey) return;
      e.stopPropagation();
      const widthPx = container.clientWidth || 1;
      const span = panFrom.win.to - panFrom.win.from;
      const deltaMs = (-(e.clientX - panFrom.clientX) / widthPx) * span;
      // Rounded like every other gesture's computed bound (brush's
      // coordRange, zoomAbout) -- a fractional-ms window would still work
      // functionally, but stamping one into the DOM/store is an avoidable
      // surprise for anything downstream that assumes integer ms.
      const shifted = {
        from: Math.round(panFrom.win.from + deltaMs),
        to: Math.round(panFrom.win.to + deltaMs),
      };
      clearTimeout(panTimer);
      panTimer = setTimeout(() => latest.current.onZoom?.(shifted), ZOOM_DEBOUNCE_MS);
    };
    const onPanUp = (e: MouseEvent) => {
      if (panFrom === null || e.button !== 0) return;
      panFrom = null;
      e.stopPropagation();
    };
    container.addEventListener("mousedown", onPanDown, true);
    container.addEventListener("mousemove", onPanMove, true);
    container.addEventListener("mouseup", onPanUp, true);
    return () => {
      clearTimeout(timer);
      clearTimeout(panTimer);
      ro.disconnect();
      container.removeEventListener("mousedown", onPanDown, true);
      container.removeEventListener("mousemove", onPanMove, true);
      container.removeEventListener("mouseup", onPanUp, true);
      instance.dispose();
      chart.current = null;
    };
  }, [groupId]);

  useEffect(() => {
    // notMerge (whole-model rebuild) MUST be synchronous — no lazyUpdate. With
    // lazyUpdate:true, ECharts installs the new GlobalModel immediately but
    // defers the data-processing pipeline to the next zr frame, so every
    // series' getData() is undefined until then. During that window an
    // axis-trigger tooltip mousemove (this is a live, hover-tracked chart)
    // reaches getDataParams → data.getRawIndex(idx) on undefined data and
    // crashes (apache/echarts#9402). A synchronous setOption runs the pipeline
    // and flushes before returning, so — JS being single-threaded — no
    // mousemove handler can ever observe a data-less series. The cheap
    // incremental patch below stays lazy: it's a notMerge:false merge that
    // reuses the live models and never touches series data, so it opens no
    // such window.
    chart.current?.setOption(option, { notMerge: true });
    // Re-arm the brush select: notMerge installs a brand-new GlobalModel, and
    // takeGlobalCursor arming lives on the ECharts INSTANCE, not the option
    // object — a whole-model rebuild silently drops it (no-op risk #1) unless
    // reissued here on every notMerge setOption.
    chart.current?.dispatchAction(BRUSH_ARM_ACTION);
    // data-echarts-point-count: stamped HERE, inside the effect that actually
    // makes the imperative setOption() call above — same reasoning as
    // data-echarts-window-to below (see its comment). SubjectPage's
    // `data-point-count` (ChartSection's render body) merely echoes the
    // `series` PROP every render, whether or not THIS effect re-ran — Task 6
    // follow-up's bug was exactly that gap: widening the live window
    // (setWindow) re-slices `series` and bumps `data-point-count` on every
    // SubjectPage render regardless, but the option memo (gated on
    // `revKey`/`range`/... — see ChartSection in SubjectPage.tsx) didn't
    // include the window's width, so this effect never re-fired and ECharts
    // kept drawing the OLD series data under a widened axis. Counting off
    // `option.series[].data` — what this call is actually handing
    // setOption() — rather than the `series` prop is what makes this
    // attribute able to fail when that one couldn't.
    if (el.current) {
      const drawn = (option.series as { data?: unknown[] }[] | undefined) ?? [];
      const pointCount = drawn.reduce((n, s) => n + (s.data?.length ?? 0), 0);
      el.current.dataset.echartsPointCount = String(pointCount);
    }
  }, [option]);

  // Cheap incremental patch (bug: window/markers were consumed inside the
  // memoized `option` but never in its dep list — a chart whose own series
  // didn't tick kept a stale x-axis even though session.endMs (and so
  // liveRange) is global and advances on ANY host's fragment). Gated on the
  // window's own bounds rather than `markers`'/`theme`'s object identity —
  // both are fresh values every SubjectPage render — via a content key, so
  // this doesn't fire on every unrelated re-render either. Deliberately a
  // MERGE (notMerge: false) touching only xAxis + the anchor series'
  // markLine/markArea, never series `data` — see options.ts's windowPatch.
  const markersKey = (markers ?? []).map((m) => `${m.id}:${m.fromMs}:${m.toMs ?? ""}`).join("|");
  // biome-ignore lint/correctness/useExhaustiveDependencies: markersKey/theme?.muted are content-based stand-ins for `markers`/`theme`, which are fresh objects every render (see comment above)
  useEffect(() => {
    if (markers === undefined) return; // no incremental-patch contract for this caller
    chart.current?.setOption(
      windowPatch({
        window: win,
        events: markers,
        theme: theme ?? { muted: "", ink: "" },
        anchorSeriesId: anchorSeriesId ?? null,
      }),
      { notMerge: false, lazyUpdate: true },
    );
    // Stamped HERE, inside the effect that actually made the imperative
    // setOption() call above — deliberately NOT echoed from a render-body
    // prop. A Task 13 review found that SubjectPage's `data-window-to`
    // (on the outer <section>, in ChartSection's render body) merely
    // reflects the `window_` prop every render, regardless of whether this
    // effect ever ran — so a browser spec asserting on it could not
    // distinguish "ECharts' axis actually advanced" from "React re-rendered
    // with a new prop" and could not fail even when this effect's own dep
    // list was broken (the exact Task 11 regression). This attribute is the
    // one genuinely gated on this effect firing with these bounds.
    // data-echarts-marker-count (Task 11, used by Task 13): same reasoning —
    // stamped from the imperative path that actually handed these markers to
    // ECharts, not echoed from the `markers` prop in a render body.
    // data-echarts-window-from (Task 13): the companion lower bound. Without
    // it a browser spec can prove the window MOVED (via -window-to alone)
    // but not that a pan gesture left its WIDTH unchanged (vs. a zoom, which
    // also moves -window-to) — proving that needs both ends of the range
    // read from the same imperative stamp.
    if (el.current) {
      el.current.dataset.echartsWindowFrom = String(win.from);
      el.current.dataset.echartsWindowTo = String(win.to);
      el.current.dataset.echartsMarkerCount = String(markers.length);
    }
  }, [win.from, win.to, markersKey, theme?.muted, anchorSeriesId]);

  return <div ref={el} data-testid={testId} style={{ height: HEIGHT_PX }} className="w-full" />;
}
