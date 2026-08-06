// Deliberately minimal (spec §12.4 defers bindings): lists exactly the three
// bindings in `BINDINGS` below, which is everything covapp binds — "?"
// (AppShell's app-wide handler), "/" (TicketSearch's SEARCH_BINDING) and Esc.
// Modal scaffolding mirrors web/src/ui/CommandMenu.tsx's react-aria stack
// (ModalOverlay + Modal + Dialog), simplified — no autocomplete/menu, just a
// static list. Escape closes via ModalOverlay's built-in dismiss-on-Escape
// behavior (same as CommandMenu's palette).
import { Dialog, Modal, ModalOverlay } from "react-aria-components";

import { Kbd } from "../../ui/Kbd";

interface Binding {
  keys: string;
  description: string;
}

const BINDINGS: Binding[] = [
  { keys: "?", description: "Open this dialog" },
  { keys: "Esc", description: "Close this dialog" },
  // TicketSearch binds this via ui/shortcuts' SEARCH_BINDING, the same
  // binding the monitor's search uses.
  { keys: "/", description: "Pin a ticket by id" },
];

export interface ShortcutsDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export function ShortcutsDialog({ isOpen, onClose }: ShortcutsDialogProps) {
  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      isDismissable
      className="fixed inset-0 z-50 flex justify-center bg-overlay/70 pt-[20vh]"
    >
      <Modal className="w-full max-w-100 px-4">
        <Dialog
          aria-label="Keyboard shortcuts"
          data-testid="shortcuts-dialog"
          className="overflow-hidden rounded-xl bg-primary p-4 shadow-2xl ring-1 ring-secondary_alt outline-hidden"
        >
          <h2 className="mb-3 text-sm font-semibold text-primary">Keyboard shortcuts</h2>
          <ul className="flex flex-col gap-2.5">
            {BINDINGS.map((binding) => (
              <li
                key={binding.keys}
                className="flex items-center justify-between gap-4 text-sm text-secondary"
              >
                <span>{binding.description}</span>
                <Kbd>{binding.keys}</Kbd>
              </li>
            ))}
          </ul>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
