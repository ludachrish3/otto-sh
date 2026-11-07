// web/src/ui/useOpenFocus.ts
import {
  createElement,
  type ReactElement,
  type RefCallback,
  type RefObject,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
} from "react";
import { type FocusManager, useFocusManager } from "react-aria";

/**
 * Focuses an overlay's first field as the overlay opens (#957).
 *
 * react-aria's useDialog focuses a Dialog that mounts with nothing focused
 * inside it, and arms a 500 ms timer that re-focuses the Dialog whenever
 * focus then sits on it or on <body>. A nested overlay (a popover, a Select,
 * a confirmation, an editor over a panel) that closes in that window leaves
 * focus on <body> until its FocusScope's restore frame, which restores only
 * from <body>: the timer took focus first, and the restore was lost. With
 * focus already inside the Dialog when its effect runs, no timer is armed.
 *
 * THE RULE for an overlay's initial focus, in one place:
 * - It hosts a nested overlay whose close restores focus inside it: use
 *   this hook.
 * - It hosts none: RAC's autoFocus suffices. Its timer may still arm, under
 *   virtual modality, but with no nested restore to race, the refocus only
 *   lands on the overlay itself, which is harmless.
 * - Never React's own autoFocus on the input: it focuses during commit,
 *   before the overlay's FocusScope registers, and an enclosing contained
 *   scope pulls focus back out.
 * - When the preferred field cannot take focus (it is disabled, e.g. the
 *   event editor reopened while its Save is still pending), focus falls back
 *   to the overlay's first tabbable control, through the FocusScope's own
 *   focus manager. It must land in the same layout effect: focusing later,
 *   once the field is enabled again, does not disarm a timer already armed.
 *
 * Why the hook, not RAC's autoFocus, for the first case: react-aria's
 * autoFocus goes through focusSafely, which under virtual (screen-reader)
 * modality waits a frame (runAfterTransition), so the Dialog's effect still
 * found nothing focused and armed the timer. A layout effect runs before
 * every passive effect in the commit, so it lands first in every modality.
 * Skipping focusSafely's wait gives up little: it waits only for CSS
 * transitions, and preventScroll gives the same no-scroll protection.
 * Call this hook in the component that RENDERS the overlay, so
 * its layout effect runs after the overlay's FocusScope's (children's
 * layout effects run first) and the scope tree already holds the overlay.
 *
 * `opening` is false or null while the overlay is closed, and true (or the
 * name of what it opened for, e.g. MarkControl's "mark" / "start") while it
 * is mounted; the field takes focus each time it changes to an open value.
 * Pass `ref` to the preferred field's element, and render `scope` anywhere
 * inside the overlay's Dialog: it hands the hook the overlay FocusScope's
 * focus manager for the fallback.
 */
export function useOpenFocus<T extends HTMLElement>(
  opening: boolean | string | null,
): { ref: RefCallback<T>; scope: ReactElement } {
  const target = useRef<T | null>(null);
  const manager = useRef<FocusManager | undefined>(undefined);
  useLayoutEffect(() => {
    if (opening === false || opening === null) return;
    // The contract, checked on every open (dev only, like theme.ts): a caller
    // that leaves `scope` out still focuses its field, and would only lose
    // the fallback, silently, the day that field is disabled.
    if (import.meta.env.DEV && manager.current === undefined) {
      throw new Error("useOpenFocus: `scope` must render inside the overlay's Dialog");
    }
    const el = target.current;
    // preventScroll: the same no-scroll focus react-aria's focusSafely uses.
    el?.focus({ preventScroll: true });
    if (el !== null && el.ownerDocument.activeElement === el) return;
    manager.current?.focusFirst({ tabbable: true });
  }, [opening]);
  const ref = useCallback((el: T | null) => {
    target.current = el;
  }, []);
  const scope = useMemo(() => createElement(OpenFocusScope, { into: manager }), []);
  return { ref, scope };
}

/** Reads the enclosing FocusScope's focus manager into the hook's ref.
 * Rendered inside the overlay, so the nearest scope is the overlay's. */
function OpenFocusScope({ into }: { into: RefObject<FocusManager | undefined> }): null {
  const manager = useFocusManager();
  useLayoutEffect(() => {
    into.current = manager;
    return () => {
      into.current = undefined;
    };
  }, [into, manager]);
  return null;
}
