// web/src/data/undoHistory.ts
// The tab's event undo history (#929): two bounded stacks of the last
// HISTORY_DEPTH event changes this tab made. eventApi.ts records into it, in
// each ordinary mutation's onSuccess; a revert never records. undo / redo
// (undoRedo.ts) replay and move its entries. A leaf module, so everything
// that records into or clears the history can import it without a cycle.
import { create } from "zustand";
import type { EventRecord } from "../api/export.gen";

/** How many changes each stack holds; an older one is evicted. */
export const HISTORY_DEPTH = 10;

/** One recorded change: the event's state after it (`after`) and before it
 * (`before`), null where there was no event. */
export interface HistoryEntry {
  verb: "add" | "edit" | "end" | "delete";
  sessionId: string;
  eventId: number;
  label: string;
  after: EventRecord | null;
  before: EventRecord | null;
  /** The change was to the span the tab was tracking, at send time. */
  trackedSpan: boolean;
}

export type Stack = "undo" | "redo";

interface UndoHistoryState {
  undo: HistoryEntry[];
  redo: HistoryEntry[];
  actions: {
    /** A new change: pushed onto undo; redo is no longer reachable. */
    record: (entry: HistoryEntry) => void;
    clear: () => void;
    shiftUndoToRedo: () => void;
    shiftRedoToUndo: () => void;
    dropTop: (stack: Stack) => void;
  };
}

/** `stack` with `entry` on top, the oldest evicted past the depth. */
const pushed = (stack: HistoryEntry[], entry: HistoryEntry): HistoryEntry[] =>
  [...stack, entry].slice(-HISTORY_DEPTH);

export const useUndoHistory = create<UndoHistoryState>()((set) => {
  const shift = (from: Stack, to: Stack) =>
    set((s) => {
      const top = s[from].at(-1);
      if (top === undefined) return s;
      return { [from]: s[from].slice(0, -1), [to]: pushed(s[to], top) } as Pick<
        UndoHistoryState,
        Stack
      >;
    });
  return {
    undo: [],
    redo: [],
    actions: {
      record: (entry) => set((s) => ({ undo: pushed(s.undo, entry), redo: [] })),
      clear: () => set({ undo: [], redo: [] }),
      shiftUndoToRedo: () => shift("undo", "redo"),
      shiftRedoToUndo: () => shift("redo", "undo"),
      dropTop: (stack) => set((s) => ({ [stack]: s[stack].slice(0, -1) })),
    },
  };
});

/** A label cut to `max` characters, the last one an ellipsis: the toast
 * copy's and the palette's (the Events panel truncates with CSS). */
export function truncateLabel(label: string, max = 40): string {
  return label.length <= max ? label : `${label.slice(0, max - 1)}…`;
}
