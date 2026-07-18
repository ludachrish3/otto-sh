// web/src/ui/SearchTrigger.tsx
// The AppBar's palette trigger (spec decision 7/9): a <button> DRESSED as
// the vendored sm input (wrapper classes mirror InputBase's AriaGroup —
// rounded-lg bg-primary shadow-xs inset ring-primary) with the "/" keycap.
// It is not a real input on purpose: focusing it must not start text
// entry, it opens the palette, which owns the real filter field.
import { SearchLg } from "@untitledui/icons";

import { Kbd } from "./Kbd";
import { formatBinding, SEARCH_BINDING } from "./shortcuts";
import { useUiStore } from "./uiStore";

export function SearchTrigger() {
  const openPalette = useUiStore((s) => s.actions.openPalette);
  return (
    <button
      type="button"
      data-testid="search-trigger"
      aria-label="Search (press / or the command menu)"
      onClick={openPalette}
      className="flex w-50 cursor-pointer items-center gap-2 rounded-lg bg-primary py-1 pr-1.5
        pl-2.5 text-sm text-quaternary shadow-xs ring-1 ring-primary outline-focus-ring
        transition duration-100 ease-linear ring-inset hover:bg-primary_hover
        focus-visible:outline-2 focus-visible:-outline-offset-2"
    >
      <SearchLg aria-hidden className="size-4 shrink-0 text-fg-quaternary" />
      <span className="grow text-left text-placeholder">Search…</span>
      <Kbd>{formatBinding(SEARCH_BINDING)}</Kbd>
    </button>
  );
}
