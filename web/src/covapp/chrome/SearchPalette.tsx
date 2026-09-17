// The ⌘K search palette (coverage search spec §3). Untitled UI first: the
// field is the free dropdown-search pattern (Autocomplete + SearchField +
// InputBase), the toggles a vendored ButtonGroup, the list the vendored
// Dropdown menu, the pill a BadgeWithDot. The modal shell is the same
// react-aria stack + classes as ui/CommandMenu.tsx, the in-house stand-in
// for the PRO command menu. Data: the two lazily loaded singleton chunks
// (data.ts); engine: search.ts.
import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Focusable } from "react-aria";
import type { Key, Selection } from "react-aria-components";
import { Autocomplete, Dialog, Modal, ModalOverlay, SearchField } from "react-aria-components";

import { BadgeWithDot } from "@/components/base/badges/badges";
import { ButtonGroup, ButtonGroupItem } from "@/components/base/button-group/button-group";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { InputBase } from "@/components/base/input/input";
import { Tooltip } from "@/components/base/tooltip/tooltip";
import { SearchLgIcon } from "@/ui/icons";

import { loadSearchChunk, loadSymbolsChunk, loadTicketChunk, StampMismatchError } from "../data";
import { navigateHash, useFocus } from "../focus";
import { encodePath } from "../format";
import { coverageSegmentsOf, hashPathOf, useHash } from "../hashState";
import { compileQuery, type FileMatches, searchFiles, searchFunctions } from "../search";
import { findNode } from "../stats";
import type { FunctionEntry, IndexPayload, SearchChunk, SymbolsChunk } from "../types";

const TEXT_RESULT_CAP = 500;
const FUNCTION_RESULT_CAP = 200;
export const REGEX_PREF_KEY = "otto-cov:palette:regex";
export const UNCOVERED_PREF_KEY = "otto-cov:palette:uncovered";

// Session-only query memory (spec §3.2: "Reopening restores the last query
// and mode for the session"), NOT localStorage: every page
// (DirectoryPage/FilePage/RunsPage/TicketsPage) renders its own <AppShell>,
// and App.tsx's route returns a different component per route, so
// navigating — the dominant flow, open, pick a result, land on the file
// page, ⌘K again — remounts SearchPalette and would otherwise reset `query`
// to "" on every navigation. `regex`/`uncovered` already survive that via
// localStorage; the spec draws the line at the query itself, which must
// live only as long as the tab/session, not persist across visits — a
// module-level variable is exactly that: it outlives one component
// instance (the remount) but not a page reload.
let sessionQuery = "";

/** Test-only escape hatch: `vitest.config`'s `sequence.shuffle` runs this
 * file's tests in random order, and several pre-existing tests type into the
 * query field without clearing it afterward — without a reset, whichever of
 * those happens to run first (order varies per seed) would leak its query
 * into every later test in the same run via this module-level variable. */
export function _resetSessionQueryForTests(): void {
  sessionQuery = "";
}

type DataState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; reason: "stamp" | "other" }
  | { status: "ready"; search: SearchChunk; symbols: SymbolsChunk };

interface Results {
  groups: FileMatches[];
  functions: FunctionEntry[];
  total: number;
  fileCount: number;
  capped: boolean;
  error: string | null;
  short: boolean;
}

function readPref(key: string): boolean {
  return localStorage.getItem(key) === "1";
}

function writePref(key: string, on: boolean): void {
  if (on) localStorage.setItem(key, "1");
  else localStorage.removeItem(key);
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    // `ms <= 0` (tests pass `debounceMs={0}` for determinism) updates
    // synchronously within the effect rather than via a real `setTimeout`
    // task: a real 0ms timer is still a genuine macrotask, which can
    // outlive the test that scheduled it and fire during the NEXT test's
    // own user-event session — a race this component's Enter-key test hit
    // empirically (React's "Should not already be working", two act()
    // flushes overlapping across tests).
    if (ms <= 0) {
      setDebounced(value);
      return;
    }
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

function textTarget(
  path: string,
  line: number,
  query: string,
  regex: boolean,
  uncovered: boolean,
): string {
  const params = new URLSearchParams();
  params.set("lines", String(line));
  params.set("q", query);
  if (regex) params.set("re", "1");
  if (uncovered) params.set("unc", "1");
  return `#/coverage/${encodePath(path)}?${params.toString()}`;
}

function functionTarget(fn: FunctionEntry): string {
  // `line === 0` is an FNDA-only orphan (no FN: record) — there is no real
  // line to jump to, and the file page rejects `?lines=` values under 1
  // anyway, so the link should not carry one at all rather than assert a
  // line number that doesn't exist.
  if (fn.line === 0) return `#/coverage/${encodePath(fn.path)}`;
  return `#/coverage/${encodePath(fn.path)}?lines=${fn.line}`;
}

function LineText({ text, spans }: { text: string; spans: [number, number][] }) {
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const [start, end] of spans) {
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(
      // `${start}:${end}` (the span's own offsets), not the loop index —
      // spans never repeat within one line, so this is a genuinely stable
      // key, unlike an array index.
      <mark key={`${start}:${end}`} className="rounded-sm bg-brand-500/30 text-inherit">
        {text.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return <span className="truncate font-mono text-xs font-normal text-secondary">{parts}</span>;
}

export function SearchPalette({
  index,
  open,
  onClose,
  debounceMs = 50,
}: {
  index: IndexPayload;
  open: boolean;
  onClose: () => void;
  debounceMs?: number | undefined;
}) {
  const [query, setQueryState] = useState(() => sessionQuery);
  function setQuery(next: string): void {
    sessionQuery = next;
    setQueryState(next);
  }
  const [regex, setRegex] = useState(() => readPref(REGEX_PREF_KEY));
  const [uncovered, setUncovered] = useState(() => readPref(UNCOVERED_PREF_KEY));
  const [data, setData] = useState<DataState>({ status: "idle" });
  const [allowedPaths, setAllowedPaths] = useState<Set<string> | null>(null);
  const { ticket } = useFocus();
  const hash = useHash();
  const debounced = useDebounced(query, debounceMs);

  // Load both chunks on the first open, never at boot (spec §6.1). Guarded
  // by a ref, not `data.status`, in the dependency array: `data.status`
  // itself changes (idle -> loading) as a DIRECT effect of this effect's
  // own `setData` call below, which would otherwise re-run this effect and
  // fire a cleanup on the very instance whose promise is still in flight.
  //
  // Cancellation here is tied to UNMOUNT ONLY, never to `open` flipping
  // false: `open` toggles on every ⌘K/Escape while the loaded chunks stay
  // cached for the component's whole session-long lifetime (the app shell mounts it once per page), so
  // a close mid-load must not drop the in-flight resolve — dropping it
  // left `loadStartedRef.current` permanently `true` with `data` stuck on
  // `"loading"` forever, since nothing re-arms the guard on reopen. So
  // this effect returns NO cleanup of its own; `mountedRef` (set false
  // only by the unmount-only effect below) is what every `setData` call
  // checks instead.
  const loadStartedRef = useRef(false);
  const mountedRef = useRef(true);
  // Portal target for the Uncovered-only chip's Tooltip (below), so it
  // renders inside the modal's own DOM instead of a document.body sibling
  // — see that Tooltip's comment for why. STATE, not a plain `useRef` read
  // during render: a ref read at render time is `null` on the render that
  // mounts the Dialog, so the FIRST open only "works" because the one-shot
  // load effect happens to force a second render; every open after that
  // (the palette stays mounted; react-aria-components unmounts the Dialog
  // itself on close, so the ref goes back to null) would silently regress
  // to the un-pinned document.body portal. The callback ref triggers a
  // real re-render once the node exists, so every open picks it up.
  const [paletteEl, setPaletteEl] = useState<HTMLDivElement | null>(null);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );
  useEffect(() => {
    if (!open || loadStartedRef.current) return;
    loadStartedRef.current = true;
    setData({ status: "loading" });
    Promise.all([loadSearchChunk(), loadSymbolsChunk()])
      .then(([search, symbols]) => {
        if (mountedRef.current) setData({ status: "ready", search, symbols });
      })
      .catch((err: unknown) => {
        if (mountedRef.current)
          setData({
            status: "error",
            reason: err instanceof StampMismatchError ? "stamp" : "other",
          });
      });
  }, [open]);

  // Ticket scoping: the same file set the tree hides to (spec §3.3).
  useEffect(() => {
    const summary = ticket === null ? undefined : index.tickets.find((t) => t.id === ticket);
    if (summary === undefined) {
      setAllowedPaths(null);
      return;
    }
    let cancelled = false;
    loadTicketChunk(summary.chunk)
      .then((chunk) => {
        if (!cancelled) setAllowedPaths(new Set(chunk.files.map((f) => f.path)));
      })
      .catch(() => {
        if (!cancelled) setAllowedPaths(null);
      });
    return () => {
      cancelled = true;
    };
  }, [ticket, index]);

  const currentChunk = useMemo(() => {
    const segments = coverageSegmentsOf(hashPathOf(hash));
    if (segments === null || segments.length === 0) return;
    const node = findNode(index.tree, segments);
    return node !== null && !("dirs" in node) ? node.chunk : undefined;
  }, [hash, index]);

  const functionMode = debounced.startsWith("@");
  const needle = functionMode ? debounced.slice(1) : debounced;

  const results = useMemo<Results | null>(() => {
    if (data.status !== "ready") return null;
    const empty: Results = {
      groups: [],
      functions: [],
      total: 0,
      fileCount: 0,
      capped: false,
      error: null,
      short: false,
    };
    if (functionMode) {
      // A pinned ticket narrows the search to the files it touched (spec
      // §3.3) for functions too, not just text lines — scope BEFORE
      // calling searchFunctions rather than filtering its output, so the
      // cap/total/capped bookkeeping it already does stays honest against
      // the scoped set.
      const scoped =
        allowedPaths === null
          ? data.symbols.functions
          : data.symbols.functions.filter((f) => allowedPaths.has(f.path));
      const fn = searchFunctions(scoped, needle, FUNCTION_RESULT_CAP);
      return { ...empty, functions: fn.rows, total: fn.total, capped: fn.capped };
    }
    // An invalid pattern surfaces immediately, even under 2 characters
    // (spec: a bad regex is always worth reporting) — only a query that
    // actually COMPILES falls through to the short-query gate below.
    const compiled = compileQuery(needle, regex);
    if (compiled.re === undefined) return { ...empty, error: compiled.error };
    if (needle.length < 2) return { ...empty, short: true };
    const found = searchFiles(data.search.files, compiled.re, {
      uncoveredOnly: uncovered,
      cap: TEXT_RESULT_CAP,
      firstChunk: currentChunk,
      allowedPaths,
    });
    return {
      ...empty,
      groups: found.groups,
      total: found.total,
      fileCount: found.fileCount,
      capped: found.capped,
    };
  }, [data, functionMode, needle, regex, uncovered, currentChunk, allowedPaths]);

  // Row id -> hash target, for the Menu's onAction (click) path.
  const targets = useMemo(() => {
    const map = new Map<string, string>();
    if (results === null) return map;
    for (const fn of results.functions)
      map.set(`fn:${fn.chunk}:${fn.line}:${fn.name}`, functionTarget(fn));
    for (const g of results.groups)
      for (const l of g.lines)
        map.set(`${g.chunk}:${l.line}`, textTarget(g.path, l.line, needle, regex, uncovered));
    return map;
  }, [results, needle, regex, uncovered]);

  function go(target: string, keepOpen: boolean): void {
    // Not `location.hash = target`: its `hashchange` is a queued task, and a
    // Ctrl+K in that gap reaches the outgoing page's AppShell (each page
    // mounts its own).
    navigateHash(target);
    if (!keepOpen) onClose();
  }

  // Owns Enter, Ctrl/⌘+Enter, and Escape — all ahead of react-aria (capture
  // phase). Escape first: react-aria's SearchField treats Escape-with-
  // content as "clear the field" and stops propagation, so ModalOverlay's
  // isDismissable/dismiss-on-Escape never sees the key and the palette never
  // closes; ModalOverlay's own Escape handling still covers an EMPTY field
  // (nothing here to intercept), and a backdrop click still closes via
  // isDismissable regardless. The query is deliberately left untouched on
  // Escape — commit "the palette remembers its query for the session" made
  // it session-sticky on purpose, and closing must not clear it. Enter/
  // Ctrl+Enter: the focused row (react-aria's virtual focus marks it
  // `data-focused`), else the first row; Ctrl or ⌘ keeps the palette open
  // for stepping.
  function onKeyDownCapture(e: KeyboardEvent<HTMLDivElement>): void {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Enter") return;
    // Enter belongs to us only from the field or a result row; from
    // anything else focused inside the dialog (the Regex/Uncovered chips,
    // most concretely) it's react-aria's own Enter handling to give — a
    // chip's native ToggleButton activation, not "navigate to the first
    // row". Without this guard, tabbing to a chip and pressing Enter fell
    // through to the unconditional row lookup below and silently hijacked
    // the keystroke.
    if (!(e.target instanceof HTMLElement) || e.target.closest('[role="menuitem"], input') === null)
      return;
    const root = e.currentTarget;
    const row =
      root.querySelector<HTMLElement>('[role="menuitem"][data-focused]') ??
      root.querySelector<HTMLElement>('[role="menuitem"]');
    const target = row?.getAttribute("data-target");
    if (!target) return;
    e.preventDefault();
    e.stopPropagation();
    go(target, e.ctrlKey || e.metaKey);
  }

  const modeKeys = new Set<Key>([...(regex ? ["regex"] : []), ...(uncovered ? ["uncovered"] : [])]);
  function onModeChange(keys: Selection): void {
    const nextRegex = keys === "all" || keys.has("regex");
    const nextUncovered = keys === "all" || keys.has("uncovered");
    writePref(REGEX_PREF_KEY, nextRegex);
    writePref(UNCOVERED_PREF_KEY, nextUncovered);
    setRegex(nextRegex);
    setUncovered(nextUncovered);
  }

  const stateColor = (state: string): string | undefined => {
    if (state === "u") return index.state_colors.uncovered;
    if (state === "x") return index.state_colors.excluded;
    if (state === "s") return index.state_colors.stale;
    if (state === "a") return index.state_colors.aging;
    return undefined;
  };

  function emptyState(): ReactNode {
    if (data.status === "loading" || data.status === "idle")
      return (
        <p data-testid="search-loading" className="px-4 py-6 text-sm text-quaternary">
          Loading search index…
        </p>
      );
    if (data.status === "error")
      return (
        <p data-testid="search-error" className="px-4 py-6 text-sm text-error-primary">
          {data.reason === "stamp"
            ? "This report changed on disk — reload the page."
            : "The search index failed to load."}
        </p>
      );
    if (results?.error)
      return (
        <p data-testid="search-error" className="px-4 py-3 font-mono text-xs text-error-primary">
          {results.error}
        </p>
      );
    if (results?.short)
      return (
        <p data-testid="search-short" className="px-4 py-6 text-sm text-quaternary">
          Type at least 2 characters
        </p>
      );
    if (functionMode && data.status === "ready" && data.symbols.functions.length === 0)
      return (
        <p data-testid="search-empty" className="px-4 py-6 text-sm text-quaternary">
          No functions in this report
        </p>
      );
    return (
      <p data-testid="search-empty" className="px-4 py-6 text-sm text-quaternary">
        No matches for <span className="font-mono">{needle}</span>
        {uncovered && !functionMode ? " (uncovered only)" : ""}
      </p>
    );
  }

  // Flat indexes for `search-result-${i}` test ids, precomputed so the JSX
  // below stays assignment-free.
  const functionRows = (results?.functions ?? []).map((fn, i) => ({ fn, i }));
  let offset = 0;
  const groupRows = (results?.groups ?? []).map((group) => {
    const rows = group.lines.map((line, k) => ({ line, i: offset + k }));
    offset += group.lines.length;
    return { group, rows };
  });

  const showFooter =
    results !== null &&
    !results.short &&
    results.error === null &&
    (results.total > 0 || results.capped);

  return (
    <ModalOverlay
      isOpen={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      isDismissable
      className="fixed inset-0 z-50 flex justify-center bg-overlay/70 pt-[12vh]"
    >
      <Modal className="w-full max-w-160 px-4">
        {/* Neither the Dialog nor its wrapper div below may gain
        `relative`/`transform`/`filter` — any of those would turn this
        `overflow-hidden` into a clipping ancestor for the Uncovered chip's
        tooltip, which now portals INSIDE this subtree (its Tooltip's
        `UNSTABLE_portalContainer`, further below). */}
        <Dialog
          aria-label="Search code and functions"
          data-testid="search-palette"
          className="overflow-hidden rounded-xl bg-primary shadow-2xl ring-1 ring-secondary_alt outline-hidden"
        >
          <div ref={setPaletteEl} onKeyDownCapture={onKeyDownCapture}>
            <Autocomplete>
              <div className="flex items-center gap-3 border-b border-secondary p-3">
                <SearchField
                  aria-label="Search code, or @ for functions"
                  autoFocus
                  value={query}
                  onChange={setQuery}
                  className="flex grow"
                >
                  <InputBase
                    size="md"
                    icon={SearchLgIcon}
                    type="search"
                    placeholder="Search code… (@ for functions)"
                    data-testid="search-input"
                    inputClassName="font-mono"
                  />
                </SearchField>
                {!functionMode && (
                  <ButtonGroup
                    size="sm"
                    selectionMode="multiple"
                    selectedKeys={modeKeys}
                    onSelectionChange={onModeChange}
                    aria-label="Search options"
                  >
                    <ButtonGroupItem id="regex" data-testid="search-chip-regex">
                      Regex
                    </ButtonGroupItem>
                    {/* Untitled UI first: the vendored Tooltip wraps the
                    item in react-aria's `Focusable` (from "react-aria",
                    not tooltip.tsx's own `TooltipTrigger`, which renders a
                    second real <button> — invalid nested inside the
                    ButtonGroupItem's own <button>). `Focusable` is needed
                    because ButtonGroupItem's ToggleButton uses its own
                    useHover and never reads the ambient context a bare
                    Tooltip trigger relies on. `UNSTABLE_portalContainer`
                    pins the tooltip's portal inside the Dialog: unlike
                    Popover, react-aria's Tooltip never opts out of the
                    modal's ariaHideOutside, so a plain document.body
                    portal gets aria-hidden the instant it opens while this
                    modal is active. */}
                    <Tooltip
                      title="Lines no tier ever hit, across the whole report"
                      // exactOptionalPropertyTypes rejects an explicit
                      // `undefined` for this prop, so it's spread in only
                      // once `paletteEl` is set.
                      {...(paletteEl ? { UNSTABLE_portalContainer: paletteEl } : {})}
                    >
                      <Focusable>
                        <ButtonGroupItem id="uncovered" data-testid="search-chip-uncovered">
                          Uncovered only
                        </ButtonGroupItem>
                      </Focusable>
                    </Tooltip>
                  </ButtonGroup>
                )}
              </div>
              <Dropdown.Menu
                aria-label="Search results"
                className="max-h-[60vh]"
                renderEmptyState={emptyState}
                onAction={(key) => {
                  const target = targets.get(String(key));
                  if (target !== undefined) go(target, false);
                }}
              >
                {functionRows.map(({ fn, i }) => {
                  const id = `fn:${fn.chunk}:${fn.line}:${fn.name}`;
                  const hit = Object.values(fn.hits).some((n) => n > 0);
                  return (
                    <Dropdown.Item
                      key={id}
                      id={id}
                      textValue={fn.name}
                      data-testid={`search-result-${i}`}
                      data-target={targets.get(id)}
                    >
                      <span className="flex w-full items-center gap-3">
                        <span className="font-mono text-sm text-primary">{fn.name}</span>
                        <span className="truncate text-xs font-normal text-quaternary">
                          {fn.path}:{fn.line}
                        </span>
                        <span className="ml-auto">
                          <BadgeWithDot size="sm" color={hit ? "success" : "error"}>
                            {hit ? "covered" : "uncovered"}
                          </BadgeWithDot>
                        </span>
                      </span>
                    </Dropdown.Item>
                  );
                })}
                {groupRows.map(({ group, rows }) => (
                  <Dropdown.Section key={group.chunk} data-testid={`search-group-${group.chunk}`}>
                    <Dropdown.SectionHeader className="px-4 pt-2 pb-1 font-mono text-xs font-medium text-quaternary">
                      {group.path}
                    </Dropdown.SectionHeader>
                    {rows.map(({ line, i }) => {
                      const id = `${group.chunk}:${line.line}`;
                      const dot = stateColor(line.state);
                      return (
                        <Dropdown.Item
                          key={id}
                          id={id}
                          textValue={line.text}
                          data-testid={`search-result-${i}`}
                          data-target={targets.get(id)}
                        >
                          <span className="flex items-center gap-3">
                            <span
                              aria-hidden
                              className="size-2 shrink-0 rounded-full"
                              style={dot ? { background: dot } : { visibility: "hidden" }}
                            />
                            <span className="w-10 shrink-0 text-right font-mono text-xs font-normal text-quaternary">
                              {line.line}
                            </span>
                            <LineText text={line.text} spans={line.spans} />
                          </span>
                        </Dropdown.Item>
                      );
                    })}
                  </Dropdown.Section>
                ))}
              </Dropdown.Menu>
            </Autocomplete>
            {showFooter && results !== null && (
              <div
                data-testid="search-footer"
                className="border-t border-secondary px-4 py-2 text-xs text-quaternary"
              >
                {functionMode
                  ? `${results.total} function${results.total === 1 ? "" : "s"}`
                  : `${results.total} line${results.total === 1 ? "" : "s"} in ${results.fileCount} file${results.fileCount === 1 ? "" : "s"}`}
                {ticket !== null && allowedPaths !== null ? ` · in ticket ${ticket}` : ""}
                {results.capped
                  ? ` — showing the first ${functionMode ? FUNCTION_RESULT_CAP : TEXT_RESULT_CAP}, refine the query`
                  : ""}
              </div>
            )}
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
