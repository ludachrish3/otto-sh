// Percentage math + tree lookup shared by every covapp chrome/page (Task 3
// brief). Thresholds are ALWAYS an argument, never a module-level constant —
// they come from `IndexPayload.thresholds` (store v4, Global Constraints),
// so a report with different thresholds colors correctly without a code
// change.
import type { DirNode, FileNode, Stats, Thresholds } from "./types";

/** `null` when `total` is 0 (nothing to divide by — e.g. an uncoverable
 * node) rather than NaN/Infinity, so callers can render "—" instead of a
 * bogus percentage. */
export function pct(hit: number, total: number): number | null {
  return total > 0 ? (100 * hit) / total : null;
}

export type PctClass = "pct-high" | "pct-mid" | "pct-low" | "pct-na";

/** `>= high` wins over `>= medium` wins over low; `null` (no data) is its
 * own bucket, never coerced into "low". */
export function pctClass(p: number | null, thresholds: Thresholds): PctClass {
  if (p === null) return "pct-na";
  if (p >= thresholds.high) return "pct-high";
  if (p >= thresholds.medium) return "pct-mid";
  return "pct-low";
}

/** One decimal place + "%", "—" for null (mirrors the mockup's `fmt()`). */
export function fmtPct(p: number | null): string {
  if (p === null) return "—";
  return `${(Math.round(p * 10) / 10).toFixed(1)}%`;
}

/** Trivial today (the tree already carries a rolled-up `Stats` per node),
 * kept as a named seam so callers don't reach into `.stats` directly —
 * Tasks 4-7 read stats through this function, not the field, in case a
 * later task needs to derive rather than read (e.g. focus-mode filtering). */
export function nodeStats(node: DirNode | FileNode): Stats {
  return node.stats;
}

/** Walks `tree` by directory name for every segment but the last, which may
 * additionally match a file. Empty `segments` resolves to `tree` itself.
 * Returns `null` as soon as a segment can't be resolved (unknown dir, or a
 * last segment that matches neither a dir nor a file). */
export function findNode(tree: DirNode, segments: string[]): DirNode | FileNode | null {
  if (segments.length === 0) return tree;

  let current: DirNode = tree;
  for (let i = 0; i < segments.length; i++) {
    const segment = segments[i];
    const dir = current.dirs.find((d) => d.name === segment);
    if (dir) {
      current = dir;
      continue;
    }
    if (i === segments.length - 1) {
      const file = current.files.find((f) => f.name === segment);
      if (file) return file;
    }
    return null;
  }
  return current;
}
