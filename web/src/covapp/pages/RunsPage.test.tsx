// RunsPage (Task 6 brief). Fixtures come from ../testUtils (makeIndex/
// makeRun) plus a small local RUNS/CONTRIB table shaped like the binding
// mockup's fixture data (contexts-page.html) so tests read the same way a
// person eyeballing that page would.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { makeIndex, makeRun, Providers } from "../testUtils";
import type { IndexPayload, RunJson } from "../types";
import { RunsPage } from "./RunsPage";

function renderPage(index: IndexPayload) {
  return render(<RunsPage index={index} />, { wrapper: Providers });
}

const RUNS: RunJson[] = [
  makeRun({
    id: 1,
    tier: "system",
    label: "nightly-full",
    board: "stm32h7-rev3",
    host: "router-a",
    labs: ["lab-1", "lab-2"],
    captured_at: "2026-07-21",
    base_commit: "8f3c21a000ff",
    note: "full regression sweep",
  }),
  makeRun({
    id: 2,
    tier: "system",
    label: "nightly-full",
    board: "stm32h7-rev3",
    host: "router-b",
    labs: ["lab-1", "lab-2"],
    captured_at: "2026-07-21",
    base_commit: "8f3c21a000ff",
    note: "full regression sweep",
  }),
  makeRun({
    id: 3,
    tier: "unit",
    label: "unit harvest",
    board: "host",
    host: "ci-01",
    labs: [],
    captured_at: "2026-07-22",
    base_commit: "8f3c21a000ff",
  }),
  makeRun({
    id: 4,
    tier: "manual",
    label: "field bring-up",
    board: "stm32h7-rev2",
    host: "bench-3",
    labs: ["lab-3"],
    captured_at: "2026-06-02",
    tester: { name: "M. Reyes" },
    ticket: "FW-1188",
    base_commit: "41d9e02aabbc",
    dirty_remap: true,
    aging: true,
  }),
  makeRun({
    id: 5,
    tier: "manual",
    label: "smoke-2025",
    board: "stm32f4-rev1",
    host: "bench-2",
    labs: ["lab-3"],
    captured_at: "2025-11-14",
    ticket: "FW-0961",
    base_commit: "c07771fzzzzz",
  }),
];

const CONTRIB: IndexPayload["run_contrib"] = {
  "1": {
    lines: 875,
    revoked: 0,
    files: [
      ["src/net/tcp.c", 289],
      ["src/core/sched.c", 171],
    ],
  },
  "2": {
    lines: 799,
    revoked: 0,
    files: [
      ["src/net/tcp.c", 200],
      ["src/main.c", 80],
    ],
  },
  "3": { lines: 968, revoked: 0, files: [["lib/parse.c", 176]] },
  "4": { lines: 71, revoked: 0, files: [["src/net/tcp.c", 44]] },
  "5": { lines: 0, revoked: 118, files: [] },
};

function buildIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return makeIndex({
    project_name: "acme-fw",
    tier_order: ["system", "unit", "manual"],
    tier_labels: { system: "System (e2e)", unit: "Unit", manual: "Manual" },
    tier_colors: { system: "green", unit: "blue", manual: "orange" },
    state_colors: { uncovered: "#f4a9a8", excluded: "grey", stale: "violet", aging: "tan" },
    total_lines: 1836,
    generated_at: "2026-07-23 14:02 UTC",
    runs: RUNS,
    run_contrib: CONTRIB,
    ...overrides,
  });
}

beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  window.location.hash = "";
  localStorage.clear();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
});

describe("RunsPage", () => {
  it("renders one row per context, with meta counting contexts and distinct hosts", () => {
    const index = buildIndex();
    renderPage(index);

    expect(screen.getByTestId("run-row-nightly-full")).toBeTruthy();
    expect(screen.getByTestId("run-row-unit harvest")).toBeTruthy();
    expect(screen.getByTestId("run-row-field bring-up")).toBeTruthy();
    expect(screen.getByTestId("run-row-smoke-2025")).toBeTruthy();

    const meta = screen.getByTestId("page-meta").textContent ?? "";
    expect(meta).toContain("4"); // 4 distinct labels -> 4 contexts
    // distinct hosts: router-a, router-b, ci-01, bench-3, bench-2 = 5
    expect(meta).toContain("5");
    expect(meta).toContain("2026-07-23 14:02 UTC");
  });

  it("renders the whole-repo stats card, same rows as the directory root", () => {
    const index = buildIndex();
    renderPage(index);
    const card = screen.getByTestId("stats-card");
    expect(card.textContent).toContain("Coverage — whole repo");
    expect(card.textContent).toContain("all contexts");
    expect(screen.getByTestId("stats-row-all")).toBeTruthy();
  });

  it("multi-host grouping: nightly-full is one row with two host pills and per-run lines", () => {
    const index = buildIndex();
    renderPage(index);
    const row = screen.getByTestId("run-row-nightly-full");
    expect(row.textContent).toContain("router-a");
    expect(row.textContent).toContain("router-b");
    // lines contributed = 875 + 799 = 1674
    expect(row.textContent).toContain("1674 / 1836");
  });

  it("renders 0 contexts, no rows, without crashing when payload.runs is empty", () => {
    const index = buildIndex({ runs: [], run_contrib: {} });
    renderPage(index);
    expect(screen.getByTestId("page-meta").textContent).toContain("0");
    expect(screen.queryByTestId("run-row-nightly-full")).toBeNull();
  });

  it("tier chips narrow the visible rows; 'All tiers' restores them", () => {
    const index = buildIndex();
    renderPage(index);

    fireEvent.click(screen.getByTestId("tier-chip-manual"));
    expect(screen.queryByTestId("run-row-nightly-full")).toBeNull();
    expect(screen.queryByTestId("run-row-unit harvest")).toBeNull();
    expect(screen.getByTestId("run-row-field bring-up")).toBeTruthy();
    expect(screen.getByTestId("run-row-smoke-2025")).toBeTruthy();

    fireEvent.click(screen.getByTestId("tier-chip-all"));
    expect(screen.getByTestId("run-row-nightly-full")).toBeTruthy();
    expect(screen.getByTestId("run-row-unit harvest")).toBeTruthy();
  });

  it("search narrows rows by ticket substring, case-insensitively", () => {
    const index = buildIndex();
    renderPage(index);

    const input = screen.getByTestId("runs-search").querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "fw-1188" } });

    expect(screen.getByTestId("run-row-field bring-up")).toBeTruthy();
    expect(screen.queryByTestId("run-row-nightly-full")).toBeNull();
    expect(screen.queryByTestId("run-row-smoke-2025")).toBeNull();
  });

  it("search narrows rows by host substring", () => {
    const index = buildIndex();
    renderPage(index);
    const input = screen.getByTestId("runs-search").querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "ci-01" } });
    expect(screen.getByTestId("run-row-unit harvest")).toBeTruthy();
    expect(screen.queryByTestId("run-row-nightly-full")).toBeNull();
  });

  it("clicking a row toggles its expanded detail (with a testid keyed on label)", () => {
    const index = buildIndex();
    renderPage(index);
    expect(screen.queryByTestId("run-detail-nightly-full")).toBeNull();
    fireEvent.click(screen.getByTestId("run-row-nightly-full"));
    expect(screen.getByTestId("run-detail-nightly-full")).toBeTruthy();
    fireEvent.click(screen.getByTestId("run-row-nightly-full"));
    expect(screen.queryByTestId("run-detail-nightly-full")).toBeNull();
  });

  it("expanded detail shows the base commit with a remap suffix when remapped", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-field bring-up"));
    const detail = screen.getByTestId("run-detail-field bring-up");
    expect(detail.textContent).toContain("41d9e02aabb"); // first 12 chars
    expect(detail.textContent).toContain("→ HEAD (remapped)");
  });

  it("expanded detail shows the base commit with NO remap suffix when not remapped", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-nightly-full"));
    const detail = screen.getByTestId("run-detail-nightly-full");
    expect(detail.textContent).toContain("8f3c21a000ff".slice(0, 12));
    expect(detail.textContent).not.toContain("remapped");
  });

  it("a stale context's contribution table replaces its body with a revoked spanning row", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-smoke-2025"));
    const detail = screen.getByTestId("run-detail-smoke-2025");
    expect(detail.textContent).toContain("118 credits revoked — anchor unverifiable");
    expect(screen.queryByTestId("contrib-branch-na")).toBeNull();
  });

  it("a stale context's row shows 'N revoked' and a zero-width bar", () => {
    const index = buildIndex();
    renderPage(index);
    const row = screen.getByTestId("run-row-smoke-2025");
    expect(row.textContent).toContain("118 revoked");
    const bar = row.querySelector('div[style*="background"]');
    expect(bar?.getAttribute("style")).toContain("width: 0%");
  });

  it("a stale context's top files show the empty-credits message", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-smoke-2025"));
    const detail = screen.getByTestId("run-detail-smoke-2025");
    expect(detail.textContent).toContain(
      "no live credits — every line this capture touched has changed",
    );
  });

  it("a non-stale context's Branch row renders the not-tracked-per-run cell", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-nightly-full"));
    const branchCell = screen.getByTestId("contrib-branch-na");
    expect(branchCell.textContent).toContain("not tracked per-run");
  });

  it("top files render a link to #/coverage/<encoded path>", () => {
    const index = buildIndex();
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-nightly-full"));
    const detail = screen.getByTestId("run-detail-nightly-full");
    const link = within(detail).getByText("src/net/tcp.c") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("#/coverage/src/net/tcp.c");
  });

  it("encodes a top-file path segment containing a reserved character", () => {
    const index = buildIndex({
      runs: [makeRun({ id: 9, label: "weird-path", tier: "system" })],
      run_contrib: { "9": { lines: 5, revoked: 0, files: [["src/100%.c", 5]] } },
    });
    renderPage(index);
    fireEvent.click(screen.getByTestId("run-row-weird-path"));
    const detail = screen.getByTestId("run-detail-weird-path");
    const link = within(detail).getByText("src/100%.c") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("#/coverage/src/100%25.c");
  });

  describe("focus", () => {
    it("'Focus this context' pins focus: the app-bar chip appears and a toast fires", () => {
      const index = buildIndex();
      window.__OTTO_COV__ = index;
      renderPage(index);
      fireEvent.click(screen.getByTestId("run-row-nightly-full"));
      const btn = screen.getByTestId("focus-context-btn");
      expect(btn.textContent).toContain("Focus this context");

      fireEvent.click(btn);

      expect(screen.getByTestId("focus-chip").textContent).toContain("nightly-full");
      expect(screen.getByTestId("toast").textContent).toBe("Focused nightly-full");
      // Detail stays open/unaffected — rows themselves are not touched by focus.
      expect(screen.getByTestId("run-detail-nightly-full")).toBeTruthy();
    });

    it("the stats card re-scopes to the focused context (single Context row, 'focused: <label>' scope)", () => {
      const index = buildIndex();
      window.__OTTO_COV__ = index;
      renderPage(index);
      fireEvent.click(screen.getByTestId("run-row-nightly-full"));
      fireEvent.click(screen.getByTestId("focus-context-btn"));

      const card = screen.getByTestId("stats-card");
      expect(card.textContent).toContain("focused: nightly-full");
      expect(card.textContent).toContain("Context");
      expect(screen.getByTestId("stats-row-ctx")).toBeTruthy();
      expect(screen.queryByTestId("stats-row-all")).toBeNull();
    });

    it("the chip's ✕ clears focus, restoring the whole-repo stats card and a 'Focus cleared' toast", () => {
      const index = buildIndex();
      window.__OTTO_COV__ = index;
      renderPage(index);
      fireEvent.click(screen.getByTestId("run-row-nightly-full"));
      fireEvent.click(screen.getByTestId("focus-context-btn"));
      expect(screen.getByTestId("focus-chip")).toBeTruthy();

      fireEvent.click(screen.getByTestId("focus-clear"));

      expect(screen.queryByTestId("focus-chip")).toBeNull();
      expect(screen.getByTestId("toast").textContent).toBe("Focus cleared");
      expect(screen.getByTestId("stats-card").textContent).toContain("all contexts");
      expect(screen.getByTestId("stats-row-all")).toBeTruthy();
    });

    it("the ⋮ menu switcher can re-pin focus to a different context from this page", async () => {
      const user = userEvent.setup();
      const index = buildIndex();
      window.__OTTO_COV__ = index;
      renderPage(index);
      fireEvent.click(screen.getByTestId("run-row-nightly-full"));
      fireEvent.click(screen.getByTestId("focus-context-btn"));

      await user.click(screen.getByTestId("appbar-menu"));
      await user.click(await screen.findByTestId("menu-focus-unit harvest"));

      expect(screen.getByTestId("focus-chip").textContent).toContain("unit harvest");
      expect(screen.getByTestId("stats-card").textContent).toContain("focused: unit harvest");
    });
  });
});
