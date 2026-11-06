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
 * stop. app.css's
 * `@custom-variant dark (&:where(.dark-mode, .dark-mode *));` points every
 * `dark:` utility (ours and Untitled UI's) at it too, so there is exactly
 * one selector to keep in sync, not two. We do not also toggle a `.dark`
 * class to shadow it — that was tried and reverted: a shadow class is
 * coupling with nobody paying for it, and it only exists to avoid touching
 * app.css. Editing app.css (ours, not vendored) instead of carrying a
 * second class is the actual fix. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark-mode", theme === "dark");
}

export function saveTheme(theme: Theme): void {
  localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

/** The plot's surface colour in each theme. */
export interface PlotSurfaces {
  light: string;
  dark: string;
}

/** The token the chart plot shows through to: the canvas is transparent over
 * the page's `bg-primary` (app.css's body), so this IS the plot surface. */
const PLOT_SURFACE_TOKEN = "--color-bg-primary";

type Declarations = Map<string, string>;

/** Every custom property the stylesheets declare on the base (`:root`) rule
 * and on the `.dark-mode` rule, later declarations winning, at any depth of
 * `@layer`/`@media` nesting. */
function readThemeDeclarations(): { base: Declarations; dark: Declarations } {
  const base: Declarations = new Map();
  const dark: Declarations = new Map();
  const visit = (rules: CSSRuleList): void => {
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSStyleRule) {
        const selectors = rule.selectorText.split(",").map((s) => s.trim());
        const target = selectors.includes(":root")
          ? base
          : selectors.length === 1 && selectors[0] === ".dark-mode"
            ? dark
            : null;
        if (target === null) continue;
        for (const name of Array.from(rule.style)) {
          if (name.startsWith("--")) target.set(name, rule.style.getPropertyValue(name).trim());
        }
      } else if ("cssRules" in rule) {
        visit((rule as CSSGroupingRule).cssRules);
      }
    }
  };
  for (const sheet of Array.from(document.styleSheets)) {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      continue; // a cross-origin sheet: none of ours
    }
    visit(rules);
  }
  return { base, dark };
}

const VAR_REF = /^var\(\s*(?<name>--[\w-]+)\s*(?:,\s*(?<fallback>.+))?\)$/u;

/** A token's value through the given scopes (first wins), following var()
 * references the same way, the way the cascade would under that theme. */
function resolveToken(name: string, scopes: Declarations[], depth = 0): string | undefined {
  const value = scopes.map((s) => s.get(name)).find((v) => v !== undefined);
  const ref = value === undefined ? null : VAR_REF.exec(value);
  if (ref?.groups === undefined || depth > 8) return value;
  const { name: next, fallback } = ref.groups as { name: string; fallback?: string };
  return resolveToken(next, scopes, depth + 1) ?? fallback?.trim();
}

let surfaces: PlotSurfaces | null = null;

/**
 * The chart plot's surface in BOTH themes, whichever is applied: read from
 * the `--color-bg-primary` the stylesheet declares, the light value on the
 * base rule and the dark one on `.dark-mode`. CSS variables inherit, so a
 * light-themed box inside `.dark-mode` cannot get the light token any other
 * way, and the vendored theme.css may not gain a light scope. The one owner
 * of the plot surface: callers paint it, never a hex constant. Cached once
 * both are found; a missing value throws in dev.
 */
export function plotSurfaces(): PlotSurfaces {
  if (surfaces !== null) return surfaces;
  const { base, dark } = readThemeDeclarations();
  const light = resolveToken(PLOT_SURFACE_TOKEN, [base]);
  const darkValue = resolveToken(PLOT_SURFACE_TOKEN, [dark, base]);
  if (light === undefined || darkValue === undefined) {
    const message = `plotSurfaces: no ${PLOT_SURFACE_TOKEN} for the ${light === undefined ? "light" : "dark"} theme in the loaded stylesheets`;
    if (import.meta.env.DEV) throw new Error(message);
    // Production: the applied theme's surface, so the preview still paints;
    // not cached, so a stylesheet that loads late is picked up next time.
    return { light: `var(${PLOT_SURFACE_TOKEN})`, dark: `var(${PLOT_SURFACE_TOKEN})` };
  }
  surfaces = { light, dark: darkValue };
  return surfaces;
}

// Apply before first paint (module side effect, same trick as v1).
applyTheme(loadTheme());
