// DirectoryPage (Task 4 brief) built on ui/TreeView + Task 3's AppShell/
// StatsCard. Fixtures come from ../testUtils (Task 4's testUtils migration).
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { emptyStats, makeIndex, Providers } from "../testUtils";
import type { DirNode, IndexPayload, RunJson } from "../types";
import { DirectoryPage } from "./DirectoryPage";

function renderPage(props: { index: IndexPayload; segments: string[] }) {
  return render(<DirectoryPage {...props} />, { wrapper: Providers });
}

const RUNS: RunJson[] = [
  {
    id: 1,
    tier: "system",
    label: "router-a (system bed)",
    board: "stm32h7-rev3",
    host: "host-1",
    labs: ["lab-1", "lab-2"],
    captured_at: "2026-07-21",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "abc123",
    dirty_remap: false,
    aging: false,
  },
  {
    id: 2,
    tier: "unit",
    label: "unit harvest",
    board: "host",
    host: "host-2",
    labs: [],
    captured_at: "2026-07-22",
    tester: { name: "M. Reyes" },
    ticket: "FW-1188",
    note: null,
    base_commit: "def456",
    dirty_remap: true,
    aging: true,
  },
];

function buildTree(): DirNode {
  const mainStats = emptyStats({
    lines: { total: 20, hit: 15, per_tier: { system: 10, unit: 12 } },
    branches: { total: 8, hit: 5, per_tier: { system: 3, unit: 4 } },
    flags: { stale: 2, aging: 1, excluded: 3 },
    ctx_lines: { "router-a (system bed)": 6 },
  });
  const utilStats = emptyStats({
    lines: { total: 5, hit: 5, per_tier: { system: 5, unit: 5 } },
    branches: { total: 2, hit: 2, per_tier: { system: 2, unit: 2 } },
    ctx_lines: { "router-a (system bed)": 5 },
  });
  const srcStats = emptyStats({
    lines: { total: 25, hit: 20, per_tier: { system: 15, unit: 17 } },
    branches: { total: 10, hit: 7, per_tier: { system: 5, unit: 6 } },
    flags: { stale: 2, aging: 1, excluded: 3 },
    ctx_lines: { "router-a (system bed)": 11 },
  });
  return {
    name: "acme-fw",
    dirs: [
      {
        name: "src",
        dirs: [],
        files: [
          { name: "main.c", path: "src/main.c", chunk: "src_main.c", stats: mainStats },
          { name: "util.c", path: "src/util.c", chunk: "src_util.c", stats: utilStats },
        ],
        stats: srcStats,
      },
    ],
    files: [],
    stats: srcStats,
  };
}

function buildIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return makeIndex({
    project_name: "acme-fw",
    tier_order: ["system", "unit"],
    tier_labels: { system: "System (e2e)", unit: "Unit" },
    tier_colors: { system: "green", unit: "blue" },
    total_lines: 25,
    runs: RUNS,
    tree: buildTree(),
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

describe("DirectoryPage", () => {
  it("renders rollup numbers, meta line, and title at the repo root", () => {
    const index = buildIndex();
    renderPage({ index, segments: [] });

    expect(screen.getByText("acme-fw", { selector: "h1" })).toBeTruthy();
    const meta = screen.getByTestId("page-meta").textContent ?? "";
    expect(meta).toContain("2"); // main.c + util.c, recursive
    expect(meta).toContain(index.generated_at);
    expect(meta).toContain(index.otto_version);

    const allRow = screen.getByTestId("stats-row-all");
    expect(allRow.textContent).toContain("80.0%"); // 20/25 lines
    expect(allRow.textContent).toContain("20/25");
  });

  it("renders one tier column per tier_order entry, in tier_order's order", () => {
    const index = buildIndex({ tier_order: ["unit", "system"] });
    renderPage({ index, segments: [] });

    const tierCols = Array.from(document.querySelectorAll('[data-testid^="tree-col-tier:"]')).map(
      (el) => el.getAttribute("data-testid"),
    );
    expect(tierCols).toEqual(["tree-col-tier:unit", "tree-col-tier:system"]);
  });

  it("renders with no tier columns and no per-tier stats rows when tier_order is empty", () => {
    const index = buildIndex({ tier_order: [], tier_labels: {}, tier_colors: {} });
    renderPage({ index, segments: [] });

    expect(document.querySelectorAll('[data-testid^="tree-col-tier:"]').length).toBe(0);
    expect(screen.getByTestId("stats-row-all")).toBeTruthy();
    expect(screen.queryByTestId("stats-row-system")).toBeNull();
    expect(screen.getByTestId("tree-col-name")).toBeTruthy();
  });

  it("renders flag pills only for counts > 0, per row", () => {
    const index = buildIndex();
    renderPage({ index, segments: [] });

    const mainRow = screen.getByTestId("tree-row-file:src/main.c");
    expect(mainRow.textContent).toContain("2 stale");
    expect(mainRow.textContent).toContain("1 aging");
    expect(mainRow.textContent).toContain("3 excl");

    const utilRow = screen.getByTestId("tree-row-file:src/util.c");
    expect(utilRow.textContent).not.toContain("stale");
    expect(utilRow.textContent).not.toContain("aging");
    expect(utilRow.textContent).not.toContain("excl");
  });

  it("drills into segments ['src']: re-scoped crumbs, title, stats scope, and rows", () => {
    const index = buildIndex();
    renderPage({ index, segments: ["src"] });

    expect(screen.getByText("src/", { selector: "h1" })).toBeTruthy();

    const crumbs = screen.getByTestId("breadcrumbs");
    expect(within(crumbs).getByText("acme-fw").getAttribute("href")).toBe("#/coverage");
    expect(within(crumbs).getByText("src").getAttribute("aria-current")).toBe("page");

    expect(screen.getByTestId("stats-card").textContent).toContain("src/");

    // Files directly under src show as top-level rows; "src" itself is not
    // shown as a row (we're already viewing inside it).
    expect(screen.getByTestId("tree-row-file:src/main.c")).toBeTruthy();
    expect(screen.getByTestId("tree-row-file:src/util.c")).toBeTruthy();
    expect(screen.queryByTestId("tree-row-dir:src")).toBeNull();
  });

  it("shows the runs disclosure (collapsed) only at the repo root", () => {
    const index = buildIndex();
    const { unmount } = renderPage({ index, segments: [] });
    const disclosure = screen.getByTestId("runs-disclosure");
    expect(disclosure.textContent).toContain("2"); // run count
    const toggle = within(disclosure).getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    unmount();

    renderPage({ index, segments: ["src"] });
    expect(screen.queryByTestId("runs-disclosure")).toBeNull();
  });

  it("clicking a directory name navigates to #/coverage/<path>", () => {
    const index = buildIndex();
    renderPage({ index, segments: [] });
    fireEvent.click(screen.getByTestId("name-dir:src"));
    expect(window.location.hash).toBe("#/coverage/src");
  });

  it("clicking a file name navigates to #/coverage/<FileNode.path>", () => {
    const index = buildIndex();
    renderPage({ index, segments: ["src"] });
    fireEvent.click(screen.getByTestId("name-file:src/main.c"));
    expect(window.location.hash).toBe("#/coverage/src/main.c");
  });

  // Regression: a raw "%"/"#" in a display name is legal (real filenames
  // like "100%.c" exist) but "%"/"#" are reserved in a URI fragment.
  // onNavigate must `encodeURIComponent` each path segment before writing
  // the hash, symmetric with App.tsx's `segmentsFromWildcard`, which
  // `decodeURIComponent`s each segment on the way back in — otherwise the
  // next render throws `URIError` with no error boundary to catch it.
  it("encodes a file name containing '%' so the emitted hash round-trips via decodeURIComponent", () => {
    const index = buildIndex({
      tree: {
        name: "acme-fw",
        dirs: [],
        files: [{ name: "100%.c", path: "100%.c", chunk: "100_.c", stats: emptyStats() }],
        stats: emptyStats(),
      },
    });
    renderPage({ index, segments: [] });
    fireEvent.click(screen.getByTestId("name-file:100%.c"));

    const hash = window.location.hash;
    expect(hash).toBe("#/coverage/100%25.c");
    expect(decodeURIComponent(hash.slice("#/coverage/".length))).toBe("100%.c");
  });

  it("encodes a directory name containing '#' so the emitted hash round-trips via decodeURIComponent", () => {
    const index = buildIndex({
      tree: {
        name: "acme-fw",
        dirs: [{ name: "notes#1", dirs: [], files: [], stats: emptyStats() }],
        files: [],
        stats: emptyStats(),
      },
    });
    renderPage({ index, segments: [] });
    fireEvent.click(screen.getByTestId("name-dir:notes#1"));

    const hash = window.location.hash;
    expect(hash).toBe("#/coverage/notes%231");
    expect(decodeURIComponent(hash.slice("#/coverage/".length))).toBe("notes#1");
  });

  describe("under focus", () => {
    function renderFocused(ctxLabel: string, segments: string[] = []) {
      const index = buildIndex();
      window.__OTTO_COV__ = index;
      window.location.hash = `#/coverage${
        segments.length ? `/${segments.join("/")}` : ""
      }?ctx=${encodeURIComponent(ctxLabel)}`;
      renderPage({ index, segments });
      return index;
    }

    // Column order (buildColumns): [name, lines, line%, branch%, tier:system,
    // tier:unit, flags] — `row.children[0]` is the name wrapper, so data
    // cells start at index 1 in that same order.
    it("Lines/Line % recompute from ctx_lines, the focused tier mirrors it, other tiers read 0.0%, Branch % is '—'", () => {
      renderFocused("router-a (system bed)");

      const mainRow = screen.getByTestId("tree-row-file:src/main.c");
      const cells = mainRow.children;
      expect(cells[1].textContent).toBe("6/20");
      expect(cells[2].textContent).toContain("30.0%");
      expect(cells[3].textContent).toBe("—");
      expect(cells[4].textContent).toContain("30.0%"); // tier:system (the focused context's tier)
      expect(cells[5].textContent).toContain("0.0%"); // tier:unit (not the focused context's tier)
    });

    it("recomputes per row, not just once (util.c differs from main.c)", () => {
      renderFocused("router-a (system bed)");
      const utilRow = screen.getByTestId("tree-row-file:src/util.c");
      const cells = utilRow.children;
      expect(cells[1].textContent).toBe("5/5");
      expect(cells[2].textContent).toContain("100.0%");
    });

    it("the stats card shows the single focused Context row (scope, header, fractions, muted branch/decision)", () => {
      renderFocused("router-a (system bed)", ["src"]);

      const card = screen.getByTestId("stats-card");
      expect(card.textContent).toContain("focused: router-a (system bed)");
      expect(card.textContent).toContain("Context");
      expect(card.textContent).not.toContain("Tier");

      const row = screen.getByTestId("stats-row-ctx");
      expect(row.textContent).toContain("router-a (system bed)");
      expect(row.textContent).toContain("11/25");
      expect(row.textContent).toContain("44.0%");
      expect(row.textContent?.match(/no data/g)?.length).toBe(2); // branch + decision
    });

    it("shows the app-bar focus chip while focused", () => {
      renderFocused("router-a (system bed)");
      expect(screen.getByTestId("focus-chip").textContent).toContain("router-a (system bed)");
    });

    it("a row with no ctx_lines entry for the focused context reads 0, not a crash", () => {
      // "unit harvest" never appears in any node's ctx_lines fixture above.
      renderFocused("unit harvest");
      const mainRow = screen.getByTestId("tree-row-file:src/main.c");
      expect(mainRow.children[1].textContent).toBe("0/20");
      expect(mainRow.children[2].textContent).toContain("0.0%");
    });
  });
});
