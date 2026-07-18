// web/src/ui/commandmenu.test.tsx
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandMenu } from "./CommandMenu";
import type { Command } from "./commands";
import { useUiStore } from "./uiStore";

// jsdom lacks CSS.escape; react-aria menus call it (same polyfill as
// shell.test.tsx — see its comment).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

function cmd(overrides: Partial<Command>): Command {
  return {
    id: "x",
    label: "X",
    section: "Actions",
    icon: () => null,
    enabled: true,
    run: () => {},
    ...overrides,
  };
}

const COMMANDS: Command[] = [
  cmd({ id: "nav-topology", label: "Topology", section: "Navigation" }),
  cmd({
    id: "nav-host-test1",
    label: "test1",
    section: "Navigation",
    sublabel: "qemu-x86 · slot 1",
  }),
  cmd({ id: "action-import", label: "Import…", binding: { key: "i", mod: true } }),
  cmd({ id: "action-export", label: "Export", binding: { key: "s", mod: true }, enabled: false }),
];

afterEach(() => {
  cleanup();
  useUiStore.setState({ paletteOpen: false, theme: "light" });
});

describe("CommandMenu", () => {
  it("renders nothing while closed, the dialog when open", async () => {
    render(<CommandMenu commands={COMMANDS} />);
    expect(screen.queryByTestId("command-menu")).toBeNull();
    useUiStore.setState({ paletteOpen: true });
    expect(await screen.findByTestId("command-menu")).toBeTruthy();
    expect(screen.getByTestId("command-item-nav-topology")).toBeTruthy();
  });

  it("shows section headers and chord keycaps (non-mac jsdom → Ctrl form)", async () => {
    useUiStore.setState({ paletteOpen: true });
    render(<CommandMenu commands={COMMANDS} />);
    const menu = await screen.findByTestId("command-menu");
    expect(menu.textContent).toContain("Navigation");
    expect(menu.textContent).toContain("Actions");
    expect(menu.textContent).toContain("Ctrl I");
    expect(screen.getByTestId("command-item-nav-host-test1").textContent).toContain(
      "qemu-x86 · slot 1",
    );
  });

  it("filters rows by typed text", async () => {
    const user = userEvent.setup();
    useUiStore.setState({ paletteOpen: true });
    render(<CommandMenu commands={COMMANDS} />);
    await user.type(await screen.findByTestId("command-input"), "test1");
    await waitFor(() => {
      expect(screen.queryByTestId("command-item-action-import")).toBeNull();
      expect(screen.getByTestId("command-item-nav-host-test1")).toBeTruthy();
    });
  });

  it("shows the empty state when nothing matches", async () => {
    const user = userEvent.setup();
    useUiStore.setState({ paletteOpen: true });
    render(<CommandMenu commands={COMMANDS} />);
    await user.type(await screen.findByTestId("command-input"), "zzzzzz");
    expect(await screen.findByTestId("command-empty")).toBeTruthy();
  });

  it("clicking a row runs it and closes the palette", async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    useUiStore.setState({ paletteOpen: true });
    render(<CommandMenu commands={[cmd({ id: "action-import", label: "Import…", run })]} />);
    await user.click(await screen.findByTestId("command-item-action-import"));
    expect(run).toHaveBeenCalledOnce();
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });

  it("a disabled row is aria-disabled and does not run", async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    useUiStore.setState({ paletteOpen: true });
    render(
      <CommandMenu
        commands={[cmd({ id: "action-export", label: "Export", enabled: false, run })]}
      />,
    );
    const row = await screen.findByTestId("command-item-action-export");
    expect(row.getAttribute("aria-disabled")).toBe("true");
    await user.click(row);
    expect(run).not.toHaveBeenCalled();
    expect(useUiStore.getState().paletteOpen).toBe(true);
  });

  it("Escape closes the palette", async () => {
    const user = userEvent.setup();
    useUiStore.setState({ paletteOpen: true });
    render(<CommandMenu commands={COMMANDS} />);
    await screen.findByTestId("command-menu");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(useUiStore.getState().paletteOpen).toBe(false));
  });
});
