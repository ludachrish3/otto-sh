// The source view's Columns menu: the vendored Dropdown with two selection
// sections (Show columns, Hit counts as) and a Reset columns action, all
// reading and writing the one source-columns store.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Providers, setViewportWidth } from "../testUtils";
import { ColumnsMenu, keysOf } from "./ColumnsMenu";

function renderMenu({ tierCount = 2, fileHasTickets = true } = {}) {
  return render(<ColumnsMenu tierCount={tierCount} fileHasTickets={fileHasTickets} />, {
    wrapper: Providers,
  });
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("columns-menu"));
  return screen.findByRole("menu");
}

const ITEM_ROLES = ["menuitem", "menuitemcheckbox", "menuitemradio"] as const;

/** Every menu item, of any of the three item roles, in document order. */
function allItems(container: HTMLElement = document.body): HTMLElement[] {
  return [
    ...container.querySelectorAll<HTMLElement>(ITEM_ROLES.map((r) => `[role="${r}"]`).join(",")),
  ];
}

function item(name: string): HTMLElement {
  const found = ITEM_ROLES.flatMap((role) => screen.queryAllByRole(role, { name }));
  if (found.length !== 1)
    throw new Error(`expected one menu item named ${name}, found ${found.length}`);
  return found[0] as HTMLElement;
}

function checked(name: string): boolean {
  return item(name).getAttribute("aria-checked") === "true";
}

/** Every column/mode item's checked state, in menu order. */
function states(): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const el of [
    ...screen.queryAllByRole("menuitemcheckbox"),
    ...screen.queryAllByRole("menuitemradio"),
  ]) {
    out[el.textContent ?? ""] = el.getAttribute("aria-checked") === "true";
  }
  return out;
}

describe("ColumnsMenu", () => {
  it("is a Columns button whose menu has Show columns, Hit counts as and Reset columns", async () => {
    const user = userEvent.setup();
    renderMenu();
    const trigger = screen.getByTestId("columns-menu");
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger.textContent).toBe("Columns");
    const menu = await openMenu(user);

    const show = within(menu).getByRole("group", { name: "Show columns" });
    expect(
      within(show)
        .getAllByRole("menuitemcheckbox")
        .map((el) => el.textContent),
    ).toEqual(["Tickets", "Line numbers", "Branches", "Hit counts"]);
    const mode = within(menu).getByRole("group", { name: "Hit counts as" });
    expect(
      within(mode)
        .getAllByRole("menuitemradio")
        .map((el) => el.textContent),
    ).toEqual(["Per tier", "Total"]);
    const items = allItems(menu);
    expect(items.at(-1)?.textContent).toBe("Reset columns");
    expect(items.at(-1)?.getAttribute("role")).toBe("menuitem");
  });

  it("has no Tickets item when the file has no tickets, before and after a toggle", async () => {
    const user = userEvent.setup();
    renderMenu({ fileHasTickets: false });
    await openMenu(user);
    expect(screen.queryByRole("menuitemcheckbox", { name: "Tickets" })).toBeNull();
    await user.click(item("Branches"));
    await user.click(item("Line numbers"));
    expect(screen.queryByRole("menuitemcheckbox", { name: "Tickets" })).toBeNull();
  });

  it("checks what resolveColumns resolves at the current width", async () => {
    const user = userEvent.setup();
    act(() => setViewportWidth(1000));
    const { unmount } = renderMenu();
    await openMenu(user);
    expect(states()).toEqual({
      Tickets: true,
      "Line numbers": true,
      Branches: true,
      "Hit counts": true,
      "Per tier": true,
      Total: false,
    });
    unmount();

    act(() => setViewportWidth(700));
    renderMenu();
    await openMenu(user);
    expect(states()).toEqual({
      Tickets: false,
      "Line numbers": true,
      Branches: false,
      "Hit counts": true,
      "Per tier": false,
      Total: true,
    });
  });

  it("toggling Branches or choosing Total keeps the menu open", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openMenu(user);
    await user.click(item("Branches"));
    expect(screen.getByRole("menu")).toBeTruthy();
    expect(checked("Branches")).toBe(false);
    await user.click(item("Total"));
    expect(screen.getByRole("menu")).toBeTruthy();
    expect(checked("Total")).toBe(true);
    expect(checked("Per tier")).toBe(false);
  });

  it("turning Hit counts off disables Per tier and Total, which keep their selection", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openMenu(user);
    await user.click(item("Hit counts"));
    expect(checked("Hit counts")).toBe(false);
    for (const name of ["Per tier", "Total"]) {
      expect(item(name).getAttribute("aria-disabled")).toBe("true");
    }
    expect(checked("Per tier")).toBe(true);

    await user.click(item("Total"));
    expect(checked("Per tier")).toBe(true);
    expect(checked("Total")).toBe(false);

    // Enter and Space dispatched on Total itself: a disabled item ignores both.
    for (const key of ["Enter", " "]) {
      fireEvent.keyDown(item("Total"), { key });
      fireEvent.keyUp(item("Total"), { key });
    }
    expect(checked("Per tier")).toBe(true);
    expect(checked("Total")).toBe(false);
  });

  it("with one tier, Per tier and Total are disabled and Per tier is selected", async () => {
    const user = userEvent.setup();
    act(() => setViewportWidth(700));
    renderMenu({ tierCount: 1 });
    await openMenu(user);
    for (const name of ["Per tier", "Total"]) {
      expect(item(name).getAttribute("aria-disabled")).toBe("true");
    }
    expect(checked("Per tier")).toBe(true);
    expect(checked("Total")).toBe(false);
  });

  it("Reset columns clears every choice and closes the menu", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openMenu(user);
    await user.click(item("Branches"));
    await user.click(item("Line numbers"));
    await user.click(item("Total"));
    await user.click(item("Reset columns"));
    expect(screen.queryByRole("menu")).toBeNull();
    await openMenu(user);
    expect(states()).toEqual({
      Tickets: true,
      "Line numbers": true,
      Branches: true,
      "Hit counts": true,
      "Per tier": true,
      Total: false,
    });
  });

  it("a resize while open re-renders the defaults and keeps chosen items", async () => {
    const user = userEvent.setup();
    act(() => setViewportWidth(1000));
    renderMenu();
    await openMenu(user);
    await user.click(item("Line numbers"));
    act(() => setViewportWidth(700));
    expect(screen.getByRole("menu")).toBeTruthy();
    expect(states()).toEqual({
      Tickets: false,
      "Line numbers": false,
      Branches: false,
      "Hit counts": true,
      "Per tier": false,
      Total: true,
    });
    act(() => setViewportWidth(1000));
    expect(states()).toEqual({
      Tickets: true,
      "Line numbers": false,
      Branches: true,
      "Hit counts": true,
      "Per tier": true,
      Total: false,
    });
  });

  it("Tab focuses Columns with its focus ring, and Escape returns focus to it", async () => {
    const user = userEvent.setup();
    renderMenu();
    const trigger = screen.getByTestId("columns-menu");
    await user.tab();
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("data-focus-visible")).toBe("true");
    for (const cls of ["outline-focus-ring", "focus-visible:outline-2"]) {
      expect(trigger.classList.contains(cls)).toBe(true);
    }
    expect(trigger.classList.contains("outline-none")).toBe(false);
    await user.keyboard("{Enter}");
    await screen.findByRole("menu");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    // react-aria restores focus to the trigger on the next frame.
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("keysOf maps an all selection to every offered key", () => {
    expect(keysOf("all", ["lineNumbers", "branches"])).toEqual(
      new Set(["lineNumbers", "branches"]),
    );
    expect(keysOf(new Set(["branches"]), ["lineNumbers", "branches"])).toEqual(
      new Set(["branches"]),
    );
  });

  it("ArrowDown moves Hit counts, Per tier, Total, Reset columns in that order", async () => {
    const user = userEvent.setup();
    renderMenu();
    await openMenu(user);
    act(() => item("Hit counts").focus());
    expect(document.activeElement).toBe(item("Hit counts"));
    for (const name of ["Per tier", "Total", "Reset columns"]) {
      await user.keyboard("{ArrowDown}");
      expect(document.activeElement).toBe(item(name));
    }
  });
});
