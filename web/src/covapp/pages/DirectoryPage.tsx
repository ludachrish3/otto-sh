// The covapp directory page (Task 4 brief). DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/directory-page.html —
// recreated with React + Tailwind semantic tokens + ui/TreeView, not the
// mockup's literal CSS. One superseded detail per the brief: the mockup's
// floating coverage-key panel is gone from this page — the key lives in
// AppShell's ⋮ menu (Task 3), already wired.

import { File02, Folder } from "@untitledui/icons";
import type { ReactNode } from "react";
import { useHashLocation } from "wouter/use-hash-location";

import { Disclosure } from "@/ui/Disclosure";
import { type TreeColumn, TreeView } from "@/ui/TreeView";
import { cx } from "@/utils/cx";

import { AppShell } from "../chrome/AppShell";
import { groupContexts } from "../contexts";
import { useFocus } from "../focus";
import { crumbsFor, encodePath, fmtCount, focusedTreeRow, tierRows } from "../format";
import { findNode, fmtPct, type PctClass, pct, pctClass } from "../stats";
import type { DirNode, FileNode, IndexPayload, Stats } from "../types";

const NAME_COLUMN: TreeColumn = {
  id: "name",
  label: "Name",
  width: "minmax(210px,1fr)",
  align: "left",
};

// A `Row` wraps a DirNode/FileNode with the path segments accumulated from
// the tree root — DirNode alone carries no path, unlike FileNode (which
// already has a full display-relative `path`), so TreeView's `getRowId`/
// `onNavigate` (called with only the item, no ancestor context) need
// somewhere to read a dir's full path from.
type Row = { kind: "dir"; node: DirNode; path: string } | { kind: "file"; node: FileNode };

function dirRow(node: DirNode, path: string): Row {
  return { kind: "dir", node, path };
}

function fileRow(node: FileNode): Row {
  return { kind: "file", node };
}

function rowId(row: Row): string {
  return row.kind === "dir" ? `dir:${row.path}` : `file:${row.node.path}`;
}

function rowChildren(row: Row): Row[] | null {
  if (row.kind === "file") return null;
  const base = row.path;
  return [
    ...row.node.dirs.map((d) => dirRow(d, base ? `${base}/${d.name}` : d.name)),
    ...row.node.files.map((f) => fileRow(f)),
  ];
}

function rowStats(row: Row): Stats {
  return row.node.stats;
}

function countFiles(node: DirNode): number {
  return node.files.length + node.dirs.reduce((sum, d) => sum + countFiles(d), 0);
}

function buildColumns(index: IndexPayload): TreeColumn[] {
  return [
    NAME_COLUMN,
    { id: "lines", label: "Lines", width: "92px" },
    { id: "line", label: "Line %", width: "74px" },
    { id: "branch", label: "Branch %", width: "74px" },
    ...index.tier_order.map(
      (tier): TreeColumn => ({
        id: `tier:${tier}`,
        label: index.tier_labels[tier] ?? tier,
        width: "64px",
      }),
    ),
    { id: "flags", label: "Flags", width: "170px" },
  ];
}

/** Column ids that aren't a fixed name (`"tier:<tier>"`) carry everything
 * `sortValue` needs in the id itself, so this needs no `index` closure. */
function rowSortValue(row: Row, columnId: string): string | number {
  const stats = rowStats(row);
  switch (columnId) {
    case "name":
      return row.node.name.toLowerCase();
    case "lines":
      return stats.lines.total;
    case "line":
      return pct(stats.lines.hit, stats.lines.total) ?? 0;
    case "branch":
      return pct(stats.branches.hit, stats.branches.total) ?? 0;
    case "flags":
      return stats.flags.stale + stats.flags.aging + stats.flags.excluded;
    default: {
      const tier = columnId.startsWith("tier:") ? columnId.slice("tier:".length) : null;
      if (tier === null) return 0;
      return pct(stats.lines.per_tier[tier] ?? 0, stats.lines.total) ?? 0;
    }
  }
}

function renderName(row: Row): ReactNode {
  const Icon = row.kind === "dir" ? Folder : File02;
  const label = row.kind === "dir" ? `${row.node.name}/` : row.node.name;
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon aria-hidden className="size-4 shrink-0 text-quaternary" />
      <span className="truncate">{label}</span>
    </span>
  );
}

// Text color for a pct-class bucket — same vocabulary StatsCard.tsx's
// PCT_COLOR uses (EventEditor.tsx/DataWarningsBanner.tsx's error/warning
// tokens), so the tree's Line %/Branch % cells read consistently with the
// stats card above them.
const PCT_TEXT: Record<PctClass, string> = {
  "pct-high": "text-success-primary",
  "pct-mid": "text-warning-primary",
  "pct-low": "text-error-primary",
  "pct-na": "text-quaternary",
};

// Minibar fill — the `fg-*` layer (not `text-*`) is this codebase's existing
// vocabulary for colored bars/dots (src/topo/nodes.tsx's STATUS_SEGMENT,
// AppShell.tsx's tier/state swatches), same green/yellow/red as PCT_TEXT.
const PCT_BAR: Record<PctClass, string> = {
  "pct-high": "bg-fg-success-primary",
  "pct-mid": "bg-fg-warning-primary",
  "pct-low": "bg-fg-error-primary",
  "pct-na": "bg-fg-quaternary",
};

function PctCell({ p, thresholds }: { p: number | null; thresholds: IndexPayload["thresholds"] }) {
  const cls = pctClass(p, thresholds);
  return (
    <div>
      <span className={cx("font-semibold tabular-nums", PCT_TEXT[cls])}>{fmtPct(p)}</span>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-tertiary">
        <div className={cx("h-full rounded-full", PCT_BAR[cls])} style={{ width: `${p ?? 0}%` }} />
      </div>
    </div>
  );
}

/** A tinted pill whose color comes from `state_colors` (data, per the
 * Global Constraints — never hard-coded), using `color-mix` for the tint so
 * arbitrary CSS color strings (hex OR named, both appear in the data
 * contract's example) work uniformly, same technique the DOM reference
 * uses for its `.flag` pills. */
function FlagPill({ label, color }: { label: string; color: string }) {
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10.5px] font-medium whitespace-nowrap"
      style={{ color, backgroundColor: `color-mix(in srgb, ${color} 16%, transparent)` }}
    >
      {label}
    </span>
  );
}

function FlagsCell({
  flags,
  stateColors,
}: {
  flags: Stats["flags"];
  stateColors: Record<string, string>;
}) {
  if (flags.stale === 0 && flags.aging === 0 && flags.excluded === 0) return null;
  return (
    <div className="flex flex-wrap items-center justify-end gap-1">
      {flags.stale > 0 && (
        <FlagPill label={`${flags.stale} stale`} color={stateColors.stale ?? "currentColor"} />
      )}
      {flags.aging > 0 && (
        <FlagPill label={`${flags.aging} aging`} color={stateColors.aging ?? "currentColor"} />
      )}
      {flags.excluded > 0 && (
        <FlagPill label={`${flags.excluded} excl`} color={stateColors.excluded ?? "currentColor"} />
      )}
    </div>
  );
}

/** Branch %'s under-focus replacement (Task 7 spec §4): "—", no minibar —
 * v4 doesn't store per-run branch contribution (Global Constraints'
 * documented data limitation), so there's nothing to recompute here. Same
 * "na" text color `PCT_TEXT`/`PctCell` already use for a real no-data
 * percentage, just without the bar underneath. */
function NaCell() {
  return <span className="text-quaternary">—</span>;
}

/** `focusedLabel`/`focusedTier` (Task 7): when a context is focused, every
 * row recomputes from `stats.ctx_lines[focusedLabel]` instead of the
 * node's overall hit/total — Lines and Line % (with its minibar, same
 * `PctCell`) show the focused count; the focused context's OWN tier column
 * mirrors that same value, every other tier column reads 0 (a context
 * belongs to exactly one tier, so those columns have nothing of their own
 * to show, but read as a real "0.0%" rather than a blank/na cell — spec-
 * pinned); Branch % becomes `NaCell` ("—", no bar — branch isn't tracked
 * per-run at all). Flags stay node-wide (not context-scoped) either way. */
function renderCellsFor(index: IndexPayload, focusedLabel: string | null, focusedTier?: string) {
  return (row: Row): ReactNode[] => {
    const stats = rowStats(row);

    if (focusedLabel !== null) {
      const hit = stats.ctx_lines[focusedLabel] ?? 0;
      const total = stats.lines.total;
      const focusedPct = pct(hit, total);
      const cells: ReactNode[] = [
        <span key="lines" className="tabular-nums">
          {hit}/{total}
        </span>,
        <PctCell key="line" p={focusedPct} thresholds={index.thresholds} />,
        <NaCell key="branch" />,
      ];
      for (const tier of index.tier_order) {
        cells.push(
          <span key={`tier:${tier}`} className="tabular-nums">
            {fmtPct(tier === focusedTier ? focusedPct : 0)}
          </span>,
        );
      }
      cells.push(<FlagsCell key="flags" flags={stats.flags} stateColors={index.state_colors} />);
      return cells;
    }

    const cells: ReactNode[] = [
      <span key="lines" className="tabular-nums">
        {stats.lines.hit}/{stats.lines.total}
      </span>,
      <PctCell
        key="line"
        p={pct(stats.lines.hit, stats.lines.total)}
        thresholds={index.thresholds}
      />,
      <PctCell
        key="branch"
        p={pct(stats.branches.hit, stats.branches.total)}
        thresholds={index.thresholds}
      />,
    ];
    for (const tier of index.tier_order) {
      const tierPct = pct(stats.lines.per_tier[tier] ?? 0, stats.lines.total);
      cells.push(
        <span key={`tier:${tier}`} className="tabular-nums">
          {fmtPct(tierPct)}
        </span>,
      );
    }
    cells.push(<FlagsCell key="flags" flags={stats.flags} stateColors={index.state_colors} />);
    return cells;
  };
}

function RunsDisclosure({ index }: { index: IndexPayload }) {
  return (
    <Disclosure
      title={`Runs & captures (${index.runs.length})`}
      defaultExpanded={false}
      testId="runs-disclosure"
    >
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="text-left text-xs font-medium tracking-wide text-quaternary uppercase">
              <th className="px-3.5 py-2 font-medium">Run</th>
              <th className="px-3.5 py-2 font-medium">Tier</th>
              <th className="px-3.5 py-2 font-medium">Board</th>
              <th className="px-3.5 py-2 font-medium">Labs</th>
              <th className="px-3.5 py-2 font-medium">Date</th>
              <th className="px-3.5 py-2 font-medium">Tester</th>
              <th className="px-3.5 py-2 font-medium">Ticket</th>
              <th className="px-3.5 py-2 font-medium">Remap</th>
            </tr>
          </thead>
          <tbody>
            {index.runs.map((run) => {
              const tierColor = index.tier_colors[run.tier] ?? "currentColor";
              return (
                <tr
                  key={run.id}
                  data-testid={`run-row-${run.id}`}
                  className="border-t border-secondary text-tertiary"
                >
                  <td className="px-3.5 py-2 font-medium text-primary">{run.label}</td>
                  <td className="px-3.5 py-2">
                    <span
                      className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
                      style={{
                        color: tierColor,
                        backgroundColor: `color-mix(in srgb, ${tierColor} 14%, transparent)`,
                      }}
                    >
                      {index.tier_labels[run.tier] ?? run.tier}
                    </span>
                  </td>
                  <td className="px-3.5 py-2">{run.board}</td>
                  <td className="px-3.5 py-2">{run.labs.length > 0 ? run.labs.join(", ") : "—"}</td>
                  <td className="px-3.5 py-2">{run.captured_at}</td>
                  <td className="px-3.5 py-2">{run.tester?.name ?? "—"}</td>
                  <td className="px-3.5 py-2">{run.ticket ?? "—"}</td>
                  <td className="px-3.5 py-2">{run.dirty_remap ? "✎ remapped" : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Disclosure>
  );
}

export interface DirectoryPageProps {
  index: IndexPayload;
  segments: string[];
}

export function DirectoryPage({ index, segments }: DirectoryPageProps) {
  const [, navigate] = useHashLocation();
  const { focus } = useFocus();
  const node = findNode(index.tree, segments);
  // App.tsx only mounts DirectoryPage once findNode(...) already resolved a
  // DirNode for these segments — this guard is defensive, not a real route.
  if (node === null || !("dirs" in node)) return null;

  // Independently re-resolved against THIS page's own `index` prop (not
  // trusted blindly off `focus`, which is only ever a label string) — same
  // defensive pattern AppShell.tsx uses, so a focus label that doesn't
  // resolve here just renders unfocused instead of crashing.
  const focusedContext = focus ? groupContexts(index).find((c) => c.label === focus) : undefined;

  const columns = buildColumns(index);
  const roots: Row[] = [
    ...node.dirs.map((d) =>
      dirRow(d, segments.length > 0 ? `${segments.join("/")}/${d.name}` : d.name),
    ),
    ...node.files.map((f) => fileRow(f)),
  ];

  function onNavigate(row: Row): void {
    if (row.kind === "dir") navigate(`/coverage/${encodePath(row.path)}`);
    else navigate(`/coverage/${encodePath(row.node.path)}`);
  }

  const isRoot = segments.length === 0;
  const title = isRoot ? index.project_name : `${node.name}/`;
  const scope = isRoot ? "whole repo" : `${segments.join("/")}/`;

  return (
    <AppShell
      crumbs={crumbsFor(index.project_name, segments)}
      title={title}
      meta={
        <>
          <b>{fmtCount(countFiles(node))}</b> covered files · report generated{" "}
          <b>{index.generated_at}</b> · otto {index.otto_version}
        </>
      }
      stats={{
        scope: focusedContext ? `focused: ${focusedContext.label}` : scope,
        title: "Coverage — this node and below",
        rows: focusedContext
          ? focusedTreeRow(index, node.stats, focusedContext)
          : tierRows(index, node.stats),
        thresholds: index.thresholds,
        keyColumnLabel: focusedContext ? "Context" : "Tier",
      }}
    >
      <div
        data-testid="directory-tree"
        className="overflow-hidden rounded-xl border border-secondary shadow-xs"
      >
        <TreeView
          roots={roots}
          getChildren={rowChildren}
          getRowId={rowId}
          columns={columns}
          renderName={renderName}
          renderCells={renderCellsFor(index, focus, focusedContext?.tier)}
          sortValue={rowSortValue}
          onNavigate={onNavigate}
          defaultExpanded
        />
      </div>
      {isRoot && <RunsDisclosure index={index} />}
    </AppShell>
  );
}
