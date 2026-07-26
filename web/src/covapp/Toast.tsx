// covapp-local toast (Task 3 brief) — component-sourcing policy caps NEW
// `ui/**` components at TreeView/CodeView (Plan C's Global Constraints), so
// this small one-off lives under `covapp/`, not `ui/`. Single toast slot
// (not a queue/stack): a fresh show() replaces whatever is currently
// visible and restarts the dismiss timer, matching the mockup's one-line
// "chip" affordance (screen reference: `.toast` in file-page.html) rather
// than a notification stack.
import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";

const AUTO_DISMISS_MS = 3200;

interface ToastContextValue {
  show(message: string): void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = useCallback((next: string) => {
    if (timer.current !== null) clearTimeout(timer.current);
    setMessage(next);
    timer.current = setTimeout(() => setMessage(null), AUTO_DISMISS_MS);
  }, []);

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      {message !== null && (
        <div
          data-testid="toast"
          role="status"
          className="fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-lg bg-primary-solid px-4 py-2 text-sm font-medium text-white shadow-lg"
        >
          {message}
        </div>
      )}
    </ToastContext.Provider>
  );
}

/** Must be called under a `ToastProvider` (mounted once near the app root,
 * same shape as `useUiStore`'s consumers elsewhere in this repo). */
export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (ctx === null) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}
