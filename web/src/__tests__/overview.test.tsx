import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { HostSnapshot, MetricRecord } from "../api/export.gen";
import { deriveElements, type NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { OverviewPage } from "../pages/OverviewPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

const T0 = Date.parse("2026-07-13T00:00:00Z");
const INTERVAL_S = 5; // HEALTH_K (3) x 5s = 15s down threshold

/** Single-host archived session, down 45s at the session's end — inside
 * HEALTH_K x cadence's sub-minute reach (Minor 5, 5b follow-ups review):
 * formatSpan(0, outageMs) rounds any outage under a minute down to "0m". */
function subMinuteDownSession(): NormalizedSession {
  const hosts: HostSnapshot[] = [{ id: "h0", element: "h0" }];
  const metrics: MetricRecord[] = [
    { host: "h0", label: "cpu", timestamp: new Date(T0 - 45_000).toISOString(), value: 42 },
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
      interval: INTERVAL_S,
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
    hostIds: new Set(["h0"]),
    elementIds: new Set(elements.map((e) => e.id)),
  } satisfies NormalizedSession;
}

function load(range: { from: number; to: number } | null = null) {
  useReviewStore.getState().actions.importMonitorSessions(KITCHEN, "kitchen-sink.json");
  if (range) useReviewStore.getState().actions.setRange(range);
  return render(<OverviewPage />);
}

afterEach(() => {
  cleanup();
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

describe("fleet grid", () => {
  it("renders a tile per host with a labeled headline", () => {
    load();
    const tile = screen.getByTestId("host-tile-chassis-a_lc1");
    expect(tile).toBeTruthy();
    expect(screen.getByTestId("headline-chassis-a_lc1").textContent).toMatch(/% cpu$/);
  });

  it("shows down · duration when the range ends inside the outage", () => {
    const start = importAndGetStart();
    load({ from: start, to: start + 70 * MIN });
    expect(screen.getByTestId("host-tile-workers_w2").textContent).toMatch(/down · 10m/);
  });

  it("shows a sub-minute down duration as seconds, not '0m' (Minor 5, 5b follow-ups review)", () => {
    const session = subMinuteDownSession();
    useReviewStore.setState({ sessions: [session], activeSessionId: session.id, range: null });
    render(<OverviewPage />);
    expect(screen.getByTestId("host-tile-h0").textContent).toMatch(/down · 45s/);
  });

  it("healthy tiles show no down text at full range", () => {
    load();
    expect(screen.getByTestId("host-tile-workers_w2").textContent).not.toMatch(/down ·/);
  });

  it("renders the element rollup with one segment per member", () => {
    load();
    const rollup = screen.getByTestId("health-rollup-chassis-a");
    expect(rollup.children).toHaveLength(3);
  });

  it("keeps the empty-chassis section with no tiles", () => {
    load();
    const section = screen.getByTestId("element-section-spare-chassis");
    expect(section.textContent).toMatch(/empty/);
    expect(section.querySelector("[data-testid^=host-tile-]")).toBeNull();
  });
});

function importAndGetStart(): number {
  useReviewStore.getState().actions.importMonitorSessions(KITCHEN, "kitchen-sink.json");
  return useReviewStore.getState().sessions[0].startMs;
}
