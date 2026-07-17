// Plan 5b follow-up: the SECOND door into an inverted-range dashboard
// blank. SubjectPage's onZoom (~line 277: `onZoom={(r) =>
// setRange(clampRange(r, bounds))}`) has no ordering guard of its own,
// unlike RangePicker's Apply. Its input is the chart's CURRENT window,
// which in live mode is `liveRange(session.endMs, windowMs)`
// (data/time.ts) — deliberately unclamped, so it routinely extends BEFORE
// `bounds.from` (session.startMs) during the first `windowMs` of any live
// session (the normal case, not an edge case: any run younger than its own
// follow window). A drag-zoom into that leading, pre-session sliver
// produces a range entirely before `bounds.from`; `clampRange`
// (data/exportDoc.ts) then INVERTS it. The fix lives at the boundary
// (reviewStore.ts's setRange refuses any non-null `from >= to`), not here —
// this test proves the boundary actually catches THIS caller's shape of
// the bug, the same way chartpanel.test.tsx proves ChartPanel's own
// debounce/no-op logic, by capturing the mocked ECharts instance's
// "datazoom" handler and driving it directly (see chartpanel.test.tsx for
// the same FakeChart pattern).
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HostSnapshot, MetricRecord } from "../api/export.gen";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

class FakeChart {
  group = "";
  handlers = new Map<string, (e: unknown) => void>();
  setOption() {}
  on(event: string, cb: (e: unknown) => void) {
    this.handlers.set(event, cb);
  }
  resize() {}
  dispose() {}
}

const instances: FakeChart[] = [];
vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => {
      const c = new FakeChart();
      instances.push(c);
      return c;
    },
    connect: () => {},
  },
}));

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: "h1" }) };
});

import { deriveElements, type NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { SubjectPage } from "../pages/SubjectPage";

const T0 = Date.parse("2026-07-13T00:00:00Z");
const SESSION_AGE_MS = 120_000; // this live run started 2 minutes ago
const WINDOW_MS = 900_000; // default live follow window (15m) -- wider than the run itself

function liveSession(): NormalizedSession {
  const hosts: HostSnapshot[] = [{ id: "h1", element: "h1" }];
  const metrics: MetricRecord[] = [
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 100_000).toISOString(), value: 1 },
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 10_000).toISOString(), value: 2 },
  ];
  const elements = deriveElements(hosts, []);
  return {
    id: "s",
    label: null,
    note: null,
    startMs: T0 - SESSION_AGE_MS,
    endMs: T0,
    lab: { hosts, links: [], explicitElements: [] },
    meta: {
      interval: 5,
      charts: [{ label: "cpu", y_title: "%", unit: "%", command: "c", chart: "CPU" }],
      tabs: [],
    },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: { cpu: "CPU" },
    tunnels: [],
    elements,
    hostIds: new Set(["h1"]),
    elementIds: new Set(elements.map((e) => e.id)),
  } satisfies NormalizedSession;
}

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
    windowMs: 900_000,
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  instances.length = 0;
  resetStore();
});

describe("SubjectPage drag-zoom", () => {
  it("does not blank the store when the zoomed window is entirely before the session start", () => {
    const session = liveSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: "s",
      mode: "live",
      range: null,
      windowMs: WINDOW_MS,
    });

    render(<SubjectPage />);
    expect(instances.length).toBeGreaterThan(0);

    // The live follow window is [endMs - windowMs, endMs] = [T0-900_000,
    // T0], but the session only started 120_000ms ago, so bounds is
    // [T0-120_000, T0]. A drag-zoom into the window's first 5% lands
    // entirely in the pre-session lead-in: zoomToRange(0, 5, window_) =
    // [T0-900_000, T0-855_000] -- wholly before bounds.from (T0-120_000).
    // Unguarded, clampRange(that, bounds) inverts it to {from: T0-120_000,
    // to: T0-855_000} and setRange would push the inversion straight into
    // the store.
    act(() => {
      instances[0].handlers.get("datazoom")?.({ start: 0, end: 5 });
      vi.advanceTimersByTime(250);
    });

    // Refused at the store boundary -- still following, not blanked.
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("still accepts a drag-zoom entirely within the session's own bounds", () => {
    const session = liveSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: "s",
      mode: "live",
      range: null,
      windowMs: WINDOW_MS,
    });

    render(<SubjectPage />);
    expect(instances.length).toBeGreaterThan(0);

    // zoomToRange(85, 95, window_) = [T0-900_000+0.85*900_000,
    // T0-900_000+0.95*900_000] = [T0-135_000, T0-45_000] -- inside bounds
    // [T0-120_000, T0], so clampRange narrows it to [T0-120_000, T0-45_000],
    // a valid, well-ordered range.
    act(() => {
      instances[0].handlers.get("datazoom")?.({ start: 85, end: 95 });
      vi.advanceTimersByTime(250);
    });

    expect(useReviewStore.getState().range).toEqual({ from: T0 - 120_000, to: T0 - 45_000 });
  });
});
