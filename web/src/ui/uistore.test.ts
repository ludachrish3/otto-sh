import { afterEach, describe, expect, it } from "vitest";

import { useUiStore } from "./uiStore";

afterEach(() => {
  useUiStore.setState({ paletteOpen: false, theme: "light" });
  localStorage.clear();
  document.documentElement.classList.remove("dark-mode");
});

describe("uiStore palette state", () => {
  it("opens, closes, and toggles", () => {
    const { openPalette, closePalette, togglePalette } = useUiStore.getState().actions;
    expect(useUiStore.getState().paletteOpen).toBe(false);
    openPalette();
    expect(useUiStore.getState().paletteOpen).toBe(true);
    closePalette();
    expect(useUiStore.getState().paletteOpen).toBe(false);
    togglePalette();
    expect(useUiStore.getState().paletteOpen).toBe(true);
    togglePalette();
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });
});

describe("uiStore theme", () => {
  it("toggleTheme flips state, persists, and applies the html class", () => {
    useUiStore.setState({ theme: "light" });
    useUiStore.getState().actions.toggleTheme();
    expect(useUiStore.getState().theme).toBe("dark");
    expect(localStorage.getItem("otto-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(true);
    useUiStore.getState().actions.toggleTheme();
    expect(useUiStore.getState().theme).toBe("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(false);
  });
});
