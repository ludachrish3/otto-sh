// The covapp file page (Task 5 brief). DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/file-page.html —
// recreated with React + Tailwind semantic tokens + ui/CodeView, not the
// mockup's literal CSS. Loads its own FileChunk (Task 2's `loadFileChunk`)
// on mount — the tree's rolled-up `Stats` (what DirectoryPage reads) has no
// per-line granularity, only the chunk does.
import { ChevronDown, File02 } from "@untitledui/icons";
import { type CSSProperties, type ReactNode, useEffect, useState } from "react";

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
} from "../format";
import { highlightLines, langForPath } from "../highlight";
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

/** `rowClassFor`'s under-focus counterpart (Task 7 spec §4): excluded still
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
  if (!rowClass.startsWith("t-")) return undefined;
  const color = index.tier_colors[rowClass.slice(2)] ?? "currentColor";
  return {
    backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)`,
    borderLeftColor: color,
  };
}

/** Task 12, the file page's own divergence from DirectoryPage's tree (spec
 * §6.3, verbatim): a pinned ticket DIMS lines it doesn't own here, rather
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

function collectRunIds(line: LineJson | undefined): Set<number> {
  if (!line) return new Set();
  const fromRun = line.run ? Object.keys(line.run).map(Number) : [];
  const fromStale = line.stale_run ?? [];
  return new Set([...fromRun, ...fromStale]);
}

function TierHeaderDot({ color, label }: { color?: string; label: string }) {
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

function HitCell({ value }: { value: number | null }) {
  if (value === null || value === 0) {
    return (
      <span aria-hidden className="text-quaternary opacity-45 tabular-nums">
        ·
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
 * UNCHANGED under focus (Task 7 spec §4: branch data isn't per-run, v4's
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

function buildCells(lineNo: number, line: LineJson | undefined, index: IndexPayload): ReactNode[] {
  const cells: ReactNode[] = [
    <span key="num" className="text-quaternary tabular-nums">
      {lineNo}
    </span>,
  ];
  for (const tier of index.tier_order) {
    cells.push(<HitCell key={tier} value={line ? (line.hits[tier] ?? 0) : null} />);
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

/** Leading gutter cell for one row's ticket chip(s) (Task 11, design
 * §6.2) — `undefined` when this line carries none, which `CodeView`
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
  if (!ids || ids.length === 0) return undefined;
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

function renderExpansionFor(chunk: FileChunk, index: IndexPayload) {
  return (codeLine: CodeLine): ReactNode => {
    const line = chunk.lines[String(codeLine.number)];
    if (!line) return null;
    const ids = collectRunIds(line);
    if (ids.size === 0) return null;
    return (
      <>
        {[...ids].map((id) => (
          <RunChip key={id} id={id} line={line} index={index} />
        ))}
      </>
    );
  };
}

export interface LineRange {
  start: number;
  end: number;
}

/** Parses the `?lines=A-B` (or bare `?lines=A`) deep link (design §6.2) via
 * `parseHashQuery()` (`focus.tsx`) — never wouter's own `useSearch()`/
 * `location.search`, per that module's header comment on why `?ctx=` (and
 * now `?lines=`) must live inside the hash fragment. Task 10's Tickets-page
 * missing-line ranges, and anyone hand-editing the URL, are the two
 * sources; both get the SAME forgiving contract, verbatim from the brief:
 * a non-numeric value, a reversed range, or either bound outside `[1,
 * totalLines]` is ignored (returns `null`) rather than thrown — these
 * links arrive from outside this component, not from data this app itself
 * validated. Exported for direct unit testing, same pattern as
 * `rowClassFor` above. */
export function parseLinesRange(totalLines: number): LineRange | null {
  const raw = parseHashQuery().get("lines");
  if (raw === null) return null;
  const match = /^(\d+)(?:-(\d+))?$/.exec(raw);
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
  // `?lines=` deep-link target (Task 11) — re-parsed per chunk load (below),
  // never a lazy `useState` initializer: this same `FilePage` instance
  // persists across a directory-tree navigation from one file to another
  // (App.tsx's wildcard route re-renders it with a new `node` rather than
  // remounting, exactly why the load effect below already keys on
  // `node.chunk` for `state`/`openLines`), so a value computed only once at
  // first-ever mount would go stale on the very next file.
  const [highlight, setHighlight] = useState<LineRange | null>(null);
  const { focus, ticket } = useFocus();
  // Independently re-resolved against THIS page's own `index` prop, same
  // defensive pattern AppShell.tsx/DirectoryPage.tsx use — a focus label
  // that doesn't resolve here just renders unfocused instead of crashing.
  const focusedContext = focus ? groupContexts(index).find((c) => c.label === focus) : undefined;
  const memberRunIds = focusedContext ? new Set(focusedContext.runs.map((r) => r.id)) : undefined;
  // `ticket` (Task 12) is already validated against `index.tickets` by
  // `FocusProvider`/`resolveTicket` before it ever reaches here — no second
  // lookup needed, unlike `focusedContext` above (which derives display
  // fields `focus`, a bare label string, doesn't carry).

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setOpenLines(new Set());
    setHighlight(null);
    loadFileChunk(node.chunk)
      .then(async (chunk) => {
        const htmlLines = await highlightLines(chunk.source, langForPath(chunk.path));
        if (cancelled) return;
        setState({ status: "ready", chunk, htmlLines });
        // Bounds-checking `?lines=` needs the file's real line count, which
        // only exists once the chunk has resolved — see `parseLinesRange`'s
        // doc comment for why an out-of-bounds bound is ignored rather than
        // clamped.
        setHighlight(parseLinesRange(chunk.source.split("\n").length));
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
  // `?lines=` target (Task 11, design §6.2). `?.` guards jsdom, which has
  // no `scrollIntoView` implementation at all under this project's pinned
  // version (throws "is not a function") rather than a harmless no-op —
  // every other DOM method this codebase calls under test tolerates jsdom's
  // gaps; this is the first real caller of this particular one. Queries the
  // DOM directly by testid rather than threading a ref through `CodeView`
  // (which renders the actual row divs) — `CodeView` has no other reason to
  // expose row nodes to its caller, and this runs at most once per file
  // load, not on a hot path.
  useEffect(() => {
    if (!highlight) return;
    const row = document.querySelector<HTMLElement>(`[data-testid="code-row-${highlight.start}"]`);
    row?.scrollIntoView?.({ block: "center" });
  }, [highlight]);

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
        : rowClassFor(line, excluded, index.tier_order);
    // Task 12: dim (never hide) a line the pinned ticket doesn't own —
    // orthogonal to `rowClass`'s coverage-state tinting above, so an
    // excluded/stale/aging/blank line dims exactly the same as a hit one.
    const dimmed = ticket !== null && !ownedByTicket(line, ticket);
    return {
      number: lineNo,
      html: htmlLines[i] ?? "",
      rowClass,
      cells:
        focusedContext && memberRunIds
          ? buildCellsFocused(lineNo, line, index, memberRunIds, focusedContext.tier)
          : buildCells(lineNo, line, index),
      expandable: collectRunIds(line).size > 0,
      style: dimStyleFor(rowClass, index, dimmed),
      ticketGutter: buildTicketGutter(lineNo, line, index),
      highlighted: highlight !== null && lineNo >= highlight.start && lineNo <= highlight.end,
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
  const rows = chunkTierRows(index, chunk);
  const allRow = rows[rows.length - 1];
  // `chunkTierRows`' own "all tiers" row always carries a real tuple here
  // (never `null` — that's only ever produced by DirectoryPage's composed
  // ctx+ticket row, Task 12 fix round 1); `?.` narrows the TYPE (TierStatRow.
  // line is now nullable for that other caller), not an actual runtime case
  // this line needs to handle.
  const totalLines = allRow?.line?.[1] ?? 0;
  const coveredLines = allRow?.line?.[0] ?? 0;
  // Ticket scoping (Task 12) composes with run focus: `ticketFileRow`
  // computes an exact owned/hit count directly from this file's own
  // per-line data (unlike DirectoryPage's tree, no placeholder counts are
  // involved) — passing `focusedContext` when both are active makes the
  // numerator "member-run hits WITHIN the ticket's owned lines" (the
  // spec's headline example: "PROJ-412's lines, as proven by the manual
  // run"), never the ticket-only answer.
  const statsRows =
    ticket !== null
      ? ticketFileRow(index, chunk, ticket, focusedContext)
      : focusedContext
        ? focusedFileRow(index, chunk, focusedContext)
        : rows;

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
        scope: focusedContext
          ? ticket !== null
            ? `focused: ${focusedContext.label} · ticket: ${ticket}`
            : `focused: ${focusedContext.label}`
          : ticket !== null
            ? `ticket: ${ticket}`
            : node.path,
        title: "Coverage — this file",
        rows: statsRows,
        thresholds: index.thresholds,
        keyColumnLabel: keyColumnLabel({
          ticket: ticket !== null,
          context: focusedContext !== null,
        }),
      }}
    >
      <div
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
