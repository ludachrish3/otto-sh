// Theme v2 (UX spec §7): initial theme seeds from the OS preference; the
// toggle is two-state light<->dark and persists the explicit choice. The
// storage key survives from v1, but v2 writes BOTH values (v1 only ever
// wrote "light" — dark was the implicit default; now absence = OS).
const STORAGE_KEY = "otto-theme";

export type Theme = "light" | "dark";

export function loadTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Toggle the theme class on <html>.
 *
 * ONE class: `.dark-mode`. Untitled UI's vendored theme.css gates its dark
 * token block on `.dark-mode` — that becomes *the* dark-mode class, full
 * stop. app.css's `@custom-variant dark (&:where(.dark-mode, .dark-mode
 * *));` points every `dark:` utility (ours and Untitled UI's) at it too, so
 * there is exactly one selector to keep in sync, not two. We do not also
 * toggle a `.dark` class to shadow it — that was tried and reverted: a
 * shadow class is coupling with nobody paying for it, and it only exists to
 * avoid touching app.css. Editing app.css (ours, not vendored) instead of
 * carrying a second class is the actual fix. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark-mode", theme === "dark");
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

// Apply before first paint (module side effect, same trick as v1).
applyTheme(loadTheme());
