// Session state for both modes: the imported/hydrated document, the active
// session, and the viewed time range. The legacy Plotly-era live store
// (store.ts) was deleted (Task 12) rather than merged into this one — the
// live hookup (Plan 5b) grows sessions in place here via appendFragment(s)
// instead, so there is only ever this one store.
import { create } from "zustand";

import type { MonitorHistoricalExportDocument, MonitorSessionFragment } from "../api/export.gen";
import {
  ExportParseError,
  type NormalizedSession,
  parseExportDocument,
  type TimeRange,
} from "./exportDoc";
import { applyFragment } from "./fragment";

/** Pure merge step shared by appendFragment/appendFragments: applies each
 * fragment in order to `sessions`, returning the SAME array reference if
 * nothing changed (callers use reference equality to skip a store write) or
 * a NEW array with only the touched session(s) replaced. Kept out of the
 * store body so a whole batch can be folded into exactly one `set()` call.
 * `warnings` is mutated in place with one entry per fragment that dropped a
 * bad-timestamp metric row (see fragment.ts's applyFragment) — the caller
 * folds it into the store's `warnings` channel in the SAME `set()` as the
 * session update, so a batch that drops rows costs exactly one extra
 * notification, not one per fragment. */
function mergeFragments(
  sessions: NormalizedSession[],
  frags: readonly MonitorSessionFragment[],
  warnings: string[],
): NormalizedSession[] {
  let next = sessions;
  let changed = false;
  for (const frag of frags) {
    const i = next.findIndex((s) => s.id === frag.session);
    if (i === -1) continue; // a fragment for a session we do not hold — ignore
    const merged = applyFragment(next[i], frag, warnings);
    if (merged === next[i]) continue;
    if (!changed) {
      next = [...next];
      changed = true;
    }
    next[i] = merged;
  }
  return next;
}

interface ReviewActions {
  /** Parse + load an export document. Returns false (and sets importError,
   * keeping any previously loaded data) on failure. */
  importMonitorSessions: (text: string, sourceName: string) => boolean;
  /** Resync variant of importMonitorSessions — used ONLY by the stream's
   * reconnect resync (data/stream.ts's onerror -> resync -> reopen; see
   * bootstrap.ts's `resync`, not its initial `hydrate`). Replaces
   * sessions/rawMonitorSessions/warnings from the freshly re-fetched
   * snapshot exactly like a fresh import. `range` and `activeSessionId` are
   * preserved — NOT reset unconditionally like a fresh import — but only
   * when `activeSessionId` still resolves against the NEW snapshot: a
   * transient network blip must not silently discard a user's paused/pinned
   * view (pause — reviewStore's togglePause — exists specifically to
   * protect that state; Plan 5b final review, Finding I3). If the monitor
   * SERVER RESTARTED while the tab was open, though, the reconnect resync
   * delivers a genuinely fresh session set that may no longer contain the
   * old id at all — preserving a `range`/`activeSessionId` that points at
   * nothing left the shell stuck in its empty state until a manual reload
   * (Plan 5b follow-ups #3). So this falls back to `sessions[0]` + `range:
   * null`, exactly like a fresh import, ONLY in that case. Returns false
   * (and sets importError, keeping any previously loaded data) on failure,
   * same contract as importMonitorSessions. */
  resyncMonitorSessions: (text: string, sourceName: string) => boolean;
  /** Append a live-stream fragment to the session it addresses. A no-op if
   * that session isn't currently held (e.g. it hasn't loaded yet). */
  appendFragment: (frag: MonitorSessionFragment) => void;
  /** Batched sibling of appendFragment: applies a whole batch of fragments
   * (the SSE client's per-frame flush, ~90 at the live bed's shape) in ONE
   * store write — the property that keeps a tick to a single zustand
   * notification / render pass instead of one per fragment. */
  appendFragments: (frags: readonly MonitorSessionFragment[]) => void;
  selectSession: (id: string) => void;
  setRange: (range: TimeRange | null) => void;
  clearImportError: () => void;
  /** Which server mode the shell booted against — set once from `/api/mode`. */
  setMode: (mode: "live" | "review" | null) => void;
  /** From /api/mode (Plan 5c): whether this server accepts event mutations
   * (live, or a .db-sourced review). Gates every marking affordance. */
  setEditable: (editable: boolean) => void;
  /** Append one message to the warnings channel (rendered by
   * DataWarningsBanner) — the surface for mutation failures issued from
   * chrome with no inline error slot of its own (palette commands). */
  addWarning: (message: string) => void;
  /** SSE connection lifecycle (see data/stream.ts); irrelevant in review mode. */
  setConnection: (connection: "connecting" | "live" | "disconnected") => void;
  /** Live-only view control. Pause and "the user picked a custom range" are
   * ONE state, not two — there is no stored `paused` flag to disagree with
   * `range` (see `useIsPaused`). Resuming clears `range` back to null —
   * following again. Pausing snapshots the currently-derived follow window
   * into an absolute `range` (see `liveRange`). Does NOT touch ingestion:
   * fragments keep applying while paused, so resume catches up with no gap
   * (spec: "pause is a view control, not a data control"). Any OTHER way of
   * pinning the view in live mode (e.g. a chart drag-zoom calling `setRange`
   * directly) is therefore *already* "paused" — `togglePause` in that state
   * resumes following rather than freezing a second, redundant window. */
  togglePause: () => void;
  /** Live-only view control: resize the follow window. `paused` is derived
   * from `range` (see `useIsPaused`/`togglePause`), never stored, so this
   * has exactly two cases, keyed on that same `range`:
   *  - Following (`range === null`): set `windowMs` only. `range` STAYS
   *    null — the widened/narrowed window is picked up on the next render
   *    via `liveRange(session.endMs, windowMs)` (SubjectPage.tsx). Pinning
   *    `range` here would silently turn "pick a window" into "pause", which
   *    is not what choosing a preset means while following.
   *  - Paused (`range !== null`): re-pin `range` to the new width, ending at
   *    the SAME `range.to` — the frozen instant the user is looking at.
   *    Choosing "1h" while paused zooms around the current view rather than
   *    silently resuming (dropping the pause) or doing nothing until the
   *    user resumes first. */
  setWindow: (windowMs: number) => void;
}

export interface ReviewState {
  sessions: NormalizedSession[];
  rawMonitorSessions: MonitorHistoricalExportDocument | null;
  sourceName: string | null;
  warnings: string[];
  importError: string | null;
  activeSessionId: string | null;
  range: TimeRange | null;
  mode: "live" | "review" | null;
  /** From `/api/mode` (Plan 5c): whether this server accepts event
   * mutations. Default false until the boot fetch resolves. */
  editable: boolean;
  connection: "connecting" | "live" | "disconnected";
  /** The follow-the-tail window width in live mode, applied whenever
   * `range === null` (see `liveRange` in data/time.ts). Default 15 min. */
  windowMs: number;
  actions: ReviewActions;
}

export const useReviewStore = create<ReviewState>()((set, get) => ({
  sessions: [],
  rawMonitorSessions: null,
  sourceName: null,
  warnings: [],
  importError: null,
  activeSessionId: null,
  range: null,
  mode: null,
  editable: false,
  connection: "connecting",
  windowMs: 900_000,
  actions: {
    importMonitorSessions: (text, sourceName) => {
      try {
        const result = parseExportDocument(text);
        set({
          sessions: result.sessions,
          rawMonitorSessions: result.document,
          sourceName,
          warnings: result.warnings,
          importError: null,
          activeSessionId: result.sessions[0]?.id ?? null,
          range: null,
        });
        return true;
      } catch (err) {
        set({
          importError:
            err instanceof ExportParseError ? err.message : `Import failed: ${String(err)}`,
        });
        return false;
      }
    },
    resyncMonitorSessions: (text, sourceName) => {
      try {
        const result = parseExportDocument(text);
        const { activeSessionId } = get();
        // The ONE condition that decides preserve-vs-fallback: does the
        // CURRENT activeSessionId still resolve against the freshly
        // re-fetched snapshot? If so, `range`/`activeSessionId` are left
        // exactly as they are (see this action's doc comment on the
        // interface above) — a transient blip must not disturb a paused
        // view. If not (a server restart handed back a wholly different
        // session set), falling back exactly like a fresh import is what
        // stops the shell from sitting in its empty state forever: `null`
        // resolves nothing, and nothing else re-derives a valid selection.
        const idStillPresent =
          activeSessionId !== null && result.sessions.some((s) => s.id === activeSessionId);
        set({
          sessions: result.sessions,
          rawMonitorSessions: result.document,
          sourceName,
          warnings: result.warnings,
          importError: null,
          ...(idStillPresent
            ? {}
            : { activeSessionId: result.sessions[0]?.id ?? null, range: null }),
        });
        return true;
      } catch (err) {
        set({
          importError:
            err instanceof ExportParseError ? err.message : `Import failed: ${String(err)}`,
        });
        return false;
      }
    },
    appendFragment: (frag) => {
      const { sessions, warnings } = get();
      const dropped: string[] = [];
      const next = mergeFragments(sessions, [frag], dropped);
      if (next !== sessions || dropped.length > 0) {
        set({
          sessions: next,
          ...(dropped.length > 0 ? { warnings: [...warnings, ...dropped] } : {}),
        });
      }
    },
    appendFragments: (frags) => {
      const { sessions, warnings } = get();
      const dropped: string[] = [];
      const next = mergeFragments(sessions, frags, dropped);
      if (next !== sessions || dropped.length > 0) {
        set({
          sessions: next,
          ...(dropped.length > 0 ? { warnings: [...warnings, ...dropped] } : {}),
        });
      }
    },
    selectSession: (id) => set({ activeSessionId: id, range: null }),
    // The ONE door every range enters through — every caller (RangePicker's
    // Apply, the chart drag-zoom's clamp, the events-panel jump) funnels
    // here, so this is the one place to refuse a range that can never be
    // valid: `null` always passes (it means "follow the tail" / "the whole
    // session"), but a non-null `from >= to` is silently ignored, leaving
    // the existing range untouched. Downstream (metricsForSubject/
    // sliceSeries, every chart window) reads an inverted or empty range as
    // "0 samples in range" for EVERY series — no error, no signal, just a
    // silently blank dashboard. That state has more than one door: a caller
    // can pass an already-inverted/empty range directly (a malformed manual
    // edit), or an ORDERED range that `clampRange` (data/exportDoc.ts)
    // itself inverts when both endpoints sit outside `bounds` on the same
    // side (seen both in RangePicker's Apply and in the live follow
    // window's pre-session lead-in via a chart drag-zoom). Guarding here
    // once, rather than at each call site, is what makes the invariant
    // unconditional — the same "one rule, one place" shape as
    // `healthForHost` and `useIsPaused` below (paused is DERIVED from
    // `range`, never stored, precisely so it cannot disagree with it; this
    // guard is the same idea applied to `range` itself).
    //
    // The `>=` comparison above is a NaN blind spot: `NaN >= NaN` (and
    // every other NaN comparison) is `false`, so an inverted/empty check
    // alone lets a NaN range walk straight through. A NaN reaches here for
    // real — `parseTs` (data/time.ts) is `Date.parse`, which returns NaN
    // for a malformed/non-ISO timestamp, and the events-panel jump builds
    // its range from a parsed event timestamp. `Number.isFinite` on both
    // edges closes that gap: it rejects NaN and +/-Infinity alike, which
    // are exactly as useless as a time range as an inverted one, and for
    // the same reason — this is the one boundary every range enters
    // through, so this is the one place that must catch them all.
    setRange: (range) => {
      if (range !== null) {
        if (!Number.isFinite(range.from) || !Number.isFinite(range.to)) return;
        if (range.from >= range.to) return;
      }
      set({ range });
    },
    clearImportError: () => set({ importError: null }),
    setMode: (mode) => set({ mode }),
    setEditable: (editable) => set({ editable }),
    addWarning: (message) => set({ warnings: [...get().warnings, message] }),
    setConnection: (connection) => set({ connection }),
    togglePause: () => {
      const { mode, range, sessions, activeSessionId, windowMs } = get();
      // `paused` is derived (see useIsPaused), never stored — so "currently
      // paused" IS "live mode with a pinned range", by construction. That
      // covers a chart drag-zoom's direct setRange(...) exactly the same as
      // a Pause click: either way, toggling resumes following instead of
      // freezing a second, redundant window on top of the user's own.
      if (mode === "live" && range !== null) {
        set({ range: null }); // resume -> follow the tail again
        return;
      }
      const session = sessions.find((s) => s.id === activeSessionId);
      // Freeze the CURRENTLY DERIVED window into an absolute range. Pause and "user
      // picked a custom range" are then the same state, so they cannot disagree.
      const to = session ? session.endMs : Date.now();
      set({ range: { from: to - windowMs, to } });
    },
    setWindow: (windowMs) => {
      const { range } = get();
      // Following: just resize the derived window (liveRange reads windowMs
      // on the next render — see the interface doc comment above). Paused:
      // `range` IS the pause, so keep its `to` and re-pin at the new width.
      set(
        range === null
          ? { windowMs }
          : { windowMs, range: { from: range.to - windowMs, to: range.to } },
      );
    },
  },
}));

export function useActiveSession(): NormalizedSession | null {
  return useReviewStore((s) => s.sessions.find((sess) => sess.id === s.activeSessionId) ?? null);
}

/** Derived, never stored: "paused" and "the user picked a custom range" are
 * ONE concept (see `togglePause`'s doc comment) — a zustand selector renders
 * exactly as stably as a stored field, and deriving it makes the two
 * structurally unable to disagree (unlike a separate boolean, which Task 9
 * proved CAN drift out of sync with `range`, e.g. via a chart drag-zoom's
 * direct `setRange` call). */
export function useIsPaused(): boolean {
  return useReviewStore((s) => s.mode === "live" && s.range !== null);
}
