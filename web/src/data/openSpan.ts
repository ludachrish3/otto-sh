// web/src/data/openSpan.ts
// The ONE decision "is the tracked open span still open?" (#868). A span can
// end or vanish through any door: End span / Stop / the palette, a row's End
// now, an editor save or delete, an SSE batch, a reconnect resync, a remote
// client. Each of those lands in the review store's `sessions`, so this
// reconciles the UI's `openSpan` against that STORED state (not the raw
// fragments, whose event rows may be dropped as invalid) from one store
// subscription, instead of each flow clearing it by hand.
import { useUiStore } from "../ui/uiStore";
import { useReviewStore } from "./reviewStore";

/** Clear `uiStore.openSpan` when its session is gone, the session has no
 * event with that id, or that event now has an end. */
function reconcileOpenSpan(): void {
  const { openSpan, actions } = useUiStore.getState();
  if (openSpan === null) return;
  const session = useReviewStore.getState().sessions.find((s) => s.id === openSpan.sessionId);
  const event = session?.events.find((e) => e.id === openSpan.eventId);
  if (event === undefined || event.end_timestamp != null) actions.setOpenSpan(null);
}

/** Subscribe the reconciler to the review store; returns the unsubscribe. */
export function installOpenSpanReconciler(): () => void {
  return useReviewStore.subscribe((state, prev) => {
    if (state.sessions !== prev.sessions) reconcileOpenSpan();
  });
}
