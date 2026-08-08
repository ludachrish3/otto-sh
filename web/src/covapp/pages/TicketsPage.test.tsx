// TicketsPage's contract (task-10 brief; stamp/dedup/link fixes are fix
// round 1). `Providers` wraps every render — AppShell (which this page uses
// for its chrome) calls `useFocus()` unconditionally, which throws without a
// `FocusProvider` ancestor (see focus.tsx's contract, and every other page's
// test file: RunsPage.test.tsx/FilePage.test.tsx both render through
// `{ wrapper: Providers }` for the same reason). `loadTicketChunk` is spied
// per test via `import * as dataModule` (not a module-level `vi.mock`), same
// technique FilePage.test.tsx uses for `loadFileChunk`, so the real-path
// tests here can exercise the real script-injection/callback wiring against
// the production `data.ts` path instead of only ever hitting a mock.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as dataModule from "../data";
import { _resetForTests } from "../data";
import { makeIndex, Providers } from "../testUtils";
import type { IndexPayload, TicketChunk } from "../types";
import { fmtLineRange, TicketsPage } from "./TicketsPage";

function renderPage(index: IndexPayload) {
  return render(<TicketsPage index={index} />, { wrapper: Providers });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  _resetForTests();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  window.location.hash = "";
  localStorage.clear();
});

function makeTicketChunk(overrides: Partial<TicketChunk> = {}): TicketChunk {
  return {
    stamp: "stamp-1",
    id: "PROJ-1",
    files: [],
    ...overrides,
  };
}

// Deliberately NOT the sum of the two tickets below (owned would sum to
// 150, covered to 95, per_tier.unit to 95) — this fixture's job is to prove
// the aggregate StatsCard reads `tickets_totals` (deduped, server-computed)
// rather than re-deriving it by summing `tickets[]`, exactly the "your
// fixture needs numbers that diverge from summing" standing requirement:
// if a future edit reverted to summing, these assertions would see
// 150/95/95 instead and fail.
const TICKETS_TOTALS = {
  owned: 120,
  covered: 80,
  uncovered: 40,
  per_tier: { unit: 80 },
  asserted: { unit: 0 },
};

const INDEX = makeIndex({
  tickets: [
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 100,
      covered: 90,
      uncovered: 10,
      per_tier: { unit: 90 },
      asserted: { unit: 0 },
      chunk: "PROJ-1",
    },
    {
      id: "PROJ-2",
      url: null,
      owned: 50,
      covered: 5,
      uncovered: 45,
      per_tier: { unit: 5 },
      asserted: { unit: 0 },
      chunk: "PROJ-2",
    },
  ],
  tickets_totals: TICKETS_TOTALS,
});

// A SEPARATE fixture for the per-tier-sort test, declared with PROJ-2 FIRST
// (the opposite raw order from INDEX above) — deliberately, so a broken
// per-tier sort (e.g. one that silently no-ops instead of comparing
// `per_tier`) can't coincidentally "pass" by leaving the array in its raw
// declaration order, which is exactly what INDEX's own [PROJ-1, PROJ-2]
// declaration order would let slip through (it happens to already equal
// the correctly-sorted-by-unit-descending output). Reusing INDEX here would
// make the assertion below true regardless of whether the sort ran at all.
const TIER_SORT_INDEX = makeIndex({
  tickets: [
    {
      id: "PROJ-2",
      url: null,
      owned: 50,
      covered: 5,
      uncovered: 45,
      per_tier: { unit: 5 },
      asserted: { unit: 0 },
      chunk: "PROJ-2",
    },
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 100,
      covered: 90,
      uncovered: 10,
      per_tier: { unit: 90 },
      asserted: { unit: 0 },
      chunk: "PROJ-1",
    },
  ],
  tickets_totals: TICKETS_TOTALS,
});

// Task 14: the two synthetic rows flow through the SAME TicketSummary shape
// as any real ticket (reporter.py/spa_data.py already guarantee that) — this
// fixture only exercises the one thing that's genuinely new at this layer,
// a subtle visual de-emphasis that must NOT come at the cost of excluding a
// sentinel from sorting (the brief explicitly forbids special-casing it out
// of the sort).
const SENTINEL_INDEX = makeIndex({
  tickets: [
    {
      id: "(no ticket)",
      url: null,
      owned: 10,
      covered: 2,
      uncovered: 8,
      per_tier: { unit: 2 },
      asserted: { unit: 0 },
      chunk: "no-ticket",
    },
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 5,
      covered: 5,
      uncovered: 0,
      per_tier: { unit: 5 },
      asserted: { unit: 0 },
      chunk: "PROJ-1",
    },
  ],
  tickets_totals: {
    owned: 15,
    covered: 7,
    uncovered: 8,
    per_tier: { unit: 7 },
    asserted: { unit: 0 },
  },
});

describe("fmtLineRange", () => {
  it("renders a single line as just its number", () => {
    expect(fmtLineRange([12, 12])).toBe("12");
  });

  it("renders a multi-line span as start-end", () => {
    expect(fmtLineRange([12, 15])).toBe("12-15");
  });
});

describe("TicketsPage", () => {
  it("sorts by uncovered descending so the worst-tested work is first", () => {
    renderPage(INDEX);
    const ids = screen.getAllByTestId("ticket-id").map((n) => n.textContent);
    expect(ids).toEqual(["PROJ-2", "PROJ-1"]);
  });

  it("renders a synthetic sentinel ticket id de-emphasized, as plain text, without excluding it from sorting", () => {
    renderPage(SENTINEL_INDEX);
    const cells = screen.getAllByTestId("ticket-id");
    const sentinelCell = cells.find((c) => c.textContent === "(no ticket)");
    expect(sentinelCell).toBeTruthy();
    expect(sentinelCell?.tagName).toBe("SPAN"); // no url -> plain text, same as any url-less ticket
    expect(sentinelCell?.className).toContain("italic");
    // Default sort is uncovered-desc: the poorly-covered sentinel
    // legitimately floats to the top here, exactly the brief's point,
    // rather than being pinned to a fixed position.
    expect(cells.map((c) => c.textContent)).toEqual(["(no ticket)", "PROJ-1"]);
  });

  it("filters rows by the search box", async () => {
    renderPage(INDEX);
    await userEvent.type(screen.getByPlaceholderText(/search tickets/i), "PROJ-1");
    expect(screen.getAllByTestId("ticket-id").map((n) => n.textContent)).toEqual(["PROJ-1"]);
  });

  it("links a ticket that has a url and leaves one without as plain text", () => {
    renderPage(INDEX);
    const rows = screen.getAllByTestId("ticket-row");
    expect(within(rows[1]).getByRole("link").getAttribute("href")).toBe("u/1");
    expect(within(rows[0]).queryByRole("link")).toBeNull();
  });

  it("states that rows overlap and do not sum to the card above", () => {
    renderPage(INDEX);
    expect(screen.getByText(/overlap/i)).toBeTruthy();
  });

  it("renders an empty state when no tickets are attributed", () => {
    renderPage(makeIndex({ tickets: [] }));
    expect(screen.getByText(/no ticket data/i)).toBeTruthy();
  });

  // Fix round 1, IMPORTANT 1: the card must read the DEDUPED
  // `tickets_totals`, never a sum of the per-ticket rows — see TICKETS_TOTALS'
  // comment above for why this fixture's numbers deliberately diverge from
  // summing.
  it("renders the aggregate StatsCard from tickets_totals (deduped), not a sum of the per-ticket rows", () => {
    renderPage(INDEX);
    const unitRow = screen.getByTestId("stats-row-unit");
    expect(unitRow.textContent).toContain("80/120"); // tickets_totals, not the 95/150 sum
    const allRow = screen.getByTestId("stats-row-all");
    expect(allRow.textContent).toContain("80/120");
  });

  // Minor: "all columns sortable" (design §6.1) — clicking a per-tier
  // column header re-sorts by THAT tier's (non-deduped) per-ticket hit
  // count. PROJ-1 has more unit hits (90) than PROJ-2 (5), the OPPOSITE of
  // the default uncovered-desc order asserted above ([PROJ-2, PROJ-1]) —
  // proving the click genuinely changes the sort, not a no-op.
  it("sorts by a per-tier column when its header is clicked", async () => {
    renderPage(TIER_SORT_INDEX);
    await userEvent.click(screen.getByRole("button", { name: "Unit" }));
    expect(screen.getAllByTestId("ticket-id").map((n) => n.textContent)).toEqual([
      "PROJ-1",
      "PROJ-2",
    ]);
  });

  it("expanding a row loads the ticket's chunk and shows missing lines grouped by file as ranges, each linking into the code", async () => {
    const chunk = makeTicketChunk({
      files: [
        {
          path: "src/a.c",
          owned: 4,
          covered: 2,
          missing: [[2, 3]],
          per_tier: {},
          asserted: {},
          asserted_only: 0,
        },
      ],
    });
    const spy = vi.spyOn(dataModule, "loadTicketChunk").mockResolvedValue(chunk);
    renderPage(INDEX);

    await userEvent.click(screen.getByTestId("ticket-toggle-PROJ-1"));

    expect(spy).toHaveBeenCalledWith("PROJ-1");
    expect(await screen.findByText("src/a.c")).toBeTruthy();
    const link = screen.getByRole("link", { name: "2-3" });
    expect(link.getAttribute("href")).toBe("#/coverage/src/a.c?lines=2-3");
  });

  it("does not re-request an already-loaded chunk on collapse/re-expand", async () => {
    const chunk = makeTicketChunk();
    const spy = vi.spyOn(dataModule, "loadTicketChunk").mockResolvedValue(chunk);
    renderPage(INDEX);

    const toggle = screen.getByTestId("ticket-toggle-PROJ-1");
    await userEvent.click(toggle); // expand
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await userEvent.click(toggle); // collapse
    await userEvent.click(toggle); // re-expand

    expect(spy).toHaveBeenCalledTimes(1);
  });

  // Fix round 1, IMPORTANT 3: a stamp mismatch must guard-screen the WHOLE
  // page (mirroring FilePage.tsx's contract for the same error), not just
  // show an inline per-row error — the report on disk changed since the
  // index loaded, so nothing else on this page can be trusted either.
  it("renders the guard screen when an expanded chunk's stamp does not match the index's", async () => {
    vi.spyOn(dataModule, "loadTicketChunk").mockRejectedValue(
      new dataModule.StampMismatchError("PROJ-1"),
    );
    renderPage(INDEX);

    await userEvent.click(screen.getByTestId("ticket-toggle-PROJ-1"));

    const guard = await screen.findByTestId("guard-screen");
    expect(guard.textContent).toContain("report changed on disk");
  });

  // Integration-style, no spy: exercises the REAL loadTicketChunk
  // script-injection + window.__OTTO_COV_TICKET__ callback path (the hard
  // platform constraint — this SPA must run from file://, so chunks are
  // classic scripts, never fetch()), the same technique data.test.ts /
  // FilePage.test.tsx use for the per-file chunk lane.
  it("loads via the real script-injection/callback path (window.__OTTO_COV_TICKET__)", async () => {
    window.__OTTO_COV__ = INDEX;
    const appendSpy = vi.spyOn(document.head, "appendChild");

    renderPage(INDEX);
    await userEvent.click(screen.getByTestId("ticket-toggle-PROJ-1"));

    await waitFor(() => expect(appendSpy).toHaveBeenCalledTimes(1));
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    expect(script.getAttribute("src")).toBe("./cov_data/tickets/PROJ-1.js");

    window.__OTTO_COV_TICKET__?.(
      "PROJ-1",
      makeTicketChunk({
        stamp: INDEX.stamp,
        files: [
          {
            path: "src/a.c",
            owned: 4,
            covered: 2,
            missing: [[2, 3]],
            per_tier: {},
            asserted: {},
            asserted_only: 0,
          },
        ],
      }),
    );

    expect(await screen.findByText("src/a.c")).toBeTruthy();
    expect(screen.getByText("2-3")).toBeTruthy();
  });

  // Fix round 1, Minor 3: the sentinel chunk file is literally named
  // `(no ticket).js` on disk (spa_data.py's `mangle_path` only replaces `/`
  // and `\`, so parentheses and the space survive verbatim). Until this
  // test, nothing actually requested that filename through the real
  // loader — `encodeURIComponent` leaving `(`/`)` unescaped while escaping
  // the space to `%20` was reasoning-only. This exercises it for real,
  // the same script-injection/callback path the test above proves for
  // "PROJ-1".
  it("loads a sentinel-named chunk through the real loader path, exercising the parenthesized filename", async () => {
    const sentinelChunkIndex = makeIndex({
      tickets: [
        {
          id: "(no ticket)",
          url: null,
          owned: 4,
          covered: 1,
          uncovered: 3,
          per_tier: { unit: 1 },
          asserted: { unit: 0 },
          chunk: "(no ticket)",
        },
      ],
      tickets_totals: {
        owned: 4,
        covered: 1,
        uncovered: 3,
        per_tier: { unit: 1 },
        asserted: { unit: 0 },
      },
    });
    window.__OTTO_COV__ = sentinelChunkIndex;
    const appendSpy = vi.spyOn(document.head, "appendChild");

    renderPage(sentinelChunkIndex);
    await userEvent.click(screen.getByTestId("ticket-toggle-(no ticket)"));

    await waitFor(() => expect(appendSpy).toHaveBeenCalledTimes(1));
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    // Parentheses pass through `encodeURIComponent` unescaped; the space
    // becomes `%20` — matching the literal `(no ticket).js` file
    // `emit_chunks` writes to disk.
    expect(script.getAttribute("src")).toBe("./cov_data/tickets/(no%20ticket).js");

    window.__OTTO_COV_TICKET__?.(
      "(no ticket)",
      makeTicketChunk({
        id: "(no ticket)",
        stamp: sentinelChunkIndex.stamp,
        files: [
          {
            path: "src/a.c",
            owned: 4,
            covered: 1,
            missing: [[2, 4]],
            per_tier: {},
            asserted: {},
            asserted_only: 0,
          },
        ],
      }),
    );

    expect(await screen.findByText("src/a.c")).toBeTruthy();
    expect(screen.getByText("2-4")).toBeTruthy();
  });
});

// Line % column (follow-up item 5b — present in the original mock, missing
// from the shipped table). Fixtures below give the two tickets DIFFERENT
// ratios that do NOT rank the same way as any existing column, so sorting
// by Line % is distinguishable from sorting by owned/covered/uncovered.
describe("line % column", () => {
  const PCT_INDEX = makeIndex({
    tickets: [
      // 20% of 100 owned — most owned, most uncovered, WORST ratio.
      {
        id: "PROJ-BIG",
        url: null,
        owned: 100,
        covered: 20,
        uncovered: 80,
        per_tier: { unit: 20 },
        asserted: { unit: 0 },
        chunk: "big",
      },
      // 90% of 10 owned — least owned, least uncovered, BEST ratio.
      {
        id: "PROJ-SMALL",
        url: null,
        owned: 10,
        covered: 9,
        uncovered: 1,
        per_tier: { unit: 9 },
        asserted: { unit: 0 },
        chunk: "small",
      },
    ],
  });

  it("renders each ticket's covered/owned percentage", () => {
    renderPage(PCT_INDEX);
    const rows = screen.getAllByTestId("ticket-row");
    expect(within(rows[0]).getByTestId("ticket-line-pct").textContent).toContain("20.0%");
    expect(within(rows[1]).getByTestId("ticket-line-pct").textContent).toContain("90.0%");
  });

  it("sorts by ratio, not by any count column", async () => {
    const user = userEvent.setup();
    renderPage(PCT_INDEX);

    await user.click(screen.getByRole("button", { name: /Line %/ }));

    // A numeric column leads descending (onSort's convention), so the 90%
    // ticket comes first. That ordering is unique to the ratio: descending
    // by owned, covered OR uncovered would all put PROJ-BIG first instead,
    // so this cannot pass against a sort that fell back to a count column.
    const ids = screen.getAllByTestId("ticket-id").map((n) => n.textContent);
    expect(ids).toEqual(["PROJ-SMALL", "PROJ-BIG"]);

    await user.click(screen.getByRole("button", { name: /Line %/ }));
    const flipped = screen.getAllByTestId("ticket-id").map((n) => n.textContent);
    expect(flipped).toEqual(["PROJ-BIG", "PROJ-SMALL"]);
  });
});

// Row-level pin controls (follow-up item 5c): before these, the tickets
// page could list and sort every ticket but could not pin one — that lived
// only in the app bar, so a reader who found the interesting ticket here had
// to go re-find it in a separate search box.
describe("row pin control", () => {
  const PIN_INDEX = makeIndex({
    tickets: [
      {
        id: "PROJ-1",
        url: null,
        owned: 10,
        covered: 5,
        uncovered: 5,
        per_tier: {},
        asserted: {},
        chunk: "PROJ-1",
      },
    ],
  });

  // FocusProvider resolves a pin against the GLOBAL index (window.
  // __OTTO_COV__), not the page's prop — an id it cannot find degrades to
  // "cleared" by design — and the app-bar chip is AppShell's, which reads
  // the same global. So the fixture has to be installed there, not just
  // handed to the page.
  beforeEach(() => {
    window.__OTTO_COV__ = PIN_INDEX;
  });

  it("pins that row's ticket, which the app-bar chip then reflects", async () => {
    const user = userEvent.setup();
    renderPage(PIN_INDEX);

    await user.click(screen.getByTestId("ticket-pin-PROJ-1"));

    expect((await screen.findByTestId("ticket-chip")).textContent).toContain("PROJ-1");
  });

  it("the control reads as pressed once its ticket is pinned", async () => {
    const user = userEvent.setup();
    renderPage(PIN_INDEX);
    const pin = screen.getByTestId("ticket-pin-PROJ-1");
    expect(pin.getAttribute("aria-pressed")).toBe("false");

    await user.click(pin);

    expect(screen.getByTestId("ticket-pin-PROJ-1").getAttribute("aria-pressed")).toBe("true");
  });

  it("clicking the pinned row again unpins it", async () => {
    const user = userEvent.setup();
    renderPage(PIN_INDEX);

    await user.click(screen.getByTestId("ticket-pin-PROJ-1"));
    await screen.findByTestId("ticket-chip");
    await user.click(screen.getByTestId("ticket-pin-PROJ-1"));

    expect(screen.queryByTestId("ticket-chip")).toBeNull();
  });
});

// Task 11: "hide asserted coverage" narrows the aggregate StatsCard's
// per-tier rows (`tickets_totals.asserted`) and each ticket row's own
// per-tier cell (`TicketSummary.asserted`) — booted from `?asserted=1`,
// FocusProvider's boot precedence (focus.test.tsx covers the mechanism
// directly). The aggregate row's "all tiers" `covered`/`owned` numbers and
// each row's covered/uncovered counts are DELIBERATELY untouched — see
// `ticketStatsRows`'s doc comment for why `tickets_totals` carries no
// deduped "asserted-only" total to subtract honestly.
describe("TicketsPage: hideAsserted (Task 11)", () => {
  function buildAssertedIndex(): IndexPayload {
    return makeIndex({
      tier_order: ["unit"],
      tickets: [
        {
          id: "PROJ-1",
          url: null,
          owned: 10,
          covered: 8,
          uncovered: 2,
          per_tier: { unit: 8 },
          asserted: { unit: 3 },
          chunk: "PROJ-1",
        },
      ],
      tickets_totals: {
        owned: 10,
        covered: 8,
        uncovered: 2,
        per_tier: { unit: 8 },
        asserted: { unit: 3 },
      },
    });
  }

  it("default: raw per-tier counts AND raw covered/uncovered/line%, scope carries no suffix — byte-identical to before this feature", () => {
    const index = buildAssertedIndex();
    renderPage(index);
    expect(screen.getByTestId("stats-row-unit").textContent).toContain("8/10");
    expect(screen.getByTestId("stats-row-all").textContent).toContain("8/10");
    const row = screen.getByTestId("ticket-row");
    // Positional cells, not whole-row bare digits: the row also renders
    // "80.0%", which satisfied the old `toContain("8")` unconditionally —
    // gate no-bare-digit-textcontent. Grid order per TicketRow: [0] toggle,
    // [1] id cell, [2] owned, [3] covered, [4] uncovered, [5] line %.
    expect(row.children[3]?.textContent).toBe("8"); // covered
    expect(row.children[4]?.textContent).toBe("2"); // uncovered
    expect(screen.getByTestId("ticket-line-pct").textContent).toContain("80.0%");
    expect(screen.queryByTestId("ticket-covered-na")).toBeNull();
    expect(screen.queryByTestId("ticket-uncovered-na")).toBeNull();
    expect(screen.getByTestId("stats-card").textContent).not.toContain("asserted hidden");
  });

  it("hideAsserted subtracts tickets_totals.asserted/ticket.asserted from the per-tier cells (honest — same denominator, no cross-tier double-count risk)", () => {
    window.location.hash = "#/tickets?asserted=1";
    renderPage(buildAssertedIndex());
    // 8 - 3 = 5, over the UNCHANGED denominator (10).
    expect(screen.getByTestId("stats-row-unit").textContent).toContain("5/10");
    expect(screen.getByTestId("stats-card").textContent).toContain("asserted hidden");
  });

  it("hideAsserted declines (never fakes) the aggregate all-tiers row's line stat — 'no data', not a guessed subtraction", () => {
    window.location.hash = "#/tickets?asserted=1";
    renderPage(buildAssertedIndex());
    const allRow = screen.getByTestId("stats-row-all");
    expect(allRow.textContent).toContain("no data");
    expect(allRow.textContent).not.toContain("8/10");
  });

  it("hideAsserted declines (never fakes) a ticket row's own covered/uncovered/line% — no deduped per-ticket asserted-only count exists", () => {
    window.location.hash = "#/tickets?asserted=1";
    renderPage(buildAssertedIndex());
    const row = screen.getByTestId("ticket-row");
    expect(screen.getByTestId("ticket-covered-na")).toBeTruthy();
    expect(screen.getByTestId("ticket-uncovered-na")).toBeTruthy();
    // Neither the raw 8/2 numbers nor a guessed subtraction (e.g. 5, from
    // naively reusing the per-tier `asserted` count) appear anywhere in the
    // row — declined entirely, not silently wrong.
    expect(row.textContent).not.toMatch(/\b8\b/);
    expect(row.textContent).not.toMatch(/\b2\b/);
    expect(screen.getByTestId("ticket-line-pct").textContent).toBe("—");
  });

  it("toggle off (default) renders identically to before this feature — no na markers, no suffix", () => {
    renderPage(buildAssertedIndex());
    expect(screen.queryByTestId("ticket-covered-na")).toBeNull();
    expect(screen.queryByTestId("ticket-uncovered-na")).toBeNull();
    // The all-tiers row's Line cell keeps its real fraction (branch/decision
    // are ALWAYS "no data" here regardless of hideAsserted — see
    // `ticketStatsRows`'s doc comment — so this checks the Line number
    // itself, not absence of "no data" text anywhere in the row).
    expect(screen.getByTestId("stats-row-all").textContent).toContain("8/10");
    expect(screen.getByTestId("stats-card").textContent).not.toContain("asserted hidden");
  });
});
