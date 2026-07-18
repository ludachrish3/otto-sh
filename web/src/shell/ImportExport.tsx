// The Import front door (UX spec §12): a hidden file input driven by the
// ⋯ menu / empty state, plus whole-window drag-drop. Export re-serializes
// the CURRENT sessions[] (client-side, no endpoint — spec §14) — rebuilt
// fresh via documentFromSessions rather than the raw boot-time document, so
// a live export stays truthful after streamed chart_map/meta/event updates
// (Plan 5b final review, Finding I2 — see exportDoc.ts's sessionToRecord).
import { type ReactNode, useCallback, useEffect, useRef } from "react";

import { documentFromSessions } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";

function useImportFile(): (file: File) => void {
  const importMonitorSessions = useReviewStore((s) => s.actions.importMonitorSessions);
  return useCallback(
    (file: File) => {
      void file.text().then((text) => importMonitorSessions(text, file.name));
    },
    [importMonitorSessions],
  );
}

export function exportLoadedDocument(): void {
  const { sessions, sourceName } = useReviewStore.getState();
  if (sessions.length === 0) return;
  const document_ = documentFromSessions(sessions);
  const blob = new Blob([JSON.stringify(document_)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sourceName ?? "otto-monitor-export.json";
  a.click();
  URL.revokeObjectURL(url);
}

/** Live gains a directly-clickable Export (spec: "Live also gains the
 * Export affordance Plan 5a deferred") — the same client-side
 * `exportLoadedDocument` the ⋯ menu's `menu-export` item calls outside live
 * mode (no extra fetch: `rawMonitorSessions` is already in memory), just
 * reachable without opening the overflow menu first. AppBar omits the
 * overflow's `menu-export` entry while this button is showing, so live mode
 * never ships two visible controls for the identical action; review mode
 * keeps the ⋯ menu entry point unchanged. */
export function ExportButton() {
  const hasData = useReviewStore((s) => s.rawMonitorSessions !== null);
  return (
    <button
      type="button"
      data-testid="export-button"
      disabled={!hasData}
      onClick={exportLoadedDocument}
      aria-label="Export"
      className="cursor-pointer rounded-md px-2 py-1 text-sm text-tertiary hover:bg-primary_hover
        disabled:cursor-not-allowed disabled:opacity-50"
    >
      Export
    </button>
  );
}

/** Mounts the hidden input + drag-drop handlers; children get the picker via context-free ref registration. */
export function ImportProvider({ children }: { children: ReactNode }) {
  const inputRef = useRef<HTMLInputElement>(null);
  const importFile = useImportFile();

  useEffect(() => {
    registerPicker(() => inputRef.current?.click());
    const onDragOver = (e: DragEvent) => e.preventDefault();
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      const file = e.dataTransfer?.files?.[0];
      if (file) importFile(file);
    };
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    return () => {
      registerPicker(null);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [importFile]);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept=".json,application/json"
        data-testid="import-input"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) importFile(file);
          e.target.value = ""; // re-importing the same file must re-fire
        }}
      />
      {children}
    </>
  );
}

let picker: (() => void) | null = null;
function registerPicker(fn: (() => void) | null): void {
  picker = fn;
}
/** Open the OS file dialog (called from the ⋯ menu and the empty state). */
export function openImportPicker(): void {
  picker?.();
}
