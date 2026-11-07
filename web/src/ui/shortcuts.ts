// The single source of keyboard-binding truth (spec §Command layer): the
// registry (commands.ts), the shortcut layer (useGlobalShortcuts.ts), and
// every visible hint (palette keycaps, dropdown addons, AppBar/search
// keycaps) all format and match THESE objects — nothing binding-shaped is
// ever stored twice, so a hint cannot drift from its handler.
//
// Reserved-key rule for future bindings (spec decision 4): never ⌘T/⌘N/⌘W/
// ⌘⇧T (browser-owned, uninterceptable), ⌘L (macOS captures focus-address-bar
// even while the dashboard has focus — the theme toggle's old ⌘L binding was
// removed for exactly this), or ⌘H/⌘M/⌘Q (macOS-owned); avoid ⌘D/⌘P
// (bookmark/print — interceptable but sacred). ⌘S below intentionally shadows
// save-page while the dashboard has focus.

import { useUiStore } from "./uiStore";

export interface Binding {
  /** KeyboardEvent.key, lowercase. */
  key: string;
  /** true = Cmd on mac / Ctrl elsewhere. Absent = bare key (only "/"). */
  mod?: boolean;
  /** Mod chords only: Shift must be held (true) or absent (default). */
  shift?: boolean;
}

/** The Apple-platform families whose modifier key is ⌘ rather than Ctrl. */
const APPLE_PLATFORM = /Mac|iPhone|iPad|iPod/u;

export function detectMac(platform: string): boolean {
  return APPLE_PLATFORM.test(platform);
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
  if (mac) return `⌘${binding.shift ? "⇧" : ""}${keyLabel}`;
  return `Ctrl ${binding.shift ? "Shift " : ""}${keyLabel}`;
}

export function formatBinding(binding: Binding): string {
  return formatBindingFor(binding, isMac());
}

/** Pure core, platform injected — what the unit tests exercise. */
export function matchesBindingFor(e: KeyboardEvent, binding: Binding, mac: boolean): boolean {
  if (e.altKey) return false;
  // Shift must match exactly for mod chords. Bare-key bindings (only "/"
  // today) must tolerate it: on intl layouts where "/" sits on a shifted key
  // (German, French, …) the browser still resolves e.key to the layout's
  // character with shiftKey=true, so rejecting shift outright would make the
  // advertised "/" binding permanently dead on those keyboards.
  if (binding.mod && e.shiftKey !== Boolean(binding.shift)) return false;
  if (e.key.toLowerCase() !== binding.key) return false;
  if (!binding.mod) return !e.ctrlKey && !e.metaKey;
  return mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

export function matchesBinding(e: KeyboardEvent, binding: Binding): boolean {
  return matchesBindingFor(e, binding, isMac());
}

/** True when keystrokes at `target` are text (or a widget's own keys): a
 * field, a contenteditable, a spinbutton, or an open menu/listbox. `instanceof
 * Element`, not `HTMLElement`: `.closest` lives on Element, and an SVG target
 * inside such a subtree is an SVGElement. */
export function isTextEditingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return (
    target.closest(
      'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="spinbutton"], [role="menu"], [role="listbox"]',
    ) !== null
  );
}

/** True while an overlay that owns editing is open: the event editor, the
 * mark popover or the palette. */
export function editingOverlayOpen(): boolean {
  const s = useUiStore.getState();
  return s.eventEditor !== null || s.markPopover !== null || s.paletteOpen;
}

/** The bare "/" guard (spec §Global shortcuts): a literal slash typed into
 * any field (series search, palette filter, a react-aria popover) must stay
 * a slash. Chords need no guard — they never type characters. */
export function shouldSuppressSlash(target: EventTarget | null, overlayOpen: boolean): boolean {
  if (overlayOpen || isTextEditingTarget(target)) return true;
  return target instanceof Element && target.closest('[role="dialog"]') !== null;
}

export const PALETTE_BINDING: Binding = { key: "k", mod: true };
export const SEARCH_BINDING: Binding = { key: "/" };
export const IMPORT_BINDING: Binding = { key: "i", mod: true };
export const EXPORT_BINDING: Binding = { key: "s", mod: true };
export const PAUSE_BINDING: Binding = { key: ".", mod: true };
// Event marking: clears the reserved-key rule above — E is not
// browser/macOS-owned (unlike the ⌘M this otherwise reads like).
export const MARK_NOW_BINDING: Binding = { key: "e", mod: true };
// Event undo / redo (#929): the platform's own chords. Both yield to text
// editing and editing overlays (Command.yieldsToEditing), so a field's
// native undo keeps working; ⌘⇧Z is redo's second chord (the macOS habit).
export const UNDO_BINDING: Binding = { key: "z", mod: true };
export const REDO_BINDING: Binding = { key: "y", mod: true };
export const REDO_ALT_BINDING: Binding = { key: "z", mod: true, shift: true };
