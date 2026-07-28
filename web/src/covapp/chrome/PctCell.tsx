// A percentage with a threshold-coloured minibar under it — the tree's
// Line %/Branch % cells and the tickets table's Line % cell are the same
// visual, so they are the same component rather than two copies that can
// drift apart in colour or rounding.
import { cx } from "@/utils/cx";
import { fmtPct, PCT_TEXT, type PctClass, pctClass } from "../stats";
import type { IndexPayload } from "../types";

// Minibar fill — the `fg-*` layer (not `text-*`) is this codebase's existing
// vocabulary for colored bars/dots (src/topo/nodes.tsx's STATUS_SEGMENT,
// AppShell.tsx's tier/state swatches), same green/yellow/red as PCT_TEXT.
const PCT_BAR: Record<PctClass, string> = {
  "pct-high": "bg-fg-success-primary",
  "pct-mid": "bg-fg-warning-primary",
  "pct-low": "bg-fg-error-primary",
  "pct-na": "bg-fg-quaternary",
};

export function PctCell({
  p,
  thresholds,
  testId,
}: {
  p: number | null;
  thresholds: IndexPayload["thresholds"];
  testId?: string;
}) {
  const cls = pctClass(p, thresholds);
  return (
    <div data-testid={testId}>
      <span className={cx("font-semibold tabular-nums", PCT_TEXT[cls])}>{fmtPct(p)}</span>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-tertiary">
        <div className={cx("h-full rounded-full", PCT_BAR[cls])} style={{ width: `${p ?? 0}%` }} />
      </div>
    </div>
  );
}
