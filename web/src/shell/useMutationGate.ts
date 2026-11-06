// web/src/shell/useMutationGate.ts
// One in-flight mutation per gate key (#867). A press that arrives while the
// key's previous request is pending is ignored outright -- there is no
// "skipped" result to confuse with success, because `action` is the WHOLE
// handler (request, success effects, error display) and a busy press never
// reaches it.
//
// The pending state is held OUTSIDE any component, in one store keyed by the
// gate's key, so it outlives the surface that pressed: closing the Events
// panel mid-request and reopening it finds the gate still shut, and every
// surface naming the same key shares it. The store is read synchronously, so
// two presses in the same tick (a double click, a held Enter) make one call.
//
// `pending` disables every button on the gate; `pendingAction` names the one
// that was pressed, so only that button shows `isLoading`.
//
// Keys in use (one per decision, built by the helpers below):
//  - `markGateKey(sessionId)`: a session's Mark / Start / End span, shared by
//    the AppBar's MarkControl, the Events panel's compose row and the palette,
//    so no two surfaces can race the same session's marking (nor double-end
//    its open span);
//  - `endNowGateKey(sessionId, eventId)`: one Events-panel row's End now;
//  - `editorGateKey(sessionId, eventId)`: the event editor's Save and Delete
//    for one target (a draft has no id yet), so a request still pending for
//    one event never makes another event's editor look busy.
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

export function useMutationGate(key: string): {
  pending: boolean;
  pendingAction: string | null;
  run: (action: () => Promise<void>, actionName?: string) => Promise<void>;
} {
  const pendingAction = useMutationGates((s) => s.pending[key]?.action ?? null);
  const run = useCallback(
    async (action: () => Promise<void>, actionName = "") => {
      if (useMutationGates.getState().pending[key] !== undefined) return;
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
    },
    [key],
  );
  return { pending: pendingAction !== null, pendingAction, run };
}
