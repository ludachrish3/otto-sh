import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// jsdom has no ResizeObserver, and (without the optional "canvas" npm
// package) HTMLCanvasElement#getContext returns null, which crashes real
// echarts/zrender on init/dispose. The Subject page (Plan 3 Task 6) now
// renders real ChartPanel instances via the full App, so this file needs
// the same two shims chartpanel.test.tsx / subjectpage.test.tsx already
// carry for the same reason.
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

import App from "../App";
import { presetRange, sessionBounds } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";

const __dir = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(__dir, "../../fixtures/kitchen-sink.json"), "utf-8");

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
}

beforeEach(() => {
  window.location.hash = "#/";
});
afterEach(() => {
  // Same rationale as reviewbar.test.tsx: cleanup() is needed because
  // vitest's config doesn't set `test.globals: true`.
  cleanup();
  resetStore();
});

async function importKitchen() {
  const file = new File([KITCHEN], "kitchen-sink.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("overview-page")).toBeTruthy());
}

describe("Overview page", () => {
  it("renders element sections incl. the empty chassis", async () => {
    render(<App />);
    await importKitchen();
    expect(screen.getByTestId("element-section-chassis-a")).toBeTruthy();
    expect(screen.getByTestId("element-section-spare-chassis")).toBeTruthy();
    expect(screen.getByTestId("subject-link-chassis-a_lc1")).toBeTruthy();
  });
});

describe("Subject page", () => {
  it("navigates by hash and shows range-scoped series counts", async () => {
    render(<App />);
    await importKitchen();
    fireEvent.click(screen.getByTestId("subject-link-workers_w2"));
    await waitFor(() => expect(screen.getByTestId("subject-page")).toBeTruthy());
    expect(window.location.hash).toBe("#/host/workers_w2");
    expect(screen.getByTestId("subject-title").textContent).toContain("workers_w2");
    const fullText = screen.getByTestId("series-summary").textContent ?? "";

    const session = useReviewStore.getState().sessions[0];
    useReviewStore.getState().actions.setRange(presetRange(sessionBounds(session), 15));
    await waitFor(() => {
      expect(screen.getByTestId("series-summary").textContent).not.toBe(fullText);
    });
  });

  it("unknown subjects render not-found, empty store renders empty state", async () => {
    render(<App />);
    await importKitchen();
    window.location.hash = "#/host/nope";
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeTruthy());
  });
});

describe("Topology page", () => {
  it("sizes the topology canvas by flex, not by a guessed chrome height", async () => {
    // h-[calc(100vh-6.5rem)] hardcoded AppBar + ReviewBar's height. ReviewBar is
    // flex-wrap, so below ~1150px it wraps and that constant is wrong — the canvas
    // ends up overtall and the page scrolls. (Playwright's default 1280x720 never
    // triggers the wrap, which is why the e2e regression guard forces 1100px.)
    // Same class as the two occlusion bugs in #134: a magic constant that is
    // stale exactly where it matters.
    render(<App />);
    await importKitchen();
    window.location.hash = "#/topology";
    const main = await screen.findByTestId("topology-page");
    expect(main.className).not.toContain("100vh");
    expect(main.className).toContain("flex-1");
  });

  it("hides the minimap by default and shows it when toggled", async () => {
    render(<App />);
    await importKitchen();
    window.location.hash = "#/topology";
    await screen.findByTestId("topology-page");
    expect(screen.queryByTestId("topo-minimap")).toBeNull();
    fireEvent.click(screen.getByTestId("minimap-toggle"));
    expect(await screen.findByTestId("topo-minimap")).toBeTruthy();
  });
});
