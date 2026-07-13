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
 * store body so a whole batch can be folded into exactly one `set()` call. */
function mergeFragments(
  sessions: NormalizedSession[],
  frags: readonly MonitorSessionFragment[],
): NormalizedSession[] {
  let next = sessions;
  let changed = false;
  for (const frag of frags) {
    const i = next.findIndex((s) => s.id === frag.session);
    if (i === -1) continue; // a fragment for a session we do not hold — ignore
    const merged = applyFragment(next[i], frag);
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
   * snapshot exactly like a fresh import, but deliberately leaves `range`
   * and `activeSessionId` untouched: a transient network blip must not
   * silently discard a user's paused/pinned view (pause — reviewStore's
   * togglePause — exists specifically to protect that state; Plan 5b final
   * review, Finding I3). Returns false (and sets importError, keeping any
   * previously loaded data) on failure, same contract as
   * importMonitorSessions. */
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
  resetView: () => void;
  clearImportError: () => void;
  /** Which server mode the shell booted against — set once from `/api/mode`. */
  setMode: (mode: "live" | "review" | null) => void;
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
        set({
          sessions: result.sessions,
          rawMonitorSessions: result.document,
          sourceName,
          warnings: result.warnings,
          importError: null,
          // range/activeSessionId deliberately NOT reset here — see this
          // action's doc comment on the interface above.
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
      const { sessions } = get();
      const next = mergeFragments(sessions, [frag]);
      if (next !== sessions) set({ sessions: next });
    },
    appendFragments: (frags) => {
      const { sessions } = get();
      const next = mergeFragments(sessions, frags);
      if (next !== sessions) set({ sessions: next });
    },
    selectSession: (id) => set({ activeSessionId: id, range: null }),
    setRange: (range) => set({ range }),
    resetView: () => set({ activeSessionId: get().sessions[0]?.id ?? null, range: null }),
    clearImportError: () => set({ importError: null }),
    setMode: (mode) => set({ mode }),
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
