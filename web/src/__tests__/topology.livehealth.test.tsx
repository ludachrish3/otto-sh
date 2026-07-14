// Plan 5b final review, Finding I4: the topology view must dim in live mode
// even when EVERY host goes silent, exactly like OverviewPage (clock.test.tsx,
// health.test.ts's live nowMs pins). Before the fix, TopologyPage called
// healthForHosts(session, range) with no third argument, so its "now"
// defaulted to session.endMs — and endMs only ever advances when a fragment
// arrives. A wedged collector (or a single-host lab) then never ticks
// session.endMs, so no host would EVER cross the down threshold, however
// long the wall clock actually runs: the exact scenario the liveness clock
// (data/clock.ts) exists to catch. This drives the real <TopologyPage/>
// (via <App/>, hash-routed to /topology) so a regression to the un-clocked
// call site fails here, not just in a lower-level unit.
import { cleanup, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { HostSnapshot, MetricRecord } from "../api/export.gen";
import { useClockStore } from "../data/clock";
import { deriveElements, type NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";

// jsdom has no ResizeObserver, which @xyflow/react needs at mount — same
// shim as topolegend.test.tsx / subjectpage.retirement.test.tsx.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

const BASE = Date.parse("2026-07-13T00:00:00Z");
const INTERVAL_S = 5; // HEALTH_K (3) x 5s = 15s down threshold

function liveSession(lastSampleMs: number): NormalizedSession {
  const metrics: MetricRecord[] = [
    { host: "h0", label: "cpu", timestamp: new Date(lastSampleMs).toISOString(), value: 1 },
  ];
  const hosts: HostSnapshot[] = [{ id: "h0", element: "h0" }];
  const elements = deriveElements(hosts, []);
  return {
    id: "s",
    label: null,
    note: null,
    startMs: lastSampleMs - 3_600_000,
    endMs: lastSampleMs,
    lab: { hosts, links: [], explicitElements: [] },
    meta: { interval: INTERVAL_S, charts: [], tabs: [] },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: {},
    elements,
    hostIds: new Set(["h0"]),
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
    connection: "connecting",
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(BASE);
  // data/clock.ts's store initializes `now: Date.now()` once at MODULE LOAD
  // time — real wall-clock time, captured long before this test's
  // vi.setSystemTime(BASE) runs. Without resetting it here, the very first
  // render (before any tick) reads that stale real-world `now`, which can
  // already be hours past BASE and misclassify the host as down before the
  // test even gets to the assertion it's pinning.
  useClockStore.setState({ now: BASE });
  window.location.hash = "#/topology";
});

afterEach(() => {
  cleanup();
  resetStore();
  vi.useRealTimers();
});

describe("TopologyPage live dimming", () => {
  it("a host that goes silent dims WITHOUT any fragment arriving — only the clock reveals it", () => {
    // Last sample lands exactly at boot: no fragment ever touches endMs again
    // (no SSE message announces silence — same premise as OverviewPage's
    // equivalent e2e pin, test_a_silent_host_dims).
    const session = liveSession(BASE);
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: "s",
      mode: "live",
      range: null,
    });

    render(<App />);
    const node = screen.getByTestId("topo-node-h0");
    expect(node.getAttribute("data-status")).toBe("ok");

    // Past HEALTH_K x cadence (15s) with session.endMs frozen the whole
    // time — only the wall clock (useNow) can reveal this.
    act(() => {
      vi.advanceTimersByTime(20_000);
    });

    expect(screen.getByTestId("topo-node-h0").getAttribute("data-status")).toBe("down");
  });
});
