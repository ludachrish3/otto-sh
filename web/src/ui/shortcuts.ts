// The single source of keyboard-binding truth (spec §Command layer): the
// registry (commands.ts), the shortcut layer (useGlobalShortcuts.ts), and
// every visible hint (palette keycaps, dropdown addons, AppBar/search
// keycaps) all format and match THESE objects — nothing binding-shaped is
// ever stored twice, so a hint cannot drift from its handler.
//
// Reserved-key rule for future bindings (spec decision 4): never ⌘T/⌘N/⌘W/
// ⌘⇧T (browser-owned, uninterceptable) or ⌘H/⌘M/⌘Q (macOS-owned); avoid
// ⌘D/⌘P (bookmark/print — interceptable but sacred). ⌘S and ⌘L below
// intentionally shadow save-page and focus-address-bar while the dashboard
// has focus.

export interface Binding {
  /** KeyboardEvent.key, lowercase. */
  key: string;
  /** true = Cmd on mac / Ctrl elsewhere. Absent = bare key (only "/"). */
  mod?: boolean;
}

export function detectMac(platform: string): boolean {
  return /Mac|iPhone|iPad|iPod/.test(platform);
}

let cachedIsMac: boolean | null = null;
function isMac(): boolean {
  cachedIsMac ??= detectMac(navigator.platform ?? "");
  return cachedIsMac;
}

/** Pure core, platform injected — what the unit tests exercise. */
export function formatBindingFor(binding: Binding, mac: boolean): string {
  const keyLabel = binding.key.length === 1 ? binding.key.toUpperCase() : binding.key;
  if (!binding.mod) return keyLabel;
  return mac ? `⌘${keyLabel}` : `Ctrl ${keyLabel}`;
}

export function formatBinding(binding: Binding): string {
  return formatBindingFor(binding, isMac());
}

/** Pure core, platform injected — what the unit tests exercise. */
export function matchesBindingFor(e: KeyboardEvent, binding: Binding, mac: boolean): boolean {
  if (e.altKey) return false;
  // Shift is only disqualifying for mod chords. Bare-key bindings (only "/"
  // today) must tolerate it: on intl layouts where "/" sits on a shifted key
  // (German, French, …) the browser still resolves e.key to the layout's
  // character with shiftKey=true, so rejecting shift outright would make the
  // advertised "/" binding permanently dead on those keyboards.
  if (binding.mod && e.shiftKey) return false;
  if (e.key.toLowerCase() !== binding.key) return false;
  if (!binding.mod) return !e.ctrlKey && !e.metaKey;
  return mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

export function matchesBinding(e: KeyboardEvent, binding: Binding): boolean {
  return matchesBindingFor(e, binding, isMac());
}

/** The bare "/" guard (spec §Global shortcuts): a literal slash typed into
 * any field (series search, palette filter, a react-aria popover) must stay
 * a slash. Chords need no guard — they never type characters. */
export function shouldSuppressSlash(target: EventTarget | null, overlayOpen: boolean): boolean {
  if (overlayOpen) return true;
  // `instanceof Element`, not `HTMLElement`: `.closest` lives on Element, and
  // an SVG event target (e.g. inside a role="dialog"/"menu" subtree that
  // contains an icon) is an SVGElement, not an HTMLElement -- the narrower
  // check let such a target dodge suppression entirely.
  if (!(target instanceof Element)) return false;
  return (
    target.closest(
      'input, textarea, select, [contenteditable="true"], [role="dialog"], [role="menu"], [role="listbox"]',
    ) !== null
  );
}

export const PALETTE_BINDING: Binding = { key: "k", mod: true };
export const SEARCH_BINDING: Binding = { key: "/" };
export const IMPORT_BINDING: Binding = { key: "i", mod: true };
export const EXPORT_BINDING: Binding = { key: "s", mod: true };
export const THEME_BINDING: Binding = { key: "l", mod: true };
export const PAUSE_BINDING: Binding = { key: ".", mod: true };
// Plan 5c marking: clears the reserved-key rule above — E is not
// browser/macOS-owned (unlike the ⌘M this otherwise reads like).
export const MARK_NOW_BINDING: Binding = { key: "e", mod: true };
