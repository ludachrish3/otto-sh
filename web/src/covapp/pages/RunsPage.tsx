// The covapp runs & contexts page (Task 6 brief). DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/contexts-page.html —
// recreated with React + Tailwind semantic tokens, not the mockup's literal
// CSS. Groups `payload.runs` into `Context`s (contexts.ts) — the common case
// is a multi-host run (one label, several hosts) sharing one row.
//
// "Focus this context" (per-context detail) dispatches the real
// `setFocus(label)` from `focus.ts` (Task 7) — pins the app-bar chip
// (AppShell.tsx), fires a toast, and re-scopes every page's stats card,
// including this one's (below): focused, it shows the same single Context
// row DirectoryPage.tsx/FilePage.tsx do instead of the per-tier matrix.
// Rows themselves (the context list) are unaffected by focus — only the
// stats card up top rescopes, matching `contexts-page.html`'s own
// `renderStats()`/`setFocus()`.
//
// Scalar per-context display fields not carried on `Context` itself (board/
// labs/captured/tester/ticket/note/base commit) are read off
// `ctx.runs[0]` — same one-value assumption contexts.ts documents for
// `tier` (a label spanning members with different boards/labs/etc. is a
// data anomaly, not a case this page tries to reconcile).
import { ChevronRight, SearchMd, Target02 } from "@untitledui/icons";
import type { ReactNode } from "react";
import { useState } from "react";

import { Badge } from "@/components/base/badges/badges";
import { Input } from "@/components/base/input/input";
import { cx } from "@/utils/cx";

import { AppShell } from "../chrome/AppShell";
import { type Context, groupContexts, searchHaystack } from "../contexts";
import { useFocus } from "../focus";
import { encodePath, fmtCount, focusedTreeRow, keyColumnLabel, tierRows } from "../format";
import { fmtPct, PCT_TEXT, pct, pctClass } from "../stats";
import type { IndexPayload } from "../types";

// Pinned column widths (contexts-page.html's `.rhead`/`.rrow` grid) — kept
// as a literal `gridTemplateColumns` string, not a Tailwind arbitrary class,
// same technique ui/TreeView.tsx and ui/CodeView.tsx already use for their
// own pinned-width grids.
const ROW_GRID = "minmax(190px,1.2fr) 92px minmax(120px,1fr) 96px 82px 110px 150px 90px";
const DETAIL_GRID = "250px 215px 230px 1fr";

const STATUS_BADGE: Record<
  Context["status"],
  { label: string; color: "success" | "warning" | "error" }
> = {
  ok: { label: "OK", color: "success" },
  aging: { label: "aging", color: "warning" },
  stale: { label: "stale", color: "error" },
};

function FilterChip({
  active,
  onClick,
  testId,
  dotColor,
  children,
}: {
  active: boolean;
  onClick: () => void;
  testId: string;
  dotColor?: string;
  children: ReactNode;
}) {
  // No vendored pill-toggle fits this shape (a single-select chip row) — a
  // hand-rolled button on tokens, matching contexts-page.html's `.chipbtn`
  // anatomy, per the Task 6 brief.
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      className={cx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-3.5 py-1 text-xs font-medium",
        active
          ? "border-fg-brand-primary_alt bg-brand-primary_alt text-brand-secondary"
          : "border-secondary text-tertiary hover:bg-secondary hover:text-primary",
      )}
    >
      {dotColor && (
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-sm"
          style={{ backgroundColor: dotColor }}
        />
      )}
      {children}
    </button>
  );
}

function TierChip({ tier, index }: { tier: string; index: IndexPayload }) {
  const color = index.tier_colors[tier] ?? "currentColor";
  return (
    <span
      className="inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
      style={{ color, backgroundColor: `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      <span aria-hidden className="size-2 shrink-0 rounded-sm" style={{ backgroundColor: color }} />
      {index.tier_labels[tier] ?? tier}
    </span>
  );
}

function HostPills({ hosts }: { hosts: [string, number][] }) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      {hosts.map(([host], i) => (
        <span
          // `hosts` is a fixed, order-stable per-run array that may legally
          // repeat the same host display twice (two runs on one physical
          // host) — index IS this pill's identity, same rationale
          // FilePage.tsx uses for its branch-pill list.
          // biome-ignore lint/suspicious/noArrayIndexKey: see above
          key={i}
          className="whitespace-nowrap rounded border border-secondary bg-tertiary px-1.5 font-mono text-[10.5px] text-tertiary"
        >
          {host}
        </span>
      ))}
    </div>
  );
}

function ContribCell({
  ctx,
  totalLines,
  tierColor,
}: {
  ctx: Context;
  totalLines: number;
  tierColor: string;
}) {
  const isStale = ctx.status === "stale";
  const width = isStale ? 0 : (pct(ctx.lines, totalLines) ?? 0);
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11.5px] tabular-nums text-tertiary">
        {isStale ? `${ctx.revoked} revoked` : `${ctx.lines} / ${totalLines}`}
      </span>
      <div className="h-1 overflow-hidden rounded-full bg-tertiary">
        <div
          className="h-full rounded-full"
          style={{ width: `${width}%`, backgroundColor: tierColor }}
        />
      </div>
    </div>
  );
}

function StatusCell({ ctx }: { ctx: Context }) {
  const { label, color } = STATUS_BADGE[ctx.status];
  return (
    <div>
      <Badge size="sm" color={color}>
        {label}
      </Badge>
      {ctx.remapped && <div className="mt-1 text-[10.5px] text-quaternary">✎ remapped</div>}
    </div>
  );
}

function ContextDetail({
  ctx,
  index,
  onFocus,
}: {
  ctx: Context;
  index: IndexPayload;
  onFocus: (label: string) => void;
}) {
  const first = ctx.runs[0];
  const tierColor = index.tier_colors[ctx.tier] ?? "currentColor";
  const maxHost = Math.max(1, ...ctx.hosts.map(([, n]) => n));
  const topFiles = ctx.files.slice(0, 5);
  const maxFile = Math.max(1, ...topFiles.map(([, n]) => n));
  const linePct = pct(ctx.lines, index.total_lines);

  return (
    <div
      data-testid={`run-detail-${ctx.label}`}
      className="grid gap-4.5 border-t border-dashed border-secondary bg-secondary px-4 py-3.5"
      style={{ gridTemplateColumns: DETAIL_GRID }}
    >
      <div>
        <h5 className="mb-1.5 text-[10.5px] font-semibold tracking-wide text-quaternary uppercase">
          Capture metadata
        </h5>
        <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          <span className="text-quaternary">Run label</span>
          <span className="font-medium text-secondary">{ctx.label}</span>
          <span className="text-quaternary">Hosts</span>
          <HostPills hosts={ctx.hosts} />
          <span className="text-quaternary">Board</span>
          <span className="font-mono font-normal text-secondary">{first?.board || "—"}</span>
          <span className="text-quaternary">Labs</span>
          <span className="font-medium text-secondary">
            {first && first.labs.length > 0 ? first.labs.join(", ") : "—"}
          </span>
          <span className="text-quaternary">Captured</span>
          <span className="font-medium text-secondary">{first?.captured_at ?? "—"}</span>
          <span className="text-quaternary">Tester</span>
          <span className="font-medium text-secondary">{first?.tester?.name ?? "—"}</span>
          <span className="text-quaternary">Ticket</span>
          <span className="font-medium text-secondary">{first?.ticket ?? "—"}</span>
          <span className="text-quaternary">Base commit</span>
          <span className="font-mono font-normal text-secondary">
            {first ? first.base_commit.slice(0, 12) : "—"}
            {ctx.remapped ? " → HEAD (remapped)" : ""}
          </span>
          <span className="text-quaternary">Note</span>
          <span className="font-medium text-secondary">{first?.note ?? "—"}</span>
        </div>
      </div>

      <div>
        <h5 className="mb-1.5 text-[10.5px] font-semibold tracking-wide text-quaternary uppercase">
          Per-host lines
        </h5>
        <div className="flex flex-col gap-1">
          {ctx.hosts.map(([host, lines], i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: see HostPills
            <div key={i} className="grid grid-cols-[auto_44px_1fr] items-center gap-2 text-xs">
              <span className="whitespace-nowrap rounded border border-secondary bg-tertiary px-1.5 font-mono text-[10.5px] text-tertiary">
                {host}
              </span>
              <span className="text-right tabular-nums text-quaternary">{lines}</span>
              <span className="h-1 overflow-hidden rounded-full bg-tertiary">
                <span
                  className="block h-full rounded-full"
                  style={{ width: `${(100 * lines) / maxHost}%`, backgroundColor: tierColor }}
                />
              </span>
            </div>
          ))}
        </div>
      </div>

      <div>
        <h5 className="mb-1.5 text-[10.5px] font-semibold tracking-wide text-quaternary uppercase">
          Contribution by type
        </h5>
        <table className="w-full border-collapse overflow-hidden rounded-lg border border-secondary bg-primary text-xs">
          <thead>
            <tr className="bg-secondary text-[10.5px] font-medium tracking-wide text-quaternary uppercase">
              <th className="px-3 py-1.5 text-left font-medium">Type</th>
              <th className="px-3 py-1.5 text-right font-medium">Hits</th>
              <th className="px-3 py-1.5 text-right font-medium">%</th>
            </tr>
          </thead>
          <tbody>
            {ctx.status === "stale" ? (
              <tr>
                <td className="px-3 py-1.5 text-left font-medium text-secondary">Line</td>
                <td
                  colSpan={2}
                  className="px-3 py-1.5 text-right font-medium"
                  style={{ color: index.state_colors.stale }}
                >
                  {ctx.revoked} credits revoked — anchor unverifiable
                </td>
              </tr>
            ) : (
              <>
                <tr>
                  <td className="px-3 py-1.5 text-left font-medium text-secondary">Line</td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-tertiary">
                    {ctx.lines}/{index.total_lines}
                  </td>
                  <td
                    className={cx(
                      "px-3 py-1.5 text-right font-semibold tabular-nums",
                      PCT_TEXT[pctClass(linePct, index.thresholds)],
                    )}
                  >
                    {fmtPct(linePct)}
                  </td>
                </tr>
                <tr>
                  <td className="px-3 py-1.5 text-left font-medium text-secondary">Branch</td>
                  <td
                    colSpan={2}
                    data-testid="contrib-branch-na"
                    className="px-3 py-1.5 text-right text-quaternary"
                  >
                    not tracked per-run
                  </td>
                </tr>
                <tr>
                  <td className="px-3 py-1.5 text-left font-medium text-secondary">Decision</td>
                  <td colSpan={2} className="px-3 py-1.5 text-right text-quaternary">
                    not tracked per-run
                  </td>
                </tr>
              </>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex min-w-0 flex-col">
        <h5 className="mb-1.5 text-[10.5px] font-semibold tracking-wide text-quaternary uppercase">
          Top files
        </h5>
        {topFiles.length === 0 ? (
          <p className="text-xs text-quaternary">
            no live credits — every line this capture touched has changed
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            {topFiles.map(([path, count]) => (
              <div
                key={path}
                className="grid grid-cols-[minmax(120px,1fr)_46px_90px] items-center gap-2.5 text-xs"
              >
                <a
                  href={`#/coverage/${encodePath(path)}`}
                  data-testid={`file-link-${path}`}
                  className="truncate font-mono text-secondary hover:text-brand-secondary hover:underline"
                >
                  {path}
                </a>
                <span className="text-right tabular-nums text-quaternary">{count}</span>
                <span className="h-1 overflow-hidden rounded-full bg-tertiary">
                  <span
                    className="block h-full rounded-full"
                    style={{ width: `${(100 * count) / maxFile}%`, backgroundColor: tierColor }}
                  />
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Its own grid row, spanning all 4 columns (`contexts-page.html`'s
          `.focusbtn { grid-column:1/-1 }`) — a sibling of the 4 column
          `<div>`s above, not nested inside the last one, so it visually
          spans the FULL detail width rather than just the Top-files
          column. */}
      <button
        type="button"
        data-testid="focus-context-btn"
        onClick={() => onFocus(ctx.label)}
        className="col-span-full inline-flex w-fit items-center gap-1.5 rounded-lg border
          border-fg-brand-primary_alt bg-brand-primary_alt px-3.5 py-1.5 text-xs font-medium
          text-brand-secondary hover:brightness-105"
      >
        <Target02 aria-hidden className="size-3.5" />
        Focus this context
      </button>
    </div>
  );
}

function ContextRow({
  ctx,
  index,
  expanded,
  onToggle,
  onFocus,
}: {
  ctx: Context;
  index: IndexPayload;
  expanded: boolean;
  onToggle: () => void;
  onFocus: (label: string) => void;
}) {
  const tierColor = index.tier_colors[ctx.tier] ?? "currentColor";
  const first = ctx.runs[0];
  return (
    <div>
      {/* A real <button>, not a clickable <div> — gets keyboard activation
          (Enter/Space) and the right a11y semantics for free, same
          established pattern as ui/Disclosure.tsx's trigger. `grid` display
          on a button works fine and keeps the pinned column layout. */}
      <button
        type="button"
        data-testid={`run-row-${ctx.label}`}
        onClick={onToggle}
        aria-expanded={expanded}
        className={cx(
          "grid w-full cursor-pointer items-center gap-x-2.5 border-t border-secondary px-4 py-2.5 text-left",
          "text-[12.5px] first:border-t-0 hover:bg-secondary",
          expanded && "bg-secondary",
        )}
        style={{ gridTemplateColumns: ROW_GRID }}
      >
        <div className="flex min-w-0 items-center gap-2 font-medium text-primary">
          <ChevronRight
            aria-hidden
            className={cx(
              "size-3.5 shrink-0 text-quaternary transition-transform",
              expanded && "rotate-90",
            )}
          />
          <span className="truncate">{ctx.label}</span>
        </div>
        <TierChip tier={ctx.tier} index={index} />
        <HostPills hosts={ctx.hosts} />
        <span className="truncate text-tertiary">{first?.board || "—"}</span>
        <span className="truncate text-tertiary">
          {first && first.labs.length > 0 ? first.labs.join(", ") : "—"}
        </span>
        <span className="text-tertiary tabular-nums">{first?.captured_at ?? "—"}</span>
        <ContribCell ctx={ctx} totalLines={index.total_lines} tierColor={tierColor} />
        <StatusCell ctx={ctx} />
      </button>
      {expanded && <ContextDetail ctx={ctx} index={index} onFocus={onFocus} />}
    </div>
  );
}

export interface RunsPageProps {
  index: IndexPayload;
}

export function RunsPage({ index }: RunsPageProps) {
  const [tier, setTier] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [expandedLabel, setExpandedLabel] = useState<string | null>(null);
  const { focus, setFocus } = useFocus();

  const contexts = groupContexts(index);
  const distinctHosts = new Set(contexts.flatMap((ctx) => ctx.hosts.map(([host]) => host))).size;
  // Independently re-resolved against THIS page's own `index` prop, same
  // defensive pattern every other focus-aware page uses.
  const focusedContext = focus ? contexts.find((ctx) => ctx.label === focus) : undefined;

  const q = query.trim().toLowerCase();
  const visible = contexts.filter((ctx) => {
    if (tier !== "all" && ctx.tier !== tier) return false;
    if (q !== "" && !searchHaystack(ctx).includes(q)) return false;
    return true;
  });

  return (
    <AppShell
      crumbs={[{ label: index.project_name, href: "#/coverage" }, { label: "runs" }]}
      title="Runs & contexts"
      meta={
        <>
          <b>{fmtCount(contexts.length)}</b> contexts · <b>{fmtCount(distinctHosts)}</b> hosts ·
          report generated <b>{index.generated_at}</b>
        </>
      }
      stats={{
        scope: focusedContext ? `focused: ${focusedContext.label}` : "all contexts",
        title: "Coverage — whole repo",
        rows: focusedContext
          ? focusedTreeRow(index, index.tree.stats, focusedContext)
          : tierRows(index, index.tree.stats),
        thresholds: index.thresholds,
        keyColumnLabel: keyColumnLabel({ ticket: false, context: Boolean(focusedContext) }),
      }}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <FilterChip active={tier === "all"} onClick={() => setTier("all")} testId="tier-chip-all">
          All tiers
        </FilterChip>
        {index.tier_order.map((t) => (
          <FilterChip
            key={t}
            active={tier === t}
            onClick={() => setTier(t)}
            testId={`tier-chip-${t}`}
            dotColor={index.tier_colors[t]}
          >
            {index.tier_labels[t] ?? t}
          </FilterChip>
        ))}
        {/* Untitled UI's `Input` doesn't forward a `data-testid` it's given
            onto the `<input>` it renders internally (see
            src/pages/SubjectPage.tsx's `log-filter-*` for the same gap) —
            this control's testid contract is on the WRAPPING element
            instead. */}
        <span data-testid="runs-search" className="ml-auto">
          <Input
            aria-label="Filter by run, host, ticket"
            size="sm"
            icon={SearchMd}
            placeholder="Filter by run, host, ticket…"
            value={query}
            onChange={setQuery}
          />
        </span>
      </div>

      <div
        data-testid="runs-card"
        className="overflow-hidden rounded-xl border border-secondary shadow-xs"
      >
        <div
          className="grid gap-x-2.5 border-b border-secondary bg-secondary px-4 py-2.5 text-[11px]
            font-medium tracking-wide text-quaternary uppercase"
          style={{ gridTemplateColumns: ROW_GRID }}
        >
          <div>Run</div>
          <div>Tier</div>
          <div>Host</div>
          <div>Board</div>
          <div>Labs</div>
          <div>Date</div>
          <div>Lines contributed</div>
          <div>Status</div>
        </div>
        {visible.map((ctx) => (
          <ContextRow
            key={ctx.label}
            ctx={ctx}
            index={index}
            expanded={expandedLabel === ctx.label}
            onToggle={() => setExpandedLabel((prev) => (prev === ctx.label ? null : ctx.label))}
            onFocus={setFocus}
          />
        ))}
      </div>
    </AppShell>
  );
}
