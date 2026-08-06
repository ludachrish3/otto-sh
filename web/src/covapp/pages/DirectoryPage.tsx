// The covapp directory page. DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/directory-page.html —
// recreated with React + Tailwind semantic tokens + ui/TreeView, not the
// mockup's literal CSS. One deliberate divergence from that mockup: its
// floating coverage-key panel is gone from this page — the key lives in
// AppShell's ⋮ menu instead.

import { File02, Folder } from "@untitledui/icons";
import { type ReactNode, useEffect, useState } from "react";

import { Disclosure } from "@/ui/Disclosure";
import { type TreeColumn, TreeView } from "@/ui/TreeView";

import { AppShell } from "../chrome/AppShell";
import { PctCell } from "../chrome/PctCell";
import { groupContexts } from "../contexts";
import { loadTicketChunk, StampMismatchError } from "../data";
import { useFocus, useHashLocation } from "../focus";
import {
  crumbsFor,
  encodePath,
  fmtCount,
  focusedTreeRow,
  keyColumnLabel,
  tierRows,
  withHideAssertedSuffix,
} from "../format";
import { findNode, fmtPct, pct } from "../stats";
import { scopeTreeToTicket, ticketChunkToFileLines, ticketTreeRow } from "../tickets";
import type { DirNode, FileNode, IndexPayload, Stats, TicketChunk } from "../types";
import { GuardScreen } from "./GuardScreen";

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
  stateColors: IndexPayload["state_colors"];
}) {
  if (flags.stale === 0 && flags.aging === 0 && flags.excluded === 0) return null;
  return (
    <div className="flex flex-wrap items-center justify-end gap-1">
      {flags.stale > 0 && <FlagPill label={`${flags.stale} stale`} color={stateColors.stale} />}
      {flags.aging > 0 && <FlagPill label={`${flags.aging} aging`} color={stateColors.aging} />}
      {flags.excluded > 0 && (
        <FlagPill label={`${flags.excluded} excl`} color={stateColors.excluded} />
      )}
    </div>
  );
}

/** Branch %'s under-focus replacement (spec §4): "—", no minibar —
 * v4 doesn't store per-run branch contribution (Global Constraints'
 * documented data limitation), so there's nothing to recompute here. Same
 * "na" text color `PCT_TEXT`/`PctCell` already use for a real no-data
 * percentage, just without the bar underneath. */
function NaCell() {
  return <span className="text-quaternary">—</span>;
}

/** `focusedLabel`/`focusedTier`: when a context is focused, every
 * row recomputes from `stats.ctx_lines[focusedLabel]` instead of the
 * node's overall hit/total — Lines and Line % (with its minibar, same
 * `PctCell`) show the focused count; the focused context's OWN tier column
 * mirrors that same value, every other tier column reads 0 (a context
 * belongs to exactly one tier, so those columns have nothing of their own
 * to show, but read as a real "0.0%" rather than a blank/na cell — spec-
 * pinned); Branch % becomes `NaCell` ("—", no bar — branch isn't tracked
 * per-run at all). Flags stay node-wide (not context-scoped) either way.
 *
 * `ticketActive`: when a ticket is pinned and NO context is focused, `row`'s
 * `stats` is already the TICKET-SCOPED one (the caller builds `roots` from
 * the scoped tree, not the original) — Lines/Line % read it directly, same
 * shape as the plain unfocused cells below. Branch % and every per-tier
 * column become `NaCell` ("no data") rather than dividing a whole-file
 * branch/per-tier count by the new, much smaller ticket-scoped total:
 * neither is tracked per-ticket at all (a `TicketChunk` carries only
 * owned/covered LINE counts), so showing a real-looking percentage computed
 * from mismatched numerator/denominator sources would read as a correctness
 * bug, not an approximation — the same reasoning `focusedLabel`'s branch
 * column above already applies for the identical "not tracked at this
 * granularity" reason. When a context IS ALSO focused (`focusedLabel !==
 * null`) while a ticket is pinned, composing the two is declined the same
 * honest way — the `ticketActive` check nested inside the `focusedLabel`
 * branch below returns all-`NaCell` rather than computing anything; see its
 * own comment for why (`stats.ctx_lines` is a whole-file numerator with no
 * per-line ticket+run cross-tab to restrict it to the ticket's owned lines,
 * so dividing it by the ticket-scoped `stats.lines.total` would read as a
 * correctness bug, not an approximation).
 *
 * `hideAsserted` (default `false` — byte-identical when omitted):
 * subtracts `stats.lines.asserted_only`/`asserted_per_tier[tier]` from the
 * plain (no context, no per-run numerator) Lines/Line %/tier-% cells —
 * mirroring `tierRows` (format.ts) exactly, since these are the same
 * rollup fields on the same `Stats` bag (ticket-scoped or not — the ticket
 * scoping helpers in tickets.ts already carry the scoped subset of both
 * fields). The context-focused branch's `stats.ctx_lines` numerator is
 * per-run evidence, never override-sourced (same reasoning `ticketFileRow`'s
 * `ctx` branch documents), so it's untouched either way. */
function renderCellsFor(
  index: IndexPayload,
  focusedLabel: string | null,
  focusedTier: string | undefined,
  ticketActive: boolean,
  hideAsserted = false,
) {
  return (row: Row): ReactNode[] => {
    const stats = rowStats(row);

    if (focusedLabel !== null) {
      // When a ticket is ALSO active, `stats.lines.total` is the
      // TICKET-scoped denominator (the caller builds `roots` from the
      // scoped tree), but `stats.ctx_lines` is still a whole-file
      // numerator — no per-line ticket+run cross-tab exists at tree
      // granularity to restrict it to the ticket's owned lines. Dividing
      // the two produced a plausible-looking but out-of-range percentage
      // (a real fixture: 10 whole-file ctx hits over a 3-line ticket scope
      // read "333.3%") — decline honestly instead, the same "not tracked
      // at this granularity" treatment Branch % already gets on its own,
      // one line below. Contrast FilePage's `ticketFileRow`, which
      // recomputes this EXACTLY under the same compose because it already
      // has one file's full per-line data — this decline is specific to
      // the tree's coarser data, not a general "compose is unsupported"
      // rule.
      if (ticketActive) {
        const cells: ReactNode[] = [
          <NaCell key="lines" />,
          <NaCell key="line" />,
          <NaCell key="branch" />,
        ];
        for (const tier of index.tier_order) {
          cells.push(<NaCell key={`tier:${tier}`} />);
        }
        cells.push(<FlagsCell key="flags" flags={stats.flags} stateColors={index.state_colors} />);
        return cells;
      }

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

    const hit = stats.lines.hit - (hideAsserted ? stats.lines.asserted_only : 0);

    if (ticketActive) {
      const cells: ReactNode[] = [
        <span key="lines" className="tabular-nums">
          {hit}/{stats.lines.total}
        </span>,
        <PctCell key="line" p={pct(hit, stats.lines.total)} thresholds={index.thresholds} />,
        <NaCell key="branch" />,
      ];
      for (const tier of index.tier_order) {
        cells.push(<NaCell key={`tier:${tier}`} />);
      }
      cells.push(<FlagsCell key="flags" flags={stats.flags} stateColors={index.state_colors} />);
      return cells;
    }

    const cells: ReactNode[] = [
      <span key="lines" className="tabular-nums">
        {hit}/{stats.lines.total}
      </span>,
      <PctCell key="line" p={pct(hit, stats.lines.total)} thresholds={index.thresholds} />,
      <PctCell
        key="branch"
        p={pct(stats.branches.hit, stats.branches.total)}
        thresholds={index.thresholds}
      />,
    ];
    for (const tier of index.tier_order) {
      const tierHit =
        (stats.lines.per_tier[tier] ?? 0) -
        (hideAsserted ? (stats.lines.asserted_per_tier[tier] ?? 0) : 0);
      const tierPct = pct(tierHit, stats.lines.total);
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

/** All-zero `Stats` for the "ticket touched nothing in this subtree" case
 * (`scopeTreeToTicket` returned `null`) — a real, render-safe `DirNode`
 * stand-in (0 dirs, 0 files) rather than special-casing `null` through every
 * downstream read (`roots`, `countFiles`, the StatsCard row). */
const EMPTY_SCOPE_STATS: Stats = {
  lines: { total: 0, hit: 0, per_tier: {}, asserted_per_tier: {}, asserted_only: 0 },
  branches: { total: 0, hit: 0, per_tier: {} },
  flags: { stale: 0, aging: 0, excluded: 0 },
  ctx_lines: {},
};

type TicketChunkState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; reason: "stamp" | "other" }
  | { status: "ready"; chunk: TicketChunk };

export function DirectoryPage({ index, segments }: DirectoryPageProps) {
  const [, navigate] = useHashLocation();
  const { focus, ticket, hideAsserted } = useFocus();

  // Ticket context: resolved/loaded here, unconditionally, BEFORE
  // the `node === null` guard below — same reasoning as `useFocus()` itself
  // (Rules of Hooks: this component's own guard can return early on some
  // renders but not others, so every hook must run regardless of whether
  // `node` resolves this time).
  const ticketSummary = ticket ? index.tickets.find((t) => t.id === ticket) : undefined;
  const [ticketChunkState, setTicketChunkState] = useState<TicketChunkState>({ status: "idle" });

  // Keyed on the CHUNK NAME, not the `ticketSummary` object (a fresh
  // reference every render via `.find`) — re-fetches only when the pinned
  // ticket actually changes, and `loadTicketChunk`'s own cache (data.ts)
  // means re-mounting on a different directory route while the SAME ticket
  // stays pinned costs no extra request either.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally keyed on the chunk name alone (see comment above)
  useEffect(() => {
    if (!ticketSummary) {
      setTicketChunkState({ status: "idle" });
      return;
    }
    let cancelled = false;
    setTicketChunkState({ status: "loading" });
    loadTicketChunk(ticketSummary.chunk)
      .then((chunk) => {
        if (!cancelled) setTicketChunkState({ status: "ready", chunk });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setTicketChunkState({
            status: "error",
            reason: err instanceof StampMismatchError ? "stamp" : "other",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [ticketSummary?.chunk]);

  // A stamp mismatch means the report on disk changed since the index
  // loaded (design §5) — the WHOLE report is stale, not just the ticket
  // scope, so this guard-screens the entire page, mirroring FilePage.tsx/
  // TicketsPage.tsx's contract for the identical error.
  if (ticketChunkState.status === "error" && ticketChunkState.reason === "stamp") {
    return <GuardScreen reason="report changed on disk" />;
  }

  const node = findNode(index.tree, segments);
  // App.tsx only mounts DirectoryPage once findNode(...) already resolved a
  // DirNode for these segments — this guard is defensive, not a real route.
  if (node === null || !("dirs" in node)) return null;

  // Independently re-resolved against THIS page's own `index` prop (not
  // trusted blindly off `focus`, which is only ever a label string) — same
  // defensive pattern AppShell.tsx uses, so a focus label that doesn't
  // resolve here just renders unfocused instead of crashing.
  const focusedContext = focus ? groupContexts(index).find((c) => c.label === focus) : undefined;

  const ticketLoading = ticketSummary !== undefined && ticketChunkState.status === "loading";
  const ticketOtherError =
    ticketSummary !== undefined &&
    ticketChunkState.status === "error" &&
    ticketChunkState.reason === "other";
  const ticketReady = ticketSummary !== undefined && ticketChunkState.status === "ready";

  // Denominator scoping (spec §6.3): keeps only the files/dirs the
  // pinned ticket touched, recomputing each one's line total/hit against the
  // ticket's OWN lines — never a passthrough of the whole-repo numbers. A
  // `null` scopeTreeToTicket result (the ticket touched nothing in THIS
  // subtree) degrades to the all-zero empty node rather than falling back
  // to the unscoped tree — falling back would silently show "everything",
  // exactly the failure mode a denominator filter must never produce.
  let scopedNode: DirNode | null = null;
  if (ticketReady && ticketChunkState.status === "ready") {
    const { lines, hits, tiers, asserted, assertedOnly } = ticketChunkToFileLines(
      ticketChunkState.chunk,
    );
    scopedNode = scopeTreeToTicket(node, lines, hits, { tiers, asserted, assertedOnly });
  }
  const effectiveNode: DirNode = ticketReady
    ? (scopedNode ?? { name: node.name, dirs: [], files: [], stats: EMPTY_SCOPE_STATS })
    : node;
  const hiddenCount = ticketReady ? countFiles(node) - countFiles(effectiveNode) : 0;

  const columns = buildColumns(index);
  const roots: Row[] = [
    ...effectiveNode.dirs.map((d) =>
      dirRow(d, segments.length > 0 ? `${segments.join("/")}/${d.name}` : d.name),
    ),
    ...effectiveNode.files.map((f) => fileRow(f)),
  ];

  function onNavigate(row: Row): void {
    if (row.kind === "dir") navigate(`/coverage/${encodePath(row.path)}`);
    else navigate(`/coverage/${encodePath(row.node.path)}`);
  }

  const isRoot = segments.length === 0;
  const title = isRoot ? index.project_name : `${node.name}/`;
  const scope = isRoot ? "whole repo" : `${segments.join("/")}/`;
  const scopeWithTicket = ticketReady ? `ticket: ${ticketSummary?.id}` : scope;

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
        scope: withHideAssertedSuffix(
          focusedContext
            ? ticketReady
              ? `focused: ${focusedContext.label} · ticket: ${ticketSummary?.id}`
              : `focused: ${focusedContext.label}`
            : scopeWithTicket,
          hideAsserted,
        ),
        title: "Coverage — this node and below",
        // When BOTH a context and a ticket are active, `ticketTreeRow(...,
        // focusedContext)` (not `focusedTreeRow`) — its `ctx` argument
        // makes it decline the Line cell honestly (see its own doc
        // comment) instead of dividing a whole-file ctx numerator by the
        // ticket-scoped denominator.
        rows: focusedContext
          ? ticketReady
            ? ticketTreeRow(index, effectiveNode, ticketSummary?.id ?? "", focusedContext)
            : focusedTreeRow(index, effectiveNode.stats, focusedContext)
          : ticketReady
            ? ticketTreeRow(index, effectiveNode, ticketSummary?.id ?? "", undefined, hideAsserted)
            : tierRows(index, node.stats, hideAsserted),
        thresholds: index.thresholds,
        keyColumnLabel: keyColumnLabel({ ticket: ticketReady, context: Boolean(focusedContext) }),
      }}
    >
      <div
        data-testid="directory-tree"
        className="overflow-hidden rounded-xl border border-secondary shadow-xs"
      >
        {ticketLoading ? (
          <div data-testid="ticket-scope-loading" className="p-8 text-center text-sm text-tertiary">
            Loading ticket scope…
          </div>
        ) : ticketOtherError ? (
          <div
            data-testid="ticket-scope-error"
            className="p-8 text-center text-sm text-error-primary"
          >
            Failed to load this ticket's scope.
          </div>
        ) : (
          <TreeView
            roots={roots}
            getChildren={rowChildren}
            getRowId={rowId}
            columns={columns}
            renderName={renderName}
            renderCells={renderCellsFor(
              index,
              focus,
              focusedContext?.tier,
              ticketReady,
              hideAsserted,
            )}
            sortValue={rowSortValue}
            onNavigate={onNavigate}
            defaultExpanded
          />
        )}
      </div>
      {ticketReady && (
        // Never silent (spec's standing requirement): a denominator filter
        // that quietly removes files must always say so, in one plain row —
        // shown even when `hiddenCount` is 0, so "a ticket is pinned but
        // hid nothing here" is just as visible as "it hid 142 files".
        // Singular/plural "file"/"files": "1 files hidden" has no singular
        // form. "1 ticket pinned" needs no such handling — exactly one
        // ticket can ever be pinned at a time.
        <p data-testid="ticket-scope-banner" className="mt-2 text-xs text-quaternary">
          {fmtCount(hiddenCount)} file{hiddenCount === 1 ? "" : "s"} hidden · 1 ticket pinned
        </p>
      )}
      {isRoot && <RunsDisclosure index={index} />}
    </AppShell>
  );
}
