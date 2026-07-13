import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

const setOptions: Record<string, unknown>[] = [];
vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => ({
      group: "",
      setOption: (o: Record<string, unknown>) => setOptions.push(o),
      on: () => {},
      resize: () => {},
      dispose: () => {},
    }),
    connect: () => {},
  },
}));

import { useReviewStore } from "../data/reviewStore";
import { SubjectPage } from "../pages/SubjectPage";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: mockSubject }) };
});
let mockSubject = "chassis-a_lc1";

function load(subject: string) {
  mockSubject = subject;
  useReviewStore.getState().actions.importMonitorSessions(KITCHEN, "kitchen-sink.json");
  return render(<SubjectPage />);
}

afterEach(() => {
  cleanup();
  setOptions.length = 0;
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

describe("SubjectPage chart stack", () => {
  it("renders one chart panel per chart group with data", () => {
    load("chassis-a_lc1");
    expect(screen.getByTestId("chart-stack")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-cpu")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-psu-temp")).toBeTruthy();
  });

  it("keeps the pinned series-summary format", () => {
    load("chassis-a_lc1");
    expect(screen.getByTestId("series-summary").textContent).toMatch(
      /^\d+ series · \d+ samples in range$/,
    );
  });

  it("unchecking a series removes it from the chart options", () => {
    load("chassis-a_lc1");
    const before = setOptions.length;
    fireEvent.click(screen.getByTestId("series-node-CPU %"));
    expect(setOptions.length).toBeGreaterThan(before);
    const last = setOptions[setOptions.length - 1] as { series: { id: string }[] };
    // The cpu chart re-rendered without its only series -> panel unmounts;
    // whichever option was applied last must not contain the cpu series id.
    expect(last.series.every((s) => s.id !== "CPU %")).toBe(true);
  });

  it("element subject renders member series", () => {
    load("chassis-a");
    expect(screen.getByTestId("chart-panel-cpu")).toBeTruthy();
    expect(screen.getByTestId("chart-panel-ambient")).toBeTruthy();
  });

  it("unknown subject keeps the not-found branch", () => {
    load("ghost");
    expect(screen.getByTestId("not-found")).toBeTruthy();
  });
});

describe("SubjectPage log tables", () => {
  it("renders the kernel table for a host with rows and filters it", () => {
    load("db-01");
    const table = screen.getByTestId("log-table-kernel");
    const rowsBefore = table.querySelectorAll("tbody tr").length;
    expect(rowsBefore).toBeGreaterThan(0);
    fireEvent.change(
      screen.getByTestId("log-filter-kernel").querySelector("input") as HTMLInputElement,
      { target: { value: "definitely-not-present" } },
    );
    expect(screen.getByTestId("log-table-kernel").querySelectorAll("tbody tr")).toHaveLength(0);
  });

  it("renders no table for a host without rows", () => {
    load("workers_w1");
    expect(screen.queryByTestId("log-table-kernel")).toBeNull();
  });
});
