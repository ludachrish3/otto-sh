// Theme v2 (UX spec §7): seed from prefers-color-scheme when nothing is
// stored; a 2-state toggle persists BOTH values (unlike v1, which only
// ever wrote "light"); applied as a `dark` class on <html> for Tailwind's
// @custom-variant.
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
  document.documentElement.classList.remove("dark");
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
  it("persists and toggles the html dark class", () => {
    saveTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    saveTheme("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("applyTheme alone does not persist", () => {
    applyTheme("dark");
    expect(localStorage.getItem("otto-theme")).toBeNull();
  });
});
