// Right-anchored slide-over (UX spec §6: events review surface). Controlled;
// react-aria Modal handles focus trap, Escape, and overlay dismiss.
import type { ReactNode } from "react";
import { Dialog, Heading, Modal, ModalOverlay } from "react-aria-components";

export function SlideOver(props: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  testId?: string;
}) {
  const { isOpen, onClose, title, children, testId } = props;
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      isDismissable
      className="fixed inset-0 z-40 bg-black/30"
    >
      <Modal className="fixed inset-y-0 right-0 z-50 w-96 max-w-full">
        <Dialog
          data-testid={testId}
          className="flex h-full flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white
            p-4 outline-none dark:border-gray-800 dark:bg-gray-950"
        >
          <div className="flex items-center justify-between">
            <Heading slot="title" className="text-sm font-semibold">
              {title}
            </Heading>
            <button
              type="button"
              aria-label="Close"
              onClick={onClose}
              className="cursor-pointer rounded px-2 text-gray-400 hover:text-gray-600
                dark:hover:text-gray-200"
            >
              ✕
            </button>
          </div>
          {children}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
