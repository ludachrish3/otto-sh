// Regression guard for Task 10's SubjectPage WIRING of retireStaleSeries
// (src/pages/SubjectPage.tsx ~L141-152). The policy itself (data/retirement.ts)
// is well covered by retirement.data.test.ts, but nothing exercised the CALL
// SITE: `subjectpage.test.tsx`'s kitchen-sink.json fixture has no proc/*
// labels at all, and the e2e `_preload` fixture (tests/e2e/monitor/dashboard/
// conftest.py) pushes every proc/* PID on every preloaded tick, so no PID
// ever goes stale in either lane — deleting the `retireStaleSeries(...)` call
// from SubjectPage stayed green everywhere. This test builds a session with
// one proc/* PID that stops reporting early (must retire) and one that keeps
// reporting through the latest tick (must stay), and asserts on the `series`
// that `buildStackOption` actually receives — the same contract
// chartoptions.test.tsx exercises directly, rather than on rendered pixels.
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HostSnapshot, MetricRecord } from "../api/export.gen";

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
      resize: () => {},
      dispose: () => {},
    }),
    connect: () => {},
  },
}));

const stackCalls: { series: { key: string }[] }[] = [];
vi.mock("../charts/options", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../charts/options")>();
  return {
    ...actual,
    buildStackOption: (args: Parameters<typeof actual.buildStackOption>[0]) => {
      stackCalls.push({ series: args.series });
      return actual.buildStackOption(args);
    },
  };
});

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: "h1" }) };
});

import type { NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { SubjectPage } from "../pages/SubjectPage";

const T0 = Date.parse("2026-07-13T00:00:00Z");
const TICK_MS = 5_000;
const tickTs = (n: number) => new Date(T0 + n * TICK_MS).toISOString();

/**
 * proc/1 stops reporting after tick 2; proc/2 keeps reporting through tick 6
 * (the latest). RETIRE_AFTER_TICKS is 3, so the latest 3 distinct ticks
 * across this chart's proc/* candidates are {4,5,6} — proc/1's last sample
 * (tick 2) falls outside that window and must retire; proc/2's does not.
 * Both proc/* labels are mapped to the SAME chart via `chartMap` (mirroring
 * export.py's real chart_map: TopCpuParser emits "proc/<pid>" alongside
 * "Overall CPU" into one "CPU" chart) — retireStaleSeries only compares
 * recency WITHIN one chart's candidate keys, so the two PIDs must land in
 * the same ChartNode for the policy to have anything to bite on.
 */
function retirementSession(): NormalizedSession {
  const metrics: MetricRecord[] = [];
  for (let t = 0; t <= 2; t++) {
    metrics.push({ host: "h1", label: "proc/1", timestamp: tickTs(t), value: 10 });
  }
  for (let t = 0; t <= 6; t++) {
    metrics.push({ host: "h1", label: "proc/2", timestamp: tickTs(t), value: 20 });
  }
  const hosts: HostSnapshot[] = [{ id: "h1", element: "h1" }];
  return {
    id: "retire-session",
    label: null,
    note: null,
    startMs: T0,
    endMs: T0 + 6 * TICK_MS,
    lab: { hosts, links: [], explicitElements: [] },
    meta: { interval: 5, charts: [], tabs: [] },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: { "proc/1": "CPU", "proc/2": "CPU" },
    elements: [],
    hostIds: new Set(["h1"]),
    elementIds: new Set(),
  } satisfies NormalizedSession;
}

afterEach(() => {
  cleanup();
  stackCalls.length = 0;
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
});

describe("SubjectPage retirement wiring", () => {
  it("drops a stale proc/* series from the built chart option but keeps a live one", () => {
    const session = retirementSession();
    useReviewStore.setState({ sessions: [session], activeSessionId: session.id });

    render(<SubjectPage />);

    expect(stackCalls.length).toBeGreaterThan(0);
    const keys = stackCalls[stackCalls.length - 1].series.map((s) => s.key);
    expect(keys).toContain("proc/2");
    expect(keys).not.toContain("proc/1");
  });
});
