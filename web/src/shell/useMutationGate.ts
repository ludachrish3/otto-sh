// web/src/shell/useMutationGate.ts
// One event mutation at a time per tab (#929). A press that arrives while ANY
// gate key is pending is ignored outright -- there is no "skipped" result to
// confuse with success, because `action` is the WHOLE handler (request,
// success effects, error display) and a busy press never reaches it. The
// serialisation is what lets an undo history compare-and-revert against a
// stable stored record: no other mutation can land between its read and write.
//
// The pending state is held OUTSIDE any component, in one store keyed by the
// gate's key, so it outlives the surface that pressed: closing the Events
// panel mid-request and reopening it finds the gate still shut. The store is
// read synchronously, so two presses in the same tick (a double click, a held
// Enter) make one call.
//
// `pending` is GLOBAL (any key pending) and disables every button on every
// gate; `pendingAction` is per key and names the one that was pressed, so
// only that button shows `isLoading`. `runGate` is the plain core for callers
// outside React (the undo history); the hook's `run` delegates to it.
//
// Keys in use (one per decision, built by the helpers below):
//  - `markGateKey(sessionId)`: a session's Mark / Start / End span, shared by
//    the AppBar's MarkControl, the Events panel's compose row and the palette;
//  - `endNowGateKey(sessionId, eventId)`: one Events-panel row's End now;
//  - `editorGateKey(sessionId, eventId)`: the event editor's Save and Delete
//    for one target (a draft has no id yet);
//  - `HISTORY_GATE_KEY`: the undo / redo history's revert.
import { useCallback } from "react";
import { create } from "zustand";

/** Each pending key's pressed action name and the run that holds it. */
interface GateState {
  pending: Readonly<Record<string, { action: string; run: symbol }>>;
}

/** The one store every gate reads. Exported for test isolation only. */
export const useMutationGates = create<GateState>()(() => ({ pending: {} }));

export const markGateKey = (sessionId: string) => `mark:${sessionId}`;
export const endNowGateKey = (sessionId: string, eventId: number) => `end:${sessionId}:${eventId}`;
export const editorGateKey = (sessionId: string, eventId: number | null) =>
  `editor:${sessionId}:${eventId ?? "draft"}`;

/** The undo / redo history's gate key. */
export const HISTORY_GATE_KEY = "history";

/** The one "any key pending" decision, over a store snapshot. */
const anyPending = (s: GateState): boolean => Object.keys(s.pending).length > 0;

/** True while any key's mutation is in flight. */
export const anyMutationPending = (): boolean => anyPending(useMutationGates.getState());

/** The non-hook core: run `action` under `key`, or return without calling it
 * when any mutation is pending. */
export async function runGate(
  key: string,
  actionName: string,
  action: () => Promise<void>,
): Promise<void> {
  if (anyMutationPending()) return;
  const mine = Symbol(key);
  useMutationGates.setState((s) => ({
    pending: { ...s.pending, [key]: { action: actionName, run: mine } },
  }));
  try {
    await action();
  } finally {
    // Only this run's own entry: a test reset may have handed the key on.
    useMutationGates.setState((s) => {
      if (s.pending[key]?.run !== mine) return s;
      const { [key]: _released, ...rest } = s.pending;
      return { pending: rest };
    });
  }
}

export function useMutationGate(key: string): {
  pending: boolean;
  pendingAction: string | null;
  run: (action: () => Promise<void>, actionName?: string) => Promise<void>;
} {
  const pendingAction = useMutationGates((s) => s.pending[key]?.action ?? null);
  const pending = useMutationGates(anyPending);
  const run = useCallback(
    (action: () => Promise<void>, actionName = "") => runGate(key, actionName, action),
    [key],
  );
  return { pending, pendingAction, run };
}
