// Module-level focus registry (spec §Global shortcuts): SeriesPanel
// registers its real <input> on mount; "/" asks here first. Same
// context-free registration pattern as ImportExport's picker.
let target: HTMLInputElement | null = null;

export function registerSearchInput(el: HTMLInputElement | null): void {
  target = el;
}

/** Focus the registered search input. Returns false when nothing usable is
 * registered — "/" is then a no-op (the command palette is a separate
 * affordance, opened with ⌘K, not by "/"). */
export function focusSearchInput(): boolean {
  if (target === null || !target.isConnected) return false;
  target.focus();
  return true;
}
