// Ticket context (Task 12 brief): a report-wide DENOMINATOR filter — the
// opposite arithmetic direction from run focus (`?ctx=`, which narrows the
// numerator: only that run's hits count, but all code stays in view).
// Pinning a ticket (`?ticket=<id>`) narrows the denominator instead: only
// that ticket's lines are in scope at all, so a file where the ticket
// touched 12 of 400 coverable lines must report coverage of those 12, never
// of the 400.
//
// Fixtures below deliberately give the ticket a STRICT SUBSET of lines in a
// STRICT SUBSET of files (never "the ticket owns every line in every file")
// — a fixture where ownership happens to be total can't distinguish a
// working denominator recompute from a no-op passthrough of the original
// stats (standing requirement: red-proof against a fixture that could fail).
import { describe, expect, it } from "vitest";
import type { Context } from "./contexts";
import { emptyStats, makeIndex } from "./testUtils";
import { scopeTreeToTicket, ticketChunkToFileLines, ticketFileRow, ticketTreeRow } from "./tickets";
import type { DirNode, FileChunk, TicketChunk } from "./types";

// `scopeTreeToTicket` returns `DirNode | null`. Every call below expects a
// tree back; asserting that explicitly (rather than with `!`) turns a
// contract break into a named failure instead of a TypeError three lines
// later, and keeps `noNonNullAssertion` enforced in test code.
// Params are borrowed from the real signature rather than restated, so adding
// an argument to `scopeTreeToTicket` cannot leave this wrapper silently
// accepting a stale shape.
function scopedOrThrow(...args: Parameters<typeof scopeTreeToTicket>): DirNode {
  const scoped = scopeTreeToTicket(...args);
  if (scoped === null) throw new Error("scopeTreeToTicket returned null; expected a scoped tree");
  return scoped;
}

const TREE = {
  name: "",
  dirs: [
    {
      name: "src",
      dirs: [],
      files: [
        { name: "a.c", path: "src/a.c", chunk: "a", stats: emptyStats() },
        { name: "b.c", path: "src/b.c", chunk: "b", stats: emptyStats() },
      ],
      stats: emptyStats(),
    },
    {
      name: "vendor",
      dirs: [],
      files: [{ name: "z.c", path: "vendor/z.c", chunk: "z", stats: emptyStats() }],
      stats: emptyStats(),
    },
  ],
  files: [],
  stats: emptyStats(),
} as unknown as DirNode;

describe("scopeTreeToTicket", () => {
  it("keeps only files the ticket touched", () => {
    const scoped = scopedOrThrow(TREE, { "src/a.c": [1, 2] }, { "src/a.c": [1] });
    expect(scoped.dirs.map((d) => d.name)).toEqual(["src"]);
    expect(scoped.dirs[0].files.map((f) => f.name)).toEqual(["a.c"]);
  });

  it("drops directories left empty rather than showing hollow rows", () => {
    const scoped = scopedOrThrow(TREE, { "src/a.c": [1] }, {});
    expect(scoped.dirs.find((d) => d.name === "vendor")).toBeUndefined();
  });

  it("returns null when the ticket touched nothing in this subtree", () => {
    expect(scopeTreeToTicket(TREE, { "other/x.c": [1] }, {})).toBeNull();
  });

  it("recomputes percentages against the ticket's lines only", () => {
    // 400 coverable lines, the ticket owns 12, 6 of those are hit:
    // the answer is 6/12, never 6/400.
    const big = {
      name: "",
      dirs: [],
      files: [
        {
          name: "big.c",
          path: "big.c",
          chunk: "big",
          stats: emptyStats({ lines: { total: 400, hit: 380, per_tier: {} } }),
        },
      ],
      stats: emptyStats(),
    } as unknown as DirNode;
    const owned = Array.from({ length: 12 }, (_, i) => i + 1);
    const scoped = scopedOrThrow(big, { "big.c": owned }, { "big.c": owned.slice(0, 6) });
    expect(scoped.files[0].stats.lines).toEqual({ total: 12, hit: 6, per_tier: {} });
  });

  // Directory rollups must be RECOMPUTED from their (already-scoped)
  // children, not carried over from the original whole-repo aggregate —
  // otherwise a directory row would still show "this node and below" as
  // the old, unscoped denominator even though every file row beneath it
  // now reports a ticket-scoped one. Both dirs' ORIGINAL totals below are
  // deliberately implausible (999) so a passthrough (rather than a real
  // sum-of-children recompute) is caught immediately.
  it("recomputes a directory's own rollup as the sum of its scoped children, not a passthrough of the original", () => {
    const tree: DirNode = {
      name: "",
      dirs: [
        {
          name: "src",
          dirs: [],
          files: [
            {
              name: "a.c",
              path: "src/a.c",
              chunk: "a",
              stats: emptyStats({ lines: { total: 999, hit: 999, per_tier: {} } }),
            },
            {
              name: "b.c",
              path: "src/b.c",
              chunk: "b",
              stats: emptyStats({ lines: { total: 999, hit: 999, per_tier: {} } }),
            },
          ],
          stats: emptyStats({ lines: { total: 999, hit: 999, per_tier: {} } }),
        },
      ],
      files: [],
      stats: emptyStats({ lines: { total: 999, hit: 999, per_tier: {} } }),
    };
    const scoped = scopedOrThrow(
      tree,
      { "src/a.c": [1, 2, 3], "src/b.c": [1] }, // 3 + 1 = 4 owned total
      { "src/a.c": [1], "src/b.c": [] }, // 1 + 0 = 1 hit total
    );
    expect(scoped.dirs[0].stats.lines).toEqual({ total: 4, hit: 1, per_tier: {} });
    expect(scoped.stats.lines).toEqual({ total: 4, hit: 1, per_tier: {} });
  });
});

function makeTicketChunk(overrides: Partial<TicketChunk> = {}): TicketChunk {
  return { stamp: "stamp-1", id: "PROJ-1", files: [], ...overrides };
}

describe("ticketChunkToFileLines", () => {
  it("builds owned/hit line-array maps sized to each file's owned/covered counts", () => {
    const chunk = makeTicketChunk({
      files: [
        { path: "src/a.c", owned: 12, covered: 6, missing: [], per_tier: {} },
        { path: "src/b.c", owned: 3, covered: 3, missing: [], per_tier: {} },
      ],
    });
    const { lines, hits } = ticketChunkToFileLines(chunk);
    expect(lines["src/a.c"]).toHaveLength(12);
    expect(hits["src/a.c"]).toHaveLength(6);
    expect(lines["src/b.c"]).toHaveLength(3);
    expect(hits["src/b.c"]).toHaveLength(3);
  });

  it("a file the ticket never touched (owned 0) still gets an entry, never crashing scopeTreeToTicket lookups", () => {
    const chunk = makeTicketChunk({
      files: [{ path: "src/dead.c", owned: 0, covered: 0, missing: [], per_tier: {} }],
    });
    const { lines, hits } = ticketChunkToFileLines(chunk);
    expect(lines["src/dead.c"]).toHaveLength(0);
    expect(hits["src/dead.c"]).toHaveLength(0);
  });

  it("composes directly with scopeTreeToTicket end to end", () => {
    const chunk = makeTicketChunk({
      files: [
        { path: "src/a.c", owned: 12, covered: 6, missing: [[7, 12]], per_tier: { unit: 4 } },
      ],
    });
    const { lines, hits, tiers } = ticketChunkToFileLines(chunk);
    const tree = {
      name: "",
      dirs: [],
      files: [
        {
          name: "a.c",
          path: "src/a.c",
          chunk: "a",
          stats: emptyStats({ lines: { total: 400, hit: 380, per_tier: { unit: 380 } } }),
        },
      ],
      stats: emptyStats(),
    } as unknown as DirNode;
    const scoped = scopedOrThrow(tree, lines, hits, tiers);
    // per_tier is the ticket's 4, never the file's whole-repo 380.
    expect(scoped.files[0].stats.lines).toEqual({ total: 12, hit: 6, per_tier: { unit: 4 } });
  });
});

describe("ticketTreeRow", () => {
  it("returns a single 'ticket' row using the (already-scoped) node's line hit/total, with no branch/decision data", () => {
    const node: DirNode = {
      name: "src",
      dirs: [],
      files: [],
      stats: emptyStats({ lines: { total: 12, hit: 6, per_tier: {} } }),
    };
    // No tiers declared, so the summary row is the whole answer.
    const rows = ticketTreeRow(makeIndex({ tier_order: [] }), node, "PROJ-1");
    expect(rows).toEqual([
      {
        key: "ticket",
        label: "PROJ-1",
        dotColor: undefined,
        line: [6, 12],
        branch: null,
        decision: null,
      },
    ]);
  });
});

const RUNS_FOR_CTX: Context["runs"] = [
  {
    id: 1,
    tier: "system",
    label: "manual run",
    board: "bench",
    host: "host-1",
    labs: [],
    captured_at: "2026-07-21",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "abc",
    dirty_remap: false,
    aging: false,
  },
];

const MANUAL_RUN_CTX: Context = {
  label: "manual run",
  tier: "system",
  runs: RUNS_FOR_CTX,
  hosts: [["host-1", 1]],
  lines: 1,
  revoked: 0,
  files: [],
  status: "ok",
  remapped: false,
};

function makeFileChunk(overrides: Partial<FileChunk> = {}): FileChunk {
  return {
    stamp: "stamp-1",
    chunk: "src_a.c",
    path: "src/a.c",
    source: "",
    lines: {},
    excluded: [],
    ...overrides,
  };
}

describe("ticketFileRow", () => {
  // Strict subset fixture: 5 lines total, ticket owns 3 (1, 2, 4), of which
  // 2 are hit (1, 2) — proves the row reads owned/hit counts, not the
  // chunk's overall 5-line total (which would read 2/5, a different number
  // entirely from the correct 2/3).
  const chunk = makeFileChunk({
    lines: {
      "1": { hits: { system: 1 }, branches: [], state: null, ticket: ["PROJ-1"] },
      "2": { hits: { system: 1 }, branches: [], state: null, ticket: ["PROJ-1"] },
      "3": { hits: { system: 1 }, branches: [], state: null }, // hit, but NOT owned by the ticket
      "4": { hits: {}, branches: [], state: null, ticket: ["PROJ-1"] }, // owned, not hit
      "5": { hits: {}, branches: [], state: null },
    },
  });
  const index = makeIndex({ tier_colors: { system: "green" } });

  it("recomputes owned/hit against only the lines the ticket owns (never the chunk's whole line count)", () => {
    const rows = ticketFileRow(index, chunk, "PROJ-1");
    expect(rows).toEqual([
      {
        key: "ticket",
        label: "PROJ-1",
        dotColor: undefined,
        line: [2, 3],
        branch: null,
        decision: null,
      },
    ]);
  });

  it("composes with a focused context: numerator becomes the member-run hits WITHIN the ticket's owned lines", () => {
    // Same chunk, but re-tag so the composed answer differs from the
    // ticket-only answer: line 1 is hit by the member run (run id 1), line 2
    // is hit by some OTHER, non-member run — so ticket-only hit=2 (lines 1
    // and 2 both have a truthy `hits.system`), but composed-with-context
    // hit=1 (only line 1 carries a member-run hit).
    const composedChunk = makeFileChunk({
      lines: {
        "1": {
          hits: { system: 1 },
          branches: [],
          state: null,
          ticket: ["PROJ-1"],
          run: { "1": 1 },
        },
        "2": {
          hits: { system: 1 },
          branches: [],
          state: null,
          ticket: ["PROJ-1"],
          run: { "9": 1 },
        },
        "4": { hits: {}, branches: [], state: null, ticket: ["PROJ-1"] },
      },
    });
    const rows = ticketFileRow(index, composedChunk, "PROJ-1", MANUAL_RUN_CTX);
    expect(rows).toEqual([
      {
        key: "ticket",
        label: "PROJ-1 · manual run",
        dotColor: "green",
        line: [1, 3],
        branch: null,
        decision: null,
      },
    ]);
  });

  it("a file with no lines owned by the ticket at all reports 0/0 (StatsCard renders '—', not a crash)", () => {
    const rows = ticketFileRow(index, makeFileChunk(), "PROJ-1");
    expect(rows[0].line).toEqual([0, 0]);
  });
});

// Per-file per-tier counts (follow-up item 6a): before these, a TicketChunk
// carried owned/covered counts only, so a ticket-scoped subtree had no tier
// breakdown to render and `ticketTreeRow` could only offer one aggregate
// row. The fixtures below give each file a DIFFERENT tier's coverage so a
// summed rollup is distinguishable from either file's own numbers — a
// fixture where both files shared one tier would pass against a rollup
// wrongly copied onto every node.
describe("ticket-scoped per-tier counts", () => {
  const TWO_FILE_TREE = {
    name: "",
    dirs: [],
    files: [
      { name: "a.c", path: "a.c", chunk: "a", stats: emptyStats() },
      { name: "b.c", path: "b.c", chunk: "b", stats: emptyStats() },
    ],
    stats: emptyStats(),
  } as unknown as DirNode;

  const TIERS = { "a.c": { unit: 1, system: 0 }, "b.c": { unit: 0, system: 1 } };

  it("scopes each file's per_tier to the ticket's own lines", () => {
    const scoped = scopedOrThrow(
      TWO_FILE_TREE,
      { "a.c": [1, 2], "b.c": [1] },
      { "a.c": [1], "b.c": [1] },
      TIERS,
    );
    const byName = Object.fromEntries(scoped.files.map((f) => [f.name, f]));
    expect(byName["a.c"].stats.lines.per_tier).toEqual({ unit: 1, system: 0 });
    expect(byName["b.c"].stats.lines.per_tier).toEqual({ unit: 0, system: 1 });
  });

  it("sums per_tier across a directory's scoped children", () => {
    const scoped = scopedOrThrow(
      TWO_FILE_TREE,
      { "a.c": [1, 2], "b.c": [1] },
      { "a.c": [1], "b.c": [1] },
      TIERS,
    );
    expect(scoped.stats.lines.per_tier).toEqual({ unit: 1, system: 1 });
  });

  it("ticketChunkToFileLines carries the chunk's per-file tier counts through", () => {
    const chunk = makeTicketChunk({
      files: [
        { path: "a.c", owned: 2, covered: 1, missing: [[2, 2]], per_tier: { unit: 1, system: 0 } },
      ],
    });
    expect(ticketChunkToFileLines(chunk).tiers).toEqual({ "a.c": { unit: 1, system: 0 } });
  });

  it("ticketTreeRow renders a real number per tier instead of one aggregate row", () => {
    const index = makeIndex({
      tier_order: ["unit", "system"],
      tier_labels: { unit: "Unit", system: "System" },
      tier_colors: { unit: "#111", system: "#222" },
    });
    const node = {
      stats: emptyStats({ lines: { total: 3, hit: 2, per_tier: { unit: 1, system: 1 } } }),
    } as unknown as DirNode;

    const rows = ticketTreeRow(index, node, "PROJ-1");

    expect(rows.map((r) => r.key)).toEqual(["unit", "system", "ticket"]);
    expect(rows[0].line).toEqual([1, 3]);
    expect(rows[1].line).toEqual([1, 3]);
    expect(rows[2].line).toEqual([2, 3]);
  });

  it("still declines to a single row when a context is ALSO focused", () => {
    // Unchanged behaviour: a ticket+context cross-tab does not exist at tree
    // granularity, so the honest answer stays "no data" (the 333.3% case).
    const index = makeIndex({ tier_order: ["unit"] });
    const node = {
      stats: emptyStats({ lines: { total: 3, hit: 2, per_tier: { unit: 1 } } }),
    } as unknown as DirNode;
    const ctx = { label: "manual" } as Context;

    const rows = ticketTreeRow(index, node, "PROJ-1", ctx);

    expect(rows).toHaveLength(1);
    expect(rows[0].line).toBeNull();
  });
});
