// The covapp tickets page (Task 10 brief). Lists per-ticket coverage
// rollups (`IndexPayload.tickets` — commit-message attribution, Task 9),
// sorted worst-covered-first by default so the least-tested owned work
// floats to the top regardless of ticket age. `#/tickets` sits outside the
// `#/coverage/...` namespace deliberately (App.tsx) so it can never collide
// with a real directory path.
//
// Rows carry no missing-line detail of their own — that's deferred to each
// ticket's own chunk (`cov_data/tickets/<chunk>.js`), loaded lazily on
// expand via `loadTicketChunk` (data.ts), the exact classic-script
// injection + `window.__OTTO_COV_TICKET__` callback mechanism
// `loadFileChunk` already uses for per-file chunks — required because this
// SPA must run from `file://`, where neither ES module imports nor `fetch`
// work.
//
// A ticket's line-ownership set is NOT a partition of the repo: one commit
// can name several tickets, so two tickets can both own the same line
// (design §2 — the normal case, not an edge case). The StatsCard above
// reads `index.tickets_totals` (spa_data.py's `_build_ticket_summaries`,
// fix round 1) — a DEDUPED repo-truth total computed server-side, where a
// shared line counts once — never a sum of the per-ticket rows below,
// which deliberately DO attribute a shared line to every ticket that names
// it. That's exactly why the rows can overlap and not sum to the card: the
// caption under the table says so explicitly rather than leaving it
// implied.
import { ChevronRight, SearchMd } from "@untitledui/icons";
import { useEffect, useState } from "react";

import { Input } from "@/components/base/input/input";
import { cx } from "@/utils/cx";

import { AppShell } from "../chrome/AppShell";
import type { TierStatRow } from "../chrome/StatsCard";
import { loadTicketChunk, StampMismatchError } from "../data";
import { encodePath, fmtCount } from "../format";
import type { IndexPayload, TicketChunk, TicketSummary } from "../types";
import { GuardScreen } from "./GuardScreen";

type SortKey = "id" | "owned" | "covered" | "uncovered" | `tier:${string}`;

// Pinned widths for the fixed columns (toggle, id, owned, covered,
// uncovered) — one more `72px` is appended per tier (buildRowGrid, below),
// same literal-`gridTemplateColumns`-string technique RunsPage.tsx's
// ROW_GRID uses.
const FIXED_COLUMNS = "28px minmax(160px,1.2fr) 84px 84px 96px";

function buildRowGrid(tierOrder: string[]): string {
  return [FIXED_COLUMNS, ...tierOrder.map(() => "72px")].join(" ");
}

/** Aggregate StatsCard rows scoped to "all attributed lines" (design
 * §6.1) — reads the DEDUPED `index.tickets_totals`, sharing ONE
 * denominator (`tickets_totals.owned`) across every tier row, exactly the
 * shape `format.ts`'s `tierRows` uses for the tree-wide card elsewhere in
 * covapp. Branch/decision are always "no data" — `tickets_totals` carries
 * line counts only (no per-ticket branch data exists to dedupe). */
function ticketStatsRows(index: IndexPayload): TierStatRow[] {
  const totals = index.tickets_totals;
  const rows: TierStatRow[] = index.tier_order.map((tier) => ({
    key: tier,
    label: index.tier_labels[tier] ?? tier,
    dotColor: index.tier_colors[tier],
    line: [totals.per_tier[tier] ?? 0, totals.owned],
    branch: null,
    decision: null,
  }));
  rows.push({
    key: "all",
    label: "All tiers",
    line: [totals.covered, totals.owned],
    branch: null,
    decision: null,
  });
  return rows;
}

/** Every column's sort value, including the per-tier ones (`tier:<tier>`,
 * spec §6.1: "All columns sortable") — reads `TicketSummary.per_tier`,
 * which (unlike `tickets_totals`) is intentionally NOT deduped; sorting by
 * a tier column ranks tickets by their OWN hit count for that tier. */
function sortValue(ticket: TicketSummary, key: SortKey): number | string {
  if (key === "id") return ticket.id;
  if (key === "owned" || key === "covered" || key === "uncovered") return ticket[key];
  return ticket.per_tier[key.slice("tier:".length)] ?? 0;
}

function sortTickets(tickets: TicketSummary[], key: SortKey, dir: "asc" | "desc"): TicketSummary[] {
  return [...tickets].sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    const cmp = typeof va === "string" ? va.localeCompare(vb as string) : va - (vb as number);
    return dir === "asc" ? cmp : -cmp;
  });
}

/** "12" for a single line, "12-15" for an inclusive range — exported for
 * direct unit testing, same pattern FilePage.tsx's `rowClassFor` uses. */
export function fmtLineRange([start, end]: [number, number]): string {
  return start === end ? String(start) : `${start}-${end}`;
}

/** The two synthetic rows (task-14 brief): `(no ticket)` for a commit that
 * named no ticket, `(uncommitted)` for a working-tree line never
 * committed. Used only for the subtle visual de-emphasis below — they are
 * otherwise ordinary `TicketSummary` rows (never `url`-linked, never
 * excluded from sort or search) all the way from `reporter.py` through
 * this page. */
const SENTINEL_TICKET_IDS = new Set(["(no ticket)", "(uncommitted)"]);

/** A ticket with a `url` renders as a link; one without renders as plain
 * text (task-10 brief, verbatim). Both variants carry the SAME
 * `data-testid="ticket-id"` — the id text itself is what every consumer
 * (sort assertions, search assertions) reads, independent of which variant
 * rendered. A synthetic sentinel id (task-14 brief) renders italic/muted
 * instead of the usual bold-primary treatment — a purely cosmetic hint that
 * this row isn't a real ticket, never a behavioral special case: it still
 * sorts, searches, and expands exactly like any other row. */
function TicketIdCell({ ticket }: { ticket: TicketSummary }) {
  const isSentinel = SENTINEL_TICKET_IDS.has(ticket.id);
  const className = cx(
    "truncate font-mono text-sm",
    isSentinel ? "text-tertiary italic" : "font-medium text-primary",
  );
  if (ticket.url) {
    return (
      <a
        href={ticket.url}
        data-testid="ticket-id"
        className={cx(className, "hover:text-brand-secondary hover:underline")}
      >
        {ticket.id}
      </a>
    );
  }
  return (
    <span data-testid="ticket-id" className={className}>
      {ticket.id}
    </span>
  );
}

/** One per-tier column's cell — mirrors `FilePage.tsx`'s `HitCell` glyph
 * convention (muted "·" for zero, a plain tabular-nums count otherwise),
 * recreated locally since `HitCell` isn't exported from that page. Each
 * tier gets its OWN column here (spec §6.1's mockup: "unit / system /
 * manual" as separate columns), not one blended cell — that's also what
 * makes per-tier sorting (`sortValue`, above) a well-defined single scalar
 * per column instead of an ambiguous multi-value one. */
function TierHitCell({ value }: { value: number }) {
  if (value === 0) {
    return (
      <span aria-hidden className="text-quaternary opacity-45 tabular-nums">
        ·
      </span>
    );
  }
  return <span className="tabular-nums text-tertiary">{value}</span>;
}

type ChunkState =
  | { status: "loading" }
  | { status: "error"; reason: "stamp" | "other" }
  | { status: "ready"; chunk: TicketChunk };

function TicketDetail({ state }: { state: ChunkState }) {
  if (state.status === "loading") {
    return (
      <div
        data-testid="ticket-detail-loading"
        className="border-t border-dashed border-secondary bg-secondary px-4 py-3 text-xs text-tertiary"
      >
        Loading missing lines…
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div
        data-testid="ticket-detail-error"
        className="border-t border-dashed border-secondary bg-secondary px-4 py-3 text-xs text-error-primary"
      >
        Failed to load this ticket's missing-line detail.
      </div>
    );
  }
  const { chunk } = state;
  return (
    <div
      data-testid="ticket-detail"
      className="border-t border-dashed border-secondary bg-secondary px-4 py-3"
    >
      <h5 className="mb-1.5 text-[10.5px] font-semibold tracking-wide text-quaternary uppercase">
        Missing lines by file
      </h5>
      <div className="flex flex-col gap-1.5">
        {chunk.files.map((file) => (
          <div
            key={file.path}
            className="grid grid-cols-[minmax(160px,1fr)_2fr] items-start gap-2.5 text-xs"
          >
            <span className="truncate font-mono text-secondary">{file.path}</span>
            {file.missing.length === 0 ? (
              <span className="text-quaternary">fully covered</span>
            ) : (
              <div className="flex flex-wrap gap-1">
                {file.missing.map((range) => (
                  // Each range links into the code (spec §6.1/§12.1): the
                  // file page's `?lines=A-B` (bare `?lines=A` for a single
                  // line, same shape `fmtLineRange` already produces)
                  // consumer scrolls to and highlights the span
                  // (FilePage.tsx's `parseLinesRange`, as committed — read,
                  // not edited, per this round's instructions).
                  <a
                    key={`${range[0]}-${range[1]}`}
                    href={`#/coverage/${encodePath(file.path)}?lines=${fmtLineRange(range)}`}
                    data-testid="missing-range-link"
                    className="rounded border border-secondary bg-primary px-1.5 font-mono text-[10.5px]
                      text-tertiary hover:border-fg-brand-primary_alt hover:text-brand-secondary"
                  >
                    {fmtLineRange(range)}
                  </a>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: "asc" | "desc";
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "flex items-center gap-1 text-left font-medium hover:text-primary",
        active ? "text-primary" : "text-quaternary",
      )}
    >
      {label}
      {active && (
        <ChevronRight
          aria-hidden
          className={cx("size-3", dir === "asc" ? "-rotate-90" : "rotate-90")}
        />
      )}
    </button>
  );
}

function TicketRow({
  ticket,
  index,
  expanded,
  chunkState,
  onToggle,
}: {
  ticket: TicketSummary;
  index: IndexPayload;
  expanded: boolean;
  chunkState: ChunkState | undefined;
  onToggle: () => void;
}) {
  return (
    <div>
      <div
        data-testid="ticket-row"
        className="grid items-center gap-x-2.5 border-t border-secondary px-4 py-2.5 text-[12.5px] first:border-t-0"
        style={{ gridTemplateColumns: buildRowGrid(index.tier_order) }}
      >
        <button
          type="button"
          data-testid={`ticket-toggle-${ticket.id}`}
          aria-expanded={expanded}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${ticket.id}`}
          onClick={onToggle}
          className="flex size-5 shrink-0 items-center justify-center rounded text-quaternary
            outline-none hover:bg-tertiary hover:text-primary"
        >
          <ChevronRight
            aria-hidden
            className={cx("size-3.5 transition-transform", expanded && "rotate-90")}
          />
        </button>
        <TicketIdCell ticket={ticket} />
        <span className="tabular-nums text-tertiary">{fmtCount(ticket.owned)}</span>
        <span className="tabular-nums text-tertiary">{fmtCount(ticket.covered)}</span>
        <span className="font-semibold tabular-nums text-primary">
          {fmtCount(ticket.uncovered)}
        </span>
        {index.tier_order.map((tier) => (
          <TierHitCell key={tier} value={ticket.per_tier[tier] ?? 0} />
        ))}
      </div>
      {expanded && <TicketDetail state={chunkState ?? { status: "loading" }} />}
    </div>
  );
}

export interface TicketsPageProps {
  index: IndexPayload;
}

export function TicketsPage({ index }: TicketsPageProps) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("uncovered");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [chunks, setChunks] = useState<Record<string, ChunkState>>({});

  const tickets = index.tickets;

  // Loads the expanded ticket's chunk exactly once per chunk id, keyed off
  // WHICH ticket is expanded — re-running this every time `chunks` itself
  // changes would re-request the chunk it just finished caching.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see comment above — deliberately keyed on expandedId alone
  useEffect(() => {
    if (expandedId === null) return;
    const ticket = tickets.find((t) => t.id === expandedId);
    if (!ticket || chunks[ticket.chunk]) return;
    let cancelled = false;
    setChunks((prev) => ({ ...prev, [ticket.chunk]: { status: "loading" } }));
    loadTicketChunk(ticket.chunk)
      .then((chunk) => {
        if (!cancelled) {
          setChunks((prev) => ({ ...prev, [ticket.chunk]: { status: "ready", chunk } }));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setChunks((prev) => ({
            ...prev,
            [ticket.chunk]: {
              status: "error",
              reason: err instanceof StampMismatchError ? "stamp" : "other",
            },
          }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [expandedId]);

  function onToggle(id: string) {
    setExpandedId((prev) => (prev === id ? null : id));
  }

  function onSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "id" ? "asc" : "desc");
    }
  }

  const q = query.trim().toLowerCase();
  const visible = sortTickets(
    tickets.filter((t) => q === "" || t.id.toLowerCase().includes(q)),
    sortKey,
    sortDir,
  );

  // A stamp mismatch means the report on disk changed since the index
  // loaded (design §5) — the WHOLE report is stale, not just this one
  // ticket's chunk, so this guard-screens the entire page exactly like
  // FilePage.tsx does on the same error, rather than leaving every other
  // row looking normal beside one broken one.
  const stampMismatch = Object.values(chunks).some(
    (c) => c.status === "error" && c.reason === "stamp",
  );
  if (stampMismatch) {
    return <GuardScreen reason="report changed on disk" />;
  }

  return (
    <AppShell
      crumbs={[{ label: index.project_name, href: "#/coverage" }, { label: "tickets" }]}
      title="Tickets"
      meta={
        <>
          <b>{fmtCount(tickets.length)}</b> tickets attributed · report generated{" "}
          <b>{index.generated_at}</b>
        </>
      }
      stats={
        tickets.length > 0
          ? {
              scope: "all attributed lines",
              title: "Coverage — attributed to a ticket",
              rows: ticketStatsRows(index),
              thresholds: index.thresholds,
            }
          : null
      }
    >
      {tickets.length === 0 ? (
        <div data-testid="tickets-empty" className="p-8 text-center text-sm text-tertiary">
          No ticket data attributed to any commit yet.
        </div>
      ) : (
        <>
          <div className="mb-3 flex items-center gap-2">
            <span data-testid="tickets-search" className="ml-auto">
              <Input
                aria-label="Search tickets by id"
                size="sm"
                icon={SearchMd}
                placeholder="Search tickets…"
                value={query}
                onChange={setQuery}
              />
            </span>
          </div>

          <div
            data-testid="tickets-card"
            className="overflow-hidden rounded-xl border border-secondary shadow-xs"
          >
            <div
              className="grid items-center gap-x-2.5 border-b border-secondary bg-secondary px-4 py-2.5
                text-[11px] font-medium tracking-wide text-quaternary uppercase"
              style={{ gridTemplateColumns: buildRowGrid(index.tier_order) }}
            >
              <span aria-hidden />
              <SortHeader
                label="Ticket"
                active={sortKey === "id"}
                dir={sortDir}
                onClick={() => onSort("id")}
              />
              <SortHeader
                label="Owned"
                active={sortKey === "owned"}
                dir={sortDir}
                onClick={() => onSort("owned")}
              />
              <SortHeader
                label="Covered"
                active={sortKey === "covered"}
                dir={sortDir}
                onClick={() => onSort("covered")}
              />
              <SortHeader
                label="Uncovered"
                active={sortKey === "uncovered"}
                dir={sortDir}
                onClick={() => onSort("uncovered")}
              />
              {index.tier_order.map((tier) => {
                const tierKey = `tier:${tier}` as const;
                return (
                  <SortHeader
                    key={tier}
                    label={index.tier_labels[tier] ?? tier}
                    active={sortKey === tierKey}
                    dir={sortDir}
                    onClick={() => onSort(tierKey)}
                  />
                );
              })}
            </div>
            {visible.map((ticket) => (
              <TicketRow
                key={ticket.id}
                ticket={ticket}
                index={index}
                expanded={expandedId === ticket.id}
                chunkState={chunks[ticket.chunk]}
                onToggle={() => onToggle(ticket.id)}
              />
            ))}
          </div>

          <p data-testid="tickets-caption" className="mt-2 text-xs text-quaternary">
            Rows overlap and do not sum to the card above — a commit can name several tickets, so
            per-ticket line sets are not a partition of the repo.
          </p>
        </>
      )}
    </AppShell>
  );
}
