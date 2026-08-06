// Ticket context: a report-wide DENOMINATOR filter —
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

/** Per-file scoping overrides for `lines.per_tier`/`asserted_per_tier`/
 * `asserted_only`, bundled into one object (rather than three positional
 * params, which would push `scopeFileStats`/`scopeFile`/`scopeTreeToTicket`
 * past the 5-param lint cap) — keyed by `FileNode.path`, matching
 * `ticketLines`/`ticketHits`. */
interface TicketFileScope {
  tiers?: Record<string, Record<string, number>>;
  asserted?: Record<string, Record<string, number>>;
  assertedOnly?: Record<string, number>;
}

/** One file's `Stats["lines"]` recomputed against the ticket's OWN line set:
 * `total`/`hit` become the LENGTH of `owned`/`hit` (never the file's
 * whole-repo counts) — the entire point of a denominator filter is that a
 * file where the ticket touched 12 of 400 coverable lines reports coverage
 * of those 12, not of the 400.
 *
 * `lines.per_tier` is scoped too when `tiers` is supplied — the emitter now
 * carries a per-file per-tier breakdown of the ticket's own lines
 * (`TicketChunk.files[].per_tier`), so a ticket-scoped row can show a real
 * number per tier rather than declining. Without it (a chunk from an older
 * report) the whole-file counts carry over and callers should keep treating
 * per-tier as "no data".
 *
 * `lines.asserted_per_tier`/`asserted_only` are scoped the SAME way, from
 * `TicketChunk.files[].asserted`/`asserted_only` (format 2) — without this a
 * scoped stats bag would otherwise carry the file's WHOLE-repo override
 * provenance (spread from `...stats.lines` below) against a ticket-scoped
 * denominator, which is a lie the moment the ticket owns a strict subset of
 * the file's asserted lines. `asserted`/`assertedOnly` default to the
 * whole-file values for the same "older report" fallback `tiers` gets.
 *
 * The remaining `Stats` fields (branches/flags/ctx_lines) still carry over
 * from the file's ORIGINAL whole-file stats verbatim: none of that data
 * exists in a per-ticket-scoped form — a known, documented limitation, so
 * callers rendering a Branch % for a ticket-scoped row should treat it as
 * "no data" (mirroring how run-focus rows already treat Branch %, for the
 * identical reason: v4 doesn't store that granularity per-run either). */
function scopeFileStats(
  stats: Stats,
  owned: number[],
  hit: number[],
  fileScope?: {
    tiers: Record<string, number> | undefined;
    asserted: Record<string, number> | undefined;
    assertedOnly: number | undefined;
  },
): Stats {
  return {
    ...stats,
    lines: {
      ...stats.lines,
      total: owned.length,
      hit: hit.length,
      per_tier: fileScope?.tiers ?? stats.lines.per_tier,
      asserted_per_tier: fileScope?.asserted ?? stats.lines.asserted_per_tier,
      asserted_only: fileScope?.assertedOnly ?? stats.lines.asserted_only,
    },
  };
}

function scopeFile(
  file: FileNode,
  ticketLines: Record<string, number[]>,
  ticketHits: Record<string, number[]>,
  scope?: TicketFileScope,
): FileNode | null {
  const owned = ticketLines[file.path];
  if (owned === undefined) return null; // the ticket never touched this file
  const hit = ticketHits[file.path] ?? [];
  return {
    ...file,
    stats: scopeFileStats(file.stats, owned, hit, {
      tiers: scope?.tiers?.[file.path],
      asserted: scope?.asserted?.[file.path],
      assertedOnly: scope?.assertedOnly?.[file.path],
    }),
  };
}

/** A directory's OWN rollup, recomputed as the sum of its (already-scoped)
 * children's `lines.total`/`lines.hit` — the same "denominator = the
 * ticket's lines only" contract `scopeFileStats` applies to one file,
 * aggregated one level up so a directory row (`DirectoryPage`'s tree, or its
 * top StatsCard for "this node and below") reports the scoped total too,
 * never the original whole-subtree one. `asserted_per_tier`/`asserted_only`
 * sum the same way — each child is already scoped (by `scopeFile` or a
 * nested `aggregateDirStats` call), so summing here never reintroduces
 * whole-repo counts. */
function aggregateDirStats(stats: Stats, dirs: DirNode[], files: FileNode[]): Stats {
  let total = 0;
  let hit = 0;
  let assertedOnly = 0;
  // Summed the same way as total/hit so a directory row's per-tier numbers
  // describe the scoped subtree, not the original whole one.
  const per_tier: Record<string, number> = {};
  const asserted_per_tier: Record<string, number> = {};
  for (const child of [...dirs, ...files]) {
    total += child.stats.lines.total;
    hit += child.stats.lines.hit;
    assertedOnly += child.stats.lines.asserted_only;
    for (const [tier, count] of Object.entries(child.stats.lines.per_tier)) {
      per_tier[tier] = (per_tier[tier] ?? 0) + count;
    }
    for (const [tier, count] of Object.entries(child.stats.lines.asserted_per_tier)) {
      asserted_per_tier[tier] = (asserted_per_tier[tier] ?? 0) + count;
    }
  }
  return {
    ...stats,
    lines: {
      ...stats.lines,
      total,
      hit,
      per_tier,
      asserted_per_tier,
      asserted_only: assertedOnly,
    },
  };
}

/** Pure recursive filter: keeps only the files/directories a
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
  scope?: TicketFileScope,
): DirNode | null {
  const files = node.files
    .map((f) => scopeFile(f, ticketLines, ticketHits, scope))
    .filter((f): f is FileNode => f !== null);
  const dirs = node.dirs
    .map((d) => scopeTreeToTicket(d, ticketLines, ticketHits, scope))
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
  tiers: Record<string, Record<string, number>>;
  asserted: Record<string, Record<string, number>>;
  assertedOnly: Record<string, number>;
} {
  const lines: Record<string, number[]> = {};
  const hits: Record<string, number[]> = {};
  // Real counts, unlike `lines`/`hits` above — the emitter breaks each
  // file's covered total down per tier, so these pass through as-is.
  const tiers: Record<string, Record<string, number>> = {};
  // Same: real per-file override-provenance counts (format 2), so the
  // ticket-scoped tree reports the SUBSET of asserted lines this ticket
  // actually owns in each file, not the file's whole-repo counts.
  const asserted: Record<string, Record<string, number>> = {};
  const assertedOnly: Record<string, number> = {};
  for (const file of chunk.files) {
    lines[file.path] = Array.from({ length: file.owned });
    hits[file.path] = Array.from({ length: file.covered });
    tiers[file.path] = file.per_tier ?? {};
    asserted[file.path] = file.asserted ?? {};
    assertedOnly[file.path] = file.asserted_only ?? 0;
  }
  return { lines, hits, tiers, asserted, assertedOnly };
}

/** `DirectoryPage`'s ticket-only StatsCard rows: one row per tier followed
 * by the ticket's own summary row. Branch/decision stay `null` — "no data"
 * — because neither is tracked per-ticket (same as `focusedTreeRow`'s
 * branch is `null` because it isn't tracked per-run).
 *
 * The per-tier rows exist because the emitter carries a per-file per-tier
 * breakdown of a ticket's lines (`TicketChunk.files[].per_tier`, summed up
 * the scoped tree by `aggregateDirStats`); before that the only honest
 * option was the summary row alone. The numerator is the ticket's lines
 * that THIS tier hit and the denominator is the ticket's line total, so the
 * rows share the summary's denominator and each answers "how much of this
 * ticket did this tier prove?".
 *
 * `node` must already be the SCOPED node (`scopeTreeToTicket`'s return
 * value) — this reads its `stats.lines` directly, verbatim, doing no
 * scoping itself. The summary row carries no `dotColor`: unlike a run-focus
 * `Context`, a ticket has no single tier of its own (design §6.1: a
 * ticket's lines can span every tier).
 *
 * Optional `ctx`: when a context is ALSO focused, `line` declines to `null`
 * ("no data", same treatment `branch`/`decision` already get) rather than
 * dividing `stats.ctx_lines[ctx.label]` (a whole-file numerator) by
 * `node.stats.lines.total` (the ticket-scoped denominator) — the two aren't
 * commensurable at tree granularity (no per-line ticket+run cross-tab
 * exists without loading every scoped file's own `FileChunk`), so the
 * honest answer is "we don't know", never a plausible-looking but
 * out-of-range percentage (a real fixture: 10 whole-file ctx hits over a
 * 3-line ticket scope reads "333.3%" if computed naively). Contrast
 * `ticketFileRow` below, whose OWN composed case recomputes exactly — it
 * already has one file's full per-line data, which this tree-level function
 * does not. */
export function ticketTreeRow(
  index: IndexPayload,
  node: DirNode,
  ticketId: string,
  ctx?: Context,
  hideAsserted = false,
): TierStatRow[] {
  const summary: TierStatRow = {
    key: "ticket",
    label: ctx ? `${ticketId} · ${ctx.label}` : ticketId,
    // `dotColor` is simply absent: a tree-level ticket row never carries a
    // tier dot. It was previously spelled `dotColor: undefined`, which under
    // `exactOptionalPropertyTypes` is a different thing from omitting it.
    //
    // `hideAsserted` (default `false` — byte-identical when
    // omitted): subtracts `asserted_only` (lines with no real, non-override
    // evidence at all) the same way `tierRows`'s "all tiers" row does — the
    // scoped `node.stats.lines` already carries the ticket's own subset of
    // that field (`scopeFileStats`/`aggregateDirStats` above), so no
    // separate ticket-scoped asserted math is needed here.
    line: ctx
      ? null
      : [
          node.stats.lines.hit - (hideAsserted ? node.stats.lines.asserted_only : 0),
          node.stats.lines.total,
        ],
    branch: null,
    decision: null,
  };
  // Composed with a run focus there is still nothing honest to say per
  // tier (see this function's doc comment), so the single declining row
  // remains the whole answer.
  if (ctx) return [summary];

  const tiers: TierStatRow[] = index.tier_order.map((tier) => ({
    key: tier,
    label: index.tier_labels[tier] ?? tier,
    dotColor: index.tier_colors[tier],
    // Numerator scoped to the ticket AND the tier (and, under
    // `hideAsserted`, minus that tier's own `asserted_per_tier` count);
    // denominator is the ticket's own line total, the same one the summary
    // row divides by.
    line: [
      (node.stats.lines.per_tier[tier] ?? 0) -
        (hideAsserted ? (node.stats.lines.asserted_per_tier[tier] ?? 0) : 0),
      node.stats.lines.total,
    ],
    branch: null,
    decision: null,
  }));
  return [...tiers, summary];
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
 * `chunkTierRows`'s "hit if any tier count > 0") is enough.
 *
 * `hideAsserted` (default `false` — byte-identical when omitted)
 * only narrows the NON-`ctx` "any tier hit" test — mirroring
 * `chunkTierRows`'s per-line recompute (a tier counts only if its hit isn't
 * override-sourced, per `LineJson.asserted`). The `ctx` branch needs no
 * such narrowing: `lineHasMemberHit` already reads `line.run` (a run
 * actually recording hits), which an override never populates — real
 * per-run evidence is never asserted-only by construction. */
export function ticketFileRow(
  index: IndexPayload,
  chunk: FileChunk,
  ticketId: string,
  ctx?: Context,
  hideAsserted = false,
): TierStatRow[] {
  const memberIds = ctx ? new Set(ctx.runs.map((r) => r.id)) : null;
  let owned = 0;
  let hit = 0;
  for (const line of Object.values(chunk.lines)) {
    if (!line.ticket?.includes(ticketId)) continue;
    owned++;
    const assertedTiers = hideAsserted ? Object.keys(line.asserted ?? {}) : [];
    const isHit = memberIds
      ? lineHasMemberHit(line, memberIds)
      : Object.entries(line.hits).some(([tier, n]) => n > 0 && !assertedTiers.includes(tier));
    if (isHit) hit++;
  }
  return [
    {
      key: "ticket",
      label: ctx ? `${ticketId} · ${ctx.label}` : ticketId,
      ...(ctx && { dotColor: index.tier_colors[ctx.tier] }),
      line: [hit, owned],
      branch: null,
      decision: null,
    },
  ];
}
