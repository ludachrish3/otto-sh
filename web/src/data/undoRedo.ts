// web/src/data/undoRedo.ts
// Undo and redo (#929): replay the top history entry (undoHistory.ts)
// through the server's revert route (revertEvent). The route compares, then
// writes, so the server is the only conflict check: the tab never pre-checks
// its own store.
//
// An undo is a mutation like any other: it runs under the history's gate
// key, so the tab has one event mutation in flight at a time
// (shell/useMutationGate.ts). Its outcomes:
//  - 200: the entry moves to the other stack, with a toast offering the
//    inverse;
//  - 409 `changed`: revertEvent has already applied its `current` through
//    the success path, with the response's stamp so the store's ordering
//    (fragment.ts) still holds (eventApi.ts's mutate, the one place a
//    conflict reaches the store). When `current` is already the target, an
//    earlier attempt committed and its response was lost: the entry moves as
//    on success;
//  - 409 `changed` otherwise: the event changed elsewhere; the entry is
//    dropped and the tab shows the event as it now stands;
//  - 422: the server refuses the write itself (an older capture's blank
//    label or backward span), so no retry can pass; the entry is dropped
//    and the toast names the refusal;
//  - `busy`, a network failure, anything else: the entry is kept to retry.
import type { EventRecord } from "../api/export.gen";
import { anyMutationPending, HISTORY_GATE_KEY, runGate } from "../shell/useMutationGate";
import { useToastStore } from "../ui/Toast";
import { isStale, revertEvent, type StaleResult, sourceGuard } from "./eventApi";
import { EventApiError } from "./eventApiError";
import { EventChangedError } from "./eventChangedError";
import { adoptOpenSpan } from "./openSpan";
import { sameEvent } from "./sameEvent";
import { type HistoryEntry, type Stack, truncateLabel, useUndoHistory } from "./undoHistory";

/** What tells undo and redo apart: which stack they take from (`name`),
 * which leg of the entry is expected and which is written, and their copy. */
interface Direction {
  name: Stack;
  done: string;
  legs: (e: HistoryEntry) => { expect: EventRecord | null; write: EventRecord | null };
  inverse: { label: string; run: () => void };
}

const UNDO: Direction = {
  name: "undo",
  done: "Undid",
  legs: (e) => ({ expect: e.after, write: e.before }),
  inverse: { label: "Redo", run: () => void redo() },
};

const REDO: Direction = {
  name: "redo",
  done: "Redid",
  legs: (e) => ({ expect: e.before, write: e.after }),
  inverse: { label: "Undo", run: () => void undo() },
};

/** Undo's own surface: it has no control to attach an error to. */
function showToast(message: string, action?: Direction["inverse"]): void {
  useToastStore.getState().showToast(message, action);
}

/** The entry reached its target: it moves to the other stack, and a change
 * to the tracked span that left its event open tracks it again -- unless a
 * newer span is tracked, or the store says the event has since ended or gone
 * (adoptOpenSpan decides both). Callers have checked the source first. */
function settle(d: Direction, entry: HistoryEntry): void {
  const { actions } = useUndoHistory.getState();
  if (d.name === "undo") actions.shiftUndoToRedo();
  else actions.shiftRedoToUndo();
  if (entry.trackedSpan) adoptOpenSpan(entry.sessionId, entry.eventId);
  showToast(`${d.done}: ${entry.verb} “${truncateLabel(entry.label)}”`, d.inverse);
}

/** A revert that threw: recover a lost response, drop a conflict or a
 * refusal, or keep the entry to retry. A conflict's `current` is already in
 * the store. */
function fail(d: Direction, entry: HistoryEntry, write: EventRecord | null, err: unknown): void {
  const { dropTop } = useUndoHistory.getState().actions;
  if (err instanceof EventApiError && err.status === 422) {
    dropTop(d.name);
    showToast(`Can't ${d.name}: “${truncateLabel(entry.label)}” — ${err.message}`);
    return;
  }
  if (!(err instanceof EventChangedError)) {
    showToast(`Couldn't ${d.name} — try again`);
    return;
  }
  if (sameEvent(err.current, write)) {
    settle(d, entry);
    return;
  }
  dropTop(d.name);
  showToast(`Can't ${d.name}: “${truncateLabel(entry.label)}” changed elsewhere`);
}

/** Replays the top of `d.name` under the history gate. Every continuation
 * checks the source captured before the request first: an import can land
 * after revertEvent's own check and before this resumes. */
async function step(d: Direction): Promise<void> {
  if (anyMutationPending()) {
    showToast("Wait for the current change to finish");
    return;
  }
  await runGate(HISTORY_GATE_KEY, d.name, async () => {
    const entry = useUndoHistory.getState()[d.name].at(-1);
    if (entry === undefined) return;
    const { expect, write } = d.legs(entry);
    const current = sourceGuard();
    let result: EventRecord | null | StaleResult;
    try {
      result = current(await revertEvent(entry.sessionId, entry.eventId, expect, write));
    } catch (err) {
      if (!isStale(current(null))) fail(d, entry, write, err);
      return;
    }
    if (!isStale(result)) settle(d, entry);
  });
}

/** Reverts the most recent change still on the undo stack. */
export function undo(): Promise<void> {
  return step(UNDO);
}

/** Re-applies the most recently undone change. */
export function redo(): Promise<void> {
  return step(REDO);
}
