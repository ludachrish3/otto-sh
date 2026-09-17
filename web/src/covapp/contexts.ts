// Context grouping: the runs & contexts page treats every
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
import type { IndexPayload, RunJson, Stats } from "./types";

/** What the numerator is narrowed to: a context (run label), a product, or
 * both — the single value every page recomputes against, so a page never
 * has to compose the two independent pins (`?ctx=`, `?product=`) itself.
 * `Context` satisfies it (`ctxLabel` = `label`, `product` = `null`), which
 * is why a ctx-only scope can stay the `Context` object itself. */
export interface FocusScope {
  /** Display: "nightly", "app", or "nightly · app". */
  label: string;
  /** `""` when the scope spans tiers (a bare product) — a product is not a
   * tier-scoped thing, so callers must treat `""` as "no tier dot/column",
   * never look it up in `tier_colors`. */
  tier: string;
  /** Member runs — the file-page numerator (`lineHasMemberHit`). */
  runs: RunJson[];
  ctxLabel: string | null;
  product: string | null;
}

export interface Context {
  label: string;
  tier: string;
  runs: RunJson[];
  /** Always the context's own `label` — the `FocusScope` half of a
   * `Context`, so a ctx-only scope needs no conversion. */
  ctxLabel: string;
  /** Always `null`: a context is a run LABEL grouping, never a product
   * one (`resolveScope` builds the composed scope instead). */
  product: null;
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

/** Host pill text. A run captured under `cov/<host>/<product>/` names its
 * product too ("h1 · app"): with several products on one host the bare host
 * name is ambiguous, and the runs page's free-text filter searches this
 * string (`searchHaystack`). An unnamed unit run (`product: ""`) keeps the
 * plain host display it has always had. */
function hostDisplay(run: RunJson): string {
  const host = run.host || run.board || "—";
  return run.product ? `${host} · ${run.product}` : host;
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
      ctxLabel: label,
      product: null,
      hosts,
      lines,
      revoked,
      files: mergeFiles(fileLists),
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
 * `product: ""` (an unnamed unit run) matches no product pin — `""` is
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
