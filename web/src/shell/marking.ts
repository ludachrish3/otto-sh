// web/src/shell/marking.ts
// Imperative marking helpers shared by MarkControl, the palette commands and
// EventsPanel's compose row — one implementation per flow, three triggers.
// All throw EventApiError upward: the CALLER owns the error surface (inline
// text for controls, the warnings banner for palette-initiated runs).
import { createEvent, endEvent } from "../data/eventApi";
import type { NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { type EventDraft, useUiStore } from "../ui/uiStore";

function requireActiveSessionId(): string {
  const id = useReviewStore.getState().activeSessionId;
  if (id === null) throw new Error("no active monitor session");
  return id;
}

export async function markNow(label: string): Promise<void> {
  await createEvent(requireActiveSessionId(), { label });
}

export async function startSpan(label: string): Promise<void> {
  const sessionId = requireActiveSessionId();
  const record = await createEvent(sessionId, { label });
  if (record.id != null) {
    useUiStore.getState().actions.setOpenSpan({ sessionId, eventId: record.id });
  }
}

export async function endOpenSpan(): Promise<void> {
  const span = useUiStore.getState().openSpan;
  if (span === null) return;
  await endEvent(span.sessionId, span.eventId);
  useUiStore.getState().actions.setOpenSpan(null);
}

export function blankDraft(session: NormalizedSession): EventDraft {
  return {
    sessionId: session.id,
    timestampMs: session.endMs,
    endTimestampMs: null,
    label: "",
    color: "#888888",
    dash: "dash",
  };
}
