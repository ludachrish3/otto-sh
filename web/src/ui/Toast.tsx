// web/src/ui/Toast.tsx
// The one toast for the dashboard and covapp (both are SPA roots that mount
// <ToastViewport /> once). A zustand store rather than a context so
// non-component code (the undo history) can raise it via
// `useToast.getState().showToast`. Single slot, not a queue: a fresh
// showToast replaces whatever is visible and restarts the dismiss timer.
// No vendored Untitled UI component fits (the free set has no snackbar), so
// this is styled with the vendored tokens, like the chip it replaced.
//
// Modals (#929: undo is live in the Events panel). React Aria's modals make
// everything outside them inert and contain focus. The region carries
// `data-react-aria-top-layer`, the marker React Aria's own toast region
// uses: hide-outside leaves it exposed, FocusScope lets focus enter it, and
// an interaction inside it does not dismiss the modal. A visible toast is
// also a landmark, so F6 moves focus to it from anywhere, a modal's Tab
// cycle included.
import { useEffect, useRef, useState } from "react";
import { useLandmark } from "react-aria";
import { create } from "zustand";

export const TOAST_MS = 3200;
export const TOAST_ACTION_MS = 8000;

interface ToastAction {
  label: string;
  run: () => void;
}

interface ToastItem {
  /** Fresh per showToast, so an identical message still restarts the timer. */
  id: number;
  message: string;
  action: ToastAction | undefined;
}

interface ToastState {
  toast: ToastItem | null;
  showToast: (message: string, action?: ToastAction) => void;
  dismissToast: () => void;
}

let nextId = 0;

export const useToastStore = create<ToastState>()((set) => ({
  toast: null,
  showToast: (message, action) => set({ toast: { id: ++nextId, message, action } }),
  dismissToast: () => set({ toast: null }),
}));

export function ToastViewport() {
  const toast = useToastStore((s) => s.toast);
  const dismissToast = useToastStore((s) => s.dismissToast);
  const regionRef = useRef<HTMLDivElement>(null);
  // Hover and focus hold the toast independently: pausing while either
  // holds, so leaving with the pointer does not release a focus hold.
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const id = toast?.id;
  const ms = toast?.action ? TOAST_ACTION_MS : TOAST_MS;

  // A replacement keeps the holds that still exist: the card element stays
  // under the pointer, and focus stays wherever the DOM has it (a removed
  // button takes it away without a blur event). A dismissed card is gone,
  // and so are both holds.
  useEffect(() => {
    if (id === undefined) setHovered(false);
    setFocused(regionRef.current?.contains(document.activeElement) ?? false);
  }, [id]);

  // Pausing = clearing the timer; the last hold's release restarts the full duration.
  const held = hovered || focused;
  useEffect(() => {
    if (id === undefined || held) return;
    const t = setTimeout(dismissToast, ms);
    return () => clearTimeout(t);
  }, [id, held, ms, dismissToast]);

  // The role="status" region is always mounted and only its content comes
  // and goes: screen readers announce changes inside an existing live
  // region reliably, but not a live region inserted together with its text.
  // It is also why the top-layer marker sits here: hide-outside looks for it
  // when a modal opens, which may be before any toast is shown.
  return (
    // biome-ignore lint/a11y/noNoninteractiveElementInteractions: hover/focus only pause the dismiss timer; the region itself has no action
    <div
      ref={regionRef}
      role="status"
      data-testid="toast-region"
      data-react-aria-top-layer
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(e) => setFocused(regionRef.current?.contains(e.relatedTarget) ?? false)}
    >
      {toast !== null && <ToastCard toast={toast} dismissToast={dismissToast} />}
    </div>
  );
}

/** The visible toast: a landmark only while it is shown, so F6 never lands
 * on an empty region. It stacks at z-[70], above every modal's backdrop
 * (z-50, and the delete dialog's z-[60] in shell/ConfirmDeleteEvent.tsx),
 * so the top layer is on top visually too and takes the pointer. */
function ToastCard({ toast, dismissToast }: { toast: ToastItem; dismissToast: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const { landmarkProps } = useLandmark({ role: "region", "aria-label": "Notification" }, ref);
  const action = toast.action;
  return (
    <div
      {...landmarkProps}
      ref={ref}
      data-testid="toast"
      className="fixed bottom-6 left-1/2 z-[70] flex -translate-x-1/2 items-center gap-3 rounded-lg
        bg-primary-solid px-4 py-2 text-sm font-medium text-white shadow-lg outline-focus-ring
        focus-visible:outline-2 focus-visible:outline-offset-2"
    >
      <span>{toast.message}</span>
      {action && (
        <button
          type="button"
          data-testid="toast-action"
          onClick={() => {
            // Dismiss first: `run` may raise its own toast (an undo
            // refused by the history), which a later dismiss would wipe.
            dismissToast();
            action.run();
          }}
          className="cursor-pointer rounded-md px-1 font-semibold underline underline-offset-2
            outline-focus-ring focus-visible:outline-2 focus-visible:outline-offset-2"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
