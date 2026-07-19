// web/src/ui/commandmenu.test.tsx
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json";
import { useReviewStore } from "../data/reviewStore";
import { TopologyPage } from "../topo/TopologyPage";
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

// xyflow's documented jsdom mocks — the Backspace regression test below
// mounts the real TopologyPage next to the palette.
class RO {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= RO as unknown as typeof ResizeObserver;
// @ts-expect-error jsdom lacks DOMMatrixReadOnly
globalThis.DOMMatrixReadOnly ??= class {
  m22 = 1;
};

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
  useReviewStore.setState({ sessions: [], rawMonitorSessions: null, activeSessionId: null });
});

describe("CommandMenu", () => {
  it("renders nothing while closed, the dialog when open", async () => {
    render(<CommandMenu commands={COMMANDS} />);
    expect(screen.queryByTestId("command-menu")).toBeNull();
    act(() => {
      useUiStore.setState({ paletteOpen: true });
    });
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

  it("backspace deletes in the palette input while the topology page is mounted", async () => {
    // Regression (proven red pre-fix): React Flow's default deleteKeyCode
    // ("Backspace") preventDefaulted the keystroke the palette's Autocomplete
    // re-dispatches on the virtually-focused row, and react-aria then
    // suppressed the REAL Backspace in the input — Backspace typed characters
    // fine but never deleted. TopologyPage now passes deleteKeyCode={null};
    // this mounts the real page (not a bare ReactFlow) so the guard lives
    // with the ReactFlow instance that owns the document-level listener.
    useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
    const user = userEvent.setup();
    useUiStore.setState({ paletteOpen: true });
    render(
      <div>
        <TopologyPage />
        <CommandMenu commands={COMMANDS} />
      </div>,
    );
    const input = (await screen.findByTestId("command-input")) as HTMLInputElement;
    await user.type(input, "topo");
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Backspace}");
    expect(input.value).toBe("top");
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
