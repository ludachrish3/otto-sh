// Render-level regression guard for SubjectPage's per-chart cap + CPU-chart
// hybrid-mute wiring (src/pages/SubjectPage.tsx ~L214-244). chartoptions.test.ts
// and chartoptions.coloring.test.ts exercise buildStackOption's coloring given a
// hand-built SeriesInput; seriestree.maxseries.test.ts exercises buildSeriesTree's
// maxSeries plumbing. Nothing exercised the CALL SITE that turns a ChartNode +
// checked-set into the SeriesInput[] buildStackOption actually receives — the
// same gap the (now-deleted, see git history at c8fddac~1) retirement wiring
// test covered for a different bug. This adapts that harness (mock
// ../charts/echarts to a no-op, mock ../charts/options' buildStackOption to
// capture its args while still delegating to the real implementation, drive
// SubjectPage off a hand-built NormalizedSession via useReviewStore) to assert
// on the cap/mute contract instead of retirement semantics:
//
//  (a) an uncapped chart (maxSeries null — the CPU chart) passes ALL active
//      series through and renders no overflow note.
//  (b) a default-capped chart slices to MAX_SERIES_PER_CHART and renders the
//      overflow note.
//  (c) a single-host CPU chart over the mute threshold forces "Overall CPU"
//      to slot 0 and mutes every "core N" series.
//  (d) a multi-host (element) view with TWO "Overall CPU" series over the
//      mute threshold does NOT collapse both onto slot 0 (finding #2's
//      regression: an element view of 2+ hosts each reporting "Overall CPU"
//      used to render N bold identical-color lines).
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChartSpecRecord, HostSnapshot, MetricRecord } from "../api/export.gen";

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

interface StackCall {
  unit: string;
  yTitle: string;
  series: { key: string; name: string; slot: number; muted?: boolean }[];
}
const stackCalls: StackCall[] = [];
vi.mock("../charts/options", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../charts/options")>();
  return {
    ...actual,
    buildStackOption: (args: Parameters<typeof actual.buildStackOption>[0]) => {
      stackCalls.push({ unit: args.unit, yTitle: args.yTitle, series: args.series });
      return actual.buildStackOption(args);
    },
  };
});

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: mockSubject }) };
});
let mockSubject = "h1";

import { MAX_SERIES_PER_CHART } from "../charts/palette";
import type { DerivedElement, NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { SubjectPage } from "../pages/SubjectPage";

const T0 = Date.parse("2026-07-18T00:00:00Z");
const TICK_MS = 5_000;
const tickTs = (n: number) => new Date(T0 + n * TICK_MS).toISOString();
const TICKS = 3;

function seriesFor(host: string, labels: string[]): MetricRecord[] {
  const out: MetricRecord[] = [];
  for (const label of labels) {
    for (let t = 0; t < TICKS; t++) out.push({ host, label, timestamp: tickTs(t), value: 10 });
  }
  return out;
}

const cpuChart: ChartSpecRecord = {
  label: "CPU",
  y_title: "CPU %",
  unit: "%",
  command: "",
  chart: "CPU",
  max_series: null, // uncapped, mirrors export.py's real CPU chart spec
};

function baseSession(overrides: Partial<NormalizedSession>): NormalizedSession {
  return {
    id: "capmute-session",
    label: null,
    note: null,
    startMs: T0,
    endMs: T0 + (TICKS - 1) * TICK_MS,
    lab: { hosts: [], links: [], explicitElements: [] },
    meta: { interval: 5, charts: [], tabs: [] },
    metrics: [],
    events: [],
    logEvents: [],
    index: buildIndex([]),
    chartMap: {},
    tunnels: [],
    elements: [],
    hostIds: new Set(),
    elementIds: new Set(),
    ...overrides,
  } satisfies NormalizedSession;
}

/** Single host "h1": an uncapped CPU chart with 11 series (1 "Overall CPU" +
 * 10 "core N", well over MAX_SERIES_PER_CHART) and a PSU chart left at the
 * default cap with 10 series (also over the cap). */
function singleHostSession(): NormalizedSession {
  const cores = Array.from({ length: 10 }, (_, i) => `core ${i}`);
  const temps = Array.from({ length: 10 }, (_, i) => `temp ${i}`);
  const metrics = [...seriesFor("h1", ["Overall CPU", ...cores]), ...seriesFor("h1", temps)];
  const psuChart: ChartSpecRecord = {
    label: "PSU Temps",
    y_title: "Temp",
    unit: "C",
    command: "",
    chart: "psu",
    // no max_series: falls back to the default cap (buildSeriesTree.ts)
  };
  const chartMap: Record<string, string> = { "Overall CPU": "CPU" };
  for (const c of cores) chartMap[c] = "CPU";
  for (const tmp of temps) chartMap[tmp] = "PSU Temps";
  return baseSession({
    lab: {
      hosts: [{ id: "h1", element: "h1" } satisfies HostSnapshot],
      links: [],
      explicitElements: [],
    },
    meta: { interval: 5, charts: [cpuChart, psuChart], tabs: [] },
    metrics,
    index: buildIndex(metrics),
    chartMap,
    hostIds: new Set(["h1"]),
  });
}

/** Element "elem1" over hosts h1+h2, each reporting "Overall CPU" + 4 cores
 * (10 series total, over the mute threshold). One decoy series ("Ambient")
 * is attached directly to the element itself so it — not either host's
 * "Overall CPU" — takes the tree's natural slot 0 (elementTarget nodes sort
 * first in buildSeriesTree); that isolates the assertion on finding #2's fix
 * from the coincidence that an alphabetically-first host's own "Overall CPU"
 * would otherwise land on slot 0 anyway. */
function multiHostSession(): NormalizedSession {
  const cores = Array.from({ length: 4 }, (_, i) => `core ${i}`);
  const metrics = [
    ...seriesFor("elem1", ["Ambient"]),
    ...seriesFor("h1", ["Overall CPU", ...cores]),
    ...seriesFor("h2", ["Overall CPU", ...cores]),
  ];
  const chartMap: Record<string, string> = { Ambient: "CPU", "Overall CPU": "CPU" };
  for (const c of cores) chartMap[c] = "CPU";
  const element: DerivedElement = {
    id: "elem1",
    type: "physical",
    explicit: true,
    description: null,
    hostIds: ["h1", "h2"],
    singleton: false,
  };
  return baseSession({
    lab: {
      hosts: [
        { id: "h1", element: "h1" } satisfies HostSnapshot,
        { id: "h2", element: "h2" } satisfies HostSnapshot,
      ],
      links: [],
      explicitElements: [],
    },
    meta: { interval: 5, charts: [cpuChart], tabs: [] },
    metrics,
    index: buildIndex(metrics),
    chartMap,
    elements: [element],
    hostIds: new Set(["h1", "h2"]),
    elementIds: new Set(["elem1"]),
  });
}

function load(subject: string, session: NormalizedSession) {
  mockSubject = subject;
  useReviewStore.setState({ sessions: [session], activeSessionId: session.id });
  return render(<SubjectPage />);
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

describe("SubjectPage cap + mute wiring", () => {
  it("(a) an uncapped chart passes every active series through and renders no overflow note", () => {
    load("h1", singleHostSession());
    const cpuCall = stackCalls.find((c) => c.yTitle === "CPU %");
    expect(cpuCall).toBeDefined();
    expect(cpuCall?.series).toHaveLength(11); // 1 Overall CPU + 10 cores, uncapped
    expect(screen.queryByTestId("series-overflow-CPU")).toBeNull();
  });

  it("(b) a default-capped chart slices to MAX_SERIES_PER_CHART and renders the overflow note", () => {
    load("h1", singleHostSession());
    const psuCall = stackCalls.find((c) => c.yTitle === "Temp");
    expect(psuCall).toBeDefined();
    expect(psuCall?.series).toHaveLength(MAX_SERIES_PER_CHART);
    const note = screen.getByTestId("series-overflow-psu");
    expect(note.textContent).toContain(`showing ${MAX_SERIES_PER_CHART} of 10`);
  });

  it('(c) a single-host CPU chart over the threshold slots "Overall CPU" at 0 and mutes every core', () => {
    load("h1", singleHostSession());
    const cpuCall = stackCalls.find((c) => c.yTitle === "CPU %");
    expect(cpuCall).toBeDefined();
    const overall = cpuCall?.series.find((s) => s.key === "Overall CPU");
    expect(overall?.slot).toBe(0);
    expect(overall?.muted).toBeFalsy();
    const cores = cpuCall?.series.filter((s) => s.key.startsWith("core ")) ?? [];
    expect(cores).toHaveLength(10);
    for (const core of cores) expect(core.muted).toBe(true);
  });

  it('(d) a multi-host view with two "Overall CPU" series does not collapse both onto slot 0', () => {
    load("elem1", multiHostSession());
    const cpuCall = stackCalls.find((c) => c.yTitle === "CPU %");
    expect(cpuCall).toBeDefined();
    const overalls = cpuCall?.series.filter((s) => s.key.endsWith("/Overall CPU")) ?? [];
    // Both hosts' "Overall CPU" survive as distinct series.
    expect(overalls).toHaveLength(2);
    const slots = overalls.map((s) => s.slot);
    // Neither is forced to slot 0 (the decoy "Ambient" series holds slot 0),
    // and — the actual regression — they don't collapse onto the SAME slot.
    for (const slot of slots) expect(slot).not.toBe(0);
    expect(slots[0]).not.toBe(slots[1]);
    for (const overall of overalls) expect(overall.muted).toBeFalsy();
    // Cores still mute regardless of the multi-host fix.
    const cores = cpuCall?.series.filter((s) => s.key.includes("/core ")) ?? [];
    expect(cores).toHaveLength(8); // 4 cores x 2 hosts
    for (const core of cores) expect(core.muted).toBe(true);
  });
});
