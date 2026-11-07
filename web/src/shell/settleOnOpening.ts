// web/src/shell/settleOnOpening.ts
// One rule for a request a surface (the event editor, the mark popover)
// sends and may outlive (#932). The mutation itself always applies its
// result to the store (data/eventApi.ts), whoever is looking. What the
// SURFACE does with the result -- close on success, show the error on
// failure -- belongs to the opening that sent it: once the surface has
// closed, or closed and reopened (even onto the same event), a late result
// leaves the surface alone, so it never closes a newer edit or paints an
// error into it. A source switch since the send (a file import) is stale the
// same way.
import { isStale, type StaleResult, sourceGuard } from "../data/eventApi";
import { type Surface, useUiStore } from "../ui/uiStore";

/** Captures `surface`'s current opening; the check holds while it is the
 * one shown. */
function openingGuard(surface: Surface): () => boolean {
  const opening = useUiStore.getState().openings[surface];
  return () => useUiStore.getState().openings[surface] === opening;
}

/** Sends one surface mutation, then settles it on the opening that sent it:
 * `done` with the result, or `fail` with the error's message, only while
 * that opening and the source are both still current. Call it before the
 * first await, so the opening captured is the sender's. */
export async function settleOnOpening<T>(
  surface: Surface,
  send: () => Promise<T | StaleResult>,
  { done, fail }: { done: (result: T) => void; fail: (message: string) => void },
): Promise<void> {
  const source = sourceGuard();
  const opening = openingGuard(surface);
  let result: T | StaleResult;
  try {
    result = source(await send());
  } catch (err) {
    if (isStale(source(null)) || !opening()) return;
    fail(err instanceof Error ? err.message : String(err));
    return;
  }
  if (isStale(result) || !opening()) return;
  done(result);
}
