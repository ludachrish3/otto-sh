// Report-wide context-focus filter: a single pinned run-label the
// app-bar chip / ⋮ menu / DirectoryPage / FilePage / RunsPage all recompute
// against. State lives in the HASH-EMBEDDED route query
// (`#/coverage/path?ctx=<label>`) — deliberately NOT the real pre-`#`
// `location.search`: wouter's own `useSearch()`/`navigate()` (see
// node_modules/wouter/src/use-hash-location.js and src/index.js) only ever
// read/write that real query, never a hash-embedded one, and — verified
// empirically against this project's pinned wouter version — regexparam's
// compiled `/coverage/*` pattern SWALLOWS a literal "?ctx=..." suffix left
// unstripped in the hash into its wildcard capture (`/coverage/a.c?ctx=x`
// captures as one segment "a.c?ctx=x"), corrupting `findNode` segment
// resolution; the exact `/coverage` route stops matching at all. So this
// module owns its own tiny hash/query parsing (`parseHashQuery`/
// `setHashQuery`/`replaceHashQuery` — push vs. replace, see their doc
// comments) instead of reaching for wouter's hooks, AND exports a drop-in
// replacement `useHashLocation` for `<Router hook={...}>` (App.tsx) that
// always strips the query before route matching sees it.
//
// Mirrors to `localStorage["otto-cov:<stamp>:focus"]` (stamp-namespaced so
// two reports served from one shared CI/artifacts origin don't stomp each
// other's pinned focus).
//
// Centralized here rather than per-page: ONE `FocusProvider` (mounted once
// at App.tsx's root, inside `ToastProvider` — `setFocus` toasts on
// pin/clear) owns the state, the persistence, and the "survive navigation"
// correction. Every page reads/writes focus through `useFocus()` only —
// never `location.hash`/`localStorage` directly.
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import { groupContexts } from "./contexts";
import { getIndex } from "./data";
import { useToast } from "./Toast";
import type { IndexPayload } from "./types";

const CTX_PARAM = "ctx";
// The ticket-context (denominator) filter's own hash-query param and
// storage key — a SECOND, entirely independent pinned value sharing
// this module's hash-query/localStorage machinery (boot precedence,
// Back/Forward stamping, "unknown value treated as cleared") rather than a
// parallel implementation, but never sharing STATE with `focus`/CTX_PARAM:
// `ctx` narrows the numerator (only that run's hits count, all code stays
// visible), `ticket` narrows the denominator (only that ticket's lines are
// in scope at all) — opposite axes that must compose, so setting/clearing
// one must never read or write the other's param/storage key.
const TICKET_PARAM = "ticket";

// Manual-overrides spec §6: a THIRD independent pinned value —
// "hide asserted coverage" — sharing this module's hash-query/localStorage
// machinery (boot precedence, Back/Forward stamping) exactly like `ticket`
// above, but a plain boolean rather than an id resolved against report data,
// so it needs no `resolveXxx`/"unknown value" degradation: the param is
// either present (serialized `"1"`) or absent, never a third "unknown"
// state. Composes with BOTH `focus`/`ticket` the same way they compose with
// each other — its own param/storage key, touched by nothing else.
const ASSERTED_PARAM = "asserted";

function storageKey(stamp: string): string {
  return `otto-cov:${stamp}:focus`;
}

function ticketStorageKey(stamp: string): string {
  return `otto-cov:${stamp}:ticket`;
}

function assertedStorageKey(stamp: string): string {
  return `otto-cov:${stamp}:asserted`;
}

function rawHash(): string {
  const { hash } = window.location;
  return hash.startsWith("#") ? hash.slice(1) : hash;
}

/** Splits a hash-fragment string (sans leading "#") into its path and raw
 * (undecoded) query, e.g. "/coverage/a.c?ctx=x" -> ["/coverage/a.c",
 * "ctx=x"]; no "?" -> the whole string is the path, query "". */
function splitHash(hash: string): [path: string, query: string] {
  const i = hash.indexOf("?");
  return i === -1 ? [hash, ""] : [hash.slice(0, i), hash.slice(i + 1)];
}

/** Reads the CURRENT hash fragment's query part. See this module's header
 * comment for why this is NOT wouter's `useSearch()`/`useLocation()`. */
export function parseHashQuery(): URLSearchParams {
  const [, query] = splitHash(rawHash());
  return new URLSearchParams(query);
}

/** Writes a full hash string via `history.replaceState` — no new session-
 * history entry — then dispatches a synthetic `hashchange` (replaceState
 * doesn't fire one natively) so wouter's subscription and this module's
 * own `useHashLocation`/`FocusProvider` notice the change. Shared by
 * `useHashLocation`'s `navigate(..., {replace: true})` branch and
 * `replaceHashQuery` below — both are "rewrite the current entry in
 * place" writes, never a real navigation.
 *
 * Passes `window.history.state` through (NOT `null`) so a REWRITE never
 * clobbers `HISTORY_STAMP_KEY` (below) if the current entry already
 * carries one — required for `FocusProvider`'s Back/Forward detection to
 * survive its own reconciliation writes (the entry this function rewrites
 * is often the exact one `stampCurrentEntry` is about to mark, or already
 * has). */
function replaceHash(next: string): void {
  const url = new URL(window.location.href);
  url.hash = next;
  window.history.replaceState(window.history.state, "", url);
  window.dispatchEvent(
    typeof HashChangeEvent !== "undefined"
      ? new HashChangeEvent("hashchange")
      : new Event("hashchange"),
  );
}

/** Same shape as `replaceHash`, but via `history.pushState` (a new session-
 * history entry, `state: null` — the same as any fresh push per the
 * `isKnownEntry()` doc comment below) — for real in-app navigations
 * (`useHashLocation`'s plain, non-`replace` `navigate()`). A bare
 * `window.location.hash = next` assignment would ALSO push a new entry, but
 * per the HTML spec its `hashchange` fires as a QUEUED TASK, not
 * synchronously — verified empirically in this project's jsdom (a
 * `location.hash` write updates `.hash` immediately but the event only
 * arrives on a later tick), which lands the resulting route change outside
 * any `act()` a caller's `fireEvent` wrapped, tripping this project's
 * console-error-is-a-failure gate on route-driven tests (App.test.tsx's "%"
 * round-trip regression). Dispatching manually, like wouter's own
 * `navigate()` and this module's `replaceHash`, keeps `useHashLocation` a
 * genuinely faithful drop-in — same synchronous contract wouter's raw hook
 * already had. */
function pushHash(next: string): void {
  const url = new URL(window.location.href);
  url.hash = next;
  window.history.pushState(null, "", url);
  window.dispatchEvent(
    typeof HashChangeEvent !== "undefined"
      ? new HashChangeEvent("hashchange")
      : new Event("hashchange"),
  );
}

/** `history.state` key `FocusProvider` stamps onto every session-history
 * entry it has itself settled (via `stampCurrentEntry`, below) — the
 * Back/Forward-vs-in-app-navigation discriminator its "survive
 * navigation" effect needs. See that effect's doc comment for why a
 * `popstate`/`history.length`-based heuristic (this module's first
 * attempt) is NOT reliable and this replaces it. */
const HISTORY_STAMP_KEY = "ottoCov";

interface StampedState {
  [HISTORY_STAMP_KEY]?: true;
}

/** Marks the CURRENT session-history entry as "known to this app" —
 * idempotent (a no-op `replaceState` call is skipped) and URL-preserving
 * (the 2-arg `replaceState(state, title)` form never touches
 * `location.href`). Call once, at the end of handling any hash
 * transition this module has fully processed (boot, and every
 * "survive navigation" pass) — never at push time itself, which is
 * exactly what keeps a genuinely fresh push's `history.state` reading
 * `null` until THIS module has had a chance to look at it. */
function stampCurrentEntry(): void {
  const prev = (window.history.state ?? {}) as StampedState;
  if (prev[HISTORY_STAMP_KEY]) return;
  window.history.replaceState({ ...prev, [HISTORY_STAMP_KEY]: true }, "");
}

/** Whether the entry the browser is CURRENTLY on was previously settled
 * by `stampCurrentEntry`. A fresh entry always lands with `state: null` —
 * whether it's a native `location.hash = …` push (`setHashQuery` or a
 * plain `<a href="#/...">` click), which browsers create with `state:
 * null` on their own, or this module's own `navigate()`, which pushes via
 * `pushHash`'s explicit `history.pushState(null, ...)` — never something
 * we can stamp in advance, since we only ever stamp AFTER already having
 * processed a hash transition once. So `isKnownEntry() === true` can
 * only mean "we've been at this exact history position before", i.e. a
 * real Back/Forward (or `history.go`) landed here — never a fresh
 * push. */
function isKnownEntry(): boolean {
  const state = window.history.state as StampedState | null;
  return Boolean(state?.[HISTORY_STAMP_KEY]);
}

/** Rewrites the hash fragment's query in place, preserving its path part,
 * via a plain `location.hash` assignment — not wouter's `navigate()`
 * (which sends a "?..." suffix on `to` to the real pre-`#`
 * `location.search` instead, per the header comment). A `location.hash`
 * write fires a native `hashchange` event AND pushes a new session-history
 * entry (same as `history.pushState`) — correct here because every caller
 * of `setHashQuery` is an explicit user action (`setFocus` pinning/
 * clearing), which SHOULD leave a Back-button stop. No-ops (no write, no
 * event) when the computed hash already matches. Reconciliation writes
 * that are NOT a user action (boot restore, the navigation-survival
 * re-append) must use `replaceHashQuery` instead — see its doc comment. */
export function setHashQuery(mutate: (params: URLSearchParams) => void): void {
  const [path, query] = splitHash(rawHash());
  const params = new URLSearchParams(query);
  mutate(params);
  const qs = params.toString();
  const next = (path || "/") + (qs ? `?${qs}` : "");
  if (next !== rawHash()) window.location.hash = next;
}

/** Same contract as `setHashQuery`, but via `replaceHash` (no new
 * session-history entry) — for RECONCILIATION writes only: `FocusProvider`'s
 * boot-time write-back and its navigation-survival re-append. Both react
 * to something that already happened (page load, or another navigation
 * that already pushed its own entry) rather than being the user action
 * itself, so they must not ALSO push — otherwise every in-app navigation
 * while focused produces two entries (the plain nav's, then this one's),
 * and Back lands on the ctx-less intermediate entry, which the re-append
 * effect immediately corrects forward again — Back can never actually
 * leave the page. `setFocus`'s own pin/clear writes (a real user action)
 * still go through `setHashQuery`'s push semantics, unchanged. */
export function replaceHashQuery(mutate: (params: URLSearchParams) => void): void {
  const [path, query] = splitHash(rawHash());
  const params = new URLSearchParams(query);
  mutate(params);
  const qs = params.toString();
  const next = (path || "/") + (qs ? `?${qs}` : "");
  if (next !== rawHash()) replaceHash(next);
}

/** `<Router hook={...}>` (App.tsx) — a drop-in replacement for wouter's own
 * `wouter/use-hash-location`, differing in exactly one respect: it ALWAYS
 * strips a trailing "?..." query before handing the path to wouter's route
 * matcher (see header comment for why that's required once `ctx` lives
 * inside the hash). `navigate()` here deliberately does NOT try to
 * preserve an existing query when writing a plain path — `FocusProvider`'s
 * re-append effect below corrects that after ANY navigation (this hook's
 * own `navigate`, wouter's, or a plain `<a href="#/...">` click), so this
 * function doesn't need special-case query-preserving logic itself. */
export function useHashLocation(): [string, (to: string, opts?: { replace?: boolean }) => void] {
  const subscribe = useCallback((callback: () => void) => {
    window.addEventListener("hashchange", callback);
    return () => window.removeEventListener("hashchange", callback);
  }, []);
  const getSnapshot = useCallback(() => splitHash(rawHash())[0] || "/", []);
  const getServerSnapshot = useCallback(() => "/", []);
  const location = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const navigate = useCallback((to: string, opts: { replace?: boolean } = {}) => {
    const path = to.startsWith("/") ? to : `/${to}`;
    if (opts.replace) {
      replaceHash(path);
    } else {
      pushHash(path);
    }
  }, []);

  return [location, navigate];
}

export interface UseFocusResult {
  focus: string | null;
  setFocus: (label: string | null) => void;
  /** The pinned ticket id (`?ticket=<id>`), or `null` — resolved/
   * persisted exactly like `focus` above (same boot precedence, same
   * Back/Forward stamping), just against `index.tickets` instead of
   * `groupContexts(index)`, and via its own, entirely independent param/
   * storage key so it can never clear (or be cleared by) `focus`. */
  ticket: string | null;
  setTicket: (id: string | null) => void;
  /** Whether asserted (override-sourced) coverage is currently
   * hidden from every stat recompute/cell that consumes it — resolved/
   * persisted exactly like `ticket` above (same boot precedence, same
   * Back/Forward stamping), but via its own, entirely independent param/
   * storage key so toggling it can never touch `focus`/`ticket`. */
  hideAsserted: boolean;
  setHideAsserted: (v: boolean) => void;
}

const FocusContext = createContext<UseFocusResult | null>(null);

/** `null` for an unknown label (a stale deep link or localStorage entry —
 * e.g. the report was regenerated with a different run set, or storage was
 * hand-edited) as well as for `null` itself — callers always get back
 * either a label `groupContexts(index)` can resolve, or `null`, never a
 * dangling pointer. */
function resolveLabel(label: string | null, index: IndexPayload | null): string | null {
  if (label === null || index === null) return null;
  const known = groupContexts(index).some((ctx) => ctx.label === label);
  return known ? label : null;
}

/** `resolveLabel`'s ticket counterpart — same "unknown/stale value degrades
 * to cleared, never a crash" contract, checked against `index.tickets` (the
 * report's per-ticket rollups) instead of `groupContexts(index)`. */
function resolveTicket(id: string | null, index: IndexPayload | null): string | null {
  if (id === null || index === null) return null;
  const known = index.tickets.some((t) => t.id === id);
  return known ? id : null;
}

/** Boot precedence (spec-pinned): the hash query wins; else localStorage. */
function initialFocus(index: IndexPayload | null): string | null {
  const fromQuery = parseHashQuery().get(CTX_PARAM);
  if (fromQuery !== null) return resolveLabel(fromQuery, index);
  if (index !== null) {
    const stored = localStorage.getItem(storageKey(index.stamp));
    if (stored !== null) return resolveLabel(stored, index);
  }
  return null;
}

/** `initialFocus`'s ticket counterpart — same query>storage precedence. */
function initialTicket(index: IndexPayload | null): string | null {
  const fromQuery = parseHashQuery().get(TICKET_PARAM);
  if (fromQuery !== null) return resolveTicket(fromQuery, index);
  if (index !== null) {
    const stored = localStorage.getItem(ticketStorageKey(index.stamp));
    if (stored !== null) return resolveTicket(stored, index);
  }
  return null;
}

/** `initialFocus`/`initialTicket`'s boolean counterpart — same query>storage
 * precedence, but a bare presence test (`"1"` in the query, or ANY
 * non-`null` stored value) rather than a lookup against report data: unlike
 * a label/id, a boolean has no "unknown value" case to degrade. */
function initialHideAsserted(index: IndexPayload | null): boolean {
  const fromQuery = parseHashQuery().get(ASSERTED_PARAM);
  if (fromQuery !== null) return fromQuery === "1";
  if (index !== null) {
    return localStorage.getItem(assertedStorageKey(index.stamp)) !== null;
  }
  return false;
}

/** Mounted once at App.tsx's root, inside `ToastProvider` (this provider
 * calls `useToast()` itself, to fire the pin/clear toast from the ONE
 * place `focus` actually changes, rather than duplicating that call at
 * every `setFocus` call site). */
export function FocusProvider({ children }: { children: ReactNode }) {
  const index = getIndex();
  const { show } = useToast();
  const [focus, setFocusState] = useState<string | null>(() => initialFocus(index));
  // A second, independent piece of state — never derived from or combined
  // with `focus` above (see this module's header comment on why ctx/ticket
  // must compose rather than share a slot).
  const [ticket, setTicketState] = useState<string | null>(() => initialTicket(index));
  // A THIRD, independent piece of state — never derived from or
  // combined with `focus`/`ticket` above (see this module's header comment
  // and `ASSERTED_PARAM`'s doc comment on why hide-asserted must compose
  // with both rather than share a slot).
  const [hideAsserted, setHideAssertedState] = useState<boolean>(() => initialHideAsserted(index));

  // Boot reconciliation (runs once, at mount): whichever source won the
  // query>storage precedence above becomes the sole source going forward —
  // write it into both channels (a no-op if already in agreement), or
  // clear both if boot resolved to null. That last branch is what makes an
  // unknown/stale label "no crash, treated as cleared" rather than a
  // dangling ?ctx= or storage entry the user can never escape. Not a user
  // action, so no toast here — only explicit `setFocus`/`setTicket` calls
  // below toast. `replaceHashQuery`, NOT `setHashQuery`: this fires on
  // every page load (including a bare reload with a pre-existing history),
  // so pushing here would grow session history by one entry per load with
  // no corresponding user action — see `replaceHashQuery`'s doc comment.
  //
  // Ticket reconciliation runs the SAME shape as ctx's, as its own
  // `replaceHashQuery` call — each call independently re-reads the
  // CURRENT hash (see `setHashQuery`/`replaceHashQuery`'s "read, mutate,
  // write" contract) and only ever touches its own param, so doing ctx
  // then ticket sequentially composes correctly (whichever ran first is
  // still present in the hash when the second one reads it) rather than one
  // clobbering the other.
  //
  // `stampCurrentEntry()` at the end (unconditionally, either branch): the
  // entry the app BOOTED on needs to be marked "known" too, same as every
  // entry the "survive navigation" effect below settles — otherwise the
  // very first Back press (with no in-app navigation in between) would
  // land on an unstamped boot entry and get misread as a fresh push
  // rather than the real traversal it is.
  // biome-ignore lint/correctness/useExhaustiveDependencies: boot-only effect (see comment above) — deliberately `[]`, not re-run on `focus`/`ticket`/`index` changes (setFocus/setTicket keep both channels in sync for those directly)
  useEffect(() => {
    if (focus === null) {
      if (parseHashQuery().has(CTX_PARAM)) replaceHashQuery((p) => p.delete(CTX_PARAM));
      if (index !== null) localStorage.removeItem(storageKey(index.stamp));
    } else {
      replaceHashQuery((p) => p.set(CTX_PARAM, focus));
      if (index !== null) localStorage.setItem(storageKey(index.stamp), focus);
    }
    if (ticket === null) {
      if (parseHashQuery().has(TICKET_PARAM)) replaceHashQuery((p) => p.delete(TICKET_PARAM));
      if (index !== null) localStorage.removeItem(ticketStorageKey(index.stamp));
    } else {
      replaceHashQuery((p) => p.set(TICKET_PARAM, ticket));
      if (index !== null) localStorage.setItem(ticketStorageKey(index.stamp), ticket);
    }
    if (!hideAsserted) {
      if (parseHashQuery().has(ASSERTED_PARAM)) replaceHashQuery((p) => p.delete(ASSERTED_PARAM));
      if (index !== null) localStorage.removeItem(assertedStorageKey(index.stamp));
    } else {
      replaceHashQuery((p) => p.set(ASSERTED_PARAM, "1"));
      if (index !== null) localStorage.setItem(assertedStorageKey(index.stamp), "1");
    }
    stampCurrentEntry();
  }, []);

  // Survive navigation: wouter's navigate() and a plain `<a href="#/...">`
  // click both REPLACE the whole hash fragment, dropping an embedded
  // `?ctx=` — restore it after every such `hashchange` so the chip stays
  // pinned across pages (the reassert branch below). A real Back/Forward
  // is the opposite case: the landed entry's OWN `?ctx=` (or lack of one)
  // is exactly what was true at that point in history, so this ADOPTS it
  // into `focus` instead of reasserting — otherwise Back could never
  // actually leave a focused page.
  //
  // Discriminating the two: `isKnownEntry()` (see its doc comment) —
  // `history.state` carries our own stamp iff this module has already
  // settled this exact history position before, which can only be true on
  // a real Back/Forward/`history.go` landing, never a brand-new push (a
  // push always creates its entry with `state: null`, and nothing stamps
  // an entry before this module has processed a hashchange for it at
  // least once).
  //
  // This replaces an EARLIER version of this discriminator that combined
  // `popstate` + "did `history.length` grow" — provably wrong, found by
  // review: a push made from MID-STACK (i.e. after at least one Back)
  // TRUNCATES the forward entries it replaces, so `history.length` does not
  // reliably grow on a push either — e.g. pin (push B) -> navigate (push C)
  // -> Back once (now at B, C still "forward") -> click any link (push D,
  // which drops C and appends D: length unchanged) got misread as a
  // Back/Forward and silently cleared the pin. The `history.state` stamp
  // has no such failure mode: it doesn't infer from a COUNT that can
  // coincidentally match, it reads a durable per-entry marker this module
  // itself wrote.
  //
  // Landing on an entry this module has genuinely never seen (a fresh
  // push, OR a Back/Forward far enough to reach a pre-app entry that
  // predates this page load, which was never stamped) both fall through
  // to the SAME reassert branch below — deliberately: an entry this
  // module can't prove is a real "clear focus" traversal is treated as
  // "keep reasserting", the old round-1 behavior. Silently DROPPING a
  // user's pin on an ambiguous landing is the worse failure; reasserting
  // it one entry too eagerly just costs an extra Back press, which the
  // (unambiguous, stamped) entries below it will still honor correctly.
  //
  // `resolveLabel`, not the raw param, in the adopt branch so a Back to a
  // stamped-but-now-stale/unknown label still degrades to "cleared"
  // rather than crashing (same contract as boot). Explicit `setFocus`
  // calls update `focus` state synchronously, so neither branch here ever
  // fights it.
  //
  // `replaceHashQuery`, NOT `setHashQuery`, in the reassert branch: the
  // navigation that triggered this handler ALREADY pushed its own history
  // entry (wouter's navigate() or the browser's own `<a href>` handling) —
  // pushing AGAIN here would leave a ctx-less intermediate entry behind
  // every single in-app navigation while focused, which is the push-based
  // version of the same trap. Rewriting the CURRENT (just-pushed) entry in
  // place instead means every navigation while focused costs exactly one
  // Back-button stop, and it always already carries the right `?ctx=`.
  //
  // The pinned ticket mirrors every branch below independently — its own
  // `resolveTicket`/`ticketStorageKey`/`TICKET_PARAM`, compared against its
  // own `ticket` state — so a landing that changes ctx but not ticket (or
  // vice versa) only touches the one that actually changed, and the
  // reassert branch's single `replaceHashQuery` call composes both params
  // in one rewrite (each `p.set` call only touches its own key, per
  // `URLSearchParams`'s ordinary contract) rather than two separate writes
  // racing each other.
  //
  // `replaceHashQuery` itself is guarded internally (a no-op mutate never
  // actually writes — see its own doc comment), so calling it
  // unconditionally here was never a CORRECTNESS bug, only pointless work
  // (constructing a `URLSearchParams`, running both `if` checks,
  // serializing back to a string) on every navigation while NEITHER filter
  // is pinned — the overwhelmingly common case. The outer guard here skips
  // that work entirely rather than relying on the callee to no-op it.
  useEffect(() => {
    function onHashChange() {
      if (isKnownEntry()) {
        const params = parseHashQuery();
        const landedFocus = resolveLabel(params.get(CTX_PARAM), index);
        if (landedFocus !== focus) {
          setFocusState(landedFocus);
          if (index !== null) {
            if (landedFocus === null) localStorage.removeItem(storageKey(index.stamp));
            else localStorage.setItem(storageKey(index.stamp), landedFocus);
          }
        }
        const landedTicket = resolveTicket(params.get(TICKET_PARAM), index);
        if (landedTicket !== ticket) {
          setTicketState(landedTicket);
          if (index !== null) {
            if (landedTicket === null) localStorage.removeItem(ticketStorageKey(index.stamp));
            else localStorage.setItem(ticketStorageKey(index.stamp), landedTicket);
          }
        }
        const landedHideAsserted = params.get(ASSERTED_PARAM) === "1";
        if (landedHideAsserted !== hideAsserted) {
          setHideAssertedState(landedHideAsserted);
          if (index !== null) {
            if (!landedHideAsserted) localStorage.removeItem(assertedStorageKey(index.stamp));
            else localStorage.setItem(assertedStorageKey(index.stamp), "1");
          }
        }
        return;
      }
      if (focus !== null || ticket !== null || hideAsserted) {
        replaceHashQuery((p) => {
          if (focus !== null) p.set(CTX_PARAM, focus);
          if (ticket !== null) p.set(TICKET_PARAM, ticket);
          if (hideAsserted) p.set(ASSERTED_PARAM, "1");
        });
      }
      stampCurrentEntry();
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [focus, ticket, hideAsserted, index]);

  const setFocus = useCallback(
    (label: string | null) => {
      const resolved = resolveLabel(label, index);
      if (resolved === null) {
        setHashQuery((p) => p.delete(CTX_PARAM));
        if (index !== null) localStorage.removeItem(storageKey(index.stamp));
        setFocusState(null);
        show("Focus cleared");
        return;
      }
      setHashQuery((p) => p.set(CTX_PARAM, resolved));
      if (index !== null) localStorage.setItem(storageKey(index.stamp), resolved);
      setFocusState(resolved);
      show(`Focused ${resolved}`);
    },
    [index, show],
  );

  // `setTicket`'s own `setHashQuery`/`localStorage` calls only ever touch
  // `TICKET_PARAM`/`ticketStorageKey` — never `CTX_PARAM`/`storageKey` — so
  // pinning/clearing a ticket can never clear (or be cleared by) `focus` —
  // the compose guarantee this module's header comment describes.
  const setTicket = useCallback(
    (id: string | null) => {
      const resolved = resolveTicket(id, index);
      if (resolved === null) {
        setHashQuery((p) => p.delete(TICKET_PARAM));
        if (index !== null) localStorage.removeItem(ticketStorageKey(index.stamp));
        setTicketState(null);
        show("Ticket pin cleared");
        return;
      }
      setHashQuery((p) => p.set(TICKET_PARAM, resolved));
      if (index !== null) localStorage.setItem(ticketStorageKey(index.stamp), resolved);
      setTicketState(resolved);
      show(`Pinned ticket ${resolved}`);
    },
    [index, show],
  );

  // Mirrors `setTicket`'s shape exactly — its own `setHashQuery`/
  // `localStorage` calls only ever touch `ASSERTED_PARAM`/`assertedStorageKey`,
  // never `focus`'s or `ticket`'s, so toggling hide-asserted can never clear
  // (or be cleared by) either.
  const setHideAsserted = useCallback(
    (v: boolean) => {
      if (!v) {
        setHashQuery((p) => p.delete(ASSERTED_PARAM));
        if (index !== null) localStorage.removeItem(assertedStorageKey(index.stamp));
        setHideAssertedState(false);
        show("Asserted coverage shown");
        return;
      }
      setHashQuery((p) => p.set(ASSERTED_PARAM, "1"));
      if (index !== null) localStorage.setItem(assertedStorageKey(index.stamp), "1");
      setHideAssertedState(true);
      show("Asserted coverage hidden");
    },
    [index, show],
  );

  const value = useMemo<UseFocusResult>(
    () => ({ focus, setFocus, ticket, setTicket, hideAsserted, setHideAsserted }),
    [focus, setFocus, ticket, setTicket, hideAsserted, setHideAsserted],
  );
  return <FocusContext.Provider value={value}>{children}</FocusContext.Provider>;
}

/** Must be called under a `FocusProvider` (mounted once near the app root —
 * see App.tsx), the same contract `Toast.tsx`'s `useToast()` already
 * establishes for this codebase. */
export function useFocus(): UseFocusResult {
  const ctx = useContext(FocusContext);
  if (ctx === null) throw new Error("useFocus must be used within a FocusProvider");
  return ctx;
}
