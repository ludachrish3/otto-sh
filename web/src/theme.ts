// Light/dark theme persistence — mirrors dashboard.js's `applyTheme()` +
// its `localStorage['otto-theme']` restore. Dark is the default: a value is
// only ever written for 'light', and anything other than the literal string
// "light" in storage (including absence) is treated as dark.
const STORAGE_KEY = "otto-theme";

export type Theme = "light" | "dark";

export function loadTheme(): Theme {
  return localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark";
}

const themeListeners = new Set<(theme: Theme) => void>();

export function saveTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  themeListeners.forEach((listener) => {
    listener(theme);
  });
}

/**
 * Subscribe to theme changes made via `saveTheme` (the `#theme-btn` click
 * handler, in `Header`). Mirrors dashboard.js's theme-btn listener, which
 * ends with a `refreshPlot()` call — `ChartGrid` (Task 6) needs to know a
 * toggle happened so it can do the same full re-render, since trace/paper/
 * plot colors are theme-derived (`plotly.ts`'s `plotTheme()`/`buildLayout`).
 * Returns an unsubscribe function.
 */
export function onThemeChange(listener: (theme: Theme) => void): () => void {
  themeListeners.add(listener);
  return () => {
    themeListeners.delete(listener);
  };
}

/** Toggles `body.light` — the class Plotly's theming (dashboard.js's `plotTheme()`) reads. */
export function applyBodyTheme(theme: Theme): void {
  document.body.classList.toggle("light", theme === "light");
}

// Apply the persisted theme immediately, as a module-evaluation side effect —
// BEFORE React ever renders a single node. Mirrors dashboard.js's own
// startup restore (`if (localStorage.getItem('otto-theme') === 'light')
// applyTheme(true);`), a top-level statement that runs synchronously at
// script-parse time, ahead of its `init()` (the async /api/meta+/api/data
// bootstrap). `main.tsx` imports `App` (which imports `Header`, which
// imports this module) before calling `createRoot(...).render(...)`, so
// this line always runs ahead of the first paint — no dark-then-light (or
// light-then-dark) flash on reload, and no dependence on an effect firing
// post-mount (see Header.tsx, which used to do this in a `useEffect` and no
// longer needs to).
applyBodyTheme(loadTheme());
