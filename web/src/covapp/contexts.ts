// Context grouping (Task 6 brief): the runs & contexts page treats every
// `RunJson` sharing a `label` as ONE context — the common case is a
// multi-host run (same otto test invocation, several hosts/DUTs), but a
// unit harvest or a one-off manual capture is just a context with a single
// member. Pure module, no React — `groupContexts`/`searchHaystack` are
// consumed directly by contexts.test.ts and by RunsPage.tsx's render/filter
// logic.
//
// Assumption (data anomaly, not a supported case): `tier` is read from the
// FIRST member run encountered for a label. The spec's grouping key is
// `label` alone — nothing requires every run sharing a label to share a
// tier — so a label spanning tiers is possible in principle. Same for the
// other per-context "scalar" display fields RunsPage.tsx reads off
// `ctx.runs[0]` (board/labs/captured_at/tester/ticket/note/base_commit):
// this module doesn't materialize them onto `Context` (the produced
// interface, verbatim per the brief, only carries `runs`), so callers
// needing them read `ctx.runs[0]` directly, with the same one-value
// assumption.
import type { IndexPayload, RunJson } from "./types";

export interface Context {
  label: string;
  tier: string;
  runs: RunJson[];
  /** `[hostDisplay, lines]` — one entry PER MEMBER RUN, even when two runs
   * report the same host display (e.g. the same physical host run twice
   * under one label). Never deduplicated by host. */
  hosts: [string, number][];
  lines: number;
  revoked: number;
  /** `[displayPath, count]`, merged across every member run, sorted desc by
   * count then asc by path (ties broken deterministically). */
  files: [string, number][];
  status: "ok" | "aging" | "stale";
  remapped: boolean;
}

function hostDisplay(run: RunJson): string {
  return run.host || run.board || "—";
}

/** Merges each member's `run_contrib[id].files` (path -> count) into one
 * sorted list. Desc by count; ties broken asc by path so output is
 * deterministic across runs/environments (JS object/Map iteration order
 * would otherwise leak insertion order into a tie). */
function mergeFiles(fileLists: [string, number][][]): [string, number][] {
  const totals = new Map<string, number>();
  for (const files of fileLists) {
    for (const [path, count] of files) {
      totals.set(path, (totals.get(path) ?? 0) + count);
    }
  }
  return [...totals.entries()].sort(([pathA, countA], [pathB, countB]) => {
    if (countB !== countA) return countB - countA;
    return pathA < pathB ? -1 : pathA > pathB ? 1 : 0;
  });
}

/** Groups `payload.runs` by `label`, insertion order of first appearance
 * (both for the returned `Context[]` order and each context's own `runs`
 * member order). Per-run contribution comes from
 * `payload.run_contrib[String(run.id)]`, defensively defaulted (`?? 0`/
 * `?? []`) — a run with no matching `run_contrib` entry (shouldn't happen,
 * but the data contract doesn't guarantee it) contributes zero lines/
 * revoked/files rather than throwing. */
export function groupContexts(payload: IndexPayload): Context[] {
  const order: string[] = [];
  const members = new Map<string, RunJson[]>();
  for (const run of payload.runs) {
    if (!members.has(run.label)) {
      order.push(run.label);
      members.set(run.label, []);
    }
    members.get(run.label)?.push(run);
  }

  return order.map((label) => {
    const runs = members.get(label) ?? [];
    const hosts: [string, number][] = [];
    let lines = 0;
    let revoked = 0;
    const fileLists: [string, number][][] = [];
    let anyAging = false;
    let anyRemapped = false;

    for (const run of runs) {
      const contrib = payload.run_contrib[String(run.id)];
      hosts.push([hostDisplay(run), contrib?.lines ?? 0]);
      lines += contrib?.lines ?? 0;
      revoked += contrib?.revoked ?? 0;
      fileLists.push(contrib?.files ?? []);
      if (run.aging) anyAging = true;
      if (run.dirty_remap) anyRemapped = true;
    }

    const status: Context["status"] =
      lines === 0 && revoked > 0 ? "stale" : anyAging ? "aging" : "ok";

    return {
      label,
      tier: runs[0]?.tier ?? "",
      runs,
      hosts,
      lines,
      revoked,
      files: mergeFiles(fileLists),
      status,
      remapped: anyRemapped,
    };
  });
}

/** Lowercase, space-joined haystack for the free-text search filter — label,
 * every member's host display, every non-null ticket, every board.
 * Substring-matched by the caller (`haystack.includes(query.toLowerCase())`),
 * so this only needs to concatenate, not tokenize. */
export function searchHaystack(ctx: Context): string {
  const parts: string[] = [ctx.label];
  for (const [host] of ctx.hosts) parts.push(host);
  for (const run of ctx.runs) {
    if (run.ticket) parts.push(run.ticket);
    parts.push(run.board);
  }
  return parts.join(" ").toLowerCase();
}
