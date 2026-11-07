import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Dialog, Modal, ModalOverlay } from "react-aria-components";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TOAST_ACTION_MS, TOAST_MS, ToastViewport, useToastStore } from "@/ui/Toast";

const show = (message: string, action?: { label: string; run: () => void }) =>
  act(() => useToastStore.getState().showToast(message, action));
const advance = (ms: number) =>
  act(() => {
    vi.advanceTimersByTime(ms);
  });

/** The nearest ancestor (or `el` itself) a modal hid: `inert` or aria-hidden. */
function hiddenBy(el: Element): Element | null {
  for (let n: Element | null = el; n !== null; n = n.parentElement) {
    if ((n as HTMLElement).inert || n.getAttribute("aria-hidden") === "true") return n;
  }
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  render(<ToastViewport />);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Toast", () => {
  it("the status region exists before any toast is shown", () => {
    expect(screen.getByRole("status").textContent).toBe("");
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("renders nothing until shown, with role=status", () => {
    expect(screen.queryByTestId("toast")).toBeNull();
    show("Saved");
    expect(screen.getByRole("status").textContent).toBe("Saved");
  });

  it("shows and replaces", () => {
    show("First");
    advance(2000);
    show("Second");
    expect(screen.getAllByTestId("toast")).toHaveLength(1);
    expect(screen.getByTestId("toast").textContent).toBe("Second");
    advance(1200); // the first toast's timer would have fired here
    expect(screen.getByTestId("toast")).toBeTruthy();
    advance(2000);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("auto-dismisses after TOAST_MS", () => {
    show("Saved");
    advance(TOAST_MS - 1);
    expect(screen.getByTestId("toast")).toBeTruthy();
    advance(1);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("action button runs and dismisses", () => {
    const run = vi.fn();
    show("Deleted", { label: "Undo", run });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(run).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("a toast raised by the action's run stays visible", () => {
    show("Deleted", {
      label: "Undo",
      run: () => useToastStore.getState().showToast("next"),
    });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByTestId("toast").textContent).toBe("next");
  });

  it("an action toast lasts TOAST_ACTION_MS", () => {
    show("Deleted", { label: "Undo", run: vi.fn() });
    advance(TOAST_ACTION_MS - 1);
    expect(screen.getByTestId("toast")).toBeTruthy();
    advance(1);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("focus on the action pauses the timer", () => {
    show("Deleted", { label: "Undo", run: vi.fn() });
    const btn = screen.getByRole("button", { name: "Undo" });
    fireEvent.focus(btn);
    advance(20_000);
    expect(screen.getByTestId("toast")).toBeTruthy();
    fireEvent.blur(btn);
    advance(TOAST_ACTION_MS);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("hover pauses the timer and leave restarts it", () => {
    show("Saved");
    const el = screen.getByTestId("toast");
    fireEvent.mouseEnter(el);
    advance(20_000);
    expect(screen.getByTestId("toast")).toBeTruthy();
    fireEvent.mouseLeave(el);
    advance(TOAST_MS);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("focus holds through a hover that ends", () => {
    show("Undid", { label: "Redo", run: vi.fn() });
    const btn = screen.getByRole("button", { name: "Redo" });
    act(() => btn.focus());
    fireEvent.mouseEnter(screen.getByTestId("toast"));
    fireEvent.mouseLeave(screen.getByTestId("toast"));
    advance(20_000);
    expect(screen.getByTestId("toast")).toBeTruthy();
  });

  it("hover holds through a blur", () => {
    show("Undid", { label: "Redo", run: vi.fn() });
    const btn = screen.getByRole("button", { name: "Redo" });
    fireEvent.mouseEnter(screen.getByTestId("toast"));
    act(() => btn.focus());
    act(() => btn.blur());
    advance(20_000);
    expect(screen.getByTestId("toast")).toBeTruthy();
    fireEvent.mouseLeave(screen.getByTestId("toast"));
    advance(TOAST_ACTION_MS);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("a replacement keeps a focus hold that still exists", () => {
    show("Undid", { label: "Redo", run: vi.fn() });
    act(() => screen.getByRole("button", { name: "Redo" }).focus());
    show("Redid", { label: "Undo", run: vi.fn() });
    expect(screen.getByRole("button", { name: "Undo" })).toBe(document.activeElement);
    advance(20_000);
    expect(screen.getByTestId("toast").textContent).toContain("Redid");
  });

  it("a replacement keeps a hover hold that still exists", () => {
    show("First");
    fireEvent.mouseEnter(screen.getByTestId("toast"));
    show("Second");
    advance(20_000);
    expect(screen.getByTestId("toast").textContent).toBe("Second");
  });

  it("a replacement that removes the focused button releases its hold", () => {
    show("Undid", { label: "Redo", run: vi.fn() });
    act(() => screen.getByRole("button", { name: "Redo" }).focus());
    show("Plain");
    advance(TOAST_MS);
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  // #929: undo is live in the Events panel, a React Aria modal. Its
  // hide-outside makes everything outside the modal inert, and its focus
  // scope pulls focus back in; the toast must stay announced and reachable.
  it("stays exposed and keyboard-reachable while a React Aria modal is open", () => {
    render(
      <ModalOverlay isOpen>
        <Modal>
          <Dialog aria-label="Events">
            <button type="button">inside</button>
          </Dialog>
        </Modal>
      </ModalOverlay>,
    );
    const inside = screen.getByRole("button", { name: "inside" });
    act(() => inside.focus());
    show("Undid: delete “x”", { label: "Redo", run: vi.fn() });
    const action = screen.getByTestId("toast-action");
    expect(hiddenBy(screen.getByTestId("toast-region"))).toBeNull();
    expect(hiddenBy(action)).toBeNull();
    // F6 is the keyboard's way out of the modal's Tab cycle to the toast.
    fireEvent.keyDown(inside, { key: "F6" });
    act(() => vi.runOnlyPendingTimers());
    expect(screen.getByTestId("toast").contains(document.activeElement)).toBe(true);
    act(() => action.focus());
    act(() => vi.runOnlyPendingTimers());
    expect(document.activeElement).toBe(action);
  });
});
