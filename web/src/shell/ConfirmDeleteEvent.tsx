// web/src/shell/ConfirmDeleteEvent.tsx
// The editor's delete confirmation: a centred alert dialog over the slideout
// that names the event. Composed straight from react-aria-components -- the
// vendored slideout-menu pieces are right-docked panels, not a centred modal,
// and vendored files are never hand-edited. It stacks at z-[60], above the
// slideout's z-50, and RAC's overlay stack gives Escape and the scrim to this
// dialog alone; focus returns to the Delete button that opened it.
import { useEffect, useRef } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";
import { Button } from "@/components/base/buttons/button";

// A double-click on the editor's Delete lands its second press on this
// dialog's scrim. Scrim presses this soon after opening are the tail of the
// click that opened it, not a decision to dismiss. 500 ms covers the slow
// end of OS double-click intervals, and erring long is fail-safe: the second
// press can never delete anything (it lands on the scrim, and only
// the dialog's own Delete button deletes), so the worst a slow double-click
// does is dismiss the dialog, which the user simply reopens.
const SCRIM_GRACE_MS = 500;

interface ConfirmDeleteEventProps {
  isOpen: boolean;
  /** The stored event's label (not an unsaved edit); empty gives the label-less wording. */
  eventLabel: string;
  /** A delete request is in flight: Confirm spins, Cancel and Escape hold. */
  pending: boolean;
  /** The last delete failure, shown in the dialog so it stays open to retry. */
  error?: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

export function ConfirmDeleteEvent({
  isOpen,
  eventLabel,
  pending,
  error = null,
  onCancel,
  onConfirm,
}: ConfirmDeleteEventProps) {
  const openedAt = useRef(0);
  useEffect(() => {
    if (isOpen) openedAt.current = performance.now();
  }, [isOpen]);
  const subject = eventLabel === "" ? "This event" : `“${eventLabel}”`;
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open && !pending) onCancel();
      }}
      isDismissable={!pending}
      shouldCloseOnInteractOutside={() => performance.now() - openedAt.current > SCRIM_GRACE_MS}
      isKeyboardDismissDisabled={pending}
      className="fixed inset-0 z-[60] flex min-h-dvh w-full items-center justify-center bg-overlay/40 p-4 outline-hidden"
    >
      <Modal className="w-full max-w-100 rounded-xl bg-primary shadow-xl ring-1 ring-secondary_alt dark:ring-primary">
        <Dialog
          role="alertdialog"
          data-testid="confirm-delete"
          className="flex flex-col gap-4 p-6 outline-hidden"
        >
          <div className="flex flex-col gap-1">
            <Heading slot="title" className="text-lg font-semibold text-primary">
              Delete event?
            </Heading>
            <p className="text-sm text-tertiary">
              {subject} will be removed. This can't be undone.
            </p>
          </div>
          {error !== null && (
            <p role="alert" className="text-xs text-error-primary">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-3">
            <Button
              size="md"
              color="secondary"
              autoFocus
              isDisabled={pending}
              data-testid="confirm-delete-cancel"
              onPress={onCancel}
            >
              Cancel
            </Button>
            <Button
              size="md"
              color="primary-destructive"
              isLoading={pending}
              data-testid="confirm-delete-confirm"
              onPress={onConfirm}
            >
              Delete
            </Button>
          </div>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
