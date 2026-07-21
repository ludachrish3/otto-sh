// The one document-level keydown listener (spec §Global shortcuts). Chords
// fire from anywhere — they never type characters — and preventDefault on
// match is load-bearing: it is what keeps ⌘S from ALSO opening the
// browser's save dialog. The bare "/" is the only guarded key
// (shouldSuppressSlash).
import { useEffect } from "react";

import type { Command } from "./commands";
import { focusSearchInput } from "./searchFocus";
import { matchesBinding, PALETTE_BINDING, SEARCH_BINDING, shouldSuppressSlash } from "./shortcuts";
import { useUiStore } from "./uiStore";

export function useGlobalShortcuts(commands: Command[]): void {
  const actions = useUiStore((s) => s.actions);
  const sweepArmed = useUiStore((s) => s.sweepArmed);
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Plan 5c marking: Escape disarming an armed chart-sweep gesture takes
      // priority over everything else below — including the palette/search
      // chords — so it's checked first and unconditionally returns.
      if (e.key === "Escape" && sweepArmed) {
        e.preventDefault();
        actions.disarmSweep();
        return;
      }
      if (matchesBinding(e, PALETTE_BINDING)) {
        e.preventDefault();
        actions.togglePalette();
        return;
      }
      if (matchesBinding(e, SEARCH_BINDING)) {
        if (shouldSuppressSlash(e.target, useUiStore.getState().paletteOpen)) return;
        // "/" is the in-page chart/host search affordance (SeriesPanel), kept
        // distinct from the global command palette (⌘K). Focus the registered
        // search box if one is present; otherwise do nothing — "/" is NOT a
        // second way into the palette, or the two searches re-conflate. Only
        // preventDefault when we actually consume it (focus a box).
        if (focusSearchInput()) e.preventDefault();
        return;
      }
      for (const command of commands) {
        if (command.binding && command.enabled && matchesBinding(e, command.binding)) {
          e.preventDefault();
          // A palette row already closes the palette before running its
          // command (see CommandMenu.tsx); the same command fired by its chord
          // must match, or it runs underneath the still-open modal (e.g.
          // ⌘I's file picker rendering beneath the palette overlay instead
          // of on top of the page).
          if (useUiStore.getState().paletteOpen) actions.closePalette();
          command.run();
          return;
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [commands, actions, sweepArmed]);
}
