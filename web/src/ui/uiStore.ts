// UI-chrome state that must be shared across the Router boundary: the
// AppBar (outside wouter's <Router> — see App.tsx) opens the palette; the
// palette itself + the shortcut layer mount inside the Router (they need
// navigation). A store, not context, so both sides reach it without a
// provider spanning that boundary. Theme lives here too (not in AppBar
// useState) because the palette's ⌘L command must flip the same reactive
// value the menu label reads.
import { create } from "zustand";

import { loadTheme, saveTheme, type Theme } from "../theme";

interface UiState {
  paletteOpen: boolean;
  theme: Theme;
  actions: {
    openPalette: () => void;
    closePalette: () => void;
    togglePalette: () => void;
    toggleTheme: () => void;
  };
}

export const useUiStore = create<UiState>()((set, get) => ({
  paletteOpen: false,
  theme: loadTheme(),
  actions: {
    openPalette: () => set({ paletteOpen: true }),
    closePalette: () => set({ paletteOpen: false }),
    togglePalette: () => set({ paletteOpen: !get().paletteOpen }),
    toggleTheme: () => {
      const next: Theme = get().theme === "dark" ? "light" : "dark";
      saveTheme(next);
      set({ theme: next });
    },
  },
}));
