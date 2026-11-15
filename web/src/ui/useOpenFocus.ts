// web/src/ui/useOpenFocus.ts
import {
  createElement,
  type ReactElement,
  type RefCallback,
  type RefObject,
  useCallback,
  useEffect,
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
 *
 * `nested` (#1032) is for an overlay that launches a nested overlay
 * rendered elsewhere in the React tree (the event editor, which App renders,
 * over the events panel). When that nested overlay closes, react-aria
 * restores focus to the control that opened it. If a Delete removed that
 * control (the deleted event's Edit button), react-aria's fallbacks do not
 * reach this overlay, and focus falls to <body>, where its Escape never
 * hears a key. Pass `{ nestedOpen }`: when the nested overlay closes, the
 * control in this overlay that last held focus (its opener) is gone, and
 * focus sits on <body> a frame later, the overlay's Dialog takes focus.
 * Gating on the opener being gone, not only on <body>, leaves an ordinary
 * close to react-aria, whose restore may land later than that frame (under
 * virtual modality focusSafely waits for transitions). The Dialog, not the
 * field: the field may be a text input, which keeps Ctrl+Z for itself
 * (ui/shortcuts.ts's isTextEditingTarget), and the delete promises Ctrl+Z
 * undoes it. From the Dialog, Escape closes the overlay and Tab reaches its
 * first control.
 */
export function useOpenFocus<T extends HTMLElement>(
  opening: boolean | string | null,
  nested: { nestedOpen: boolean } = { nestedOpen: false },
): { ref: RefCallback<T>; scope: ReactElement } {
  const { nestedOpen } = nested;
  const target = useRef<T | null>(null);
  const manager = useRef<FocusManager | undefined>(undefined);
  // The control in this overlay that last held focus: when a nested overlay
  // opens, its opener. Declared before the opening focus below, so the
  // listener already hears that focus.
  const lastFocused = useRef<Element | null>(null);
  useLayoutEffect(() => {
    if (opening === false || opening === null) return;
    const doc = target.current?.ownerDocument ?? document;
    const onFocusIn = (e: FocusEvent) => {
      const dialog = dialogOf(target.current);
      if (dialog && e.target instanceof Element && dialog.contains(e.target)) {
        lastFocused.current = e.target;
      }
    };
    doc.addEventListener("focusin", onFocusIn);
    return () => {
      doc.removeEventListener("focusin", onFocusIn);
      lastFocused.current = null;
    };
  }, [opening]);
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
  const wasNestedOpen = useRef(nestedOpen);
  useEffect(() => {
    const closed = wasNestedOpen.current && !nestedOpen;
    wasNestedOpen.current = nestedOpen;
    if (!closed) return;
    const frame = requestAnimationFrame(() => {
      // The overlay itself closed meanwhile: nothing to reclaim focus into.
      if (manager.current === undefined) return;
      // The opener is still here: react-aria restores to it, perhaps later.
      const opener = lastFocused.current;
      if (opener === null || opener.isConnected) return;
      const dialog = dialogOf(target.current);
      if (dialog === null || dialog.ownerDocument.activeElement !== dialog.ownerDocument.body) {
        return;
      }
      dialog.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [nestedOpen]);
  const ref = useCallback((el: T | null) => {
    target.current = el;
  }, []);
  const scope = useMemo(() => createElement(OpenFocusScope, { into: manager }), []);
  return { ref, scope };
}

/** The overlay's Dialog: the nearest one enclosing its preferred field. */
function dialogOf(el: HTMLElement | null): HTMLElement | null {
  return el?.closest<HTMLElement>('[role="dialog"]') ?? null;
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
