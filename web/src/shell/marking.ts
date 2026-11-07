// web/src/shell/marking.ts
// Imperative marking helpers shared by MarkControl, the palette commands and
// EventsPanel's compose row — one implementation per flow, three triggers.
// All throw EventApiError upward: the CALLER owns the error surface (inline
// text for controls, the warnings banner for palette-initiated runs).
// Each flow resolves to eventApi's StaleResult when a file import overtook
// its request (#873): the flow then skips its own follow-up effects, and so
// does its caller. Each layer re-checks with its own sourceGuard after its
// await, so an import landing between the layers still counts as stale.
import { DEFAULT_EVENT_COLOR, DEFAULT_EVENT_DASH } from "../api/eventStyle.gen";
import {
  createEvent,
  endEvent,
  isStale,
  reportMutationFailure,
  type StaleResult,
  sourceGuard,
} from "../data/eventApi";
import type { NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { displayEndMs } from "../data/time";
import { type EventDraft, useUiStore } from "../ui/uiStore";

function requireActiveSessionId(): string {
  const id = useReviewStore.getState().activeSessionId;
  if (id === null) throw new Error("no active monitor session");
  return id;
}

export async function markNow(label: string): Promise<StaleResult | undefined> {
  const current = sourceGuard();
  const result = current(await createEvent(requireActiveSessionId(), { label }));
  return isStale(result) ? result : undefined;
}

export async function startSpan(label: string): Promise<StaleResult | undefined> {
  const sessionId = requireActiveSessionId();
  const current = sourceGuard();
  const record = current(await createEvent(sessionId, { label }, { trackSpan: true }));
  if (isStale(record)) return record;
  if (record.id != null) {
    useUiStore.getState().actions.setOpenSpan({ sessionId, eventId: record.id });
  }
  return undefined;
}

export async function endOpenSpan(): Promise<StaleResult | undefined> {
  const span = useUiStore.getState().openSpan;
  if (span === null) return undefined;
  const current = sourceGuard();
  const result = current(await endEvent(span.sessionId, span.eventId));
  if (isStale(result)) return result;
  // No clear here: endEvent applied the ended record to the store, and the
  // open-span reconciler (data/openSpan.ts) clears the tracked span from that.
  return undefined;
}

/** End the open span for a surface with no inline error slot (the menu, the
 * palette): a failure lands in the warnings banner, never as a rejection. */
export function endSpanReporting(): Promise<void> {
  return endOpenSpan().then(() => undefined, reportMutationFailure("End span"));
}

/** The ONE draft builder: a new event in the registry's default style, at the
 * display horizon (displayEndMs, so "Add event…" on a stalled collector
 * pre-fills a time inside the live window, #866) or over a swept range
 * (SubjectPage). */
export function blankDraft(
  session: NormalizedSession,
  fromMs: number = displayEndMs(session),
  toMs: number | null = null,
): EventDraft {
  return {
    sessionId: session.id,
    timestampMs: fromMs,
    endTimestampMs: toMs,
    label: "",
    color: DEFAULT_EVENT_COLOR,
    dash: DEFAULT_EVENT_DASH,
  };
}
