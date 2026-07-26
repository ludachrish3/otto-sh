// Generic per-line source grid (Task 5 brief) — covapp-agnostic, reusable
// outside covapp, the second (and per the plan, LAST) sanctioned new ui/**
// component after TreeView.tsx. Deliberately API-consistent with that
// sibling: a caller-supplied `columns` array drives one shared CSS grid
// template (header row + every body row), a `cells`/render-per-column
// contract (TreeView's `renderCells`), and the outer bordered "card" chrome
// stays OWNED BY THE CALLER (FilePage wraps `<CodeView>` the same way
// DirectoryPage.tsx wraps `<TreeView>`) — this component only renders the
// header slot, the sticky column-header row, and the line rows themselves.
//
// DOM/anatomy reference: docs/superpowers/specs/assets/2026-07-24-coverage-ui/
// file-page.html's `.code-cols`/`.cl`/`.ctxpanel` — recreated with Tailwind
// utilities over the vendored semantic tokens, not the mockup's literal CSS.
//
// Expansion is FULLY CONTROLLED (`openLines`/`onToggleLine`): a plain
// internal Set<number> would need a second, parallel "expand all" signal
// prop to let FilePage's "Expand contexts" button drive every row from
// outside — two sources of truth for the same state. Lifting it to the
// caller means FilePage's button is just "call onToggleLine for every
// expandable line," no extra prop shape needed here.
import { ChevronDown } from "@untitledui/icons";
import type { CSSProperties, ReactNode } from "react";
import { Children, Fragment, isValidElement } from "react";

import { cx } from "@/utils/cx";

export interface GutterCol {
  id: string;
  /** CSS grid track size, e.g. "46px" or "40px". */
  width: string;
  header: ReactNode;
}

export interface CodeLine {
  number: number;
  /** Pre-rendered HTML for the source cell (e.g. Shiki output) — see the
   * `dangerouslySetInnerHTML` note on the source cell below for why this is
   * safe to inject verbatim. */
  html: string;
  /** Row tint/state class (e.g. "t-system", "s-excl", "" for uncoverable) —
   * applied to the row div verbatim; CodeView has no opinion on what these
   * class names mean. */
  rowClass: string;
  /** One entry per `columns` entry, same order (mirrors TreeView's
   * `renderCells` contract: `columns[0]` here is `cells[0]`, no offset). */
  cells: ReactNode[];
  /** Whether this line HAS expandable content at all — the actual content
   * comes from `renderExpansion`; a line can be `expandable` and still
   * render no button if `renderExpansion` returns nothing for it (see
   * `hasExpansion` below). */
  expandable?: boolean;
  /** Extra inline style merged onto the row div, layered under `rowClass`.
   * Exists because one state family (per-tier tint/accent color) is
   * data-driven with an unbounded set of tier names — there is no finite
   * set of "t-<tier>" CSS rules to predeclare, so the caller computes the
   * actual color (e.g. via `color-mix`) and supplies it here rather than
   * inventing a class per tier. The fixed-cardinality states (excluded/
   * stale/aging/uncovered) instead rely on plain `rowClass` CSS rules. */
  style?: CSSProperties;
}

export interface CodeViewProps {
  lines: CodeLine[];
  /** Arbitrary content rendered above the column-header row — e.g.
   * FilePage's file icon + name + language badge + "Expand contexts"
   * button. CodeView renders it as-is; it owns none of its markup. */
  header: ReactNode;
  columns: GutterCol[];
  /** Content for a line's expansion panel (e.g. FilePage's run-chip list).
   * Absent, or returning `null`/`undefined` for a given line, means that
   * line never shows an expander button even if `expandable` is set. */
  renderExpansion?: (line: CodeLine) => ReactNode;
  /** Which lines' panels are currently open, by line number. Fully
   * controlled — CodeView holds no expansion state of its own. */
  openLines: ReadonlySet<number>;
  onToggleLine: (lineNumber: number) => void;
}

/** `["0px", ...column widths, "1fr", "66px"].join(" ")` — the reserved
 * ticket gutter (zero-width until per-ticket plumbing exists — a spec
 * non-goal today, kept as a real grid column so adding it later is a
 * one-line width change, not a template rewrite) leads, the source column
 * flexes, and the runs/expansion toggle owns a fixed trailing column. */
function gridTemplateFor(columns: GutterCol[]): string {
  return ["0px", ...columns.map((c) => c.width), "1fr", "66px"].join(" ");
}

const ROW_BASE =
  "grid items-center border-l-[3px] border-l-transparent text-[12.5px] leading-relaxed";

/** `renderExpansion` callers almost always return a JSX fragment
 * (`<>chip chip chip</>`) grouping several sibling chips — that's ONE
 * React element (`type === Fragment`) as far as `Children.count` is
 * concerned, not three, so the naive count would always read "1". Unwrap
 * exactly one fragment level before counting; anything else (a single
 * element, an array, `null`) counts as-is. */
function countExpansionItems(expansion: ReactNode): number {
  if (isValidElement(expansion) && expansion.type === Fragment) {
    return Children.count((expansion.props as { children?: ReactNode }).children);
  }
  return Children.count(expansion);
}

function ExpanderCell({
  line,
  open,
  expansion,
  onToggleLine,
}: {
  line: CodeLine;
  open: boolean;
  expansion: ReactNode;
  onToggleLine: (n: number) => void;
}) {
  if (expansion === null || expansion === undefined) return <div />;
  const count = countExpansionItems(expansion);
  return (
    <div className="flex items-center justify-end pr-2.5">
      <button
        type="button"
        data-testid={`code-expander-${line.number}`}
        aria-expanded={open}
        title="runs covering this line"
        onClick={() => onToggleLine(line.number)}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-quaternary
          outline-none hover:bg-secondary hover:text-primary"
      >
        <ChevronDown
          aria-hidden
          className={cx("size-3 transition-transform", open && "rotate-180")}
        />
        {count}
      </button>
    </div>
  );
}

export function CodeView({
  lines,
  header,
  columns,
  renderExpansion,
  openLines,
  onToggleLine,
}: CodeViewProps) {
  const gridTemplate = gridTemplateFor(columns);

  return (
    <div data-testid="code-view">
      {header}
      <div
        data-testid="code-columns"
        className="sticky top-0 z-[2] grid items-center border-b border-secondary bg-secondary
          py-1 text-[10px] font-semibold tracking-wide text-quaternary uppercase"
        style={{ gridTemplateColumns: gridTemplate }}
      >
        <div aria-hidden />
        {columns.map((col) => (
          <div key={col.id} data-testid={`code-col-${col.id}`} className="text-center">
            {col.header}
          </div>
        ))}
        <div className="pl-3.5 text-left">Source</div>
        <div />
      </div>
      <div className="font-mono">
        {lines.map((line) => {
          const expansion = line.expandable && renderExpansion ? renderExpansion(line) : null;
          const hasExpansion = expansion !== null && expansion !== undefined;
          const open = hasExpansion && openLines.has(line.number);
          return (
            <Fragment key={line.number}>
              <div
                data-testid={`code-row-${line.number}`}
                className={cx(ROW_BASE, line.rowClass)}
                style={{ gridTemplateColumns: gridTemplate, ...line.style }}
              >
                <div aria-hidden />
                {columns.map((col, i) => (
                  <div key={col.id} className="flex items-center justify-center px-0.5 text-center">
                    {line.cells[i]}
                  </div>
                ))}
                <div
                  className="cv-src shiki overflow-x-auto py-0.5 pr-3.5 pl-3.5 whitespace-pre"
                  // `html` is Shiki-rendered output (see highlight.ts) — Shiki
                  // HTML-escapes source text itself before wrapping it in token
                  // spans, so this never injects unescaped source content
                  // regardless of what the file on disk contains.
                  // biome-ignore lint/security/noDangerouslySetInnerHtml: see above
                  dangerouslySetInnerHTML={{ __html: line.html || "&nbsp;" }}
                />
                <ExpanderCell
                  line={line}
                  open={open}
                  expansion={expansion}
                  onToggleLine={onToggleLine}
                />
              </div>
              {hasExpansion && open && (
                <div
                  data-testid={`ctx-panel-${line.number}`}
                  className="flex flex-wrap items-center gap-1.5 border-y border-dashed
                    border-secondary bg-secondary py-2 pr-3.5 pl-[60px]"
                  style={{ gridColumn: "1 / -1" }}
                >
                  {expansion}
                </div>
              )}
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}
