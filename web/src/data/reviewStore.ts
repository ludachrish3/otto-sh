// Review-mode state: the imported document, the active session, and the
// viewed time range. Deliberately separate from the legacy live store
// (store.ts) — that one keeps serving the SSE/live path and the two merge
// at the live-hookup phase, not before.
import { create } from "zustand";

import type { MonitorHistoricalExportDocument } from "../api/export.gen";
import {
  ExportParseError,
  type NormalizedSession,
  parseExportDocument,
  type TimeRange,
} from "./exportDoc";

interface ReviewActions {
  /** Parse + load an export document. Returns false (and sets importError,
   * keeping any previously loaded data) on failure. */
  importText: (text: string, sourceName: string) => boolean;
  selectSession: (id: string) => void;
  setRange: (range: TimeRange | null) => void;
  resetView: () => void;
  clearImportError: () => void;
}

export interface ReviewState {
  sessions: NormalizedSession[];
  rawDocument: MonitorHistoricalExportDocument | null;
  sourceName: string | null;
  warnings: string[];
  importError: string | null;
  activeSessionId: string | null;
  range: TimeRange | null;
  actions: ReviewActions;
}

export const useReviewStore = create<ReviewState>()((set, get) => ({
  sessions: [],
  rawDocument: null,
  sourceName: null,
  warnings: [],
  importError: null,
  activeSessionId: null,
  range: null,
  actions: {
    importText: (text, sourceName) => {
      try {
        const result = parseExportDocument(text);
        set({
          sessions: result.sessions,
          rawDocument: result.document,
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
    selectSession: (id) => set({ activeSessionId: id, range: null }),
    setRange: (range) => set({ range }),
    resetView: () => set({ activeSessionId: get().sessions[0]?.id ?? null, range: null }),
    clearImportError: () => set({ importError: null }),
  },
}));

export function useActiveSession(): NormalizedSession | null {
  return useReviewStore((s) => s.sessions.find((sess) => sess.id === s.activeSessionId) ?? null);
}
