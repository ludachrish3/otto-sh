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
import { afterEach, describe, expect, it, vi } from "vitest";

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
const TICKETS_TOTALS = { owned: 120, covered: 80, uncovered: 40, per_tier: { unit: 80 } };

const INDEX = makeIndex({
  tickets: [
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 100,
      covered: 90,
      uncovered: 10,
      per_tier: { unit: 90 },
      chunk: "PROJ-1",
    },
    {
      id: "PROJ-2",
      url: null,
      owned: 50,
      covered: 5,
      uncovered: 45,
      per_tier: { unit: 5 },
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
      chunk: "PROJ-2",
    },
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 100,
      covered: 90,
      uncovered: 10,
      per_tier: { unit: 90 },
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
      chunk: "no-ticket",
    },
    {
      id: "PROJ-1",
      url: "u/1",
      owned: 5,
      covered: 5,
      uncovered: 0,
      per_tier: { unit: 5 },
      chunk: "PROJ-1",
    },
  ],
  tickets_totals: { owned: 15, covered: 7, uncovered: 8, per_tier: { unit: 7 } },
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
      files: [{ path: "src/a.c", owned: 4, covered: 2, missing: [[2, 3]] }],
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
        files: [{ path: "src/a.c", owned: 4, covered: 2, missing: [[2, 3]] }],
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
          chunk: "(no ticket)",
        },
      ],
      tickets_totals: { owned: 4, covered: 1, uncovered: 3, per_tier: { unit: 1 } },
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
        files: [{ path: "src/a.c", owned: 4, covered: 1, missing: [[2, 4]] }],
      }),
    );

    expect(await screen.findByText("src/a.c")).toBeTruthy();
    expect(screen.getByText("2-4")).toBeTruthy();
  });
});
