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

/** Toggle the `dark` class on <html> — what app.css's @custom-variant reads. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

// Apply before first paint (module side effect, same trick as v1).
applyTheme(loadTheme());
