// Generic expandable/sortable tree grid (Task 4 brief) — covapp-agnostic,
// reusable outside covapp. Props are a pinned, verbatim contract (a second
// consumer, Task 5's CodeView, is compared against it for consistency), so
// treat the exported names/shapes as frozen, not house style.
//
// Library choice: this does NOT use react-aria-components' Tree/TreeItem.
// That pair models a single-column ARIA tree collection (one row of mixed
// content per item, built via a synchronous collection-walk of JSX
// children/`items`+render-prop) — it fights this component's actual
// contract on three points: (1) a uniform CSS-grid column template shared
// by every row and the header, computed from a caller-supplied `columns`
// array; (2) two independently-clickable regions within one row (the name
// text navigates, the chevron only toggles — Tree's `onAction` fires on
// whole-row activation, not a sub-target); (3) column-header-driven sorting
// that reorders each sibling group independently while preserving nesting,
// which is orthogonal to Tree's own (unsupported here) drag/selection
// machinery. Per the brief, visual anatomy and the props contract win over
// library choice in this situation — see other `ui/` files (Breadcrumbs.tsx,
// Disclosure.tsx) for the project's usual "react-aria primitive + Tailwind"
// style; this file instead hand-rolls the accessible tree markup
// (`role="tree"`/`"treeitem"`/`aria-expanded`/`aria-level"`) the brief calls
// out as the sanctioned fallback, keeping full control over row hit-testing
// and grid layout.

import { ChevronRight } from "@untitledui/icons";
import type { KeyboardEvent, ReactNode } from "react";
import { useState } from "react";

import { cx } from "@/utils/cx";

export interface TreeColumn {
  id: string;
  label: string;
  /** CSS grid track size, e.g. "92px" or "minmax(210px,1fr)". The name
   * column (`columns[0]`) is always first in the composed grid template. */
  width: string;
  /** @default "right" (matches the DOM reference's numeric-column convention) */
  align?: "left" | "right";
}

interface TreeViewProps<T> {
  roots: T[];
  /** `null` marks a leaf (no chevron, never expandable). */
  getChildren: (item: T) => T[] | null;
  /** Must be unique across the WHOLE tree, not just a sibling group — it
   * backs both the row testid and expand-state tracking. */
  getRowId: (item: T) => string;
  /** `columns[0]` is the name column; the rest pair up positionally with
   * `renderCells`'s return array. */
  columns: TreeColumn[];
  renderName: (item: T) => ReactNode;
  /** One entry per non-name column (`columns.slice(1)`), same order. */
  renderCells: (item: T) => ReactNode[];
  /** Backs column-header sorting; return type drives comparison (numeric vs
   * string), independently per sibling group at every depth. */
  sortValue: (item: T, columnId: string) => string | number;
  /** Fires only for a click on the rendered name — never for the chevron or
   * a data cell. */
  onNavigate: (item: T) => void;
  /** @default false */
  defaultExpanded?: boolean;
}

interface SortState {
  columnId: string;
  dir: "asc" | "desc";
}

export function TreeView<T>({
  roots,
  getChildren,
  getRowId,
  columns,
  renderName,
  renderCells,
  sortValue,
  onNavigate,
  defaultExpanded = false,
}: TreeViewProps<T>) {
  const [sort, setSort] = useState<SortState | null>(null);
  // Only rows the user has actively toggled — everything else falls back to
  // `defaultExpanded`, so flipping that prop doesn't require pre-seeding
  // state for a tree whose full id set isn't known up front.
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});

  const gridTemplate = columns.map((c) => c.width).join(" ");

  function isOpen(id: string): boolean {
    return overrides[id] ?? defaultExpanded;
  }

  function toggle(id: string): void {
    setOverrides((prev) => ({ ...prev, [id]: !isOpen(id) }));
  }

  function cycleSort(columnId: string): void {
    setSort((prev) => {
      if (!prev || prev.columnId !== columnId) return { columnId, dir: "asc" };
      if (prev.dir === "asc") return { columnId, dir: "desc" };
      return null;
    });
  }

  function sortSiblings(items: T[]): T[] {
    if (!sort) return items;
    const { columnId, dir } = sort;
    const factor = dir === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
      const va = sortValue(a, columnId);
      const vb = sortValue(b, columnId);
      const cmp =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va).localeCompare(String(vb));
      return cmp * factor;
    });
  }

  function renderRow(item: T, depth: number): ReactNode {
    const id = getRowId(item);
    const children = getChildren(item);
    const isLeaf = children === null;
    const open = !isLeaf && isOpen(id);
    const cells = renderCells(item);

    // WAI-ARIA APG "Tree View" keyboard pattern for a treeitem: Enter
    // activates (our "navigate"); ArrowRight expands a collapsed parent,
    // ArrowLeft collapses an expanded one. The row itself carries the one
    // tab stop per visible node (chevron/name buttons below opt out of the
    // tab sequence via tabIndex=-1, staying mouse/touch-clickable) — a
    // per-row-tabbable model rather than full roving-tabindex/arrow-key
    // sibling navigation, which nothing in this component's contract needs.
    function onRowKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
      if (event.key === "Enter") {
        event.preventDefault();
        onNavigate(item);
      } else if (event.key === "ArrowRight" && !isLeaf && !open) {
        event.preventDefault();
        toggle(id);
      } else if (event.key === "ArrowLeft" && !isLeaf && open) {
        event.preventDefault();
        toggle(id);
      }
    }

    return (
      <div key={id}>
        <div
          role="treeitem"
          tabIndex={0}
          aria-level={depth + 1}
          aria-expanded={isLeaf ? undefined : open}
          data-testid={`tree-row-${id}`}
          onKeyDown={onRowKeyDown}
          className="grid h-9 items-center gap-2 rounded-lg px-2 text-sm outline-focus-ring
            hover:bg-secondary focus-visible:outline-2 focus-visible:-outline-offset-2"
          style={{ gridTemplateColumns: gridTemplate }}
        >
          <div className="flex min-w-0 items-center gap-1" style={{ paddingLeft: depth * 22 }}>
            {isLeaf ? (
              <span aria-hidden className="size-[22px] shrink-0" />
            ) : (
              <button
                type="button"
                tabIndex={-1}
                data-testid={`chevron-${id}`}
                aria-label={open ? "Collapse" : "Expand"}
                onClick={() => toggle(id)}
                className="flex size-[22px] shrink-0 items-center justify-center rounded-md
                  text-quaternary outline-none hover:bg-tertiary hover:text-primary"
              >
                <ChevronRight
                  aria-hidden
                  className={cx("size-3.5 transition-transform", open && "rotate-90")}
                />
              </button>
            )}
            <button
              type="button"
              tabIndex={-1}
              data-testid={`name-${id}`}
              onClick={() => onNavigate(item)}
              className="min-w-0 truncate text-left font-medium text-primary outline-none
                hover:underline"
            >
              {renderName(item)}
            </button>
          </div>
          {cells.map((cell, i) => {
            const col = columns[i + 1];
            return (
              <div
                key={col?.id ?? i}
                className={cx(
                  "min-w-0 truncate text-tertiary",
                  col?.align === "left" ? "text-left" : "text-right",
                )}
              >
                {cell}
              </div>
            );
          })}
        </div>
        {!isLeaf && open && (
          // role="group" is the correct WAI-ARIA APG pattern for wrapping a
          // treeitem's children when not nesting a second role="tree" — the
          // linter's suggested native replacement (<fieldset>, a FORM
          // grouping element) doesn't apply to this composite widget.
          // biome-ignore lint/a11y/useSemanticElements: see comment above.
          <div role="group">
            {sortSiblings(children).map((child) => renderRow(child, depth + 1))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      {/* No role="row"/"columnheader" here: this isn't a complete ARIA grid
          (no role="grid" ancestor, no per-cell role="columnheader"/"cell") —
          each header is a real, independently focusable <button>, which is
          all the sort-toggle interaction needs. Deliberately OUTSIDE the
          role="tree" container below: role="tree" only permits
          treeitem/group content, and these eight buttons live in the
          sortable header row, not the tree widget itself. */}
      <div
        className="grid items-center gap-2 border-b border-secondary bg-secondary px-2 py-2
          text-xs font-medium tracking-wide text-quaternary uppercase"
        style={{ gridTemplateColumns: gridTemplate }}
      >
        {columns.map((col) => {
          const active = sort?.columnId === col.id;
          return (
            <button
              key={col.id}
              type="button"
              data-testid={`tree-col-${col.id}`}
              onClick={() => cycleSort(col.id)}
              className={cx(
                "flex cursor-pointer items-center gap-1 rounded px-1 py-0.5 outline-none hover:bg-tertiary hover:text-secondary",
                col.align === "left" ? "justify-start text-left" : "justify-end text-right",
              )}
            >
              {col.label}
              {active && <span aria-hidden>{sort?.dir === "asc" ? "▲" : "▼"}</span>}
            </button>
          );
        })}
      </div>
      <div role="tree" className="p-1.5">
        {sortSiblings(roots).map((item) => renderRow(item, 0))}
      </div>
    </div>
  );
}
