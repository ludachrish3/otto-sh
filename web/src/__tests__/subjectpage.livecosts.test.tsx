// Plan 5b final review, Finding I6: two more O(total-run)-per-tick costs on
// SubjectPage, both missed by Task 9's equivalent fix for collectSeriesPoints.
//
// (1) metricsForSubject(session, id, range) was passed the raw (possibly
//     null) range instead of the derived `window_` — while following live
//     (range === null), that returns every sample this subject has EVER
//     reported, and the "N series · M samples in range" summary lied about
//     what it was counting.
// (2) session.logEvents.map(...) + groupRowsFromData re-mapped EVERY log row
//     ever held into fresh objects on every render, including ticks that
//     never touched logEvents at all (a metrics-only fragment still bumps
//     session identity and re-renders this page).
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => ({
      group: "",
      setOption: () => {},
      on: () => {},
      dispatchAction: () => {},
      resize: () => {},
      dispose: () => {},
    }),
    connect: () => {},
  },
}));

vi.mock("../data/logevents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../data/logevents")>();
  return { ...actual, groupRowsFromData: vi.fn(actual.groupRowsFromData) };
});

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: "h1" }) };
});

import type { HostSnapshot, MetricRecord } from "../api/export.gen";
import { deriveElements, type NormalizedSession } from "../data/exportDoc";
import { groupRowsFromData } from "../data/logevents";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { SubjectPage } from "../pages/SubjectPage";

const T0 = Date.parse("2026-07-13T00:00:00Z");

function baseSession(): NormalizedSession {
  const hosts: HostSnapshot[] = [{ id: "h1", element: "h1" }];
  const metrics: MetricRecord[] = [
    // Outside the 60s live window (10 and 9 minutes before endMs).
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 10 * 60_000).toISOString(), value: 1 },
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 9 * 60_000).toISOString(), value: 1 },
    // Inside the 60s live window.
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 30_000).toISOString(), value: 1 },
    { host: "h1", label: "cpu", timestamp: new Date(T0 - 15_000).toISOString(), value: 1 },
    { host: "h1", label: "cpu", timestamp: new Date(T0).toISOString(), value: 1 },
  ];
  const elements = deriveElements(hosts, []);
  return {
    id: "s",
    label: null,
    note: null,
    startMs: T0 - 3_600_000,
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

afterEach(() => {
  cleanup();
  resetStore();
  vi.clearAllMocks();
});

describe("SubjectPage live-tick costs", () => {
  it("series-summary reflects the live-follow WINDOW, not the subject's total-ever samples", () => {
    const session = baseSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: "s",
      mode: "live",
      range: null,
      windowMs: 60_000,
    });

    render(<SubjectPage />);

    // 5 samples exist total; only 3 fall inside the 60s live window.
    expect(screen.getByTestId("series-summary").textContent).toBe("1 series · 3 samples in range");
  });

  it("log-table grouping is memoized: a metrics-only tick does not re-group log rows", () => {
    const session = baseSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: "s",
      mode: "live",
      range: null,
      windowMs: 60_000,
    });

    render(<SubjectPage />);
    const callsAfterMount = vi.mocked(groupRowsFromData).mock.calls.length;
    expect(callsAfterMount).toBeGreaterThan(0);

    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "s",
        metrics: [
          { host: "h1", label: "cpu", timestamp: new Date(T0 + 5_000).toISOString(), value: 2 },
        ],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });
    // The page re-rendered (session identity changed — a new metric
    // landed), but logEvents did not grow: the memoized grouping must NOT
    // have re-run.
    expect(vi.mocked(groupRowsFromData).mock.calls.length).toBe(callsAfterMount);

    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "s",
        metrics: [],
        events: [],
        log_events: [
          {
            host: "h1",
            tab: "kernel",
            timestamp: new Date(T0 + 6_000).toISOString(),
            fields: { msg: "oops" },
          },
        ],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });
    // logEvents DID grow this time — the memo must invalidate and re-run.
    expect(vi.mocked(groupRowsFromData).mock.calls.length).toBeGreaterThan(callsAfterMount);
  });
});
