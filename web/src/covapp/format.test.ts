// The StatsCard key-column header has to describe whatever `rows` actually
// put in that column. Three pages built the label with their own inline
// ternary and two of them disagreed in the COMPOSED case: with both a
// ticket and a context active, DirectoryPage checked the ticket first and
// said "Ticket" while FilePage checked the context first and said
// "Context" — for a row whose key cell reads `PROJ-1 · manual` on both.
// One shared function now answers it, so the pages cannot drift again.
import { describe, expect, it } from "vitest";

import { chunkTierRows, keyColumnLabel, tierRows, withHideAssertedSuffix } from "./format";
import { emptyStats, makeIndex } from "./testUtils";
import type { FileChunk } from "./types";

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

// Task 11: "hide asserted coverage" — a line/count asserted in a tier no
// longer counts as proof that tier (or "all tiers") covers it. Both
// recompute helpers default `hideAsserted` to `false`, so every existing
// call site (which never passes the new argument) stays byte-identical.
describe("chunkTierRows: hideAsserted (Task 11)", () => {
  function makeChunk(): FileChunk {
    return {
      stamp: "stamp-1",
      chunk: "src_a.c",
      path: "src/a.c",
      source: "l1\nl2\nl3",
      lines: {
        // line1: asserted-only in "bench" — its sole evidence is an override.
        "1": { hits: { bench: 1 }, branches: [], state: null, asserted: { bench: [0] } },
        // line2: a REAL bench hit — recorded, not override-sourced.
        "2": { hits: { bench: 1 }, branches: [], state: null },
        // line3: unhit in every tier.
        "3": { hits: { bench: 0 }, branches: [], state: null },
      },
      excluded: [],
    };
  }

  it("counts asserted lines as hit by default (hideAsserted omitted)", () => {
    const index = makeIndex({ tier_order: ["bench"], tier_labels: {}, tier_colors: {} });
    const rows = chunkTierRows(index, makeChunk());
    const bench = rows.find((r) => r.key === "bench");
    const all = rows.find((r) => r.key === "all");
    expect(bench?.line).toEqual([2, 3]);
    expect(all?.line).toEqual([2, 3]);
  });

  it("subtracts asserted-only lines from both the tier row and the all-tiers row when hideAsserted", () => {
    const index = makeIndex({ tier_order: ["bench"], tier_labels: {}, tier_colors: {} });
    const rows = chunkTierRows(index, makeChunk(), true);
    const bench = rows.find((r) => r.key === "bench");
    const all = rows.find((r) => r.key === "all");
    // line1 no longer counts (its only hit is asserted); line2 still does.
    expect(bench?.line).toEqual([1, 3]);
    expect(all?.line).toEqual([1, 3]);
  });

  it("never changes the denominator (lineTotal) or branch counts", () => {
    const index = makeIndex({ tier_order: ["bench"], tier_labels: {}, tier_colors: {} });
    const hidden = chunkTierRows(index, makeChunk(), true);
    const shown = chunkTierRows(index, makeChunk(), false);
    expect(hidden.find((r) => r.key === "bench")?.line?.[1]).toBe(
      shown.find((r) => r.key === "bench")?.line?.[1],
    );
    expect(hidden.find((r) => r.key === "all")?.branch).toEqual(
      shown.find((r) => r.key === "all")?.branch,
    );
  });
});

describe("tierRows: hideAsserted (Task 11)", () => {
  function makeStats() {
    return emptyStats({
      lines: {
        total: 10,
        hit: 5,
        per_tier: { bench: 5 },
        asserted_per_tier: { bench: 2 },
        asserted_only: 2,
      },
    });
  }

  it("uses the raw hit/per-tier counts by default", () => {
    const index = makeIndex({ tier_order: ["bench"], tier_labels: {}, tier_colors: {} });
    const rows = tierRows(index, makeStats());
    expect(rows.find((r) => r.key === "bench")?.line).toEqual([5, 10]);
    expect(rows.find((r) => r.key === "all")?.line).toEqual([5, 10]);
  });

  it("subtracts asserted_per_tier/asserted_only when hideAsserted", () => {
    const index = makeIndex({ tier_order: ["bench"], tier_labels: {}, tier_colors: {} });
    const rows = tierRows(index, makeStats(), true);
    expect(rows.find((r) => r.key === "bench")?.line).toEqual([3, 10]);
    expect(rows.find((r) => r.key === "all")?.line).toEqual([3, 10]);
  });
});

describe("withHideAssertedSuffix (Task 11)", () => {
  it("appends the suffix while active", () => {
    expect(withHideAssertedSuffix("whole repo", true)).toBe("whole repo · asserted hidden");
  });

  it("is a no-op when inactive — byte-identical to the bare scope string", () => {
    expect(withHideAssertedSuffix("whole repo", false)).toBe("whole repo");
  });
});
