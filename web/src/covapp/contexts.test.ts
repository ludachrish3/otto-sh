import { describe, expect, it } from "vitest";

import { groupContexts, resolveScope, scopeTreeLines, searchHaystack } from "./contexts";
import { emptyStats, makeIndex, makeRun } from "./testUtils";
import type { IndexPayload, RunJson } from "./types";

describe("groupContexts", () => {
  it("groups two runs sharing a label into one context with a host-pill entry per run", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "nightly-full", host: "router-a" }),
      makeRun({ id: 2, label: "nightly-full", host: "router-b" }),
    ];
    const index = makeIndex({
      runs,
      run_contrib: {
        "1": { lines: 875, revoked: 0, files: [] },
        "2": { lines: 799, revoked: 0, files: [] },
      },
    });

    const contexts = groupContexts(index);
    expect(contexts).toHaveLength(1);
    const ctx = contexts[0];
    expect(ctx.label).toBe("nightly-full");
    expect(ctx.runs).toHaveLength(2);
    expect(ctx.hosts).toEqual([
      ["router-a", 875],
      ["router-b", 799],
    ]);
    expect(ctx.lines).toBe(875 + 799);
  });

  it("does not dedupe hosts: the same host display twice yields two host entries", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "dup-host", host: "bench-1" }),
      makeRun({ id: 2, label: "dup-host", host: "bench-1" }),
    ];
    const index = makeIndex({
      runs,
      run_contrib: {
        "1": { lines: 10, revoked: 0, files: [] },
        "2": { lines: 20, revoked: 0, files: [] },
      },
    });

    const [ctx] = groupContexts(index);
    expect(ctx.hosts).toEqual([
      ["bench-1", 10],
      ["bench-1", 20],
    ]);
  });

  it("falls back host display to board, then em-dash, when host is empty", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "board-only", host: "", board: "stm32h7-rev2" }),
      makeRun({ id: 2, label: "no-host-no-board", host: "", board: "" }),
    ];
    const index = makeIndex({ runs, run_contrib: {} });
    const contexts = groupContexts(index);
    expect(contexts.find((c) => c.label === "board-only")?.hosts).toEqual([["stm32h7-rev2", 0]]);
    expect(contexts.find((c) => c.label === "no-host-no-board")?.hosts).toEqual([["—", 0]]);
  });

  it("preserves insertion order of first appearance for both contexts and member runs", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "b-label", host: "h1" }),
      makeRun({ id: 2, label: "a-label", host: "h2" }),
      makeRun({ id: 3, label: "b-label", host: "h3" }),
    ];
    const index = makeIndex({ runs, run_contrib: {} });
    const contexts = groupContexts(index);
    expect(contexts.map((c) => c.label)).toEqual(["b-label", "a-label"]);
    expect(contexts[0].runs.map((r) => r.host)).toEqual(["h1", "h3"]);
  });

  it("defaults run_contrib lookups to 0/[] when a run id has no matching entry", () => {
    const runs: RunJson[] = [makeRun({ id: 42, label: "orphan" })];
    const index = makeIndex({ runs, run_contrib: {} });
    const [ctx] = groupContexts(index);
    expect(ctx.lines).toBe(0);
    expect(ctx.revoked).toBe(0);
    expect(ctx.files).toEqual([]);
    expect(ctx.hosts).toEqual([["router-a", 0]]);
  });

  it("uses the first member run's tier when a label spans tiers (documented data anomaly)", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "mixed", tier: "system" }),
      makeRun({ id: 2, label: "mixed", tier: "unit" }),
    ];
    const index = makeIndex({ runs, run_contrib: {} });
    const [ctx] = groupContexts(index);
    expect(ctx.tier).toBe("system");
  });

  it("merges per-path file counts across members: summed, sorted desc by count then asc by path", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "nightly-full" }),
      makeRun({ id: 2, label: "nightly-full" }),
    ];
    const index = makeIndex({
      runs,
      run_contrib: {
        "1": {
          lines: 10,
          revoked: 0,
          files: [
            ["src/net/tcp.c", 100],
            ["src/main.c", 5],
          ],
        },
        "2": {
          lines: 10,
          revoked: 0,
          files: [
            ["src/net/tcp.c", 50],
            ["src/core/sched.c", 5],
          ],
        },
      },
    });
    const [ctx] = groupContexts(index);
    expect(ctx.files).toEqual([
      ["src/net/tcp.c", 150],
      ["src/core/sched.c", 5],
      ["src/main.c", 5],
    ]);
  });

  describe("status", () => {
    it("is 'stale' when lines === 0 and revoked > 0", () => {
      const runs: RunJson[] = [makeRun({ id: 1, label: "smoke-2025" })];
      const index = makeIndex({
        runs,
        run_contrib: { "1": { lines: 0, revoked: 118, files: [] } },
      });
      expect(groupContexts(index)[0].status).toBe("stale");
    });

    it("is 'aging' when any member run is aging (and not stale)", () => {
      const runs: RunJson[] = [
        makeRun({ id: 1, label: "field bring-up", aging: false }),
        makeRun({ id: 2, label: "field bring-up", aging: true }),
      ];
      const index = makeIndex({
        runs,
        run_contrib: {
          "1": { lines: 40, revoked: 0, files: [] },
          "2": { lines: 31, revoked: 0, files: [] },
        },
      });
      expect(groupContexts(index)[0].status).toBe("aging");
    });

    it("is 'ok' otherwise", () => {
      const runs: RunJson[] = [makeRun({ id: 1, label: "RF cert sweep" })];
      const index = makeIndex({
        runs,
        run_contrib: { "1": { lines: 52, revoked: 0, files: [] } },
      });
      expect(groupContexts(index)[0].status).toBe("ok");
    });

    it("prefers 'stale' over 'aging' when both conditions are technically met", () => {
      const runs: RunJson[] = [makeRun({ id: 1, label: "weird", aging: true })];
      const index = makeIndex({
        runs,
        run_contrib: { "1": { lines: 0, revoked: 5, files: [] } },
      });
      expect(groupContexts(index)[0].status).toBe("stale");
    });

    it("a context with zero lines and zero revoked (no data at all) is 'ok', not 'stale'", () => {
      const runs: RunJson[] = [makeRun({ id: 1, label: "nodata" })];
      const index = makeIndex({ runs, run_contrib: {} });
      expect(groupContexts(index)[0].status).toBe("ok");
    });
  });

  it("remapped is true when any member run has dirty_remap set", () => {
    const runs: RunJson[] = [
      makeRun({ id: 1, label: "field bring-up", dirty_remap: false }),
      makeRun({ id: 2, label: "field bring-up", dirty_remap: true }),
    ];
    const index = makeIndex({ runs, run_contrib: {} });
    expect(groupContexts(index)[0].remapped).toBe(true);
  });

  it("remapped is false when no member run has dirty_remap set", () => {
    const runs: RunJson[] = [makeRun({ id: 1, label: "clean", dirty_remap: false })];
    const index = makeIndex({ runs, run_contrib: {} });
    expect(groupContexts(index)[0].remapped).toBe(false);
  });

  it("returns [] for an empty runs list, without crashing", () => {
    const index = makeIndex({ runs: [], run_contrib: {} });
    expect(groupContexts(index)).toEqual([]);
  });
});

// Per-product spec §10: a pinned product narrows the NUMERATOR the same way
// a focused context does, and the two compose — `resolveScope` is the one
// place that turns the two independent pins (`?ctx=`, `?product=`) into the
// single `FocusScope` every page recomputes against.
describe("resolveScope / scopeTreeLines", () => {
  const index = makeIndex({
    products: ["agent", "app"],
    runs: [
      makeRun({ id: 0, label: "nightly", product: "app", host: "h1" }),
      makeRun({ id: 1, label: "nightly", product: "agent", host: "h1" }),
      makeRun({ id: 2, label: "smoke", product: "app", host: "h2" }),
    ],
  });
  const stats = emptyStats({
    ctx_lines: { nightly: 5, smoke: 2 },
    product_lines: { app: 6, agent: 1 },
    ctx_product_lines: { nightly: { app: 4, agent: 1 }, smoke: { app: 2 } },
  });

  it("a bare product scope spans contexts and tiers", () => {
    const scope = resolveScope(index, null, "app");
    expect(scope).toMatchObject({ label: "app", tier: "", ctxLabel: null, product: "app" });
    expect(scope?.runs.map((r) => r.id)).toEqual([0, 2]);
    expect(scope && scopeTreeLines(stats, scope)).toBe(6);
  });

  it("ctx ∧ product intersects the run sets and reads the pair count", () => {
    const scope = resolveScope(index, "nightly", "app");
    expect(scope).toMatchObject({ label: "nightly · app", ctxLabel: "nightly", product: "app" });
    expect(scope?.runs.map((r) => r.id)).toEqual([0]);
    expect(scope && scopeTreeLines(stats, scope)).toBe(4);
  });

  it("a bare ctx scope is the Context itself", () => {
    const scope = resolveScope(index, "nightly", null);
    expect(scope?.runs.length).toBe(2);
    expect(scope && scopeTreeLines(stats, scope)).toBe(5);
  });

  it("an empty intersection yields no scope", () => {
    expect(resolveScope(index, "smoke", "agent")).toBeUndefined();
  });

  it("a product absent from index.products is ignored, falling back to the ctx scope", () => {
    // `FocusProvider`/`resolveProduct` already validates `?product=` against
    // `index.products` before it reaches a page; this is the defensive half,
    // so an unknown name can never silently empty the numerator.
    expect(resolveScope(index, "nightly", "ghost")).toMatchObject({
      label: "nightly",
      product: null,
    });
    expect(resolveScope(index, null, "ghost")).toBeUndefined();
  });

  it("a run with no product ('' — an unnamed unit run) never matches a product pin", () => {
    const unnamed = makeIndex({
      products: ["app"],
      runs: [makeRun({ id: 7, label: "unit harvest", product: "" })],
    });
    expect(resolveScope(unnamed, null, "app")).toBeUndefined();
  });

  it("an unresolvable focus label yields no scope, product pinned or not", () => {
    expect(resolveScope(index, "ghost-label", null)).toBeUndefined();
    expect(resolveScope(index, "ghost-label", "app")).toBeUndefined();
  });

  it("resolves off a caller-supplied contexts list identically (RunsPage's one-grouping path)", () => {
    const contexts = groupContexts(index);
    expect(resolveScope(index, "nightly", "app", contexts)).toEqual(
      resolveScope(index, "nightly", "app"),
    );
    expect(resolveScope(index, "nightly", null, contexts)).toEqual(contexts[0]);
    expect(resolveScope(index, "ghost-label", null, contexts)).toBeUndefined();
  });

  it("hostDisplay appends the product and the haystack includes it", () => {
    const ctx = groupContexts(index)[0];
    expect(ctx.hosts.map(([h]) => h)).toEqual(["h1 · app", "h1 · agent"]);
    expect(searchHaystack(ctx)).toContain("agent");
  });
});

describe("searchHaystack", () => {
  function haystackFor(overrides: Partial<IndexPayload> = {}): string {
    const index = makeIndex(overrides);
    return searchHaystack(groupContexts(index)[0]);
  }

  it("includes the label, lowercased", () => {
    const runs = [makeRun({ id: 1, label: "Nightly-Full" })];
    expect(haystackFor({ runs, run_contrib: {} })).toContain("nightly-full");
  });

  it("includes every member's host display, lowercased", () => {
    const runs = [
      makeRun({ id: 1, label: "l", host: "Router-A" }),
      makeRun({ id: 2, label: "l", host: "Router-B" }),
    ];
    const hay = haystackFor({ runs, run_contrib: {} });
    expect(hay).toContain("router-a");
    expect(hay).toContain("router-b");
  });

  it("includes non-null tickets, lowercased, and skips null tickets", () => {
    const runs = [
      makeRun({ id: 1, label: "l", ticket: "FW-1204" }),
      makeRun({ id: 2, label: "l", ticket: null }),
    ];
    const hay = haystackFor({ runs, run_contrib: {} });
    expect(hay).toContain("fw-1204");
  });

  it("includes every member's board, lowercased", () => {
    const runs = [makeRun({ id: 1, label: "l", board: "STM32H7-Rev3" })];
    expect(haystackFor({ runs, run_contrib: {} })).toContain("stm32h7-rev3");
  });

  it("matches a ticket substring case-insensitively (the search filter's own use case)", () => {
    const runs = [makeRun({ id: 1, label: "field bring-up", ticket: "FW-1188" })];
    const hay = haystackFor({ runs, run_contrib: {} });
    expect(hay.includes("fw-1188".toLowerCase())).toBe(true);
    expect(hay.includes("1188")).toBe(true);
  });
});
