import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { OverviewPage } from "../pages/OverviewPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

function load(range: { from: number; to: number } | null = null) {
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  if (range) useReviewStore.getState().actions.setRange(range);
  return render(<OverviewPage />);
}

afterEach(() => {
  cleanup();
  useReviewStore.setState({
    sessions: [],
    rawDocument: null,
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
  useReviewStore.getState().actions.importText(KITCHEN, "kitchen-sink.json");
  return useReviewStore.getState().sessions[0].startMs;
}
