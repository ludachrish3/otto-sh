import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import minimal from "../../fixtures/minimal.json" with { type: "json" };
import { useReviewStore } from "../data/reviewStore";
import { type Command, useCommands } from "./commands";
import { registerSearchInput } from "./searchFocus";
import { useUiStore } from "./uiStore";
import { useGlobalShortcuts } from "./useGlobalShortcuts";

function press(init: KeyboardEventInit, target: EventTarget = document.body): KeyboardEvent {
  const e = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(e);
  return e;
}

function makeCommand(overrides: Partial<Command>): Command {
  return {
    id: "action-test",
    label: "Test",
    section: "Actions",
    icon: () => null,
    enabled: true,
    run: () => {},
    ...overrides,
  };
}

afterEach(() => {
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) registration
  // never kicks in — without this, hook state and DOM fragments from one
  // test can leak into the next.
  cleanup();
  useUiStore.setState({
    paletteOpen: false,
    theme: "light",
    sweepArmed: false,
    exportDialogOpen: false,
  });
  useReviewStore.setState({ sessions: [], rawMonitorSessions: null, sourceName: null });
  registerSearchInput(null);
  document.body.innerHTML = "";
});

describe("useGlobalShortcuts — palette chord", () => {
  it("Ctrl+K toggles the palette and prevents default, even from an input", () => {
    renderHook(() => useGlobalShortcuts([]));
    const input = document.createElement("input");
    document.body.appendChild(input);
    const e = press({ key: "k", ctrlKey: true }, input);
    expect(useUiStore.getState().paletteOpen).toBe(true);
    expect(e.defaultPrevented).toBe(true);
    press({ key: "k", ctrlKey: true }, input);
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });
});

describe("useGlobalShortcuts — action chords", () => {
  it("runs a matching enabled command and prevents default", () => {
    const run = vi.fn();
    renderHook(() => useGlobalShortcuts([makeCommand({ binding: { key: "s", mod: true }, run })]));
    const e = press({ key: "s", ctrlKey: true });
    expect(run).toHaveBeenCalledOnce();
    expect(e.defaultPrevented).toBe(true);
  });

  it("ignores a disabled command's chord (no run, default NOT prevented)", () => {
    const run = vi.fn();
    renderHook(() =>
      useGlobalShortcuts([makeCommand({ binding: { key: "s", mod: true }, enabled: false, run })]),
    );
    const e = press({ key: "s", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("ignores bare letters and unbound keys", () => {
    const run = vi.fn();
    renderHook(() => useGlobalShortcuts([makeCommand({ binding: { key: "s", mod: true }, run })]));
    press({ key: "s" });
    press({ key: "x", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
  });

  it("closes an open palette before running a matching chord's command", () => {
    useUiStore.setState({ paletteOpen: true });
    const run = vi.fn();
    renderHook(() => useGlobalShortcuts([makeCommand({ binding: { key: "s", mod: true }, run })]));
    const e = press({ key: "s", ctrlKey: true });
    expect(run).toHaveBeenCalledOnce();
    expect(useUiStore.getState().paletteOpen).toBe(false);
    expect(e.defaultPrevented).toBe(true);
  });
});

describe("useGlobalShortcuts — the Export chord", () => {
  it("Ctrl+S opens the export dialog and prevents the browser default", () => {
    useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "run.json");
    renderHook(() => useGlobalShortcuts(useCommands()));
    let e!: KeyboardEvent;
    act(() => {
      e = press({ key: "s", ctrlKey: true });
    });
    expect(useUiStore.getState().exportDialogOpen).toBe(true);
    expect(e.defaultPrevented).toBe(true);
  });
});

describe("useGlobalShortcuts — bare slash", () => {
  it("focuses a registered search input", () => {
    renderHook(() => useGlobalShortcuts([]));
    const search = document.createElement("input");
    document.body.appendChild(search);
    registerSearchInput(search);
    const e = press({ key: "/" });
    expect(document.activeElement).toBe(search);
    expect(e.defaultPrevented).toBe(true);
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });

  it("does nothing when no search input is registered (does NOT open the palette)", () => {
    // "/" is the in-page search affordance only; the global palette is ⌘K.
    // With no chart/host search box present, "/" must be a no-op — not a
    // second way into the palette (that would re-conflate the two searches).
    renderHook(() => useGlobalShortcuts([]));
    const e = press({ key: "/" });
    expect(useUiStore.getState().paletteOpen).toBe(false);
    expect(e.defaultPrevented).toBe(false);
  });

  it("stays inert while typing in an input (the literal slash survives)", () => {
    renderHook(() => useGlobalShortcuts([]));
    const other = document.createElement("input");
    document.body.appendChild(other);
    const e = press({ key: "/" }, other);
    expect(e.defaultPrevented).toBe(false);
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });

  it("stays inert while the palette is open", () => {
    useUiStore.setState({ paletteOpen: true });
    renderHook(() => useGlobalShortcuts([]));
    const e = press({ key: "/" });
    expect(e.defaultPrevented).toBe(false);
    expect(useUiStore.getState().paletteOpen).toBe(true);
  });
});

describe("useGlobalShortcuts — sweep disarm (Plan 5c)", () => {
  it("Escape disarms an armed sweep before any other handling, and no command runs", () => {
    useUiStore.setState({ sweepArmed: true });
    const run = vi.fn();
    // A command bound to bare Escape would otherwise match too — proving it
    // does NOT run is what shows the disarm branch returns before the
    // command-matching loop, not merely that disarmSweep got called.
    renderHook(() => useGlobalShortcuts([makeCommand({ binding: { key: "escape" }, run })]));
    // The disarm writes to a store the hook subscribes to — act-wrap the press.
    let e!: KeyboardEvent;
    act(() => {
      e = press({ key: "Escape" });
    });
    expect(useUiStore.getState().sweepArmed).toBe(false);
    expect(e.defaultPrevented).toBe(true);
    expect(run).not.toHaveBeenCalled();
  });

  it("Escape is inert (no preventDefault, sweepArmed stays false) when no sweep is armed", () => {
    renderHook(() => useGlobalShortcuts([]));
    const e = press({ key: "Escape" });
    expect(e.defaultPrevented).toBe(false);
    expect(useUiStore.getState().sweepArmed).toBe(false);
  });
});

describe("useGlobalShortcuts — lifecycle", () => {
  it("removes its listener on unmount", () => {
    const run = vi.fn();
    const { unmount } = renderHook(() =>
      useGlobalShortcuts([makeCommand({ binding: { key: "s", mod: true }, run })]),
    );
    unmount();
    press({ key: "s", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
  });
});

describe("useGlobalShortcuts — yieldsToEditing", () => {
  const UNDO = { key: "z", mod: true };
  function setup() {
    const run = vi.fn();
    renderHook(() =>
      useGlobalShortcuts([makeCommand({ binding: UNDO, yieldsToEditing: true, run })]),
    );
    return run;
  }
  it("is inert in an input, a textarea, a [role=spinbutton] and a [role=listbox]", () => {
    const run = setup();
    for (const [tag, role] of [
      ["input", null],
      ["textarea", null],
      ["div", "spinbutton"],
      ["div", "listbox"],
    ] as const) {
      const el = document.createElement(tag);
      if (role) el.setAttribute("role", role);
      document.body.appendChild(el);
      const e = press({ key: "z", ctrlKey: true }, el);
      expect(e.defaultPrevented).toBe(false);
    }
    expect(run).not.toHaveBeenCalled();
  });
  it.each([
    ["eventEditor", { eventEditor: { mode: "create" } }],
    ["markPopover", { markPopover: "mark" }],
    ["palette", { paletteOpen: true }],
  ])("is inert while the %s is open", (_n, state) => {
    const run = setup();
    useUiStore.setState(state as never);
    const e = press({ key: "z", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
    useUiStore.setState({ eventEditor: null, markPopover: null, paletteOpen: false });
  });
  it("is NOT inert inside a [role=dialog] that is no editing overlay", () => {
    const run = setup();
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const btn = document.createElement("button");
    dialog.appendChild(btn);
    document.body.appendChild(dialog);
    const e = press({ key: "z", ctrlKey: true }, btn);
    expect(run).toHaveBeenCalledOnce();
    expect(e.defaultPrevented).toBe(true);
  });
  it("a command without yieldsToEditing still fires in an input", () => {
    const run = vi.fn();
    renderHook(() => useGlobalShortcuts([makeCommand({ binding: UNDO, run })]));
    const input = document.createElement("input");
    document.body.appendChild(input);
    press({ key: "z", ctrlKey: true }, input);
    expect(run).toHaveBeenCalledOnce();
  });
  it("matches extraBindings (shift) but only when the shift is exact", () => {
    const run = vi.fn();
    renderHook(() =>
      useGlobalShortcuts([
        makeCommand({
          binding: { key: "y", mod: true },
          extraBindings: [{ key: "z", mod: true, shift: true }],
          run,
        }),
      ]),
    );
    press({ key: "z", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
    press({ key: "Z", ctrlKey: true, shiftKey: true });
    expect(run).toHaveBeenCalledOnce();
  });
});
