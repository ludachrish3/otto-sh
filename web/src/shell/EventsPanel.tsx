// Events slide-over (UX spec §11, review-mode subset): reverse-chron list;
// a row jumps the charts to its time (±15 min, clamped). Marking/editing
// needs the backend API — live-hookup phase.
import { clampRange, sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatSpan, parseTs } from "../data/time";
import { SlideOver } from "../ui/SlideOver";

const JUMP_PAD_MS = 15 * 60_000;

export function EventsPanel(props: { isOpen: boolean; onClose: () => void }) {
  const { isOpen, onClose } = props;
  const session = useActiveSession();
  const setRange = useReviewStore((s) => s.actions.setRange);
  if (!session) return null;

  const rows = session.events
    .map((ev, i) => ({
      // Wire ids are non-negative; negative synthetics can't collide (matches eventMarkers).
      id: ev.id ?? -1 - i,
      label: ev.label ?? "",
      color: ev.color ?? "#7c5cff",
      source: ev.source ?? "manual",
      fromMs: parseTs(ev.timestamp),
      toMs: ev.end_timestamp != null ? parseTs(ev.end_timestamp) : null,
    }))
    .sort((a, b) => b.fromMs - a.fromMs);

  const jump = (fromMs: number, toMs: number | null) => {
    setRange(
      clampRange(
        { from: fromMs - JUMP_PAD_MS, to: (toMs ?? fromMs) + JUMP_PAD_MS },
        sessionBounds(session),
      ),
    );
    onClose();
  };

  return (
    <SlideOver isOpen={isOpen} onClose={onClose} title="Events" testId="events-panel">
      {rows.length === 0 && <p className="text-sm text-gray-400">No events in this session.</p>}
      <ul className="flex flex-col gap-1">
        {rows.map((ev) => (
          <li key={ev.id}>
            <button
              type="button"
              data-testid={`event-row-${ev.id}`}
              onClick={() => jump(ev.fromMs, ev.toMs)}
              className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5
                text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-900"
            >
              <span
                aria-hidden
                className="h-3 w-3 shrink-0 rounded-sm"
                style={{ backgroundColor: ev.color }}
              />
              <span className="min-w-0 grow truncate">{ev.label}</span>
              <span className="shrink-0 text-xs text-gray-400">
                {new Date(ev.fromMs).toLocaleTimeString()}
                {ev.toMs !== null ? ` · ${formatSpan(ev.fromMs, ev.toMs)}` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </SlideOver>
  );
}
