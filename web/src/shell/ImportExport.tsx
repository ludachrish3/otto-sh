// The Import front door (UX spec §12): a hidden file input driven by the
// ⋯ menu / empty state, plus whole-window drag-drop. Export asks for a file
// name (shell/ExportDialog.tsx), then re-serializes the CURRENT sessions[]
// (client-side, no endpoint — spec §14) — rebuilt fresh via
// documentFromSessions rather than the raw boot-time document, so a live
// export stays truthful after streamed chart_map/meta/event updates (see
// exportDoc.ts's sessionToRecord).
import { type ReactNode, useEffect, useRef } from "react";

import type { Mode } from "../api/export.gen";
import { documentFromSessions } from "../data/exportDoc";
import { exportAcknowledgement, useReviewStore } from "../data/reviewStore";
import { importLocalFile, importOffered } from "../data/source";
import { acknowledgeExport } from "../data/sourceApi";
import { useUiStore } from "../ui/uiStore";

// Both UI paths (the picker and drag-drop) go through importLocalFile, which
// makes a loaded file the shell's only data source (#873), or on a .json
// server source opens the switch prompt.
const importFile = (file: File): void => void importLocalFile(file);

/** The one name a live export (or a source with no name) is offered under. */
export const LIVE_EXPORT_NAME = "otto-monitor-export.json";

const EDITED_SUFFIX = "-edited";

/** `name` with its last extension removed. A name with no extension, or a
 * dotfile such as `.json`, is its own stem. The export name is built on it,
 * and the dialog selects it so retyping keeps the extension. */
export function fileStem(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

/** The name the export dialog offers. A live source, or one with no name,
 * gets LIVE_EXPORT_NAME. Otherwise it is the source's name with its last
 * extension removed, `-edited` when the source has writes since its last
 * export (never twice), and `.json`: an export is always a `.json` document,
 * whatever it was loaded from. */
export function exportFileName(s: {
  mode: Mode | null;
  sourceName: string | null;
  unsaved: number;
}): string {
  if (s.mode === "live" || s.sourceName === null || s.sourceName === "") return LIVE_EXPORT_NAME;
  const stem = fileStem(s.sourceName);
  const edited = s.unsaved > 0 && !stem.endsWith(EDITED_SUFFIX) ? EDITED_SUFFIX : "";
  return `${stem}${edited}.json`;
}

/** The Export command, the AppBar's export glyph and its menu item: opens
 * the name dialog (shell/ExportDialog.tsx), which downloads on confirm. */
export function openExportDialog(): void {
  if (useReviewStore.getState().sessions.length === 0) return;
  useUiStore.getState().actions.openExportDialog();
}

/** Downloads the current sessions as `name`, exactly, then acknowledges
 * what the file holds (data/reviewStore.ts's exportAcknowledgement, which
 * may withhold it). The document and the acknowledgement are read in ONE
 * synchronous turn, so the revision acknowledged is one the document holds.
 * Every installed server source is acknowledged: the server lowers the
 * unsaved count of a source whose writes an export saves, and answers any
 * other with its state unchanged. */
export function downloadExport(name: string): void {
  const state = useReviewStore.getState();
  const { sessions } = state;
  if (sessions.length === 0) return;
  const acknowledgement = exportAcknowledgement(state);
  const document_ = documentFromSessions(sessions);
  const blob = new Blob([JSON.stringify(document_)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  if (acknowledgement !== null) void acknowledgeExport(acknowledgement);
}

/** Mounts the hidden input + drag-drop handlers; children get the picker via context-free ref registration. */
export function ImportProvider({ children }: { children: ReactNode }) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    registerPicker(() => inputRef.current?.click());
    const onDragOver = (e: DragEvent) => e.preventDefault();
    // A drop where Import is not offered (a live or .db source) is still
    // prevented, so the browser does not navigate to the file, and ignored.
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      if (!importOffered(useReviewStore.getState())) return;
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
  }, []);

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
