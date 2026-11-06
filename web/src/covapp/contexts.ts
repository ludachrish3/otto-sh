// Context grouping: the runs & contexts page treats every
// `RunJson` sharing a `label` as ONE context — the common case is a
// multi-host run (same otto test invocation, several hosts/DUTs), but a
// unit harvest or a one-off manual capture is just a context with a single
// member. Pure module, no React — `groupContexts`/`searchHaystack` are
// consumed directly by contexts.test.ts and by RunsPage.tsx's render/filter
// logic.
//
// A label is one product's identity (store v9, `<host>/<product>`), not one
// tier's: the same product captured in `manual` and in `bench` is ONE
// context with captures in both tiers. So a context carries every tier it
// has a capture in (`tiers`), and its per-capture metadata (board/labs/
// captured_at/tester/ticket/note/base_commit) stays on each member run —
// callers list `ctx.runs`, never read one run as the context's.
//
// Line counts are never computed here. A context's total and per-file
// counts are the tree's `Stats.ctx_lines` rollup (spa_data.py's
// `_file_stats`, the one place that decides "lines this label hit"), which
// counts a line once however many of the label's captures hit it. Summing
// per-run `run_contrib` counts would credit such a line once per capture.
import type { DirNode, IndexPayload, RunJson, Stats } from "./types";

/** What the numerator is narrowed to: a context (run label), a product, or
 * both — the single value every page recomputes against, so a page never
 * has to compose the two independent pins (`?ctx=`, `?product=`) itself.
 * `Context` satisfies it (`ctxLabel` = `label`, `product` = `null`), which
 * is why a ctx-only scope can stay the `Context` object itself. */
export interface FocusScope {
  /** Display: "nightly", "app", or "nightly · app". */
  label: string;
  /** `""` when the scope spans tiers (a bare product, or a context with
   * captures in several tiers) — callers must treat `""` as "no tier
   * dot/column", never look it up in `tier_colors`. */
  tier: string;
  /** Member runs — the file-page numerator (`lineHasMemberHit`). */
  runs: RunJson[];
  ctxLabel: string | null;
  product: string | null;
}

export interface Context {
  label: string;
  /** Every tier this context has a capture in, in `tier_order` precedence
   * (a tier missing from `tier_order` follows, in first-appearance order). */
  tiers: string[];
  /** The one tier when `tiers` has exactly one, else `""` — the
   * `FocusScope` "spans tiers" value. */
  tier: string;
  runs: RunJson[];
  /** Always the context's own `label` — the `FocusScope` half of a
   * `Context`, so a ctx-only scope needs no conversion. */
  ctxLabel: string;
  /** Always `null`: a context is a run LABEL grouping, never a product
   * one (`resolveScope` builds the composed scope instead). */
  product: null;
  /** `[hostDisplay, lines]` — one entry PER MEMBER RUN, each that run's own
   * `run_contrib` count, even when two runs report the same host display
   * (e.g. the same product captured in two tiers). Never deduplicated. */
  hosts: [string, number][];
  /** Distinct lines any member hit: the tree root's `ctx_lines[label]`. */
  lines: number;
  /** Revoked CREDITS, summed over member runs (a credit is one run's hit on
   * one line, so two runs' revoked credits on one line are two). */
  revoked: number;
  /** `[displayPath, count]` from each file node's `ctx_lines[label]`,
   * sorted desc by count then asc by path (ties broken deterministically). */
  files: [string, number][];
  status: "ok" | "aging" | "stale";
  remapped: boolean;
}

/** Host pill text. A run captured under `cov/<host>/<product>/` names its
 * product too ("h1 · app"): with several products on one host the bare host
 * name is ambiguous, and the runs page's free-text filter searches this
 * string (`searchHaystack`). A `--tier` tracefile run (`product: ""`) keeps the
 * plain host display it has always had. */
function hostDisplay(run: RunJson): string {
  const host = run.host || run.board || "—";
  return run.product ? `${host} · ${run.product}` : host;
}

/** Each label's `[displayPath, count]` list, read off every file node's
 * `stats.ctx_lines` in one walk of the tree. Desc by count; ties broken asc
 * by path so output is deterministic. */
function filesByLabel(tree: DirNode): Map<string, [string, number][]> {
  const out = new Map<string, [string, number][]>();
  const walk = (node: DirNode) => {
    for (const child of node.dirs) walk(child);
    for (const file of node.files) {
      for (const [label, count] of Object.entries(file.stats.ctx_lines)) {
        if (count <= 0) continue;
        const list = out.get(label) ?? [];
        list.push([file.path, count]);
        out.set(label, list);
      }
    }
  };
  walk(tree);
  for (const list of out.values()) {
    list.sort(([pathA, countA], [pathB, countB]) => {
      if (countB !== countA) return countB - countA;
      return pathA < pathB ? -1 : pathA > pathB ? 1 : 0;
    });
  }
  return out;
}

/** The distinct tiers of `runs`, in `tierOrder` precedence; a tier not in
 * `tierOrder` follows in first-appearance order. */
function tiersOf(runs: RunJson[], tierOrder: string[]): string[] {
  const seen = new Set(runs.map((run) => run.tier));
  return [
    ...tierOrder.filter((tier) => seen.has(tier)),
    ...[...seen].filter((tier) => !tierOrder.includes(tier)),
  ];
}

/** Groups `payload.runs` by `label`, insertion order of first appearance
 * (both for the returned `Context[]` order and each context's own `runs`
 * member order). Line totals and per-file counts come from the tree's
 * `ctx_lines` rollup; the per-host rows and revoked credits from
 * `payload.run_contrib[String(run.id)]`, defensively defaulted (`?? 0`) — a
 * run with no matching entry (the data contract doesn't guarantee one)
 * contributes zero rather than throwing. */
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
  const files = filesByLabel(payload.tree);

  return order.map((label) => {
    const runs = members.get(label) ?? [];
    const hosts: [string, number][] = [];
    let revoked = 0;
    let anyAging = false;
    let anyRemapped = false;

    for (const run of runs) {
      const contrib = payload.run_contrib[String(run.id)];
      hosts.push([hostDisplay(run), contrib?.lines ?? 0]);
      revoked += contrib?.revoked ?? 0;
      if (run.aging) anyAging = true;
      if (run.dirty_remap) anyRemapped = true;
    }
    const lines = payload.tree.stats.ctx_lines[label] ?? 0;
    const tiers = tiersOf(runs, payload.tier_order);

    const status: Context["status"] =
      lines === 0 && revoked > 0 ? "stale" : anyAging ? "aging" : "ok";

    return {
      label,
      tiers,
      tier: tiers.length === 1 ? (tiers[0] ?? "") : "",
      runs,
      ctxLabel: label,
      product: null,
      hosts,
      lines,
      revoked,
      files: files.get(label) ?? [],
      status,
      remapped: anyRemapped,
    };
  });
}

/** Resolves the two independent pins into the ONE scope every page's
 * numerator narrows to (per-product spec §10). Four cases:
 *   - neither pinned -> `undefined` (no narrowing at all);
 *   - ctx only -> that `Context` itself (a `Context` IS a `FocusScope`);
 *   - product only -> every run of that product, across contexts AND tiers
 *     (hence `tier: ""`);
 *   - both -> the INTERSECTION, i.e. the context's own runs of that product.
 * `undefined` for an unresolvable focus label (same defensive re-resolution
 * every page already did inline) and for an intersection that is empty — a
 * scope with no member runs would credit nothing anywhere, so callers treat
 * it as "no scope" rather than "everything at zero".
 *
 * A product NOT in `index.products` is ignored (falls back to the ctx
 * scope): `FocusProvider`/`resolveProduct` already validates `?product=`
 * against that same list, so this is the defensive half. A run with
 * `product: ""` (a `--tier` tracefile run) matches no product pin — `""` is
 * never a member of `index.products`, which carries non-empty names only.
 *
 * Optional `contexts`: a caller that already holds `groupContexts(index)`
 * (RunsPage renders off that same list) passes it so the grouping runs ONCE
 * per render instead of twice. It must be `groupContexts` of this same
 * `index` — it is used only to look the focus label up. Omitted, the
 * grouping is computed here, and only when a focus is actually pinned. */
export function resolveScope(
  index: IndexPayload,
  focus: string | null,
  product: string | null,
  contexts?: Context[],
): FocusScope | undefined {
  const ctx =
    focus === null ? undefined : (contexts ?? groupContexts(index)).find((c) => c.label === focus);
  if (focus !== null && ctx === undefined) return undefined;
  if (product === null) return ctx;
  if (!index.products.includes(product)) return ctx;
  const pool = ctx ? ctx.runs : index.runs;
  const runs = pool.filter((r) => r.product === product);
  if (runs.length === 0) return undefined;
  return {
    label: ctx ? `${ctx.label} · ${product}` : product,
    tier: ctx ? ctx.tier : "",
    runs,
    ctxLabel: ctx ? ctx.label : null,
    product,
  };
}

/** Hit lines credited to `scope` within whatever tree node `stats` came
 * from — the tree-level numerator behind every scoped row/cell. Reads the
 * one rollup map that matches the scope's shape: the (ctx, product) pair
 * count when both are pinned, `product_lines` for a bare product,
 * `ctx_lines` otherwise. Never multiplies or intersects two maps: the
 * emitter already carries the pair breakdown precisely because the
 * intersection cannot be derived from the two one-dimensional maps. */
export function scopeTreeLines(stats: Stats, scope: FocusScope): number {
  if (scope.ctxLabel !== null && scope.product !== null) {
    return stats.ctx_product_lines[scope.ctxLabel]?.[scope.product] ?? 0;
  }
  if (scope.product !== null) return stats.product_lines[scope.product] ?? 0;
  return stats.ctx_lines[scope.label] ?? 0;
}

/** Lowercase, space-joined haystack for the free-text search filter — label,
 * every member's host display (which now carries the product), every
 * non-null ticket, every board, every non-empty product.
 * Substring-matched by the caller (`haystack.includes(query.toLowerCase())`),
 * so this only needs to concatenate, not tokenize. */
export function searchHaystack(ctx: Context): string {
  const parts: string[] = [ctx.label];
  for (const [host] of ctx.hosts) parts.push(host);
  for (const run of ctx.runs) {
    if (run.ticket) parts.push(run.ticket);
    parts.push(run.board);
    if (run.product) parts.push(run.product);
  }
  return parts.join(" ").toLowerCase();
}
