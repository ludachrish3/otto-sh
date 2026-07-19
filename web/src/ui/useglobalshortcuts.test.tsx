import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Command } from "./commands";
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
  useUiStore.setState({ paletteOpen: false, theme: "light", sweepArmed: false });
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

  it("opens the palette when no search input is registered", () => {
    renderHook(() => useGlobalShortcuts([]));
    press({ key: "/" });
    expect(useUiStore.getState().paletteOpen).toBe(true);
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
