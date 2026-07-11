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
import { zoomToRange } from "./options";

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
  onZoom?: (range: TimeRange) => void;
  testId?: string;
}) {
  const { option, groupId, window: win, onZoom, testId } = props;
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
    chart.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  return <div ref={el} data-testid={testId} style={{ height: HEIGHT_PX }} className="w-full" />;
}
