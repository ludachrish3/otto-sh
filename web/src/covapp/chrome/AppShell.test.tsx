// AppShell's contract (task-3 brief): app bar (brand, theme toggle, ⋮ menu
// with an inline coverage key sourced from getIndex()), header grid
// (crumbs/title/meta/stats), and the "?" keyboard shortcut wiring to
// ShortcutsDialog. AppShell reads tier/state legend data straight off
// window.__OTTO_COV__ (getIndex()) rather than via props — the same fixture
// technique data.test.ts uses.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { makeIndex, makeRun, Providers } from "../testUtils";
import type { IndexPayload } from "../types";
import { AppShell } from "./AppShell";

beforeEach(() => {
  window.__OTTO_COV__ = makeIndex();
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  localStorage.clear();
  window.location.hash = "";
  document.documentElement.classList.remove("dark-mode");
});

function renderShell(children = <div>child content</div>) {
  return render(
    <AppShell crumbs={[{ label: "acme-fw" }]} title="acme-fw" meta="42 files" stats={null}>
      {children}
    </AppShell>,
    { wrapper: Providers },
  );
}

describe("AppShell", () => {
  it("renders the brand with the project name, crumbs, title, and meta", () => {
    renderShell();
    expect(screen.getByTestId("brand").textContent).toContain("otto coverage");
    expect(screen.getByTestId("brand").textContent).toContain("acme-fw");
    expect(screen.getByTestId("breadcrumbs")).toBeTruthy();
    expect(screen.getByText("acme-fw", { selector: "h1" })).toBeTruthy();
    expect(screen.getByTestId("page-meta").textContent).toBe("42 files");
  });

  it("renders children in the page body", () => {
    renderShell(<div data-testid="child-marker">hi</div>);
    expect(screen.getByTestId("child-marker")).toBeTruthy();
  });

  it("opens the ⋮ menu showing the shortcuts item and tier labels from the fixture payload", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("appbar-menu"));
    expect(await screen.findByTestId("menu-shortcuts")).toBeTruthy();
    expect(screen.getByText("System (e2e)")).toBeTruthy();
    expect(screen.getByText("Unit")).toBeTruthy();
  });

  it("⋮ menu key shows the States and Branches sections", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("appbar-menu"));
    expect(await screen.findByText("uncovered")).toBeTruthy();
    expect(screen.getByText("excluded")).toBeTruthy();
    expect(screen.getByText("stale (revoked)")).toBeTruthy();
    expect(screen.getByText("aging")).toBeTruthy();
    expect(screen.getByText("taken")).toBeTruthy();
    expect(screen.getByText("not taken")).toBeTruthy();
    expect(screen.getByText("unreachable")).toBeTruthy();
  });

  it("⋮ menu renders without crashing when tier_order is empty (data-less store)", async () => {
    window.__OTTO_COV__ = makeIndex({ tier_order: [], tier_labels: {}, tier_colors: {} });
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("appbar-menu"));
    expect(await screen.findByTestId("menu-shortcuts")).toBeTruthy();
    // No tier rows, but States/Branches sections still render.
    expect(screen.getByText("uncovered")).toBeTruthy();
  });

  it("theme toggle flips the documentElement dark-mode class and persists it", () => {
    renderShell();
    const before = document.documentElement.classList.contains("dark-mode");
    fireEvent.click(screen.getByTestId("theme-toggle"));
    expect(document.documentElement.classList.contains("dark-mode")).toBe(!before);
    expect(localStorage.getItem("otto-theme")).toBe(before ? "light" : "dark");
    fireEvent.click(screen.getByTestId("theme-toggle"));
    expect(document.documentElement.classList.contains("dark-mode")).toBe(before);
  });

  it("'Keyboard shortcuts' menu item opens ShortcutsDialog", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("appbar-menu"));
    await user.click(await screen.findByTestId("menu-shortcuts"));
    expect(await screen.findByTestId("shortcuts-dialog")).toBeTruthy();
  });

  it("pressing '?' opens ShortcutsDialog; Escape closes it", async () => {
    const user = userEvent.setup();
    renderShell();
    fireEvent.keyDown(window, { key: "?" });
    expect(await screen.findByTestId("shortcuts-dialog")).toBeTruthy();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("shortcuts-dialog")).toBeNull();
  });

  it("ignores '?' when the keydown target is a text input", () => {
    renderShell(<input data-testid="text-input" />);
    const input = screen.getByTestId("text-input");
    fireEvent.keyDown(input, { key: "?" });
    expect(screen.queryByTestId("shortcuts-dialog")).toBeNull();
  });

  it("coverage-key rows render at full opacity, not the vendored disabled dimming", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByTestId("appbar-menu"));
    const label = await screen.findByText("uncovered");
    const row = label.closest('[role="menuitem"]') as HTMLElement;
    expect(row).toBeTruthy();
    expect(row.className).toContain("opacity-100");
    expect(row.className).toContain("cursor-default");
    expect(row.className).not.toContain("opacity-50");
  });

  describe("focus", () => {
    beforeEach(() => {
      window.__OTTO_COV__ = makeIndex({
        stamp: "stamp-focus",
        tier_order: ["system", "unit"],
        tier_colors: { system: "green", unit: "blue" },
        runs: [
          makeRun({ id: 1, label: "nightly-full", tier: "system" }),
          makeRun({ id: 2, label: "unit harvest", tier: "unit" }),
        ],
      });
    });

    it("no chip when nothing is focused", () => {
      renderShell();
      expect(screen.queryByTestId("focus-chip")).toBeNull();
    });

    it("⋮ menu shows 'All contexts' checked (✓) by default, and one item per context", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.click(screen.getByTestId("appbar-menu"));
      expect((await screen.findByTestId("menu-focus-all")).querySelector("svg")).toBeTruthy();
      expect(screen.getByTestId("menu-focus-nightly-full").querySelector("svg")).toBeNull();
      expect(screen.getByTestId("menu-focus-unit harvest")).toBeTruthy();
    });

    it("clicking a context in the menu pins it, showing the chip with its tier color", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.click(screen.getByTestId("appbar-menu"));
      await user.click(screen.getByTestId("menu-focus-nightly-full"));

      const chip = await screen.findByTestId("focus-chip");
      expect(chip.textContent).toContain("nightly-full");
      const dot = chip.querySelector("[aria-hidden]");
      expect((dot as HTMLElement).style.backgroundColor).toBe("green");
    });

    it("chip ✕ clears focus, hiding the chip", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.click(screen.getByTestId("appbar-menu"));
      await user.click(screen.getByTestId("menu-focus-nightly-full"));
      await screen.findByTestId("focus-chip");

      await user.click(screen.getByTestId("focus-clear"));
      expect(screen.queryByTestId("focus-chip")).toBeNull();
    });

    it("the ✓ moves in the menu as the switcher selection changes", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.click(screen.getByTestId("appbar-menu"));
      await user.click(screen.getByTestId("menu-focus-nightly-full"));

      await user.click(screen.getByTestId("appbar-menu"));
      let allItem = screen.getByTestId("menu-focus-all");
      let nightlyItem = screen.getByTestId("menu-focus-nightly-full");
      expect(allItem.querySelector("svg")).toBeNull();
      expect(nightlyItem.querySelector("svg")).toBeTruthy();

      await user.click(screen.getByTestId("menu-focus-all"));
      await user.click(screen.getByTestId("appbar-menu"));
      allItem = screen.getByTestId("menu-focus-all");
      nightlyItem = screen.getByTestId("menu-focus-nightly-full");
      expect(allItem.querySelector("svg")).toBeTruthy();
      expect(nightlyItem.querySelector("svg")).toBeNull();
    });
  });
});
