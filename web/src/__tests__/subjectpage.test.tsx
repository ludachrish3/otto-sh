import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

// jsdom lacks CSS.escape; react-aria's tab selection calls it on click (the
// live-window presets are button-border tabs now) — same polyfill as
// viewswitcher.test.tsx / shell.test.tsx.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

const setOptions: Record<string, unknown>[] = [];
vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => ({
      group: "",
      setOption: (o: Record<string, unknown>) => setOptions.push(o),
      on: () => {},
      dispatchAction: () => {},
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

  it("unchecking a series removes it from the chart options", async () => {
    load("chassis-a_lc1");
    const before = setOptions.length;
    // Untitled UI's Checkbox puts data-testid on the <label>, not the
    // visually-hidden <input> — react-aria's Checkbox needs the real
    // pointer-event sequence userEvent produces (fireEvent.click doesn't
    // drive it), and jsdom doesn't replay that sequence's label->control
    // forwarding reliably, so this clicks the checkbox role directly (see
    // seriespanel.test.tsx's "checkbox toggle reports the series key").
    const user = userEvent.setup();
    const checkbox = within(screen.getByTestId("series-node-CPU %")).getByRole("checkbox");
    await user.click(checkbox);
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

// Task 7 (spec decision 10): the live-window presets (button-border tabs) moved here from
// AppBar (Task 6, Plan 5b follow-ups) — 5m/15m/1h, live-only, selection
// derived from `windowMs` rather than stored (same "derive, don't store"
// lesson as reviewStore's `useIsPaused`). Moved verbatim from
// livechrome.test.tsx, now driving <SubjectPage /> instead of <AppBar />.
describe("SubjectPage live window control", () => {
  afterEach(() => {
    // `mode`/`windowMs` aren't part of the top-level afterEach's reset
    // above — these tests are the only ones in this file that touch them,
    // so they're restored here rather than leaking "live" mode (which
    // changes window_'s derivation to liveRange, not session bounds) into
    // every other test in this file. This afterEach runs before the outer
    // one's cleanup(), so the component is still mounted -> act.
    act(() => {
      useReviewStore.setState({ mode: null, windowMs: 900_000 });
    });
  });

  it("renders only in live mode", () => {
    load("chassis-a_lc1");
    expect(screen.queryByTestId("live-window")).toBeNull();
    cleanup();
    useReviewStore.setState({ mode: "live" });
    load("chassis-a_lc1");
    expect(screen.getByTestId("live-window")).toBeTruthy();
  });

  it("the selected item reflects windowMs, not a separately stored choice", () => {
    useReviewStore.setState({ mode: "live" });
    load("chassis-a_lc1");
    // Default windowMs (900_000, the store's own default) -> "15m" selected.
    expect(screen.getByTestId("live-window-15m").getAttribute("data-selected")).not.toBeNull();
    expect(screen.getByTestId("live-window-5m").getAttribute("data-selected")).toBeNull();
    expect(screen.getByTestId("live-window-1h").getAttribute("data-selected")).toBeNull();

    cleanup();
    useReviewStore.setState({ windowMs: 3_600_000 });
    load("chassis-a_lc1");
    expect(screen.getByTestId("live-window-1h").getAttribute("data-selected")).not.toBeNull();
    expect(screen.getByTestId("live-window-15m").getAttribute("data-selected")).toBeNull();
  });

  it("clicking a preset calls setWindow with that preset's width", async () => {
    // usePress (react-aria) listens for pointer events, not the single
    // synthetic `click` fireEvent dispatches — userEvent synthesizes the
    // full pointerdown/pointerup/click sequence (same reasoning as
    // overview.test.tsx's session-picker helper).
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live" });
    load("chassis-a_lc1");
    await user.click(screen.getByTestId("live-window-5m"));
    expect(useReviewStore.getState().windowMs).toBe(300_000);
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
