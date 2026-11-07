// UI-chrome state that must be shared across the Router boundary: the
// AppBar (outside wouter's <Router> — see App.tsx) opens the palette; the
// palette itself + the shortcut layer mount inside the Router (they need
// navigation). A store, not context, so both sides reach it without a
// provider spanning that boundary. Theme lives here too (not in AppBar
// useState) because the palette's theme command must flip the same reactive
// value the menu label reads.
import { create } from "zustand";

import type { EventDash } from "../api/eventStyle.gen";
import { loadTheme, saveTheme, type Theme } from "../theme";

// Event marking: the compose-row shape shared by the palette's "Add
// event…" draft and (eventually) EventsPanel's own compose row — ONE shape,
// two producers, so marking.ts's blankDraft and the editor that consumes it
// never have to reconcile two slightly different drafts.
export interface EventDraft {
  sessionId: string;
  timestampMs: number;
  endTimestampMs: number | null;
  label: string;
  color: string;
  dash: EventDash;
}

/** What the (future) event editor is pointed at: an existing event being
 * edited, or a not-yet-created draft. */
export type EventEditorTarget =
  | { kind: "edit"; sessionId: string; eventId: number }
  | { kind: "draft"; draft: EventDraft };

/** The surfaces a request can outlive: a late result must tell their
 * openings apart (shell/settleOnOpening.ts). */
export type Surface = "eventEditor" | "markPopover";

interface UiState {
  paletteOpen: boolean;
  theme: Theme;
  /** Event marking: the event editor's target, or null when closed. */
  eventEditor: EventEditorTarget | null;
  /** Event marking: true while "Sweep span on chart" is armed, awaiting
   * the user's chart drag. useGlobalShortcuts' Escape disarms it before
   * anything else runs (spec §Global shortcuts). */
  sweepArmed: boolean;
  /** Event marking: the in-progress span started via "Start span…", or
   * null. Drives "End span"'s enabled state (commands.ts). */
  openSpan: { sessionId: string; eventId: number } | null;
  /** Event marking: which inline popover ("Mark now…" vs "Start span…")
   * is open, or null. */
  markPopover: "mark" | "start" | null;
  /** Bumped by every open and close of each surface, so an opening is told
   * apart from the next one even on the same target. */
  openings: Readonly<Record<Surface, number>>;
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

/** The state change for `surface` becoming `value`: a new opening. */
function reopen<K extends Surface>(s: UiState, surface: K, value: UiState[K]): Partial<UiState> {
  return { [surface]: value, openings: { ...s.openings, [surface]: s.openings[surface] + 1 } };
}

export const useUiStore = create<UiState>()((set, get) => ({
  paletteOpen: false,
  theme: loadTheme(),
  eventEditor: null,
  sweepArmed: false,
  openSpan: null,
  markPopover: null,
  openings: { eventEditor: 0, markPopover: 0 },
  actions: {
    openPalette: () => set({ paletteOpen: true }),
    closePalette: () => set({ paletteOpen: false }),
    togglePalette: () => set({ paletteOpen: !get().paletteOpen }),
    toggleTheme: () => {
      const next: Theme = get().theme === "dark" ? "light" : "dark";
      saveTheme(next);
      set({ theme: next });
    },
    openEventEditor: (target) => set((s) => reopen(s, "eventEditor", target)),
    closeEventEditor: () => set((s) => reopen(s, "eventEditor", null)),
    armSweep: () => set({ sweepArmed: true }),
    disarmSweep: () => set({ sweepArmed: false }),
    setOpenSpan: (span) => set({ openSpan: span }),
    openMarkPopover: (kind) => set((s) => reopen(s, "markPopover", kind)),
    closeMarkPopover: () => set((s) => reopen(s, "markPopover", null)),
  },
}));
