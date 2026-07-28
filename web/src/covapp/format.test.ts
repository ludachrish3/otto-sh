// The StatsCard key-column header has to describe whatever `rows` actually
// put in that column. Three pages built the label with their own inline
// ternary and two of them disagreed in the COMPOSED case: with both a
// ticket and a context active, DirectoryPage checked the ticket first and
// said "Ticket" while FilePage checked the context first and said
// "Context" — for a row whose key cell reads `PROJ-1 · manual` on both.
// One shared function now answers it, so the pages cannot drift again.
import { describe, expect, it } from "vitest";

import { keyColumnLabel } from "./format";

describe("keyColumnLabel", () => {
  it("names both dimensions when a ticket and a context compose", () => {
    // The row's key cell is `${ticketId} · ${ctx.label}` (tickets.ts's
    // ticketTreeRow/ticketFileRow), so naming only one of them mislabels it.
    expect(keyColumnLabel({ ticket: true, context: true })).toBe("Ticket · Context");
  });

  it("names the ticket alone when only a ticket is pinned", () => {
    expect(keyColumnLabel({ ticket: true, context: false })).toBe("Ticket");
  });

  it("names the context alone when only a run focus is active", () => {
    expect(keyColumnLabel({ ticket: false, context: true })).toBe("Context");
  });

  it("falls back to the per-tier matrix header", () => {
    expect(keyColumnLabel({ ticket: false, context: false })).toBe("Tier");
  });
});
