// stats.ts's contract (task-3 brief): percentage math + threshold coloring
// (thresholds are ALWAYS data-driven — never hard-coded constants, hence the
// second thresholds fixture below proving no baked-in 80/70) and findNode's
// tree walk.
import { describe, expect, it } from "vitest";

import { findNode, fmtPct, nodeStats, pct, pctClass } from "./stats";
import type { DirNode, FileNode, Stats } from "./types";

function emptyStats(overrides: Partial<Stats> = {}): Stats {
  return {
    lines: { total: 0, hit: 0, per_tier: {}, asserted_per_tier: {}, asserted_only: 0 },
    branches: { total: 0, hit: 0, per_tier: {} },
    flags: { stale: 0, aging: 0, excluded: 0 },
    ctx_lines: {},
    ...overrides,
  };
}

describe("pct", () => {
  it("returns null when total is 0 (avoids division by zero)", () => {
    expect(pct(0, 0)).toBeNull();
  });

  it("computes a percentage otherwise", () => {
    expect(pct(50, 100)).toBe(50);
    expect(pct(1, 3)).toBeCloseTo(33.333, 2);
  });
});

describe("pctClass", () => {
  const thresholds = { high: 80, medium: 70 };

  it("boundary: exactly high -> pct-high", () => {
    expect(pctClass(80, thresholds)).toBe("pct-high");
  });

  it("boundary: just under high -> pct-mid", () => {
    expect(pctClass(79.9, thresholds)).toBe("pct-mid");
  });

  it("boundary: exactly medium -> pct-mid", () => {
    expect(pctClass(70, thresholds)).toBe("pct-mid");
  });

  it("boundary: just under medium -> pct-low", () => {
    expect(pctClass(69.9, thresholds)).toBe("pct-low");
  });

  it("null -> pct-na", () => {
    expect(pctClass(null, thresholds)).toBe("pct-na");
  });

  it("thresholds come from the payload, not hard-coded constants", () => {
    // With high=80/medium=70, 50 would be pct-low. With a DIFFERENT
    // thresholds object (high=40/medium=30), the same 50 must be pct-high —
    // proving pctClass reads its thresholds arg, not a baked-in constant.
    expect(pctClass(50, thresholds)).toBe("pct-low");
    expect(pctClass(50, { high: 40, medium: 30 })).toBe("pct-high");
  });
});

describe("fmtPct", () => {
  it("formats to one decimal with a percent sign", () => {
    expect(fmtPct(71.9403)).toBe("71.9%");
    expect(fmtPct(100)).toBe("100.0%");
    expect(fmtPct(0)).toBe("0.0%");
  });

  it("renders an em dash for null", () => {
    expect(fmtPct(null)).toBe("—");
  });

  it("rounds half-up to one decimal", () => {
    expect(fmtPct(33.333)).toBe("33.3%");
    expect(fmtPct(66.666)).toBe("66.7%");
  });
});

describe("nodeStats", () => {
  it("returns the node's own stats bag verbatim", () => {
    const stats = emptyStats({
      lines: { total: 10, hit: 7, per_tier: { unit: 7 }, asserted_per_tier: {}, asserted_only: 0 },
    });
    const file: FileNode = { name: "x.c", path: "a/x.c", chunk: "a_x.c", stats };
    expect(nodeStats(file)).toBe(stats);

    const dir: DirNode = { name: "a", dirs: [], files: [file], stats };
    expect(nodeStats(dir)).toBe(stats);
  });
});

describe("findNode", () => {
  function tree(): DirNode {
    const y: FileNode = { name: "y.c", path: "a/b/y.c", chunk: "a_b_y.c", stats: emptyStats() };
    const b: DirNode = { name: "b", dirs: [], files: [y], stats: emptyStats() };
    const x: FileNode = { name: "x.c", path: "a/x.c", chunk: "a_x.c", stats: emptyStats() };
    const a: DirNode = { name: "a", dirs: [b], files: [x], stats: emptyStats() };
    return { name: "root", dirs: [a], files: [], stats: emptyStats() };
  }

  it("empty segments resolves to the root node", () => {
    const root = tree();
    expect(findNode(root, [])).toBe(root);
  });

  it("walks into a directory by name", () => {
    const root = tree();
    const found = findNode(root, ["a"]);
    expect(found).not.toBeNull();
    expect((found as DirNode).name).toBe("a");
    expect((found as DirNode).dirs).toHaveLength(1);
  });

  it("walks into a nested directory", () => {
    const root = tree();
    const found = findNode(root, ["a", "b"]);
    expect((found as DirNode)?.name).toBe("b");
  });

  it("resolves a file as the last segment", () => {
    const root = tree();
    const found = findNode(root, ["a", "x.c"]);
    expect((found as FileNode)?.path).toBe("a/x.c");
  });

  it("resolves a nested file as the last segment", () => {
    const root = tree();
    const found = findNode(root, ["a", "b", "y.c"]);
    expect((found as FileNode)?.path).toBe("a/b/y.c");
  });

  it("returns null for an unknown top-level segment", () => {
    const root = tree();
    expect(findNode(root, ["nope"])).toBeNull();
  });

  it("returns null when a middle segment doesn't exist (can't descend further)", () => {
    const root = tree();
    expect(findNode(root, ["a", "nope", "y.c"])).toBeNull();
  });

  it("returns null when the last segment matches neither a dir nor a file", () => {
    const root = tree();
    expect(findNode(root, ["a", "nope"])).toBeNull();
  });
});
