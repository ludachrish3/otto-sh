// The historical review bar (UX spec §12): HISTORICAL tag + source ·
// session picker (only >1) · the range picker card.
import { Badge } from "@/components/base/badges/badges";
import { Select } from "@/components/base/select/select";
import { sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { RangePicker } from "../ui/RangePicker";

export function ReviewBar() {
  const sessions = useReviewStore((s) => s.sessions);
  const sourceName = useReviewStore((s) => s.sourceName);
  const activeSessionId = useReviewStore((s) => s.activeSessionId);
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  const { selectSession, setRange } = useReviewStore((s) => s.actions);
  const session = useActiveSession();

  const bounds = session ? sessionBounds(session) : null;

  // UX spec §12: "Review bar (per-view context row, historical only)". Live
  // mode gets its own follow/pause chrome in AppBar instead (Task 9) — the
  // HISTORICAL tag + range picker here would otherwise contradict the
  // "Live"/pause-toggle chrome rendering at the same time. This hiding was
  // reverted (commit 7a9e849) only because bootstrap.ts set mode="live"
  // before a boot hydrate had actually succeeded, so an empty live server
  // claimed to be live and broke the dashboard Playwright suite; that root
  // cause is now fixed (mode is set only after a successful hydrate — see
  // bootstrap.ts), so hiding is safe to restore (Plan 5b final review, C1).
  if (!session || !bounds || mode === "live") return null;

  return (
    <div
      data-testid="review-bar"
      className="flex flex-wrap items-center gap-3 border-b border-secondary px-4 py-2"
    >
      <span data-testid="historical-tag">
        <Badge type="color" size="sm" color="blue">
          HISTORICAL
        </Badge>
      </span>
      <span data-testid="source-name" className="text-sm text-tertiary">
        {sourceName}
      </span>
      {sessions.length > 1 && (
        <Select
          aria-label="Session"
          items={sessions.map((s) => ({
            id: s.id,
            label: s.label ?? s.id,
            supportingText: s.note ?? undefined,
          }))}
          selectedKey={activeSessionId ?? ""}
          onSelectionChange={(key) => {
            if (key !== null) selectSession(String(key));
          }}
          data-testid="session-picker"
        >
          {(item) => (
            <Select.Item id={item.id} label={item.label} supportingText={item.supportingText} />
          )}
        </Select>
      )}
      <RangePicker bounds={bounds} value={range} onChange={setRange} />
    </div>
  );
}
