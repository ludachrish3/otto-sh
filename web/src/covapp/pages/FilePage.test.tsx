// FilePage's contract (Task 5 brief). `loadFileChunk` is spied per test via
// `import * as dataModule` (not `vi.mock` at module scope) so the LAST test
// in this file can exercise the real script-injection/callback wiring
// (same technique data.test.ts uses) without fighting a module-level mock.
// Shiki highlighting is NEVER mocked here — createJavaScriptRegexEngine is
// pure JS, so real highlighting runs under vitest; every rendering
// assertion below `await`s past the chunk-load + highlight promises via
// `findBy*` queries.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as dataModule from "../data";
import { _resetForTests, StampMismatchError } from "../data";
import { emptyStats, makeIndex, Providers } from "../testUtils";
import type { FileChunk, FileNode, IndexPayload, RunJson } from "../types";
import { FilePage, rowClassFor, rowClassForFocus } from "./FilePage";

function renderPage(props: { index: IndexPayload; segments: string[]; node: FileNode }) {
  return render(<FilePage {...props} />, { wrapper: Providers });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  _resetForTests();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  window.location.hash = "";
  localStorage.clear();
});

describe("rowClassFor", () => {
  const tierOrder = ["system", "unit"];

  it("excluded beats a tier hit", () => {
    expect(rowClassFor({ hits: { system: 4 }, branches: [], state: null }, true, tierOrder)).toBe(
      "s-excl",
    );
  });

  it("tier 0 (system) beats tier 1 (unit) when both have hits", () => {
    expect(
      rowClassFor({ hits: { system: 1, unit: 1 }, branches: [], state: null }, false, tierOrder),
    ).toBe("t-system");
  });

  it("falls through to tier 1 when only it has a hit", () => {
    expect(
      rowClassFor({ hits: { system: 0, unit: 1 }, branches: [], state: null }, false, tierOrder),
    ).toBe("t-unit");
  });

  it("a tier hit beats both aging and stale state flags", () => {
    expect(
      rowClassFor({ hits: { system: 1 }, branches: [], state: "aging" }, false, tierOrder),
    ).toBe("t-system");
    expect(
      rowClassFor({ hits: { system: 1 }, branches: [], state: "stale" }, false, tierOrder),
    ).toBe("t-system");
  });

  it("aging beats stale when neither has a hit", () => {
    expect(rowClassFor({ hits: {}, branches: [], state: "aging" }, false, tierOrder)).toBe(
      "s-aging",
    );
  });

  it("stale beats plain uncovered", () => {
    expect(rowClassFor({ hits: {}, branches: [], state: "stale" }, false, tierOrder)).toBe(
      "s-stale",
    );
  });

  it("a record with no hits and no state is plain uncovered (s-unc)", () => {
    expect(rowClassFor({ hits: {}, branches: [], state: null }, false, tierOrder)).toBe("s-unc");
  });

  it("no LineRecord at all is uncoverable — empty string, never a state class", () => {
    expect(rowClassFor(undefined, false, tierOrder)).toBe("");
  });

  it("excluded still wins even with no LineRecord", () => {
    expect(rowClassFor(undefined, true, tierOrder)).toBe("s-excl");
  });
});

describe("rowClassForFocus", () => {
  const memberIds = new Set([1]);

  it("excluded beats a member-run hit", () => {
    expect(
      rowClassForFocus(
        { hits: {}, branches: [], state: null, run: { "1": 4 } },
        true,
        memberIds,
        "system",
      ),
    ).toBe("s-excl");
  });

  it("a member run's hit tints by the focused context's tier", () => {
    expect(
      rowClassForFocus(
        { hits: {}, branches: [], state: null, run: { "1": 3 } },
        false,
        memberIds,
        "system",
      ),
    ).toBe("t-system");
  });

  it("a non-member run's hit is plain uncovered (s-unc), not tinted", () => {
    expect(
      rowClassForFocus(
        { hits: {}, branches: [], state: null, run: { "2": 9 } },
        false,
        memberIds,
        "system",
      ),
    ).toBe("s-unc");
  });

  it("no run data at all is plain uncovered (s-unc) regardless of aging/stale state", () => {
    expect(
      rowClassForFocus({ hits: {}, branches: [], state: "stale" }, false, memberIds, "system"),
    ).toBe("s-unc");
  });

  it("no LineRecord at all is uncoverable — empty string, never a state class", () => {
    expect(rowClassForFocus(undefined, false, memberIds, "system")).toBe("");
  });

  it("excluded still wins even with no LineRecord", () => {
    expect(rowClassForFocus(undefined, true, memberIds, "system")).toBe("s-excl");
  });
});

const RUNS: RunJson[] = [
  {
    id: 1,
    tier: "system",
    label: "nightly-full",
    board: "",
    host: "router-a",
    labs: [],
    captured_at: "2026-07-20",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "abc",
    dirty_remap: false,
    aging: false,
  },
  {
    id: 2,
    tier: "system",
    label: "legacy checksum",
    board: "bench-3",
    host: "",
    labs: [],
    captured_at: "2026-06-01",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "def",
    dirty_remap: false,
    aging: false,
  },
  {
    id: 3,
    tier: "unit",
    label: "field bring-up",
    board: "",
    host: "bench-2",
    labs: [],
    captured_at: "2026-07-22",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "ghi",
    dirty_remap: false,
    aging: true,
  },
];

const SOURCE = [
  "int main(void) {",
  "    return 0;",
  "    /* excluded */",
  "}",
  "static void legacy(void)",
  "    do_thing();",
  "}",
].join("\n");

function makeChunk(overrides: Partial<FileChunk> = {}): FileChunk {
  return {
    stamp: "stamp-1",
    chunk: "src_net_tcp.c",
    path: "src/net/tcp.c",
    source: SOURCE,
    lines: {
      "1": { hits: { system: 5, unit: 0 }, branches: [], state: null, run: { "1": 5 } },
      "2": {
        hits: { system: 0, unit: 0 },
        branches: [
          {
            block: 0,
            branch: 0,
            hits: { system: 0, unit: 0 },
            reachable: { system: true, unit: true },
          },
          {
            block: 0,
            branch: 1,
            hits: { system: 0, unit: 0 },
            reachable: { system: false, unit: false },
          },
        ],
        state: null,
      },
      "5": {
        hits: { system: 0, unit: 0 },
        branches: [],
        state: "stale",
        run: { "2": 41 },
        stale_run: [2],
      },
      "6": { hits: { system: 0, unit: 0 }, branches: [], state: null, run: { "3": 9 } },
      // Past-EOF: no source line 999 exists (only 7 lines) — must count in
      // stats without ever becoming a rendered row.
      "999": { hits: { system: 2, unit: 0 }, branches: [], state: null },
    },
    excluded: [3],
    ...overrides,
  };
}

function makeFileIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return makeIndex({
    project_name: "acme-fw",
    generated_at: "2026-07-25 14:02 UTC",
    otto_version: "0.7.5",
    tier_order: ["system", "unit"],
    tier_labels: { system: "System (e2e)", unit: "Unit" },
    tier_colors: { system: "#2f9e6e", unit: "#eab308" },
    state_colors: { uncovered: "#f4a9a8", excluded: "grey", stale: "violet", aging: "tan" },
    thresholds: { high: 80, medium: 70 },
    runs: RUNS,
    ...overrides,
  });
}

const NODE: FileNode = {
  name: "tcp.c",
  path: "src/net/tcp.c",
  chunk: "src_net_tcp.c",
  stats: emptyStats(),
};

/** Spies on the real `loadFileChunk` export (not a module-level `vi.mock`,
 * so the last test in this file can leave it un-spied and exercise the
 * genuine script-injection path). */
function mockChunkLoad(result: { resolve: FileChunk } | { reject: Error }) {
  const spy = vi.spyOn(dataModule, "loadFileChunk");
  if ("resolve" in result) spy.mockResolvedValue(result.resolve);
  else spy.mockRejectedValue(result.reject);
  return spy;
}

describe("FilePage", () => {
  it("shows a minimal loading state before the chunk resolves", () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    expect(screen.getByTestId("file-loading").textContent).toContain("tcp.c");
  });

  it("routes a StampMismatchError to the guard screen with the stamp-mismatch reason", async () => {
    mockChunkLoad({ reject: new StampMismatchError("src_net_tcp.c") });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const guard = await screen.findByTestId("guard-screen");
    expect(guard.textContent).toContain("report changed on disk");
  });

  it("routes any other load error to the guard screen with the missing-data reason", async () => {
    mockChunkLoad({ reject: new Error("network broke") });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const guard = await screen.findByTestId("guard-screen");
    expect(guard.textContent).toContain("missing data");
  });

  it("renders one row per SOURCE line (not per chunk.lines key) — past-EOF '999' never becomes a row", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    await screen.findByTestId("code-row-1");

    for (let n = 1; n <= 7; n++) {
      expect(screen.getByTestId(`code-row-${n}`)).toBeTruthy();
    }
    expect(screen.queryByTestId("code-row-999")).toBeNull();
    expect(screen.queryByTestId("code-row-8")).toBeNull();
  });

  it("shows the C lang badge for a .c file", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const badge = await screen.findByTestId("lang-badge");
    expect(badge.textContent).toBe("C");
  });

  it("shows a muted '·' for a zero-hit cell, and the raw count for a hit cell", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const row1 = await screen.findByTestId("code-row-1");
    expect(row1.textContent).toContain("5"); // system hit count
    expect(row1.textContent).toContain("·"); // unit column, zero

    const row2 = screen.getByTestId("code-row-2");
    // both tier columns zero on row 2
    expect((row2.textContent?.match(/·/g) ?? []).length).toBeGreaterThanOrEqual(2);
  });

  it("gives an excluded line 's-excl' even though it has no chunk.lines record", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const row3 = await screen.findByTestId("code-row-3");
    expect(row3.className).toContain("s-excl");
  });

  it("gives a blank/uncoverable line the empty rowClass (no tint class)", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const row4 = await screen.findByTestId("code-row-4");
    expect(row4.className).not.toMatch(/\bt-|\bs-/);
  });

  it("renders branch pills: not-taken and unreachable (struck) on row 2", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const row2 = await screen.findByTestId("code-row-2");
    const pills = within(row2).getAllByTestId("branch-pill");
    expect(pills).toHaveLength(2);
    expect(pills[0].textContent).toBe("B0");
    expect(pills[0].className).toContain("text-error-primary"); // not-taken
    expect(pills[1].textContent).toBe("B1");
    expect(pills[1].className).toContain("line-through"); // unreachable
  });

  it("expander opens the revoked run as a struck 'revoked' chip", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const expander = await screen.findByTestId("code-expander-5");
    fireEvent.click(expander);
    const panel = screen.getByTestId("ctx-panel-5");
    const chip = within(panel).getByTestId("run-chip");
    expect(chip.textContent).toContain("legacy checksum");
    expect(chip.textContent).toContain("revoked");
    const host = within(chip).getByTestId("host-pill");
    expect(host.textContent).toBe("bench-3"); // falls back to board (no host)
  });

  it("expander opens an aging run with the '· aging' suffix on its count", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const expander = await screen.findByTestId("code-expander-6");
    fireEvent.click(expander);
    const panel = screen.getByTestId("ctx-panel-6");
    const chip = within(panel).getByTestId("run-chip");
    expect(chip.textContent).toContain("× 9");
    expect(chip.textContent).toContain("aging");
  });

  it("'Expand contexts' opens every expandable line's panel at once", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    await screen.findByTestId("code-row-1");
    expect(screen.queryByTestId("ctx-panel-5")).toBeNull();
    expect(screen.queryByTestId("ctx-panel-6")).toBeNull();

    fireEvent.click(screen.getByTestId("expand-contexts"));
    expect(screen.getByTestId("ctx-panel-5")).toBeTruthy();
    expect(screen.getByTestId("ctx-panel-6")).toBeTruthy();

    fireEvent.click(screen.getByTestId("expand-contexts"));
    expect(screen.queryByTestId("ctx-panel-5")).toBeNull();
    expect(screen.queryByTestId("ctx-panel-6")).toBeNull();
  });

  it("computes the stats card + meta line from the chunk, including past-EOF records", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const allRow = await screen.findByTestId("stats-row-all");
    // chunk.lines has 5 keys (1,2,5,6,999); hits>0 on 2 of them (1 and 999).
    expect(allRow.textContent).toContain("2/5");
    expect(allRow.textContent).toContain("40.0%");

    const meta = screen.getByTestId("page-meta");
    expect(meta.textContent).toContain("5");
    expect(meta.textContent).toContain("2");
    expect(meta.textContent).toContain("2026-07-25 14:02 UTC");
    expect(meta.textContent).toContain("0.7.5");
  });

  it("renders crumbs and the monospace file-name title", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    await screen.findByTestId("code-row-1");
    // Title is wrapped in a monospace <span> inside the <h1> (mockup's
    // font-mono h1 styling), so match on the heading role rather than
    // getByText's exact-node text matching.
    const heading = screen.getByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("tcp.c");
    expect(heading.querySelector("span")?.className).toContain("font-mono");
    const crumbs = screen.getByTestId("breadcrumbs");
    expect(within(crumbs).getByText("acme-fw").getAttribute("href")).toBe("#/coverage");
  });

  // Regression: `overflow-hidden` on the code-card container establishes a
  // scroll container, which would make CodeView's `sticky top-0`
  // column-header row stick to THIS card instead of the page (it'd scroll
  // off with everything else) — the exact CSS bug the file-page.html mockup
  // has. `overflow-clip` still clips content to the rounded corners without
  // creating a scroll container, so `position: sticky` keeps chaining up to
  // the page. jsdom doesn't lay out/scroll, so this can only pin the class
  // choice, not the actual sticky behavior — that's verified in a real
  // browser by the Task 9 browser lane.
  it("code-card uses overflow-clip, not overflow-hidden, so the sticky header isn't trapped in a scroll container", async () => {
    mockChunkLoad({ resolve: makeChunk() });
    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    const card = await screen.findByTestId("code-card");
    expect(card.className).toContain("overflow-clip");
    expect(card.className).not.toContain("overflow-hidden");
  });

  // Integration-style, no spy: exercises the REAL loadFileChunk (script
  // injection + window.__OTTO_COV_FILE__ callback), same technique
  // data.test.ts uses, proving FilePage's async wiring works against the
  // production data.ts path, not just a mock.
  it("loads via the real loadFileChunk script-injection/callback path", async () => {
    window.__OTTO_COV__ = makeFileIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    renderPage({ index: makeFileIndex(), segments: ["src", "net", "tcp.c"], node: NODE });
    expect(screen.getByTestId("file-loading")).toBeTruthy();

    await waitFor(() => expect(appendSpy).toHaveBeenCalledTimes(1));
    window.__OTTO_COV_FILE__?.(makeChunk());

    await screen.findByTestId("code-row-1");
    expect(screen.getByTestId("code-row-1").textContent).toContain("5");
  });

  describe("under focus", () => {
    // "nightly-full" = run id 1 (tier system) — the only member run on this
    // fixture's label. Line 1 is its hit (run: {"1": 5}); line 6 is hit by
    // run 3 ("field bring-up", NOT a member); line 5 carries run id 2 in
    // `stale_run` (a different context's revoked evidence, irrelevant here).
    function renderFocused(ctxLabel: string) {
      const index = makeFileIndex();
      window.__OTTO_COV__ = index;
      window.location.hash = `#/coverage/src/net/tcp.c?ctx=${encodeURIComponent(ctxLabel)}`;
      mockChunkLoad({ resolve: makeChunk() });
      renderPage({ index, segments: ["src", "net", "tcp.c"], node: NODE });
    }

    it("tints a member-run-hit line by the context's tier", async () => {
      renderFocused("nightly-full");
      const row1 = await screen.findByTestId("code-row-1");
      expect(row1.className).toContain("t-system");
    });

    it("a hit line NOT covered by a member run renders neutral (s-unc), not tinted", async () => {
      renderFocused("nightly-full");
      const row6 = await screen.findByTestId("code-row-6"); // hit by run 3, not a member
      expect(row6.className).toContain("s-unc");
      expect(row6.className).not.toMatch(/\bt-/);
    });

    it("excluded still wins over a member-run hit", async () => {
      renderFocused("nightly-full");
      const row3 = await screen.findByTestId("code-row-3");
      expect(row3.className).toContain("s-excl");
    });

    it("an uncoverable line stays blank under focus", async () => {
      renderFocused("nightly-full");
      const row4 = await screen.findByTestId("code-row-4");
      expect(row4.className).not.toMatch(/\bt-|\bs-/);
    });

    it("hit-count cells: the focused context's tier column sums member-run hits", async () => {
      renderFocused("nightly-full");
      const row1 = await screen.findByTestId("code-row-1");
      expect(row1.textContent).toContain("5");
    });

    it("hit-count cells: a non-member-run hit line shows '·' in every tier column", async () => {
      renderFocused("nightly-full");
      const row6 = await screen.findByTestId("code-row-6");
      expect((row6.textContent?.match(/·/g) ?? []).length).toBe(2); // system AND unit columns
    });

    it("branch pills render unchanged under focus", async () => {
      renderFocused("nightly-full");
      const row2 = await screen.findByTestId("code-row-2");
      const pills = within(row2).getAllByTestId("branch-pill");
      expect(pills).toHaveLength(2);
    });

    it("the stats card shows the single focused Context row over the chunk's coverable lines", async () => {
      renderFocused("nightly-full");
      const row = await screen.findByTestId("stats-row-ctx");
      expect(row.textContent).toContain("nightly-full");
      expect(row.textContent).toContain("1/5"); // only line 1 is a member-run hit, of 5 chunk.lines keys
      expect(row.textContent).toContain("20.0%");
      expect(row.textContent?.match(/no data/g)?.length).toBe(2); // branch + decision

      const card = screen.getByTestId("stats-card");
      expect(card.textContent).toContain("focused: nightly-full");
      expect(card.textContent).toContain("Context");
    });

    it("the meta line keeps reporting the file's OVERALL coverage, unaffected by focus", async () => {
      renderFocused("nightly-full");
      await screen.findByTestId("code-row-1");
      const meta = screen.getByTestId("page-meta");
      expect(meta.textContent).toContain("2"); // unfocused "all tiers" covered count (2/5)
    });

    it("shows the app-bar focus chip while focused", async () => {
      renderFocused("nightly-full");
      await screen.findByTestId("code-row-1");
      expect(screen.getByTestId("focus-chip").textContent).toContain("nightly-full");
    });
  });
});
