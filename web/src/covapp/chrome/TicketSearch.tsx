// The app-bar ticket picker (follow-up item 5c). The ⋮ menu used to list
// every attributed ticket flat, which a mature repo turns into hundreds of
// rows nobody can reach; pinning now lives in its own search box in the app
// bar, to the LEFT of the ⋮ menu rather than inside it, so the list is
// filtered rather than scrolled.
//
// "/" focuses it, matching the monitor's search box. The binding and the
// "don't eat a slash the user is typing" guard both come from `ui/shortcuts`
// and the focus registry from `ui/searchFocus`, so the two apps cannot
// disagree about what "/" means — but the listener is local rather than
// `useGlobalShortcuts`, which is wired to the monitor's command registry and
// zustand store that covapp deliberately does not carry.
import { useEffect, useRef, useState } from "react";

import { Input } from "@/components/base/input/input";
import { SearchIcon } from "@/ui/icons";
import { registerSearchInput } from "@/ui/searchFocus";
import { matchesBinding, SEARCH_BINDING, shouldSuppressSlash } from "@/ui/shortcuts";
import { cx } from "@/utils/cx";

import type { TicketSummary } from "../types";

/** How many options render at once. A filtered list is the affordance; a
 * long one is the problem being fixed, so the cap is deliberately small and
 * the remainder is COUNTED rather than silently dropped. */
const MAX_OPTIONS = 8;

export function TicketSearch({
  tickets,
  ticket,
  onPin,
}: {
  tickets: TicketSummary[];
  ticket: string | null;
  onPin: (id: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    registerSearchInput(inputRef.current);
    return () => registerSearchInput(null);
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!matchesBinding(e, SEARCH_BINDING)) return;
      // No overlay concept in covapp, so the second argument is always
      // false; the target check is what keeps a typed slash literal.
      if (shouldSuppressSlash(e.target, false)) return;
      const el = inputRef.current;
      if (el === null) return;
      e.preventDefault();
      el.focus();
      setOpen(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // A click anywhere else dismisses the options without pinning anything.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!(e.target instanceof Node)) return;
      if (boxRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const q = query.trim().toLowerCase();
  const matches = tickets.filter((t) => q === "" || t.id.toLowerCase().includes(q));
  const shown = matches.slice(0, MAX_OPTIONS);
  const overflow = matches.length - shown.length;

  function pin(id: string | null) {
    onPin(id);
    setQuery("");
    setOpen(false);
  }

  return (
    <div ref={boxRef} data-testid="ticket-search" className="relative">
      <Input
        ref={inputRef}
        data-testid="ticket-search-input"
        aria-label="Pin a ticket by id"
        size="sm"
        icon={SearchIcon}
        shortcut="/"
        placeholder="Pin ticket…"
        value={query}
        onChange={(v: string) => {
          setQuery(v);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && (
        <div
          data-testid="ticket-search-options"
          role="listbox"
          className="absolute right-0 z-50 mt-1 w-72 overflow-hidden rounded-lg border
            border-secondary bg-primary py-1 shadow-lg"
        >
          <button
            type="button"
            role="option"
            aria-selected={ticket === null}
            data-testid="ticket-search-option-all"
            onClick={() => pin(null)}
            className="flex w-full items-center px-3 py-1.5 text-left text-xs text-tertiary
              hover:bg-secondary"
          >
            All tickets
          </button>
          {shown.map((t) => (
            <button
              key={t.id}
              type="button"
              role="option"
              aria-selected={ticket === t.id}
              data-testid={`ticket-search-option-${t.id}`}
              onClick={() => pin(t.id)}
              className={cx(
                "flex w-full items-center px-3 py-1.5 text-left font-mono text-xs hover:bg-secondary",
                ticket === t.id ? "font-semibold text-primary" : "text-secondary",
              )}
            >
              {t.id}
            </button>
          ))}
          {shown.length === 0 && (
            <p data-testid="ticket-search-empty" className="px-3 py-1.5 text-xs text-quaternary">
              No matching ticket
            </p>
          )}
          {overflow > 0 && (
            <p
              data-testid="ticket-search-overflow"
              className="border-t border-secondary px-3 pt-1.5 pb-1 text-xs text-quaternary"
            >
              …{overflow} more — keep typing
            </p>
          )}
        </div>
      )}
    </div>
  );
}
