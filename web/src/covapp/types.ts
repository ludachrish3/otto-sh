// TypeScript mirrors of the data contract Task 1's spa_data.py emits (see
// docs/superpowers/plans/2026-07-25-coverage-spa.md, "The data contract").
// Field names are verbatim Python dict keys (snake_case), NOT camelCased —
// these types describe the wire payload exactly as JSON.parse hands it
// back, with no renaming layer in between. Tasks 3-7 import these names, so
// treat them as a frozen interface, not house style.

/** `IndexPayload["format"]` / `FileChunk["stamp"]`-adjacent format marker.
 * Mirrors `OTTO_COV_DATA_FORMAT` in spa_data.py — bump both together or
 * never (Global Constraints). */
export const EXPECTED_DATA_FORMAT = 1;

export interface Thresholds {
  high: number;
  medium: number;
}

/** One run row (`RunRecord.to_dict()` verbatim). */
export interface RunJson {
  id: number;
  tier: string;
  label: string;
  board: string;
  host: string;
  labs: string[];
  captured_at: string;
  tester: Record<string, string> | null;
  ticket: string | null;
  note: string | null;
  base_commit: string;
  dirty_remap: boolean;
  aging: boolean;
}

/** One run's contribution summary (`IndexPayload["run_contrib"]` values). */
export interface RunContrib {
  lines: number;
  revoked: number;
  /** `[display_path, count]` pairs, sorted desc by count. */
  files: [string, number][];
}

/** Total/hit counts for one stat type (line or branch), per-tier broken out. */
export interface StatBucket {
  total: number;
  hit: number;
  per_tier: Record<string, number>;
}

/** Rollup stats carried by every tree node (dir or file) — Global
 * Constraints' `tier_order` can be `[]` for a data-less store, so
 * `per_tier`/`ctx_lines` must be read as possibly-empty objects, never
 * assumed to contain a particular key. */
export interface Stats {
  lines: StatBucket;
  branches: StatBucket;
  flags: {
    stale: number;
    aging: number;
    excluded: number;
  };
  /** Hit-line counts per run label, within this node — powers the focus filter. */
  ctx_lines: Record<string, number>;
}

export interface FileNode {
  name: string;
  /** Display-relative path, e.g. "display/rel/path.c". */
  path: string;
  /** Mangled chunk id — key into `cov_data/files/<chunk>.js`. */
  chunk: string;
  stats: Stats;
}

export interface DirNode {
  name: string;
  dirs: DirNode[];
  files: FileNode[];
  stats: Stats;
}

/** `cov_data/index.js` payload shape (`window.__OTTO_COV__`). */
export interface IndexPayload {
  format: number;
  /** UTC time + `uuid4().hex[:8]`; carried verbatim onto every file chunk. */
  stamp: string;
  generated_at: string;
  otto_version: string;
  project_name: string;
  /** Precedence order, index 0 = highest. Can be `[]` for a data-less store —
   * no phantom "system" fallback; tolerate an empty list without crashing. */
  tier_order: string[];
  tier_labels: Record<string, string>;
  tier_colors: Record<string, string>;
  state_colors: Record<string, string>;
  thresholds: Thresholds;
  stat_types: string[];
  runs: RunJson[];
  run_contrib: Record<string, RunContrib>;
  /** Repo-wide coverable line count. */
  total_lines: number;
  /** Root dir node; `name` == `project_name`. */
  tree: DirNode;
  /** Per-ticket rollups (commit-message attribution). Missing-line detail
   * is deferred to each ticket's `chunk` (`cov_data/tickets/<chunk>.js`). */
  tickets: TicketSummary[];
  /** DEDUPED repo-truth behind the tickets page's aggregate StatsCard
   * (design §6.1: "the overall card is repo-truth; the rows sum to
   * more"). A line named by several tickets (§2 — the normal case, not an
   * edge case) counts ONCE here, unlike `tickets[].owned`/`covered`, which
   * deliberately attribute it to every ticket that names it — summing
   * `tickets[]` is therefore NOT a substitute for this field. */
  tickets_totals: TicketTotals;
}

/** One ticket's index-level rollup (`IndexPayload["tickets"]`). */
export interface TicketSummary {
  id: string;
  url: string | null;
  owned: number;
  covered: number;
  uncovered: number;
  per_tier: Record<string, number>;
  /** Chunk id — key into `cov_data/tickets/<chunk>.js`. */
  chunk: string;
}

/** `IndexPayload["tickets_totals"]` — see its doc comment for why this is
 * NOT the same as summing `TicketSummary[]`. */
export interface TicketTotals {
  owned: number;
  covered: number;
  uncovered: number;
  per_tier: Record<string, number>;
}

/** One ticket's deferred detail chunk. */
export interface TicketChunk {
  /** Same value as the index payload's `stamp` at emit time — a mismatch
   * means the report changed on disk since the index was loaded (design
   * §5: every data chunk carries the stamp). */
  stamp: string;
  id: string;
  files: {
    path: string;
    owned: number;
    covered: number;
    missing: [number, number][];
    /** Per-tier count of THIS ticket's lines in THIS file that the tier
     * hit — the breakdown `TicketSummary.per_tier` rolls up across files
     * and therefore cannot be split back apart. Lets a ticket-scoped
     * subtree render real tier rows instead of one aggregate row. */
    per_tier: Record<string, number>;
  }[];
}

export interface BranchJson {
  block: number;
  branch: number;
  hits: Record<string, number>;
  reachable: Record<string, boolean>;
}

export interface LineJson {
  hits: Record<string, number>;
  branches: BranchJson[];
  state: "stale" | "aging" | null;
  /** Present only when at least one run recorded hits on this line. */
  run?: Record<string, number>;
  /** Present only when at least one run's evidence for this line was revoked. */
  stale_run?: number[];
  /** Ticket ids owning this line (commit-message attribution); omitted
   * when the line has none. */
  ticket?: string[];
}

/** `cov_data/files/<chunk>.js` payload shape (`window.__OTTO_COV_FILE__` argument). */
export interface FileChunk {
  /** Same value as the index payload's `stamp` at emit time — a mismatch
   * means the report changed on disk since the index was loaded. */
  stamp: string;
  chunk: string;
  path: string;
  /** Full source text, read with `errors="replace"`. */
  source: string;
  /** Keyed by line number (as a string) — may contain linenos past EOF. */
  lines: Record<string, LineJson>;
  /** Sorted line numbers excluded via a source marker (e.g. LCOV_EXCL_LINE). */
  excluded: number[];
}

// The classic-script globals the emitted JS assigns/calls (index, per-file
// chunks, per-ticket chunks). Declared here (not in data.ts) so every
// consumer of these wire types picks up the same ambient `Window`
// augmentation without a second import.
declare global {
  interface Window {
    __OTTO_COV__?: IndexPayload;
    __OTTO_COV_FILE__?: (chunk: FileChunk) => void;
    __OTTO_COV_TICKET__?: (id: string, chunk: TicketChunk) => void;
  }
}
