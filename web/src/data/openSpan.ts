// web/src/data/openSpan.ts
// The ONE decision "is the tracked open span still open?" (#868). A span can
// end or vanish through any door: End span / Stop / the palette, a row's End
// now, an editor save or delete, an SSE batch, a reconnect resync, a remote
// client. Each of those lands in the review store's `sessions`, so this
// reconciles the UI's `openSpan` against that STORED state (not the raw
// fragments, whose event rows may be dropped as invalid) from one store
// subscription, instead of each flow clearing it by hand.
import { useUiStore } from "../ui/uiStore";
import { storedEvent, useReviewStore } from "./reviewStore";

/** The ONE "present and still open" predicate: the session exists, it has
 * an event with that id, and that event has no end. */
export function isTrackedSpanLive(sessionId: string, eventId: number): boolean {
  const event = storedEvent(sessionId, eventId);
  return event !== undefined && event.end_timestamp == null;
}

/** Clear `uiStore.openSpan` when it is no longer live. */
function reconcileOpenSpan(): void {
  const { openSpan, actions } = useUiStore.getState();
  if (openSpan === null) return;
  if (!isTrackedSpanLive(openSpan.sessionId, openSpan.eventId)) actions.setOpenSpan(null);
}

/** Track `(sessionId, eventId)` again after an undo or redo (#929) -- only
 * when no span is tracked and the event is live. A re-adoption YIELDS: it
 * never replaces a tracked span, unlike an explicit Start span
 * (shell/marking.ts's startSpan), which does. Returns whether it adopted. */
export function adoptOpenSpan(sessionId: string, eventId: number): boolean {
  const { openSpan, actions } = useUiStore.getState();
  if (openSpan !== null || !isTrackedSpanLive(sessionId, eventId)) return false;
  actions.setOpenSpan({ sessionId, eventId });
  return true;
}

/** Subscribe the reconciler to the review store; returns the unsubscribe. */
export function installOpenSpanReconciler(): () => void {
  return useReviewStore.subscribe((state, prev) => {
    if (state.sessions !== prev.sessions) reconcileOpenSpan();
  });
}
