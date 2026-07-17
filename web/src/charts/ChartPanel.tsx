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

interface EChartsLike {
  group: string;
  setOption: (option: Record<string, unknown>, opts?: Record<string, unknown>) => void;
  on: (event: string, handler: (e: unknown) => void) => void;
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
  theme?: Pick<ChartTheme, "muted">;
  /** id (SeriesInput.key) of the chart's index-0 series — the one
   * buildStackOption/eventOverlay attach markLine/markArea to. */
  anchorSeriesId?: string | null;
  onZoom?: (range: TimeRange) => void;
  testId?: string;
}) {
  const { option, groupId, window: win, markers, theme, anchorSeriesId, onZoom, testId } = props;
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsLike | null>(null);
  const latest = useRef({ win, onZoom });
  latest.current = { win, onZoom };

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
    return () => {
      clearTimeout(timer);
      ro.disconnect();
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
        theme: theme ?? { muted: "" },
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
    if (el.current) el.current.dataset.echartsWindowTo = String(win.to);
  }, [win.from, win.to, markersKey, theme?.muted, anchorSeriesId]);

  return <div ref={el} data-testid={testId} style={{ height: HEIGHT_PX }} className="w-full" />;
}
