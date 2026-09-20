// AppShell's contract (task-3 brief): app bar (brand, theme toggle, ⋮ menu
// with an inline coverage key sourced from getIndex()), header grid
// (crumbs/title/meta/stats), and the "?" keyboard shortcut wiring to
// ShortcutsDialog. AppShell reads tier/state legend data straight off
// window.__OTTO_COV__ (getIndex()) rather than via props — the same fixture
// technique data.test.ts uses.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useLayoutEffect, useRef } from "react";
import { setInteractionModality } from "react-aria";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as dataModule from "../data";
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

/** A '?' key press the way a browser delivers one: dispatched at the DOCUMENT,
 * whence it bubbles to `window` (AppShell's listener) AND passes react-aria's
 * document-level keydown listener, which is what sets its module-level
 * interaction modality back to "keyboard". Dispatching AT `window` — the
 * previous spelling — skips document entirely, so a modality left at "virtual"
 * by an earlier `fireEvent.click` in this file stayed virtual, and the dialog
 * then opened WITHOUT taking focus (see the injected-hostile guard below and
 * the note in web/vitest.setup.ts). */
function pressQuestionMark() {
  fireEvent.keyDown(document.body, { key: "?" });
}

function renderShell(children = <div>child content</div>) {
  return render(
    <AppShell crumbs={[{ label: "acme-fw" }]} title="acme-fw" meta="42 files" stats={null}>
      {children}
    </AppShell>,
    { wrapper: Providers },
  );
}

/** Fires one keydown from its OWN `useLayoutEffect`, at MOUNT. Layout
 * effects run in post-order (children before parent), siblings in order, so
 * a `<Probe>` rendered as `<AppShell>`'s SIBLING (never nested inside it —
 * see `renderShellSibling`) runs after the shell's commit, before paint and
 * before any passive effect (`useEffect`) anywhere in the tree — a
 * deterministic probe for "is AppShell's listener attached yet": present
 * only if AppShell registers it in a layout effect too, absent if it's
 * still in a `useEffect`. */
function KeydownProbe({
  target,
  init,
}: {
  target: "document" | "window";
  init: KeyboardEventInit;
}) {
  // Dispatch at most once, even if StrictMode or a prop change re-runs the effect.
  const fired = useRef(false);
  useLayoutEffect(() => {
    if (fired.current) return;
    fired.current = true;
    (target === "document" ? document : window).dispatchEvent(new KeyboardEvent("keydown", init));
  }, [target, init]);
  return null;
}

/** `renderShell`'s sibling-tree counterpart, for `KeydownProbe` — the probe
 * MUST NOT be `children` (AppShell's own descendant): React runs a child's
 * layout effects before its parent's, which would fire the probe's keydown
 * BEFORE AppShell's own listener-registering effect, the opposite of what
 * these tests need to prove. */
function renderShellSibling(probe: ReactNode, children = <div>child content</div>) {
  return render(
    <>
      <AppShell crumbs={[{ label: "acme-fw" }]} title="acme-fw" meta="42 files" stats={null}>
        {children}
      </AppShell>
      {probe}
    </>,
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
    pressQuestionMark();
    expect(await screen.findByTestId("shortcuts-dialog")).toBeTruthy();
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("shortcuts-dialog")).toBeNull();
  });

  // #357's guard, with the hostile state INJECTED rather than inherited from
  // whichever ⋮-menu test the shuffle happened to run first: a jsdom
  // `fireEvent.click` is a detail-0 MouseEvent, i.e. a VIRTUAL click to
  // react-aria, which pins its module-level interaction modality at "virtual".
  // In that modality `focusSafely()` defers the dialog's focus move through
  // `runAfterTransition()`, so the dialog mounts with focus still on <body> and
  // an Escape aimed at `document.activeElement` never reaches the overlay.
  // Red before the fix above (dialog still open), and red again if the '?'
  // keydown goes back to being dispatched AT `window`, where react-aria's own
  // document-level keydown listener — the thing that puts the modality back to
  // "keyboard", exactly as a real browser key press would — cannot see it.
  it("'?' survives a preceding virtual (detail-0) click leaving react-aria in virtual modality", async () => {
    const user = userEvent.setup();
    renderShell();
    setInteractionModality("virtual");
    pressQuestionMark();
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

  it("the '?' listener is live before ANY passive effect could run — attached in the commit that paints the shell", () => {
    renderShellSibling(<KeydownProbe target="window" init={{ key: "?", bubbles: true }} />);
    expect(screen.getByTestId("shortcuts-dialog")).toBeTruthy();
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
            asserted: {},
            chunk: "PROJ-1",
          },
          {
            id: "PROJ-2",
            url: null,
            owned: 4,
            covered: 4,
            uncovered: 0,
            per_tier: {},
            asserted: {},
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
      // findByRole proves the menu actually opened; the absence pin is
      // TEXT-level inside it. The old form queried `menu-ticket-*` testids
      // that nothing has rendered since this 5c change — a phantom-id absence
      // check passes against any product, including one that regrew the flat
      // ticket list under a new id (testid_integrity.test.ts bans the shape).
      // No row text may mention either fixture ticket, whatever its testid.
      // (Known limit: within(menu) sees the role="menu" subtree only — a
      // regrowth as a react-aria SubmenuTrigger renders its own portal
      // outside this element and would evade the pin.)
      const menu = await screen.findByRole("menu");
      expect(within(menu).queryByText(/PROJ-/)).toBeNull();
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
          asserted: {},
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

    it("the '/' listener is live before ANY passive effect could run — attached in the commit that paints the shell", () => {
      document.body.focus();
      renderShellSibling(<KeydownProbe target="document" init={{ key: "/", bubbles: true }} />);
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

  // Task 15 (per-product spec §10): a FOURTH chip + ⋮ menu section, for the
  // product pin. Same anatomy as `describe("ticket"` above (neutral dot, no
  // tier colour — a product spans every tier), gated on the report actually
  // carrying products: a unit-only report lists none, so it gets no section.
  describe("product", () => {
    it("renders the product chip when a product is pinned, and clears it", async () => {
      window.__OTTO_COV__ = makeIndex({ products: ["app"] });
      window.location.hash = "#/coverage?product=app";
      renderShell();

      const chip = await screen.findByTestId("product-chip");
      expect(chip.textContent).toContain("app");

      fireEvent.click(screen.getByTestId("product-clear"));
      expect(screen.queryByTestId("product-chip")).toBeNull();
    });

    it("the ⋮ menu lists every product with All products first", async () => {
      window.__OTTO_COV__ = makeIndex({ products: ["agent", "app"] });
      renderShell();

      fireEvent.click(screen.getByTestId("appbar-menu"));

      const all = await screen.findByTestId("product-menu-all");
      const agent = screen.getByTestId("product-menu-agent");
      const app = screen.getByTestId("product-menu-app");
      // "All products" precedes every per-product row.
      expect(all.compareDocumentPosition(agent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(all.compareDocumentPosition(app) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    it("no product section when the report has no products", async () => {
      window.__OTTO_COV__ = makeIndex({ products: [] });
      renderShell();

      fireEvent.click(screen.getByTestId("appbar-menu"));

      // The shortcuts row proves the menu actually opened, so the absence
      // below is a real absence — the same "menu opened, section missing"
      // shape the overrides section's test uses.
      expect(await screen.findByTestId("menu-shortcuts")).toBeTruthy();
      expect(screen.queryByTestId("product-menu-all")).toBeNull();
    });
  });
});

// Task 11 (manual-overrides spec §6): the ⋮ menu's "Overrides" section (a
// hide-asserted toggle plus one disabled row per entry) and the always-
// visible badge — both gated on `index.overrides.length > 0`, same "no
// data, no control" pattern the ticket search box above already uses.
describe("AppShell: overrides badge + hide-asserted toggle (Task 11)", () => {
  function withOverrides(): IndexPayload {
    return makeIndex({
      stamp: "stamp-ovr",
      overrides: [
        {
          id: 0,
          tier: "system",
          key: "src/net/tcp.c:12",
          reason: "manual smoke test",
          as_of: null,
        },
        {
          id: 1,
          tier: "unit",
          key: "src/net/udp.c:5",
          reason: "flaky sim, verified by hand",
          as_of: null,
        },
      ],
    });
  }

  it("shows the overrides badge with the entry count when overrides exist", () => {
    window.__OTTO_COV__ = withOverrides();
    renderShell();
    expect(screen.getByTestId("overrides-badge").textContent).toBe("2 overrides");
  });

  it("singularizes the badge count for exactly one override", () => {
    window.__OTTO_COV__ = makeIndex({
      stamp: "stamp-ovr-1",
      overrides: [{ id: 0, tier: "system", key: "a.c:1", reason: "r", as_of: null }],
    });
    renderShell();
    expect(screen.getByTestId("overrides-badge").textContent).toBe("1 override");
  });

  it("renders neither the badge nor the menu section when there are no overrides", async () => {
    const user = userEvent.setup();
    window.__OTTO_COV__ = makeIndex({ stamp: "stamp-no-ovr", overrides: [] });
    renderShell();
    expect(screen.queryByTestId("overrides-badge")).toBeNull();

    await user.click(screen.getByTestId("appbar-menu"));
    expect(await screen.findByTestId("menu-shortcuts")).toBeTruthy();
    expect(screen.queryByTestId("toggle-hide-asserted")).toBeNull();
    expect(screen.queryByTestId("override-entry")).toBeNull();
  });

  it("⋮ menu lists one override-entry row per entry, with key + tier + reason", async () => {
    const user = userEvent.setup();
    window.__OTTO_COV__ = withOverrides();
    renderShell();

    await user.click(screen.getByTestId("appbar-menu"));
    const entries = await screen.findAllByTestId("override-entry");
    expect(entries).toHaveLength(2);
    expect(entries[0]?.textContent).toContain("src/net/tcp.c:12");
    expect(entries[0]?.textContent).toContain("manual smoke test");
    expect(entries[1]?.textContent).toContain("src/net/udp.c:5");
    expect(entries[1]?.textContent).toContain("flaky sim, verified by hand");
  });

  it("⋮ menu shows the hide-asserted toggle, labeled and not yet active", async () => {
    const user = userEvent.setup();
    window.__OTTO_COV__ = withOverrides();
    renderShell();

    await user.click(screen.getByTestId("appbar-menu"));
    const toggle = await screen.findByTestId("toggle-hide-asserted");
    expect(toggle.textContent).toContain("Hide asserted coverage");
    expect(toggle.querySelector("svg")).toBeFalsy(); // no ✓ yet
  });

  it("clicking the toggle flips hideAsserted (✓ appears in the menu) and toasts", async () => {
    const user = userEvent.setup();
    window.__OTTO_COV__ = withOverrides();
    window.location.hash = "#/coverage";
    renderShell();

    await user.click(screen.getByTestId("appbar-menu"));
    await user.click(await screen.findByTestId("toggle-hide-asserted"));

    expect(screen.getByTestId("toast").textContent).toMatch(/hidden/i);
    expect(window.location.hash).toBe("#/coverage?asserted=1");

    await user.click(screen.getByTestId("appbar-menu"));
    const toggle = await screen.findByTestId("toggle-hide-asserted");
    expect(toggle.querySelector("svg")).toBeTruthy(); // ✓ now present
  });

  // F3 fix (final review): a hand-typed `?asserted=1` deep link on a report
  // with NO overrides used to blank the tickets/coverage data (hideAsserted
  // reads true from the query) with no visible control anywhere to clear
  // it — the toggle row was gated on `overrides.length > 0`, which is false
  // here. The toggle row must stay reachable whenever hideAsserted is
  // already active, regardless of whether this report has overrides; the
  // always-visible badge stays overrides-gated since it announces "overrides
  // are active", which isn't true in this scenario.
  it("shows the hide-asserted toggle (and lets it clear) even with no overrides, when ?asserted=1", async () => {
    const user = userEvent.setup();
    window.__OTTO_COV__ = makeIndex({ stamp: "stamp-no-ovr-deep-link", overrides: [] });
    window.location.hash = "#/coverage?asserted=1";
    renderShell();

    expect(screen.queryByTestId("overrides-badge")).toBeNull();

    await user.click(screen.getByTestId("appbar-menu"));
    const toggle = await screen.findByTestId("toggle-hide-asserted");
    expect(toggle.querySelector("svg")).toBeTruthy(); // ✓ — hideAsserted started true
    expect(screen.queryByTestId("override-entry")).toBeNull(); // still no entries to list

    await user.click(toggle);
    expect(window.location.hash).toBe("#/coverage");

    await user.click(screen.getByTestId("appbar-menu"));
    expect(screen.queryByTestId("toggle-hide-asserted")).toBeNull(); // gone once cleared
  });
});

describe("search palette wiring", () => {
  beforeEach(() => {
    vi.spyOn(dataModule, "loadSearchChunk").mockResolvedValue({ stamp: "stamp-1", files: [] });
    vi.spyOn(dataModule, "loadSymbolsChunk").mockResolvedValue({ stamp: "stamp-1", functions: [] });
  });

  it("Ctrl+K toggles the palette (jsdom is non-mac), and the trigger opens it", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.queryByTestId("search-palette")).toBeNull();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(await screen.findByTestId("search-palette")).toBeTruthy();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    await waitFor(() => expect(screen.queryByTestId("search-palette")).toBeNull());
    await user.click(screen.getByTestId("search-trigger"));
    expect(await screen.findByTestId("search-palette")).toBeTruthy();
  });

  it("the Ctrl+K listener is live before ANY passive effect could run — attached in the commit that paints the shell", async () => {
    // The palette itself opens synchronously (the probe's whole point). This
    // describe's mocked `loadSearchChunk`/`loadSymbolsChunk` promises
    // (`beforeEach`) resolve AFTER the synchronous test body returns, and
    // SearchPalette's own effect then calls setData from that resolution,
    // outside any act() — which the console guard (vitest.setup.ts) would
    // fail the test over. `async` + `findBy*` (not a bare `getBy*`) so its
    // async act() wrapping absorbs that update instead.
    renderShellSibling(
      <KeydownProbe target="document" init={{ key: "k", ctrlKey: true, bubbles: true }} />,
    );
    expect(await screen.findByTestId("search-palette")).toBeTruthy();
  });

  it("Escape closes the palette", async () => {
    const user = userEvent.setup();
    renderShell();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    await screen.findByTestId("search-palette");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("search-palette")).toBeNull());
  });

  it("the shortcuts dialog lists the palette binding", async () => {
    renderShell();
    pressQuestionMark();
    const dialog = await screen.findByTestId("shortcuts-dialog");
    expect(dialog.textContent).toContain("Search code and functions");
    expect(dialog.textContent).toContain("Ctrl K");
  });
});
