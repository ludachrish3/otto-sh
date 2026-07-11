// The Import front door (UX spec §12): a hidden file input driven by the
// ⋯ menu / empty state, plus whole-window drag-drop. Export re-serializes
// the loaded raw document (client-side, no endpoint — spec §14).
import { type ReactNode, useCallback, useEffect, useRef } from "react";

import { useReviewStore } from "../data/reviewStore";

export function useImportFile(): (file: File) => void {
  const importText = useReviewStore((s) => s.actions.importText);
  return useCallback(
    (file: File) => {
      void file.text().then((text) => importText(text, file.name));
    },
    [importText],
  );
}

export function exportLoadedDocument(): void {
  const { rawDocument, sourceName } = useReviewStore.getState();
  if (!rawDocument) return;
  const blob = new Blob([JSON.stringify(rawDocument)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sourceName ?? "otto-monitor-export.json";
  a.click();
  URL.revokeObjectURL(url);
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
