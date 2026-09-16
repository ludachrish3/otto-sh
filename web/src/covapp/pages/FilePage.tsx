// The covapp file page. DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/file-page.html —
// recreated with React + Tailwind semantic tokens + ui/CodeView, not the
// mockup's literal CSS. Loads its own FileChunk (`loadFileChunk`, data.ts)
// on mount — the tree's rolled-up `Stats` (what DirectoryPage reads) has no
// per-line granularity, only the chunk does.
import { ChevronDown, File02 } from "@untitledui/icons";
import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { type CodeLine, CodeView, type GutterCol } from "@/ui/CodeView";
import { cx } from "@/utils/cx";

import { AppShell } from "../chrome/AppShell";
import { groupContexts } from "../contexts";
import { loadFileChunk, StampMismatchError } from "../data";
import { parseHashQuery, useFocus } from "../focus";
import {
  chunkTierRows,
  crumbsFor,
  focusedFileRow,
  keyColumnLabel,
  lineHasMemberHit,
  withHideAssertedSuffix,
} from "../format";
import { hashQueryOf, useHash } from "../hashState";
import { highlightLines, langForPath } from "../highlight";
import { clearMatches, paintMatches } from "../matchHighlight";
import { compileQuery, matchesInLines, statesFromChunk } from "../search";
import { ticketFileRow } from "../tickets";
import type { BranchJson, FileChunk, FileNode, IndexPayload, LineJson } from "../types";
import { GuardScreen } from "./GuardScreen";

/** Row precedence (Global Constraints, verbatim): excluded beats the
 * highest-precedence tier with a hit (`tierOrder[0]` first) beats aging
 * beats stale beats uncovered; a line with no `LineJson` at all is
 * "uncoverable" — muted, never red, never any of the other states.
 * Exported standalone (not folded into a component) so this precedence can
 * be unit-tested directly against a precedence table, independent of
 * rendering. */
export function rowClassFor(
  line: LineJson | undefined,
  excluded: boolean,
  tierOrder: string[],
): string {
  if (excluded) return "s-excl";
  if (!line) return "";
  for (const tier of tierOrder) {
    if ((line.hits[tier] ?? 0) > 0) return `t-${tier}`;
  }
  if (line.state === "aging") return "s-aging";
  if (line.state === "stale") return "s-stale";
  return "s-unc";
}

/** `rowClassFor`'s under-focus counterpart (spec §4): excluded still
 * wins; otherwise a line tints by the FOCUSED CONTEXT's tier iff any of
 * its member run ids recorded a hit (`lineHasMemberHit`, shared with
 * `focusedFileRow` in format.ts) — no aging/stale distinction here (those
 * are report-wide staleness flags, not per-context; the spec collapses
 * everything that isn't a member-run hit into plain "uncovered/neutral").
 * `""` (uncoverable) only when there's no `LineJson` at all, same as
 * `rowClassFor`. */
export function rowClassForFocus(
  line: LineJson | undefined,
  excluded: boolean,
  memberRunIds: Set<number>,
  tier: string,
): string {
  if (excluded) return "s-excl";
  if (!line) return "";
  return lineHasMemberHit(line, memberRunIds) ? `t-${tier}` : "s-unc";
}

/** Inline tint/accent for a `t-<tier>` row — tier names are data-driven and
 * unbounded, so there's no fixed `t-<tier>` CSS rule to predeclare the way
 * `covapp.css` does for the 4 fixed `s-*` states (see `CodeLine.style`'s
 * doc comment in ui/CodeView.tsx). `null` for every other rowClass — those
 * rely on `covapp.css` rules reading the `--state-*` custom properties
 * `FilePage` sets once on the code-card container below. */
function tierStyleFor(rowClass: string, index: IndexPayload): CSSProperties | undefined {
  if (!rowClass.startsWith("t-")) return;
  const color = index.tier_colors[rowClass.slice(2)] ?? "currentColor";
  return {
    backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
    borderLeftColor: color,
  };
}

/** The file page's own divergence from DirectoryPage's tree (spec §6.3,
 * verbatim): a pinned ticket DIMS lines it doesn't own here, rather
 * than hiding them — you cannot read code with lines removed from the
 * middle of it. Applied via `style.opacity`, layered on top of
 * `tierStyleFor`'s own inline style (never via `rowClass` itself — that
 * string is also parsed by `tierStyleFor` above via `.slice(2)` to look up
 * a tier color, so appending anything to it would corrupt that lookup).
 * Dims regardless of coverage state (hit/uncovered/excluded/blank) —
 * "owned by the ticket" is orthogonal to a line's coverage state. */
function dimStyleFor(
  rowClass: string,
  index: IndexPayload,
  dimmed: boolean,
): CSSProperties | undefined {
  const tierStyle = tierStyleFor(rowClass, index);
  if (!dimmed) return tierStyle;
  return { ...tierStyle, opacity: 0.4 };
}

/** Whether `line` is owned by the pinned `ticketId` — `undefined`/no
 * `ticket` field at all (a line with no `[coverage.tickets]` attribution)
 * reads as "not owned", same as an explicit list that doesn't include this
 * id. */
function ownedByTicket(line: LineJson | undefined, ticketId: string): boolean {
  return line?.ticket?.includes(ticketId) ?? false;
}

/** `rowClassFor`'s hide-asserted input adjustment — deliberately NOT a
 * change inside `rowClassFor` itself (its own doc comment: exported
 * standalone so its precedence table stays unit-testable independent of
 * this page's rendering). A tier whose sole hits are override-sourced
 * (`lineAssertedIn`) gets its `hits[tier]` zeroed before `rowClassFor` ever
 * sees it, so a line with ONLY asserted evidence in every tier falls
 * through to `rowClassFor`'s uncovered/stale/aging branches instead of
 * tinting by that tier — the row-tint half of "hide asserted coverage",
 * mirroring `buildCells`'s cell-level zeroing above. A no-op (`line`
 * unchanged) when `hideAsserted` is `false`. */
function lineForRowClass(line: LineJson | undefined, hideAsserted: boolean): LineJson | undefined {
  if (!hideAsserted || !line?.asserted) return line;
  const hits = { ...line.hits };
  for (const tier of Object.keys(line.asserted)) {
    if ((line.asserted[tier]?.length ?? 0) > 0) hits[tier] = 0;
  }
  return { ...line, hits };
}

function collectRunIds(line: LineJson | undefined): Set<number> {
  if (!line) return new Set();
  const fromRun = line.run ? Object.keys(line.run).map(Number) : [];
  const fromStale = line.stale_run ?? [];
  return new Set([...fromRun, ...fromStale]);
}

/** Whether `tier`'s hits on this line are override-sourced (spec §3: the
 * marker exists only while the tier's SOLE hits come from an override —
 * the emitter enforces that, so presence of the key is the whole test). */
export function lineAssertedIn(line: LineJson | undefined, tier: string): boolean {
  return (line?.asserted?.[tier]?.length ?? 0) > 0;
}

/** `color?: string | undefined` explicitly — see TierStatRow.dotColor. The
 * `?? "currentColor"` below is this component already saying it accepts one. */
function TierHeaderDot({ color, label }: { color?: string | undefined; label: string }) {
  return (
    <span title={`${label} hits`} className="flex items-center justify-center">
      <span
        aria-hidden
        className="inline-block size-2 rounded-sm"
        style={{ backgroundColor: color ?? "currentColor" }}
      />
    </span>
  );
}

function buildColumns(index: IndexPayload): GutterCol[] {
  return [
    { id: "num", width: "46px", header: "#" },
    ...index.tier_order.map(
      (tier): GutterCol => ({
        id: `tier:${tier}`,
        width: "40px",
        header: (
          <TierHeaderDot color={index.tier_colors[tier]} label={index.tier_labels[tier] ?? tier} />
        ),
      }),
    ),
    { id: "branches", width: "96px", header: "Branches" },
  ];
}

function HitCell({ value, asserted = false }: { value: number | null; asserted?: boolean }) {
  if (value === null || value === 0) {
    return (
      <span aria-hidden className="text-quaternary opacity-45 tabular-nums">
        ·
      </span>
    );
  }
  if (asserted) {
    return (
      <span
        data-testid="hit-asserted"
        title="asserted by a manual-testing override — not proven by a recorded run"
        className="inline-flex items-center justify-center rounded-full border border-dashed
          border-current px-1 text-tertiary tabular-nums opacity-80"
      >
        {value}
      </span>
    );
  }
  return <span className="text-tertiary tabular-nums">{value}</span>;
}

/** Every tier `reachable` false -> unreachable (struck, muted); any tier hit
 * > 0 -> taken (green); otherwise not-taken (red). Reads whatever tier keys
 * `branch.reachable`/`branch.hits` actually carry rather than assuming
 * `index.tier_order` — a branch's own recorded tiers are the ground truth
 * for "was this ever reachable/hit", independent of which tiers the report
 * happens to display columns for. */
function branchState(branch: BranchJson): "taken" | "not-taken" | "unreachable" {
  const reachableValues = Object.values(branch.reachable);
  const unreachable = reachableValues.length > 0 && reachableValues.every((v) => v === false);
  if (unreachable) return "unreachable";
  const taken = Object.values(branch.hits).some((h) => h > 0);
  return taken ? "taken" : "not-taken";
}

function branchTitle(branch: BranchJson, idx: number, index: IndexPayload): string {
  const state = branchState(branch);
  const summary = index.tier_order
    .map((tier) => `${index.tier_labels[tier] ?? tier}×${branch.hits[tier] ?? 0}`)
    .join(", ");
  return `block ${branch.block} · B${idx} — ${state}${summary ? ` — ${summary}` : ""}`;
}

const BRANCH_PILL_CLASS: Record<ReturnType<typeof branchState>, string> = {
  taken: "text-success-primary bg-success-secondary",
  "not-taken": "text-error-primary bg-error-secondary",
  unreachable: "text-quaternary bg-secondary line-through",
};

function BranchPill({
  branch,
  idx,
  index,
}: {
  branch: BranchJson;
  idx: number;
  index: IndexPayload;
}) {
  const state = branchState(branch);
  return (
    <span
      data-testid="branch-pill"
      title={branchTitle(branch, idx, index)}
      className={cx(
        "rounded px-1 font-mono text-[10px] font-semibold whitespace-nowrap",
        BRANCH_PILL_CLASS[state],
      )}
    >
      B{idx}
    </span>
  );
}

/** Shared by `buildCells`/`buildCellsFocused` — branch pills render
 * UNCHANGED under focus (spec §4: branch data isn't per-run, v4's
 * `run_hits` is line-only, so there's nothing context-specific to show
 * here either way). */
function branchesCell(line: LineJson | undefined, index: IndexPayload): ReactNode {
  return (
    <span key="branches" className="flex flex-wrap items-center justify-center gap-0.5">
      {(line?.branches ?? []).map((branch, i) => (
        // A line's branches are a fixed, order-stable array from the report
        // data, not a reorderable/filterable list — index IS the branch's
        // identity within this line (it's what "B<n>" itself means).
        // biome-ignore lint/suspicious/noArrayIndexKey: see above
        <BranchPill key={i} branch={branch} idx={i} index={index} />
      ))}
    </span>
  );
}

/** `hideAsserted` (default `false` — byte-identical when omitted):
 * a tier column whose SOLE hits are override-sourced (`lineAssertedIn`)
 * renders as a plain zero — `HitCell`'s muted "·" glyph, same as a real
 * uncovered cell — instead of the dashed asserted pill, so hiding asserted
 * coverage genuinely hides it from the grid, not just from its own dashed
 * styling. A tier with BOTH real and override evidence can't occur
 * (`LineJson.asserted`'s doc comment: the marker exists only while a
 * tier's hits are ENTIRELY override-sourced), so zeroing is never lossy for
 * a tier that also has real hits. */
function buildCells(
  lineNo: number,
  line: LineJson | undefined,
  index: IndexPayload,
  hideAsserted = false,
): ReactNode[] {
  const cells: ReactNode[] = [
    <span key="num" className="text-quaternary tabular-nums">
      {lineNo}
    </span>,
  ];
  for (const tier of index.tier_order) {
    const asserted = lineAssertedIn(line, tier);
    const hidden = hideAsserted && asserted;
    cells.push(
      <HitCell
        key={tier}
        value={hidden ? 0 : line ? (line.hits[tier] ?? 0) : null}
        asserted={asserted && !hideAsserted}
      />,
    );
  }
  cells.push(branchesCell(line, index));
  return cells;
}

/** `buildCells`'s under-focus counterpart: the focused context's OWN tier
 * column shows the summed hits of its member runs on this line (`null`
 * when there's no `LineJson` at all, rendering `HitCell`'s muted "·" same
 * as a real zero); every OTHER tier column reads `null` too — a context
 * belongs to one tier, so those columns have no context-scoped number to
 * show (spec-pinned "other tiers ·", distinct from `DirectoryPage`'s tree
 * columns, which show a real 0.0% instead — files use HitCell's existing
 * "no data" glyph, trees use a percentage). Branch pills unchanged (see
 * `branchesCell`). */
function buildCellsFocused(
  lineNo: number,
  line: LineJson | undefined,
  index: IndexPayload,
  memberRunIds: Set<number>,
  focusedTier: string,
): ReactNode[] {
  const cells: ReactNode[] = [
    <span key="num" className="text-quaternary tabular-nums">
      {lineNo}
    </span>,
  ];
  let runHitSum: number | null = null;
  if (line) {
    runHitSum = 0;
    for (const id of memberRunIds) runHitSum += line.run?.[String(id)] ?? 0;
  }
  for (const tier of index.tier_order) {
    cells.push(<HitCell key={tier} value={tier === focusedTier ? runHitSum : null} />);
  }
  cells.push(branchesCell(line, index));
  return cells;
}

/** Leading gutter cell for one row's ticket chip(s) (design §6.2) —
 * `undefined` when this line carries none, which `CodeView`
 * renders as its historical bare placeholder (see `CodeLine.ticketGutter`'s
 * doc comment there), so a report with no `[coverage.tickets]` attribution
 * anywhere stays byte-identical to before this feature existed. Multiple
 * ids collapse to the first id plus a "+N" overflow count (design's
 * explicit contract, verbatim); the visible id links to the tracker via
 * the matching `IndexPayload.tickets` summary's `url` when one is
 * configured — `LineJson.ticket` itself carries only bare ids, never a
 * url, so this is the one place that has to cross-reference the two. */
function buildTicketGutter(
  lineNo: number,
  line: LineJson | undefined,
  index: IndexPayload,
): ReactNode | undefined {
  const ids = line?.ticket;
  if (!ids || ids.length === 0) return;
  const [first, ...rest] = ids;
  const url = index.tickets.find((t) => t.id === first)?.url ?? null;
  const chipClass = "truncate font-mono text-[10px] font-medium text-tertiary";
  return (
    <div
      data-testid={`ticket-gutter-${lineNo}`}
      title={ids.join(", ")}
      className="flex items-center justify-center gap-0.5 overflow-hidden px-0.5"
    >
      {url ? (
        <a href={url} className={cx(chipClass, "hover:text-brand-secondary hover:underline")}>
          {first}
        </a>
      ) : (
        <span className={chipClass}>{first}</span>
      )}
      {rest.length > 0 && (
        <span className="shrink-0 text-[9px] text-quaternary">+{rest.length}</span>
      )}
    </div>
  );
}

/** Struck "revoked" chip (id in `stale_run`) or a live chip: tier dot +
 * label + host pill (`host || board || "—"`) + "× N" (aging appends
 * " · aging"). Ids with no matching `RunJson` (shouldn't happen — every id
 * on a chunk should resolve against `index.runs` — but the data contract
 * doesn't guarantee it) are silently skipped rather than rendering a
 * broken chip. */
function RunChip({ id, line, index }: { id: number; line: LineJson; index: IndexPayload }) {
  const run = index.runs.find((r) => r.id === id);
  if (!run) return null;
  const revoked = (line.stale_run ?? []).includes(id);
  const count = line.run?.[String(id)] ?? 0;
  const countText = revoked ? "revoked" : `× ${count}${run.aging ? " · aging" : ""}`;
  return (
    <span
      data-testid="run-chip"
      style={revoked ? { color: index.state_colors.stale } : undefined}
      className="inline-flex items-center gap-1.5 rounded-full border border-secondary bg-primary
        px-2.5 py-1 text-xs font-medium text-secondary shadow-xs"
    >
      <span
        aria-hidden
        className="size-2 shrink-0 rounded-sm"
        style={{ backgroundColor: index.tier_colors[run.tier] ?? "currentColor" }}
      />
      {run.label}
      <span
        data-testid="host-pill"
        className="rounded border border-secondary bg-tertiary px-1.5 font-mono text-[10.5px] text-tertiary"
      >
        {run.host || run.board || "—"}
      </span>
      <span className={cx("tabular-nums text-quaternary", revoked && "line-through")}>
        {countText}
      </span>
    </span>
  );
}

/** Expander chip for one asserted override ref: dashed-border variant of
 * `RunChip` so provenance reads as "declared", never "recorded". */
function AssertedChip({ entryId, index }: { entryId: number; index: IndexPayload }) {
  const entry = index.overrides.find((o) => o.id === entryId);
  if (!entry) return null;
  return (
    <span
      data-testid="asserted-chip"
      className="inline-flex items-center gap-1.5 rounded-full border border-dashed
        border-secondary bg-primary px-2.5 py-1 text-xs font-medium text-secondary"
    >
      <span
        aria-hidden
        className="size-2 shrink-0 rounded-sm border border-current"
        style={{ borderColor: index.tier_colors[entry.tier] ?? "currentColor" }}
      />
      {entry.key}
      <span className="text-quaternary">{entry.reason}</span>
    </span>
  );
}

function renderExpansionFor(chunk: FileChunk, index: IndexPayload) {
  return (codeLine: CodeLine): ReactNode => {
    const line = chunk.lines[String(codeLine.number)];
    if (!line) return null;
    const runIds = collectRunIds(line);
    const assertedIds = [...new Set(Object.values(line.asserted ?? {}).flat())];
    if (runIds.size === 0 && assertedIds.length === 0) return null;
    return (
      <>
        {[...runIds].map((id) => (
          <RunChip key={id} id={id} line={line} index={index} />
        ))}
        {assertedIds.map((id) => (
          <AssertedChip key={`a${id}`} entryId={id} index={index} />
        ))}
      </>
    );
  };
}

export interface LineRange {
  start: number;
  end: number;
}

/** The whole accepted grammar of `?lines=`: `A` or `A-B`, digits only. */
const LINES_PARAM = /^(\d+)(?:-(\d+))?$/;

/** Parses the `?lines=A-B` (or bare `?lines=A`) deep link (design §6.2) via
 * `parseHashQuery()` (`focus.tsx`) — never wouter's own
 * `useSearch()`/`location.search`, per that module's header comment on why
 * `?ctx=` (and now `?lines=`) must live inside the hash fragment. The
 * Tickets page's missing-line ranges, and anyone hand-editing the URL, are
 * the two sources; both get the SAME forgiving contract: a non-numeric
 * value, a reversed range, or either bound outside `[1, totalLines]` is
 * ignored (returns `null`) rather than thrown — these links arrive from
 * outside this component, not from data this app itself validated. Exported
 * for direct unit testing, same pattern as `rowClassFor` above. */
export function parseLinesRange(
  totalLines: number,
  params: URLSearchParams = parseHashQuery(),
): LineRange | null {
  const raw = params.get("lines");
  if (raw === null) return null;
  const match = LINES_PARAM.exec(raw);
  if (!match) return null;
  const start = Number(match[1]);
  const end = match[2] === undefined ? start : Number(match[2]);
  if (start > end) return null;
  if (start < 1 || end > totalLines) return null;
  return { start, end };
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; reason: "stamp" | "other" }
  | { status: "ready"; chunk: FileChunk; htmlLines: string[] };

export interface FilePageProps {
  index: IndexPayload;
  segments: string[];
  node: FileNode;
}

export function FilePage({ index, segments, node }: FilePageProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [openLines, setOpenLines] = useState<Set<number>>(new Set());
  const { focus, ticket, hideAsserted } = useFocus();
  // Independently re-resolved against THIS page's own `index` prop, same
  // defensive pattern AppShell.tsx/DirectoryPage.tsx use — a focus label
  // that doesn't resolve here just renders unfocused instead of crashing.
  const focusedContext = focus ? groupContexts(index).find((c) => c.label === focus) : undefined;
  const memberRunIds = focusedContext ? new Set(focusedContext.runs.map((r) => r.id)) : undefined;
  // `ticket` is already validated against `index.tickets` by
  // `FocusProvider`/`resolveTicket` before it ever reaches here — no second
  // lookup needed, unlike `focusedContext` above (which derives display
  // fields `focus`, a bare label string, doesn't carry).

  const hash = useHash();
  // Derived, not stored: re-parsed on every hash change (a same-file
  // `?lines=` navigation never remounts this page) and bounds-checked
  // against the loaded chunk's real line count. `parseLinesRange` returns a
  // fresh object every call, so a hash change that never touched `?lines=`
  // (e.g. toggling the search pill) still produces a new `highlight`
  // identity here — the scroll effect below sidesteps that by keying on the
  // resolved bounds (`highlight?.start`/`.end`, both primitives) instead of
  // on `highlight` itself, rather than this memo trying to preserve
  // reference equality (which would mean writing a ref during render).
  const highlight = useMemo(
    () =>
      state.status === "ready"
        ? parseLinesRange(state.chunk.source.split("\n").length, hashQueryOf(hash))
        : null,
    [hash, state],
  );

  // `?q=` (+ `re=1`, `unc=1`) — the palette's link state. Recomputed with
  // the same engine the palette ran, over this chunk alone.
  const searchQuery = hashQueryOf(hash).get("q");
  const matchSpans = useMemo(() => {
    if (state.status !== "ready" || searchQuery === null || searchQuery.length === 0) return null;
    const params = hashQueryOf(hash);
    const compiled = compileQuery(searchQuery, params.get("re") === "1");
    if (compiled.re === undefined) return new Map<number, [number, number][]>();
    const rows = matchesInLines(
      state.chunk.source.split("\n"),
      compiled.re,
      statesFromChunk(state.chunk),
      params.get("unc") === "1",
    );
    return new Map(rows.map((r) => [r.line, r.spans]));
  }, [hash, state, searchQuery]);

  const cardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (matchSpans === null || cardRef.current === null) return;
    paintMatches(cardRef.current, matchSpans);
    return () => clearMatches();
  }, [matchSpans]);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setOpenLines(new Set());
    loadFileChunk(node.chunk)
      // `loadedChunk`, not `chunk`: the render body below destructures its own
      // `chunk` out of `state`, and these two are a lifecycle apart — this one
      // is the value that just resolved, that one is whatever is committed.
      .then(async (loadedChunk) => {
        const htmlLines = await highlightLines(loadedChunk.source, langForPath(loadedChunk.path));
        if (cancelled) return;
        setState({ status: "ready", chunk: loadedChunk, htmlLines });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          reason: err instanceof StampMismatchError ? "stamp" : "other",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [node.chunk]);

  // Scrolls the FIRST highlighted row into view, once per resolved
  // `?lines=` target (design §6.2) — runs whenever the highlight's START
  // LINE changes, not on every hash write. `highlight` is a fresh object on
  // every hash change (`parseLinesRange` always allocates), including one
  // that leaves `?lines=` untouched (e.g. clearing the search pill), so
  // keying this effect on `highlight` itself would re-fire — and re-scroll
  // the user — on those too. Destructured into a `highlightStart` local
  // (rather than reading `highlight.start` inside the effect body while
  // depending on the split-out primitive) so the effect depends on exactly
  // what it reads — the primitive bound, not the object's identity; a
  // `highlightEnd` counterpart was dropped from the dependency list per
  // Biome's `lint/correctness/useExhaustiveDependencies` ("This hook
  // specifies more dependencies than necessary: highlightEnd") — the body
  // never reads the range's end, only its start row, so depending on end
  // too would be dishonest. `?.` guards jsdom, which has no `scrollIntoView`
  // implementation at all under this project's pinned version (throws "is
  // not a function") rather than a harmless no-op — every other DOM method
  // this codebase calls under test tolerates jsdom's gaps; this is the
  // first real caller of this particular one. Queries the DOM directly by
  // testid rather than threading a ref through `CodeView` (which renders
  // the actual row divs) — `CodeView` has no other reason to expose row
  // nodes to its caller.
  const highlightStart = highlight?.start;
  useEffect(() => {
    if (highlightStart === undefined) return;
    const row = document.querySelector<HTMLElement>(`[data-testid="code-row-${highlightStart}"]`);
    row?.scrollIntoView?.({ block: "center" });
  }, [highlightStart]);

  function onToggleLine(lineNo: number): void {
    setOpenLines((prev) => {
      const next = new Set(prev);
      if (next.has(lineNo)) next.delete(lineNo);
      else next.add(lineNo);
      return next;
    });
  }

  if (state.status === "loading") {
    return (
      <div data-testid="file-loading" className="p-8 text-center text-sm text-tertiary">
        Loading {node.name}…
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <GuardScreen reason={state.reason === "stamp" ? "report changed on disk" : "missing data"} />
    );
  }

  const { chunk, htmlLines } = state;
  const excludedSet = new Set(chunk.excluded);
  const sourceLines = chunk.source.split("\n");
  const lang = langForPath(chunk.path);
  const columns = buildColumns(index);

  const codeLines: CodeLine[] = sourceLines.map((_line, i) => {
    const lineNo = i + 1;
    const line = chunk.lines[String(lineNo)];
    const excluded = excludedSet.has(lineNo);
    const rowClass =
      focusedContext && memberRunIds
        ? rowClassForFocus(line, excluded, memberRunIds, focusedContext.tier)
        : rowClassFor(lineForRowClass(line, hideAsserted), excluded, index.tier_order);
    // Dim (never hide) a line the pinned ticket doesn't own — orthogonal
    // to `rowClass`'s coverage-state tinting above, so an
    // excluded/stale/aging/blank line dims exactly the same as a hit one.
    const dimmed = ticket !== null && !ownedByTicket(line, ticket);
    return {
      number: lineNo,
      html: htmlLines[i] ?? "",
      rowClass,
      cells:
        focusedContext && memberRunIds
          ? buildCellsFocused(lineNo, line, index, memberRunIds, focusedContext.tier)
          : buildCells(lineNo, line, index, hideAsserted),
      expandable: collectRunIds(line).size > 0 || Object.keys(line?.asserted ?? {}).length > 0,
      style: dimStyleFor(rowClass, index, dimmed),
      ticketGutter: buildTicketGutter(lineNo, line, index),
      highlighted: highlight !== null && lineNo >= highlight.start && lineNo <= highlight.end,
      matched: matchSpans?.has(lineNo) ?? false,
    };
  });

  // Gutter column only activates when some RENDERED row actually carries a
  // ticket — a past-EOF `chunk.lines` record (see FilePage.test.tsx's
  // "past-EOF" regression) can carry ticket data with nowhere to render
  // it, and must not by itself turn the column on. Keeps a report with no
  // `[coverage.tickets]` attribution anywhere byte-identical to before this
  // feature (design §6.2, Global Constraints).
  const hasTicketGutter = codeLines.some((l) => l.ticketGutter !== undefined);

  // Meta line ("N lines · M covered") always reflects the file's OVERALL
  // coverage, unaffected by focus — only the stats card below rescopes.
  // Meta line reads the UNHIDDEN rows deliberately (see the comment two
  // lines below) — `hideAsserted` only narrows the STATS CARD's own numbers
  // (`statsRows`), never this header count.
  const rows = chunkTierRows(index, chunk);
  const allRow = rows.at(-1);
  // `chunkTierRows`' own "all tiers" row always carries a real tuple here
  // (never `null` — that's only ever produced by DirectoryPage's composed
  // ctx+ticket row); `?.` narrows the TYPE (TierStatRow.
  // line is now nullable for that other caller), not an actual runtime case
  // this line needs to handle.
  const totalLines = allRow?.line?.[1] ?? 0;
  const coveredLines = allRow?.line?.[0] ?? 0;
  // Ticket scoping composes with run focus: `ticketFileRow`
  // computes an exact owned/hit count directly from this file's own
  // per-line data (unlike DirectoryPage's tree, no placeholder counts are
  // involved) — passing `focusedContext` when both are active makes the
  // numerator "member-run hits WITHIN the ticket's owned lines" (the
  // spec's headline example: "PROJ-412's lines, as proven by the manual
  // run"), never the ticket-only answer.
  const statsRows =
    ticket !== null
      ? ticketFileRow(index, chunk, ticket, focusedContext, hideAsserted)
      : focusedContext
        ? focusedFileRow(index, chunk, focusedContext)
        : chunkTierRows(index, chunk, hideAsserted);

  const expandableNumbers = codeLines.filter((l) => l.expandable).map((l) => l.number);
  const allOpen = expandableNumbers.length > 0 && expandableNumbers.every((n) => openLines.has(n));

  const header = (
    <div className="flex items-center justify-between gap-2.5 border-b border-secondary bg-secondary px-3.5 py-2.5">
      <div className="flex min-w-0 items-center gap-2 font-mono text-sm font-semibold text-primary">
        <File02 aria-hidden className="size-4 shrink-0 text-quaternary" />
        <span className="truncate">{node.name}</span>
        <span
          data-testid="lang-badge"
          className="rounded-full border border-secondary bg-tertiary px-2 py-0.5 font-sans text-[10.5px]
            font-semibold text-tertiary uppercase"
        >
          {lang.toUpperCase()}
        </span>
      </div>
      <button
        type="button"
        data-testid="expand-contexts"
        aria-pressed={allOpen}
        onClick={() => setOpenLines(allOpen ? new Set() : new Set(expandableNumbers))}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-secondary px-2.5
          py-1 text-xs font-medium text-tertiary outline-none hover:bg-tertiary hover:text-primary"
      >
        <ChevronDown
          aria-hidden
          className={cx("size-3 transition-transform", allOpen && "rotate-180")}
        />
        Expand contexts
      </button>
    </div>
  );

  // Fixed-cardinality row states (s-unc/s-excl/s-stale/s-aging) read their
  // tint colors from these custom properties (covapp.css) rather than from
  // hard-coded values — set once here from IndexPayload.state_colors
  // (Global Constraints: thresholds/colors always come from report data).
  const stateVars = {
    "--state-unc": index.state_colors.uncovered,
    "--state-excl": index.state_colors.excluded,
    "--state-stale": index.state_colors.stale,
    "--state-aging": index.state_colors.aging,
  } as CSSProperties;

  return (
    <AppShell
      crumbs={crumbsFor(index.project_name, segments)}
      title={<span className="font-mono">{node.name}</span>}
      meta={
        <>
          <b>{totalLines}</b> lines · <b>{coveredLines}</b> covered · report generated{" "}
          <b>{index.generated_at}</b> · otto {index.otto_version}
        </>
      }
      stats={{
        scope: withHideAssertedSuffix(
          focusedContext
            ? ticket !== null
              ? `focused: ${focusedContext.label} · ticket: ${ticket}`
              : `focused: ${focusedContext.label}`
            : ticket !== null
              ? `ticket: ${ticket}`
              : node.path,
          hideAsserted,
        ),
        title: "Coverage — this file",
        rows: statsRows,
        thresholds: index.thresholds,
        keyColumnLabel: keyColumnLabel({
          ticket: ticket !== null,
          context: Boolean(focusedContext),
        }),
      }}
      searchHit={
        searchQuery !== null && matchSpans !== null
          ? { query: searchQuery, count: matchSpans.size }
          : null
      }
    >
      <div
        ref={cardRef}
        data-testid="code-card"
        // `overflow-clip`, not `overflow-hidden`: `overflow-hidden` creates a
        // scroll container (a "scrollport"), and CodeView's `sticky top-0`
        // column-header row sticks to the NEAREST scroll container, not the
        // page — with `overflow-hidden` here it would stick to this
        // never-scrolling card instead of the viewport and scroll off with
        // the rest of the page (the file-page.html mockup has this exact
        // bug; the spec text pins a *working* sticky header, so the mockup's
        // CSS isn't authoritative here). `overflow-clip` still clips content
        // to the rounded corners but does NOT establish a scroll container,
        // so `position: sticky` keeps chaining up to the page's own scroll.
        className="overflow-clip rounded-xl border border-secondary shadow-xs"
        style={stateVars}
      >
        <CodeView
          lines={codeLines}
          header={header}
          columns={columns}
          renderExpansion={renderExpansionFor(chunk, index)}
          openLines={openLines}
          onToggleLine={onToggleLine}
          ticketGutterWidth={hasTicketGutter ? "72px" : "0px"}
        />
      </div>
    </AppShell>
  );
}
