// web/src/shell/ExportDialog.tsx
// The export name dialog: every way to export (the palette's Export, its
// chord, the AppBar's export glyph and menu item) opens it, and its Export
// downloads under the name typed (ImportExport.tsx's downloadExport).
// Composed from react-aria-components the way ConfirmDeleteEvent.tsx is.
// The field is mounted with the dialog's content, so each opening offers
// the name exportFileName gives at that moment, focused with its stem
// selected: typing replaces the name and keeps its extension, and Enter
// right after the chord exports under the offered name.
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/base/buttons/button";
import { useReviewStore } from "../data/reviewStore";
import { TextInput } from "../ui/TextInput";
import { useUiStore } from "../ui/uiStore";
import { downloadExport, exportFileName, fileStem } from "./ImportExport";

export function ExportDialog() {
  const isOpen = useUiStore((s) => s.exportDialogOpen);
  const close = useUiStore((s) => s.actions.closeExportDialog);
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) close();
      }}
      isDismissable
      className="fixed inset-0 z-[60] flex min-h-dvh w-full items-center justify-center bg-overlay/40 p-4 outline-hidden"
    >
      <Modal className="w-full max-w-100 rounded-xl bg-primary shadow-xl ring-1 ring-secondary_alt dark:ring-primary">
        <Dialog data-testid="export-dialog" className="flex flex-col gap-4 p-6 outline-hidden">
          <ExportForm onDone={close} />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function ExportForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState(() => {
    const { mode, sourceName, unsaved } = useReviewStore.getState();
    return exportFileName({ mode, sourceName, unsaved });
  });
  const field = useRef<HTMLInputElement | null>(null);
  // Runs before the Dialog's own mount effect, which then leaves focus
  // where it is because it is already inside the dialog.
  useEffect(() => {
    const input = field.current;
    if (input === null) return;
    input.focus();
    input.setSelectionRange(0, fileStem(input.value).length);
  }, []);
  const blank = name.trim() === "";
  const confirm = () => {
    if (blank) return;
    downloadExport(name);
    onDone();
  };
  return (
    <>
      <Heading slot="title" className="text-lg font-semibold text-primary">
        Export
      </Heading>
      <TextInput
        label="File name"
        value={name}
        onChange={setName}
        testId="export-name"
        inputRef={(el) => {
          field.current = el;
        }}
        onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
          // An IME's Enter commits its composition, not the dialog.
          if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
          e.preventDefault();
          confirm();
        }}
      />
      <div className="flex justify-end gap-3">
        <Button size="md" color="secondary" onPress={onDone}>
          Cancel
        </Button>
        <Button
          size="md"
          color="primary"
          isDisabled={blank}
          data-testid="export-confirm"
          onPress={confirm}
        >
          Export
        </Button>
      </div>
    </>
  );
}
