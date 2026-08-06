// Tier x Line/Branch/Decision matrix (DOM/anatomy reference:
// docs/superpowers/specs/assets/2026-07-24-coverage-ui/file-page.html's
// `.stats` table / `.pct`/`.frac`/`tr.all`/`.tierdot`) — recreated with
// Tailwind utilities over the vendored semantic tokens, not the mockup's
// literal CSS.
import { cx } from "@/utils/cx";

import { fmtPct, PCT_TEXT, pct, pctClass } from "../stats";
import type { Thresholds } from "../types";

export interface TierStatRow {
  key: string;
  label: string;
  /** `| undefined` explicitly, not just `?`: the colour comes from the wire
   * payload's `tier_colors` record, so a tier listed in `tier_order` with no
   * colour entry reads as `undefined` — a real state this row RENDERS (no
   * swatch), not a caller forgetting to pass one. Under
   * exactOptionalPropertyTypes a bare `?` would reject that value and push
   * nine call sites into conditional spreads to say the same thing. */
  dotColor?: string | undefined;
  /** `null` (a composed ctx+ticket row on DirectoryPage's tree) mirrors
   * `branch`/`decision`'s "no data" rendering — the ctx numerator
   * (`ctx_lines`, whole-file) and the ticket denominator (scoped to the
   * ticket's own lines) are no longer commensurable once both filters
   * compose at tree granularity, so showing ANY computed percentage
   * there would be a plausible-looking but out-of-range number, not an
   * approximation. Every other caller still passes a tuple — the `null`
   * case is purely additive. */
  line: [number, number] | null;
  /** `null` (the focused-context single row) mirrors `decision`'s
   * "no data" rendering — v4's `run_hits` is per-line only (Global
   * Constraints' documented data limitation: per-run branch contribution
   * isn't stored), so a focused row has nothing to show here. Every
   * unfocused caller still passes a tuple — the `null` case is purely
   * additive. */
  branch: [number, number] | null;
  /** `null` when this stat type has no data for this row (e.g. decision
   * counts aren't rolled up onto tree nodes) — rendered as a muted
   * "no data", never a bogus 0%. */
  decision: [number, number] | null;
}

export interface StatsCardProps {
  scope: string;
  title: string;
  rows: TierStatRow[];
  thresholds: Thresholds;
  /** First column header text — "Tier" for the normal per-tier matrix,
   * "Context" for the focused single-row variant.
   * @default "Tier" */
  keyColumnLabel?: string;
}

function StatCell({
  hit,
  total,
  thresholds,
}: {
  hit: number;
  total: number;
  thresholds: Thresholds;
}) {
  const p = pct(hit, total);
  const cls = pctClass(p, thresholds);
  return (
    <td className="py-1.5 pr-4 text-right whitespace-nowrap">
      <span className={cx("font-semibold tabular-nums", PCT_TEXT[cls])}>{fmtPct(p)}</span>
      {total > 0 && (
        <span className="ml-1.5 text-xs font-normal tabular-nums text-quaternary">
          {hit}/{total}
        </span>
      )}
    </td>
  );
}

/** Shared by the Branch and Decision columns: both can be `null` ("no
 * data" for this row — the focused single row has no branch data
 * per-run, and decision counts were never rolled up onto tree nodes in the
 * first place), rendered identically muted rather than as a bogus 0%. */
function NullableStatCell({
  value,
  thresholds,
}: {
  value: [number, number] | null;
  thresholds: Thresholds;
}) {
  if (value === null) {
    return (
      <td className="py-1.5 pr-4 text-right whitespace-nowrap">
        <span className="text-xs font-normal text-quaternary">no data</span>
      </td>
    );
  }
  const [hit, total] = value;
  return <StatCell hit={hit} total={total} thresholds={thresholds} />;
}

export function StatsCard({
  scope,
  title,
  rows,
  thresholds,
  keyColumnLabel = "Tier",
}: StatsCardProps) {
  return (
    <div
      data-testid="stats-card"
      className="w-full max-w-md rounded-xl bg-primary p-3 ring-1 ring-secondary sm:w-auto"
    >
      <div className="mb-2 flex items-baseline justify-between gap-3">
        <span className="text-sm font-semibold text-secondary">{title}</span>
        <span className="truncate text-xs text-quaternary">{scope}</span>
      </div>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="text-left text-xs font-medium text-quaternary">
            <th className="pb-1 pr-4 font-medium">{keyColumnLabel}</th>
            <th className="pb-1 pr-4 text-right font-medium">Line</th>
            <th className="pb-1 pr-4 text-right font-medium">Branch</th>
            <th className="pb-1 text-right font-medium">Decision</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isAll = row.key === "all";
            return (
              <tr
                key={row.key}
                data-testid={`stats-row-${row.key}`}
                className={cx(
                  isAll && "border-t border-secondary bg-secondary font-semibold text-primary",
                )}
              >
                <td className="py-1.5 pr-4 font-medium text-secondary">
                  {row.dotColor && (
                    <span
                      aria-hidden
                      data-testid="tier-dot"
                      className="mr-1.5 inline-block size-2 rounded-sm align-middle"
                      style={{ backgroundColor: row.dotColor }}
                    />
                  )}
                  {row.label}
                </td>
                <NullableStatCell value={row.line} thresholds={thresholds} />
                <NullableStatCell value={row.branch} thresholds={thresholds} />
                <NullableStatCell value={row.decision} thresholds={thresholds} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
