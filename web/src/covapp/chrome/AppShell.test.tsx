// AppShell's contract (task-3 brief): app bar (brand, theme toggle, ⋮ menu
// with an inline coverage key sourced from getIndex()), header grid
// (crumbs/title/meta/stats), and the "?" keyboard shortcut wiring to
// ShortcutsDialog. AppShell reads tier/state legend data straight off
// window.__OTTO_COV__ (getIndex()) rather than via props — the same fixture
// technique data.test.ts uses.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

/** The ticket picker's real <input>: covapp's `Input` spreads unknown props
 * onto react-aria's TextField wrapper, so a `data-testid` would land on the
 * wrapper div rather than the field — the accessible name is what reaches
 * the input itself. */
function ticketSearchInput(): HTMLInputElement {
  return screen.getByRole("textbox", { name: "Pin a ticket by id" }) as HTMLInputElement;
}

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

  // Task 10: neither the Runs nor the Tickets nav link existed before this
  // task — both are added together here (see AppShell.tsx's header nav
  // comment) so `#/tickets` is actually reachable from the app chrome, not
  // just from a hand-typed hash.
  it("renders top-level nav links to Runs and Tickets", () => {
    renderShell();
    const nav = screen.getByTestId("app-nav");
    expect(within(nav).getByTestId("nav-runs").getAttribute("href")).toBe("#/runs");
    expect(within(nav).getByTestId("nav-tickets").getAttribute("href")).toBe("#/tickets");
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

  // Task 12: a SECOND, independent app-bar chip + ⋮ menu switcher for the
  // ticket-context (denominator) filter — same anatomy as `describe("focus"`
  // above, deliberately never sharing state with it (a ticket has no tier of
  // its own, so its chip/menu rows carry no dot color, unlike a context's).
  describe("ticket", () => {
    beforeEach(() => {
      window.__OTTO_COV__ = makeIndex({
        stamp: "stamp-ticket",
        tickets: [
          {
            id: "PROJ-1",
            url: null,
            owned: 10,
            covered: 5,
            uncovered: 5,
            per_tier: {},
            chunk: "PROJ-1",
          },
          {
            id: "PROJ-2",
            url: null,
            owned: 4,
            covered: 4,
            uncovered: 0,
            per_tier: {},
            chunk: "PROJ-2",
          },
        ],
      });
    });

    it("no ticket chip when nothing is pinned", () => {
      renderShell();
      expect(screen.queryByTestId("ticket-chip")).toBeNull();
    });

    // Follow-up item 5c: the ⋮ menu used to list EVERY ticket flat, which a
    // mature repo turns into hundreds of unreachable rows. Pinning now lives
    // in a search box of its own, in the app bar to the LEFT of the ⋮ menu
    // (not inside it), reachable with "/" like the monitor's search.
    it("app bar carries a ticket search box, and the ⋮ menu no longer lists tickets", async () => {
      const user = userEvent.setup();
      renderShell();
      expect(screen.getByTestId("ticket-search")).toBeTruthy();
      await user.click(screen.getByTestId("appbar-menu"));
      expect(screen.queryByTestId("menu-ticket-all")).toBeNull();
      expect(screen.queryByTestId("menu-ticket-PROJ-1")).toBeNull();
    });

    it("the search box sits before the ⋮ menu in the app bar", () => {
      renderShell();
      const bar = screen.getByTestId("app-bar");
      const order = bar.compareDocumentPosition(screen.getByTestId("ticket-search"));
      expect(order & Node.DOCUMENT_POSITION_CONTAINED_BY).toBeTruthy();
      const search = screen.getByTestId("ticket-search");
      const menu = screen.getByTestId("appbar-menu");
      // FOLLOWING = search comes before menu in document order.
      expect(search.compareDocumentPosition(menu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it("typing narrows the options to matching ticket ids", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.type(ticketSearchInput(), "PROJ-2");
      expect(screen.getByTestId("ticket-search-option-PROJ-2")).toBeTruthy();
      expect(screen.queryByTestId("ticket-search-option-PROJ-1")).toBeNull();
    });

    it("choosing an option pins that ticket, showing the chip", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.type(ticketSearchInput(), "PROJ-1");
      await user.click(screen.getByTestId("ticket-search-option-PROJ-1"));

      const chip = await screen.findByTestId("ticket-chip");
      expect(chip.textContent).toContain("PROJ-1");
    });

    it("caps the option list and says how many more matched", async () => {
      // The scale case the flat menu could not handle: many tickets must not
      // render as many rows.
      window.__OTTO_COV__ = makeIndex({
        stamp: "stamp-ticket",
        tickets: Array.from({ length: 40 }, (_, i) => ({
          id: `PROJ-${i}`,
          url: null,
          owned: 1,
          covered: 0,
          uncovered: 1,
          per_tier: {},
          chunk: `PROJ-${i}`,
        })),
      });
      const user = userEvent.setup();
      renderShell();
      await user.click(ticketSearchInput());

      const options = screen.getAllByTestId(/^ticket-search-option-/);
      expect(options.length).toBeLessThan(40);
      expect(screen.getByTestId("ticket-search-overflow").textContent).toContain("more");
    });

    it("chip ✕ clears the ticket pin, hiding the chip", async () => {
      const user = userEvent.setup();
      renderShell();
      await user.type(ticketSearchInput(), "PROJ-1");
      await user.click(screen.getByTestId("ticket-search-option-PROJ-1"));
      await screen.findByTestId("ticket-chip");

      await user.click(screen.getByTestId("ticket-clear"));
      expect(screen.queryByTestId("ticket-chip")).toBeNull();
    });

    it('"/" focuses the ticket search from anywhere on the page', async () => {
      const user = userEvent.setup();
      renderShell();
      document.body.focus();

      await user.keyboard("/");

      expect(document.activeElement).toBe(ticketSearchInput());
    });

    it('"/" typed inside a text field stays a literal slash', async () => {
      // The shared shouldSuppressSlash guard: the shortcut must never eat a
      // character the user is actually typing.
      const user = userEvent.setup();
      renderShell(<input data-testid="other-field" />);
      const other = screen.getByTestId("other-field") as HTMLInputElement;
      other.focus();

      await user.keyboard("/");

      expect(other.value).toBe("/");
      expect(document.activeElement).toBe(other);
    });

    it("renders no ticket search when no tickets are attributed", () => {
      window.__OTTO_COV__ = makeIndex({ stamp: "stamp-ticket", tickets: [] });
      renderShell();
      expect(screen.queryByTestId("ticket-search")).toBeNull();
    });
  });
});
