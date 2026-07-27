// Ticket context (Task 12 brief): a report-wide DENOMINATOR filter —
// deliberately the OPPOSITE arithmetic direction from run focus (focus.tsx's
// `?ctx=`, which narrows the NUMERATOR: only that run's hits count, but all
// code stays in view). Pinning a ticket (`?ticket=<id>`, also focus.tsx)
// narrows the DENOMINATOR instead: only that ticket's lines are in scope at
// all. The two compose (focus.tsx's `ctx`/`ticket` are independent hash-
// query params, neither ever clears the other), so "PROJ-412's lines, as
// proven by the manual run" is expressible: ticket scoping picks the
// denominator (which lines exist at all), then a focused context's existing
// numerator logic (ctx_lines / member-run hits) applies on top of that
// narrowed set.
//
// Two callers, two precision levels, because of where each one's data comes
// from:
//   - DirectoryPage scopes a whole subtree using a loaded `TicketChunk`
//     (cov_data/tickets/<chunk>.js) — which carries only per-file OWNED/
//     COVERED *counts* (plus the real line numbers of the "missing"
//     uncovered-owned subset, unused here), never the full real owned/hit
//     line-number identities. `ticketChunkToFileLines` converts those counts
//     into the length-only line-number-array shape `scopeTreeToTicket`
//     consumes — loading every file's own chunk just to scope a directory
//     listing isn't reasonable, so this is the coarsest precision level.
//   - FilePage already has ONE file's full `FileChunk` loaded (per-line
//     `LineJson.ticket`/`hits`/`run`) once the user is looking at it, so
//     `ticketFileRow` computes an EXACT owned/hit count directly from real
//     per-line data, no placeholder arrays involved.

import type { TierStatRow } from "./chrome/StatsCard";
import type { Context } from "./contexts";
import { lineHasMemberHit } from "./format";
import type { DirNode, FileChunk, FileNode, IndexPayload, Stats, TicketChunk } from "./types";

/** One file's `Stats["lines"]` recomputed against the ticket's OWN line set:
 * `total`/`hit` become the LENGTH of `owned`/`hit` (never the file's
 * whole-repo counts) — the entire point of a denominator filter is that a
 * file where the ticket touched 12 of 400 coverable lines reports coverage
 * of those 12, not of the 400. Every other `Stats` field (branches/flags/
 * per_tier/ctx_lines) is carried over from the file's ORIGINAL, whole-file
 * stats verbatim: none of that data exists in a per-ticket-scoped form (a
 * `TicketChunk` carries owned/covered LINE counts only) — a known,
 * documented limitation, not an oversight; callers that render a Branch %
 * or per-tier % for a ticket-scoped row should treat those as "no data"
 * (mirroring how `DirectoryPage`'s existing run-focus rows already treat
 * Branch % under focus, for the identical reason: v4 doesn't store that
 * granularity per-run either). */
function scopeFileStats(stats: Stats, owned: number[], hit: number[]): Stats {
  return {
    ...stats,
    lines: { ...stats.lines, total: owned.length, hit: hit.length },
  };
}

function scopeFile(
  file: FileNode,
  ticketLines: Record<string, number[]>,
  ticketHits: Record<string, number[]>,
): FileNode | null {
  const owned = ticketLines[file.path];
  if (owned === undefined) return null; // the ticket never touched this file
  const hit = ticketHits[file.path] ?? [];
  return { ...file, stats: scopeFileStats(file.stats, owned, hit) };
}

/** A directory's OWN rollup, recomputed as the sum of its (already-scoped)
 * children's `lines.total`/`lines.hit` — the same "denominator = the
 * ticket's lines only" contract `scopeFileStats` applies to one file,
 * aggregated one level up so a directory row (`DirectoryPage`'s tree, or its
 * top StatsCard for "this node and below") reports the scoped total too,
 * never the original whole-subtree one. */
function aggregateDirStats(stats: Stats, dirs: DirNode[], files: FileNode[]): Stats {
  let total = 0;
  let hit = 0;
  for (const d of dirs) {
    total += d.stats.lines.total;
    hit += d.stats.lines.hit;
  }
  for (const f of files) {
    total += f.stats.lines.total;
    hit += f.stats.lines.hit;
  }
  return { ...stats, lines: { ...stats.lines, total, hit } };
}

/** Pure recursive filter (Task 12 brief): keeps only the files/directories a
 * pinned ticket actually touched, recomputing each surviving file's
 * `stats.lines` denominator (total) AND numerator (hit) against the
 * ticket's OWN line set (`scopeFileStats`) and each surviving directory's
 * rollup as the sum of its scoped children (`aggregateDirStats`) — never a
 * passthrough of the original, whole-repo numbers. A directory left with no
 * surviving files or subdirectories is DROPPED rather than rendered as a
 * hollow row (design §6.3: hide non-participating files and the
 * directories they empty, rather than dimming — the deliberate divergence
 * from run focus, which dims instead of hiding because it narrows the
 * numerator only, leaving all code on screen). The whole call returns
 * `null` when the ticket touched nothing anywhere in this subtree — the
 * caller (`DirectoryPage`) treats that as "nothing to show here", not a
 * crash.
 *
 * `ticketLines`/`ticketHits` are keyed by `FileNode.path` (display-relative,
 * matching `TicketChunk.files[].path`) — see `ticketChunkToFileLines` for
 * how `DirectoryPage` builds them from a loaded `TicketChunk`. */
export function scopeTreeToTicket(
  node: DirNode,
  ticketLines: Record<string, number[]>,
  ticketHits: Record<string, number[]>,
): DirNode | null {
  const files = node.files
    .map((f) => scopeFile(f, ticketLines, ticketHits))
    .filter((f): f is FileNode => f !== null);
  const dirs = node.dirs
    .map((d) => scopeTreeToTicket(d, ticketLines, ticketHits))
    .filter((d): d is DirNode => d !== null);

  if (files.length === 0 && dirs.length === 0) return null;

  return { ...node, dirs, files, stats: aggregateDirStats(node.stats, dirs, files) };
}

/** Converts a loaded `TicketChunk` into the `{lines, hits}` per-file
 * line-number-array shape `scopeTreeToTicket` consumes. A `TicketChunk`
 * carries only per-file OWNED/COVERED *counts* (plus the real line numbers
 * of the "missing" — owned-but-uncovered — subset; see
 * `TicketChunk.files[].missing` in types.ts, used by TicketsPage's
 * missing-line links) — never the full set of real owned/hit line-number
 * IDENTITIES (that granularity lives in each file's own `FileChunk`, not
 * loaded for a whole tree at once just to pin a ticket). `scopeTreeToTicket`
 * only ever reads array LENGTH to recompute a node's denominator/numerator,
 * so these are placeholder arrays sized to match the real counts — the
 * values themselves are never read by anything. */
export function ticketChunkToFileLines(chunk: TicketChunk): {
  lines: Record<string, number[]>;
  hits: Record<string, number[]>;
} {
  const lines: Record<string, number[]> = {};
  const hits: Record<string, number[]> = {};
  for (const file of chunk.files) {
    lines[file.path] = Array.from({ length: file.owned });
    hits[file.path] = Array.from({ length: file.covered });
  }
  return { lines, hits };
}

/** `DirectoryPage`'s ticket-only StatsCard row (mirrors `format.ts`'s
 * `focusedTreeRow` shape exactly, for the same reason: branch/decision are
 * always `null` — "no data" — because neither is tracked per-ticket, same
 * as `focusedTreeRow`'s branch is `null` because it isn't tracked per-run).
 * `node` must already be the SCOPED node (`scopeTreeToTicket`'s return
 * value) — this reads its `stats.lines` directly, verbatim, doing no
 * scoping itself. No `dotColor`: unlike a run-focus `Context`, a ticket has
 * no single tier of its own (design §6.1: a ticket's lines can span every
 * tier).
 *
 * Optional `ctx` (Task 12 fix round 1, IMPORTANT): when a context is ALSO
 * focused, `line` declines to `null` ("no data", same treatment `branch`/
 * `decision` already get) rather than dividing `stats.ctx_lines[ctx.label]`
 * (a whole-file numerator) by `node.stats.lines.total` (the ticket-scoped
 * denominator) — the two aren't commensurable at tree granularity (no
 * per-line ticket+run cross-tab exists without loading every scoped file's
 * own `FileChunk`), so the honest answer is "we don't know", never a
 * plausible-looking but out-of-range percentage (a real fixture: 10
 * whole-file ctx hits over a 3-line ticket scope reads "333.3%" if computed
 * naively). Contrast `ticketFileRow` below, whose OWN composed case
 * recomputes exactly — it already has one file's full per-line data, which
 * this tree-level function does not. */
export function ticketTreeRow(node: DirNode, ticketId: string, ctx?: Context): TierStatRow[] {
  return [
    {
      key: "ticket",
      label: ctx ? `${ticketId} · ${ctx.label}` : ticketId,
      dotColor: undefined,
      line: ctx ? null : [node.stats.lines.hit, node.stats.lines.total],
      branch: null,
      decision: null,
    },
  ];
}

/** `FilePage`'s ticket-scoped StatsCard row — unlike `ticketTreeRow` (which
 * reads pre-scoped counts off a `DirNode`), this recomputes owned/hit
 * directly from the loaded `FileChunk`'s real per-line data (`LineJson.
 * ticket`/`hits`), which FilePage already has in full for the one file it's
 * showing — no placeholder counts needed at this granularity.
 *
 * Optional `ctx` composes run focus's numerator with the ticket's
 * denominator (spec's headline example: "PROJ-412's lines, as proven by the
 * manual run") — a line counts toward `hit` only if BOTH the ticket owns it
 * AND a member run of `ctx` hit it (`lineHasMemberHit`, the same per-run
 * membership test `FilePage.tsx`'s own row tinting and `format.ts`'s
 * `focusedFileRow` use); without `ctx`, "any tier recorded a hit" (mirrors
 * `chunkTierRows`'s "hit if any tier count > 0") is enough. */
export function ticketFileRow(
  index: IndexPayload,
  chunk: FileChunk,
  ticketId: string,
  ctx?: Context,
): TierStatRow[] {
  const memberIds = ctx ? new Set(ctx.runs.map((r) => r.id)) : null;
  let owned = 0;
  let hit = 0;
  for (const line of Object.values(chunk.lines)) {
    if (!line.ticket?.includes(ticketId)) continue;
    owned++;
    const isHit = memberIds
      ? lineHasMemberHit(line, memberIds)
      : Object.values(line.hits).some((n) => n > 0);
    if (isHit) hit++;
  }
  return [
    {
      key: "ticket",
      label: ctx ? `${ticketId} · ${ctx.label}` : ticketId,
      dotColor: ctx ? index.tier_colors[ctx.tier] : undefined,
      line: [hit, owned],
      branch: null,
      decision: null,
    },
  ];
}
