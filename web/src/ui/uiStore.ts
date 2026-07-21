// UI-chrome state that must be shared across the Router boundary: the
// AppBar (outside wouter's <Router> — see App.tsx) opens the palette; the
// palette itself + the shortcut layer mount inside the Router (they need
// navigation). A store, not context, so both sides reach it without a
// provider spanning that boundary. Theme lives here too (not in AppBar
// useState) because the palette's theme command must flip the same reactive
// value the menu label reads.
import { create } from "zustand";

import { loadTheme, saveTheme, type Theme } from "../theme";

// Plan 5c marking: the compose-row shape shared by the palette's "Add
// event…" draft and (eventually) EventsPanel's own compose row — ONE shape,
// two producers, so marking.ts's blankDraft and the editor that consumes it
// never have to reconcile two slightly different drafts.
export interface EventDraft {
  sessionId: string;
  timestampMs: number;
  endTimestampMs: number | null;
  label: string;
  color: string;
  dash: string;
}

/** What the (future) event editor is pointed at: an existing event being
 * edited, or a not-yet-created draft. */
export type EventEditorTarget =
  | { kind: "edit"; sessionId: string; eventId: number }
  | { kind: "draft"; draft: EventDraft };

interface UiState {
  paletteOpen: boolean;
  theme: Theme;
  /** Plan 5c marking: the event editor's target, or null when closed. */
  eventEditor: EventEditorTarget | null;
  /** Plan 5c marking: true while "Sweep span on chart" is armed, awaiting
   * the user's chart drag. useGlobalShortcuts' Escape disarms it before
   * anything else runs (spec §Global shortcuts). */
  sweepArmed: boolean;
  /** Plan 5c marking: the in-progress span started via "Start span…", or
   * null. Drives "End span"'s enabled state (commands.ts). */
  openSpan: { sessionId: string; eventId: number } | null;
  /** Plan 5c marking: which inline popover ("Mark now…" vs "Start span…")
   * is open, or null. */
  markPopover: "mark" | "start" | null;
  actions: {
    openPalette: () => void;
    closePalette: () => void;
    togglePalette: () => void;
    toggleTheme: () => void;
    openEventEditor: (target: EventEditorTarget) => void;
    closeEventEditor: () => void;
    armSweep: () => void;
    disarmSweep: () => void;
    setOpenSpan: (span: { sessionId: string; eventId: number } | null) => void;
    openMarkPopover: (kind: "mark" | "start") => void;
    closeMarkPopover: () => void;
  };
}

export const useUiStore = create<UiState>()((set, get) => ({
  paletteOpen: false,
  theme: loadTheme(),
  eventEditor: null,
  sweepArmed: false,
  openSpan: null,
  markPopover: null,
  actions: {
    openPalette: () => set({ paletteOpen: true }),
    closePalette: () => set({ paletteOpen: false }),
    togglePalette: () => set({ paletteOpen: !get().paletteOpen }),
    toggleTheme: () => {
      const next: Theme = get().theme === "dark" ? "light" : "dark";
      saveTheme(next);
      set({ theme: next });
    },
    openEventEditor: (target) => set({ eventEditor: target }),
    closeEventEditor: () => set({ eventEditor: null }),
    armSweep: () => set({ sweepArmed: true }),
    disarmSweep: () => set({ sweepArmed: false }),
    setOpenSpan: (span) => set({ openSpan: span }),
    openMarkPopover: (kind) => set({ markPopover: kind }),
    closeMarkPopover: () => set({ markPopover: null }),
  },
}));
