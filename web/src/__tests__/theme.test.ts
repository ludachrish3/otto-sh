// Theme v2 (UX spec §7): seed from prefers-color-scheme when nothing is
// stored; a 2-state toggle persists BOTH values (unlike v1, which only
// ever wrote "light"); applied as a `dark-mode` class on <html> — Untitled
// UI's vendored theme.css gates its dark token block on that class, and
// app.css's `@custom-variant dark` reads the same class, so it is the only
// one theme.ts needs to touch.
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyTheme, loadTheme, saveTheme } from "../theme";

function mockMedia(dark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: dark && query.includes("dark"),
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove("dark", "dark-mode");
});

describe("loadTheme", () => {
  it("honors stored values over the OS preference", () => {
    mockMedia(true);
    localStorage.setItem("otto-theme", "light");
    expect(loadTheme()).toBe("light");
    localStorage.setItem("otto-theme", "dark");
    expect(loadTheme()).toBe("dark");
  });

  it("seeds from prefers-color-scheme when nothing stored", () => {
    mockMedia(true);
    expect(loadTheme()).toBe("dark");
    mockMedia(false);
    expect(loadTheme()).toBe("light");
  });
});

describe("saveTheme / applyTheme", () => {
  it("persists and toggles the html dark-mode class", () => {
    saveTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(true);
    saveTheme("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(false);
  });

  it("applyTheme alone does not persist", () => {
    applyTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBeNull();
  });

  // .dark-mode is the ONLY class: it's what the vendored, byte-exact
  // theme.css gates its dark token block on, and app.css's `@custom-variant
  // dark` reads the same class for every `dark:` utility (ours and Untitled
  // UI's). We do not shadow it with a second `.dark` class — that was tried
  // and reverted (a coupling nobody pays for). This must fail if the class
  // is absent, not just assert something true by default.
  it("toggles .dark-mode on <html>, and nothing else", () => {
    expect(document.documentElement.classList.contains("dark-mode")).toBe(false);
    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    applyTheme("light");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(false);
  });
});
