// Minimal shared formatting + page-glue helpers (Task 3 brief; `crumbsFor`/
// `tierRows` moved here in Task 4 so both App.tsx's remaining FilePlaceholder
// and DirectoryPage.tsx share one implementation instead of two copies).
// `meta` stays a plain `ReactNode` prop on AppShell rather than growing a
// `metaLine()` builder — there is nothing today that composing a handful of
// `<b>{n}</b> label` spans inline in each page's glue code doesn't already
// cover cleanly.
import type { Crumb } from "../ui/Breadcrumbs";
import type { TierStatRow } from "./chrome/StatsCard";
import type { Context } from "./contexts";
import type { FileChunk, IndexPayload, LineJson, Stats } from "./types";

/** Plain digit string — no thousands separators, no rounding. Counts here
 * (line/file/run totals) are page-glue text, not locale-formatted UI
 * copy. */
export function fmtCount(n: number): string {
  return String(n);
}

/** Home crumb (project name -> `#/coverage`) plus one crumb per path
 * segment, each linking to its own prefix (`#/coverage/a`, `#/coverage/a/b`,
 * …). `Breadcrumbs` (ui/Breadcrumbs.tsx) always renders the LAST item as
 * the current, non-link crumb regardless of whether it carries an `href` —
 * so giving every segment an href here is harmless, not a bug.
 *
 * Each segment is `encodeURIComponent`-ed into the href (the `label` stays
 * raw, for display) — the symmetric write-side half of App.tsx's
 * `segmentsFromWildcard`, which `decodeURIComponent`s each segment on read.
 * Without this, a legal file/dir name containing `%`/`#`/other reserved
 * characters round-trips into a `URIError` at render time (no error
 * boundary catches it — the whole app white-screens). */
export function crumbsFor(projectName: string, segments: string[]): Crumb[] {
  const crumbs: Crumb[] = [{ label: projectName, href: "#/coverage" }];
  let acc = "";
  for (const segment of segments) {
    acc += `/${encodeURIComponent(segment)}`;
    crumbs.push({ label: segment, href: `#/coverage${acc}` });
  }
  return crumbs;
}

/** `encodeURIComponent`-encodes every "/"-separated piece of a raw display
 * path, then rejoins with a literal "/" — the write-side half of App.tsx's
 * `segmentsFromWildcard` (which splits a wildcard capture on raw "/" and
 * `decodeURIComponent`s each piece). A dir/file's raw display path is never
 * pre-encoded, so a name containing `%`, `#`, or other reserved characters
 * would otherwise throw `URIError` on the next render (no error boundary
 * catches it — the whole app white-screens) once the read side tries to
 * decode it. Encoding the whole path in one shot instead of per-segment
 * would also escape the "/" separators themselves, collapsing a
 * multi-segment path into one unrecognizable segment — hence splitting
 * first. Moved here from DirectoryPage.tsx (Task 6) so RunsPage.tsx's
 * top-files links share the same implementation instead of a second private
 * copy — DirectoryPage.tsx now imports this instead of declaring its own. */
export function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

/** Every tier row's decision cell is `null` ("no data"): `Stats` (types.ts)
 * carries no per-tier decision bucket — decision counts are a file-view
 * concept (paired branch blocks), computed from a loaded `FileChunk` in
 * Tasks 5-6, not rolled up onto tree nodes. Always appends the "all tiers"
 * summary row (key "all") — StatsCard's implicit contract: that row is
 * caller-supplied, not synthesized inside StatsCard itself. */
export function tierRows(index: IndexPayload, stats: Stats): TierStatRow[] {
  const rows: TierStatRow[] = index.tier_order.map((tier) => ({
    key: tier,
    label: index.tier_labels[tier] ?? tier,
    dotColor: index.tier_colors[tier],
    line: [stats.lines.per_tier[tier] ?? 0, stats.lines.total],
    branch: [stats.branches.per_tier[tier] ?? 0, stats.branches.total],
    decision: null,
  }));
  rows.push({
    key: "all",
    label: "All tiers",
    line: [stats.lines.hit, stats.lines.total],
    branch: [stats.branches.hit, stats.branches.total],
    decision: null,
  });
  return rows;
}

/** StatsCard's focused-context variant (Task 7 spec §4): a single
 * `{key: "ctx"}` row in place of `tierRows`'s per-tier matrix — line =
 * `stats.ctx_lines[ctx.label]` (hit lines credited to this context, within
 * whatever tree node `stats` came from) over that node's line total;
 * branch/decision both `null` ("no data" — v4 doesn't store per-run branch
 * contribution, Global Constraints' documented data limitation). Used by
 * DirectoryPage.tsx and RunsPage.tsx (both read a tree `Stats`); FilePage.
 * tsx uses `focusedFileRow` instead (its coverable-line total/hit count
 * comes from the loaded `FileChunk`, not a tree node). */
export function focusedTreeRow(index: IndexPayload, stats: Stats, ctx: Context): TierStatRow[] {
  return [
    {
      key: "ctx",
      label: ctx.label,
      dotColor: index.tier_colors[ctx.tier],
      line: [stats.ctx_lines[ctx.label] ?? 0, stats.lines.total],
      branch: null,
      decision: null,
    },
  ];
}

/** Shared by `focusedFileRow` and `FilePage.tsx`'s own row tinting: a line
 * is "covered" under a focused context iff any of its member run ids
 * recorded hits > 0 on it — `line.run` (present only when at least one run
 * hit the line, per types.ts) is the only per-run source v4 carries. */
export function lineHasMemberHit(line: LineJson | undefined, memberRunIds: Set<number>): boolean {
  if (!line?.run) return false;
  for (const id of memberRunIds) {
    if ((line.run[String(id)] ?? 0) > 0) return true;
  }
  return false;
}

/** `focusedTreeRow`'s file-page counterpart: line hit/total computed
 * directly from the loaded `FileChunk` rather than a tree `Stats` — a line
 * counts as "hit" for this context iff `lineHasMemberHit` (the same
 * per-run membership test `FilePage.tsx`'s row tinting uses), over the
 * same `lineTotal` `chunkTierRows` uses (every key in `chunk.lines`,
 * including past-EOF ones — Task 1's emitter pins those still count). */
export function focusedFileRow(index: IndexPayload, chunk: FileChunk, ctx: Context): TierStatRow[] {
  const memberIds = new Set(ctx.runs.map((r) => r.id));
  const lineTotal = Object.keys(chunk.lines).length;
  let hit = 0;
  for (const line of Object.values(chunk.lines)) {
    if (lineHasMemberHit(line, memberIds)) hit++;
  }
  return [
    {
      key: "ctx",
      label: ctx.label,
      dotColor: index.tier_colors[ctx.tier],
      line: [hit, lineTotal],
      branch: null,
      decision: null,
    },
  ];
}

/** File-page counterpart to `tierRows` (Task 5 brief): the tree's rolled-up
 * `Stats` has no per-file granularity to read a StatsCard from, so this
 * computes the same Line/Branch/"All tiers" matrix directly from a loaded
 * `FileChunk` instead — hit-vs-total over every key in `chunk.lines`
 * (INCLUDING out-of-range keys past the source's actual EOF; Task 1's
 * emitter pins that those still count, and this mirrors it rather than
 * filtering them out), decision always `null` (branch-pair "decision"
 * coverage isn't derived here, same as `tierRows`). A line counts as "hit"
 * if ANY tier recorded a hit on it; a branch counts as "hit" the same way
 * (mirrors `spa_data.py`'s "branch hit = any tier count > 0"). Appends the
 * {key:"all", label:"All tiers"} row itself — StatsCard's implicit
 * contract (see `tierRows`) that row is always caller-supplied. */
export function chunkTierRows(index: IndexPayload, chunk: FileChunk): TierStatRow[] {
  const lineTotal = Object.keys(chunk.lines).length;
  let lineHitAll = 0;
  const lineHitByTier: Record<string, number> = {};
  let branchTotal = 0;
  let branchHitAll = 0;
  const branchHitByTier: Record<string, number> = {};

  for (const line of Object.values(chunk.lines)) {
    const hitValues = Object.values(line.hits);
    if (hitValues.some((n) => n > 0)) lineHitAll++;
    for (const tier of index.tier_order) {
      if ((line.hits[tier] ?? 0) > 0) lineHitByTier[tier] = (lineHitByTier[tier] ?? 0) + 1;
    }
    for (const branch of line.branches) {
      branchTotal++;
      const branchHitValues = Object.values(branch.hits);
      if (branchHitValues.some((n) => n > 0)) branchHitAll++;
      for (const tier of index.tier_order) {
        if ((branch.hits[tier] ?? 0) > 0) {
          branchHitByTier[tier] = (branchHitByTier[tier] ?? 0) + 1;
        }
      }
    }
  }

  const rows: TierStatRow[] = index.tier_order.map((tier) => ({
    key: tier,
    label: index.tier_labels[tier] ?? tier,
    dotColor: index.tier_colors[tier],
    line: [lineHitByTier[tier] ?? 0, lineTotal],
    branch: [branchHitByTier[tier] ?? 0, branchTotal],
    decision: null,
  }));
  rows.push({
    key: "all",
    label: "All tiers",
    line: [lineHitAll, lineTotal],
    branch: [branchHitAll, branchTotal],
    decision: null,
  });
  return rows;
}
