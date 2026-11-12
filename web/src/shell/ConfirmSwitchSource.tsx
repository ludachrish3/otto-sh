// web/src/shell/ConfirmSwitchSource.tsx
// The prompt an Import on a `.json` source opens: opening the file closes
// the source this URL serves, for every tab on it, and discards its unsaved
// changes. Composed from react-aria-components the way ConfirmDeleteEvent.tsx
// is; confirming asks the server to switch (data/source.ts's
// requestSourceSwitch). A source change closes it (data/source.ts's
// supersede): its counts belong to the old source.
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/base/buttons/button";
import { changeNoun } from "../data/changeNoun";
import { requestSourceSwitch } from "../data/source";
import { type SwitchPrompt, useUiStore } from "../ui/uiStore";

/** The prompt's one line, for the source as the prompt showed it. */
export function switchPromptText({ name, shown }: SwitchPrompt): string {
  if (shown.unsaved === 0) return `Opening ${name} closes ${shown.source}.`;
  return `Opening ${name} discards ${shown.unsaved} unsaved ${changeNoun(shown.unsaved)} to ${shown.source}. Export first to keep them.`;
}

export function ConfirmSwitchSource() {
  const prompt = useUiStore((s) => s.switchPrompt);
  const close = useUiStore((s) => s.actions.closeSwitchPrompt);
  return (
    <ModalOverlay
      isOpen={prompt !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
      isDismissable
      className="fixed inset-0 z-[60] flex min-h-dvh w-full items-center justify-center bg-overlay/40 p-4 outline-hidden"
    >
      <Modal className="w-full max-w-100 rounded-xl bg-primary shadow-xl ring-1 ring-secondary_alt dark:ring-primary">
        <Dialog
          role="alertdialog"
          data-testid="confirm-switch"
          className="flex flex-col gap-4 p-6 outline-hidden"
        >
          <div className="flex flex-col gap-1">
            <Heading slot="title" className="text-lg font-semibold text-primary">
              Open {prompt?.name}?
            </Heading>
            <p className="text-sm text-tertiary">{prompt && switchPromptText(prompt)}</p>
          </div>
          <div className="flex justify-end gap-3">
            {/* Cancel takes focus, so Enter never discards unsaved edits. */}
            <Button
              size="md"
              color="secondary"
              autoFocus
              data-testid="confirm-switch-cancel"
              onPress={close}
            >
              Cancel
            </Button>
            <Button
              size="md"
              color={
                prompt !== null && prompt.shown.unsaved > 0 ? "primary-destructive" : "primary"
              }
              data-testid="confirm-switch-confirm"
              onPress={() => void requestSourceSwitch()}
            >
              Open
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
