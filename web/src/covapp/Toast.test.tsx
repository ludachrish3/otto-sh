// Toast.tsx's contract (task-3 brief): a single, auto-dismissing toast
// rendered fixed-bottom-center via ToastProvider/useToast.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider, useToast } from "./Toast";

function Trigger({ message }: { message: string }) {
  const { show } = useToast();
  return (
    <button type="button" data-testid="trigger" onClick={() => show(message)}>
      show
    </button>
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Toast", () => {
  it("renders nothing until show() is called", () => {
    render(
      <ToastProvider>
        <Trigger message="Saved" />
      </ToastProvider>,
    );
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("show() renders the toast with the given message", () => {
    render(
      <ToastProvider>
        <Trigger message="Saved" />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByTestId("trigger"));
    expect(screen.getByTestId("toast").textContent).toBe("Saved");
  });

  it("auto-dismisses after ~3.2s", () => {
    render(
      <ToastProvider>
        <Trigger message="Saved" />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByTestId("trigger"));
    expect(screen.getByTestId("toast")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(3200);
    });
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("a second show() replaces the message and restarts the dismiss timer (single toast)", () => {
    render(
      <ToastProvider>
        <Trigger message="First" />
        <Trigger message="Second" />
      </ToastProvider>,
    );
    fireEvent.click(screen.getAllByTestId("trigger")[0]);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    fireEvent.click(screen.getAllByTestId("trigger")[1]);
    expect(screen.getByTestId("toast").textContent).toBe("Second");
    // Original timer (would have fired at 3200ms from the FIRST show, i.e.
    // 1200ms from now) must not dismiss the second toast early.
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(screen.getByTestId("toast")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.queryByTestId("toast")).toBeNull();
  });
});
