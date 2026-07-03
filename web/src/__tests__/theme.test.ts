// Pins theme.ts's localStorage persistence + subscriber notification —
// ported from dashboard.js's `applyTheme()`, the theme-btn click handler
// (which ends with a `refreshPlot()` call `ChartGrid` hooks via
// `onThemeChange`), and the startup `localStorage['otto-theme']` restore.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyBodyTheme, loadTheme, onThemeChange, saveTheme } from "../theme";

beforeEach(() => {
  localStorage.clear();
  document.body.className = "";
});

afterEach(() => {
  localStorage.clear();
  document.body.className = "";
});

describe("loadTheme (dashboard.js's startup localStorage restore)", () => {
  it("defaults to dark when nothing is stored", () => {
    expect(loadTheme()).toBe("dark");
  });

  it("treats any non-'light' stored value as dark", () => {
    localStorage.setItem("otto-theme", "sepia");
    expect(loadTheme()).toBe("dark");
  });

  it("restores 'light' when that was saved", () => {
    localStorage.setItem("otto-theme", "light");
    expect(loadTheme()).toBe("light");
  });
});

describe("saveTheme + onThemeChange (dashboard.js's theme-btn handler notifying ChartGrid's refreshPlot())", () => {
  const unsubscribes: (() => void)[] = [];

  afterEach(() => {
    unsubscribes.splice(0).forEach((unsubscribe) => {
      unsubscribe();
    });
  });

  it("persists the theme to localStorage", () => {
    saveTheme("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
  });

  it("notifies a subscriber with the new theme", () => {
    const listener = vi.fn();
    unsubscribes.push(onThemeChange(listener));
    saveTheme("light");
    expect(listener).toHaveBeenCalledWith("light");
  });

  it("notifies every subscriber, in no particular guaranteed order", () => {
    const a = vi.fn();
    const b = vi.fn();
    unsubscribes.push(onThemeChange(a), onThemeChange(b));
    saveTheme("dark");
    expect(a).toHaveBeenCalledWith("dark");
    expect(b).toHaveBeenCalledWith("dark");
  });

  it("stops notifying once unsubscribed", () => {
    const listener = vi.fn();
    const unsubscribe = onThemeChange(listener);
    unsubscribe();
    saveTheme("light");
    expect(listener).not.toHaveBeenCalled();
  });
});

describe("applyBodyTheme (dashboard.js's applyTheme()'s body.light toggle)", () => {
  it("adds body.light for 'light'", () => {
    applyBodyTheme("light");
    expect(document.body.classList.contains("light")).toBe(true);
  });

  it("removes body.light for 'dark'", () => {
    document.body.classList.add("light");
    applyBodyTheme("dark");
    expect(document.body.classList.contains("light")).toBe(false);
  });
});

// Task 8 (T5-review known gap): dashboard.js applies the persisted theme at
// top-level script-parse time (`if (localStorage.getItem('otto-theme') ===
// 'light') applyTheme(true);`), BEFORE its async init() ever runs — so the
// page never paints a dark frame before flipping to light on a reload with
// 'light' saved. The React module ports this as a module-evaluation side
// effect in theme.ts (see its comment) rather than a post-mount effect,
// which would let React commit/paint at least once first. `vi.resetModules`
// + a fresh dynamic import re-triggers that side effect under a controlled
// localStorage value, without mounting any component — proving the class is
// set purely by importing the module, not by any render.
describe("module-load side effect (Task 8: theme applied before first paint)", () => {
  it("applies body.light on import when 'light' was persisted, before any component renders", async () => {
    localStorage.setItem("otto-theme", "light");
    vi.resetModules();
    await import("../theme");
    expect(document.body.classList.contains("light")).toBe(true);
  });

  it("leaves body.light absent on import when nothing was persisted (dark default)", async () => {
    vi.resetModules();
    await import("../theme");
    expect(document.body.classList.contains("light")).toBe(false);
  });
});
