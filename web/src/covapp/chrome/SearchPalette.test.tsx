// The ⌘K palette's contract (spec §3): loads both chunks on first open,
// text vs @function modes, the two persisted toggles, smart-case, the cap
// footer, current-file-first grouping, ticket scoping, Enter vs Ctrl+Enter.
// The field is queried by role: the vendored InputBase owns where a
// data-testid lands, the accessible name is what reaches the <input>.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as dataModule from "../data";
import { _resetForTests } from "../data";
import { makeIndex, Providers } from "../testUtils";
import type { IndexPayload, SearchChunk, SymbolsChunk, TicketChunk } from "../types";
import {
  _resetSessionQueryForTests,
  REGEX_PREF_KEY,
  SearchPalette,
  UNCOVERED_PREF_KEY,
} from "./SearchPalette";

const search: SearchChunk = {
  stamp: "stamp-1",
  files: [
    {
      chunk: "product_main.c",
      path: "product/main.c",
      text: "int checked_add(int a, int b) {\n  return a + b;\n}\n",
      states: "cc-",
    },
    {
      chunk: "product_utils.c",
      path: "product/utils.c",
      text: "int double_it(int x) {\n  return checked_add(x, x);\n}\n",
      states: "cu-",
    },
  ],
};
const symbols: SymbolsChunk = {
  stamp: "stamp-1",
  functions: [
    {
      name: "checked_add",
      chunk: "product_main.c",
      path: "product/main.c",
      line: 1,
      end: 3,
      hits: { unit: 4 },
    },
    {
      name: "double_it",
      chunk: "product_utils.c",
      path: "product/utils.c",
      line: 1,
      end: 3,
      hits: {},
    },
  ],
};

function renderPalette(open = true, index: IndexPayload = makeIndex()) {
  const onClose = vi.fn();
  const view = render(
    <SearchPalette index={index} open={open} onClose={onClose} debounceMs={0} />,
    {
      wrapper: Providers,
    },
  );
  return { onClose, ...view };
}

function field(): Promise<HTMLElement> {
  return screen.findByRole("searchbox", { name: "Search code, or @ for functions" });
}

beforeEach(() => {
  window.__OTTO_COV__ = makeIndex();
  vi.spyOn(dataModule, "loadSearchChunk").mockResolvedValue(search);
  vi.spyOn(dataModule, "loadSymbolsChunk").mockResolvedValue(symbols);
});

afterEach(() => {
  cleanup();
  _resetForTests();
  _resetSessionQueryForTests();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  window.location.hash = "";
  localStorage.clear();
});

describe("SearchPalette", () => {
  it("renders nothing while closed and loads both chunks once opened", async () => {
    const { rerender } = renderPalette(false);
    expect(screen.queryByTestId("search-palette")).toBeNull();
    expect(dataModule.loadSearchChunk).not.toHaveBeenCalled();
    rerender(<SearchPalette index={makeIndex()} open={true} onClose={() => {}} debounceMs={0} />);
    expect(await screen.findByTestId("search-palette")).toBeTruthy();
    await waitFor(() => expect(dataModule.loadSearchChunk).toHaveBeenCalledTimes(1));
    expect(dataModule.loadSymbolsChunk).toHaveBeenCalledTimes(1);
  });

  it("a close before the chunks resolve still finishes the load on reopen", async () => {
    const user = userEvent.setup();
    let resolveSearch!: (chunk: SearchChunk) => void;
    const pending = new Promise<SearchChunk>((resolve) => {
      resolveSearch = resolve;
    });
    vi.spyOn(dataModule, "loadSearchChunk").mockReturnValueOnce(pending);
    const index = makeIndex();
    const { rerender } = renderPalette(true, index);
    expect(await screen.findByTestId("search-loading")).toBeTruthy();
    rerender(<SearchPalette index={index} open={false} onClose={() => {}} debounceMs={0} />);
    rerender(<SearchPalette index={index} open={true} onClose={() => {}} debounceMs={0} />);
    resolveSearch(search);
    const input = await field();
    await user.type(input, "checked_add");
    expect(await screen.findByTestId("search-group-product_main.c")).toBeTruthy();
  });

  it("asks for at least 2 characters, then lists matches grouped by file with a total footer", async () => {
    const user = userEvent.setup();
    renderPalette();
    const input = await field();
    await user.type(input, "c");
    expect(await screen.findByTestId("search-short")).toBeTruthy();
    await user.type(input, "hecked_add");
    expect(await screen.findByTestId("search-group-product_main.c")).toBeTruthy();
    expect(screen.getByTestId("search-group-product_utils.c")).toBeTruthy();
    expect(screen.getByTestId("search-footer").textContent).toContain("2 lines in 2 files");
    expect(screen.getByTestId("search-result-0").textContent).toContain("checked_add(int a");
  });

  it("lists the open file page's group first", async () => {
    const user = userEvent.setup();
    const stats = makeIndex().tree.stats;
    const index = makeIndex({
      tree: {
        name: "acme-fw",
        dirs: [
          {
            name: "product",
            dirs: [],
            files: [
              { name: "main.c", path: "product/main.c", chunk: "product_main.c", stats },
              { name: "utils.c", path: "product/utils.c", chunk: "product_utils.c", stats },
            ],
            stats,
          },
        ],
        files: [],
        stats,
      },
    });
    window.location.hash = "#/coverage/product/utils.c";
    renderPalette(true, index);
    await user.type(await field(), "checked_add");
    await screen.findByTestId("search-result-0");
    const groups = screen
      .getAllByTestId(/^search-group-/)
      .map((el) => el.getAttribute("data-testid"));
    expect(groups).toEqual(["search-group-product_utils.c", "search-group-product_main.c"]);
  });

  it("the Regex toggle switches regex mode, persists, and surfaces an invalid pattern", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.click(await screen.findByTestId("search-chip-regex"));
    expect(localStorage.getItem(REGEX_PREF_KEY)).toBe("1");
    const input = await field();
    await user.type(input, "ret.rn");
    expect(await screen.findByTestId("search-result-0")).toBeTruthy();
    await user.clear(input);
    await user.type(input, "(");
    expect((await screen.findByTestId("search-error")).textContent).toMatch(/Invalid|Unterminated/);
  });

  it("Enter while a chip is focused toggles it instead of navigating", async () => {
    const user = userEvent.setup();
    renderPalette();
    // A query with actual results is required: with nothing typed there is
    // no `[role="menuitem"]` row for the capture handler to navigate to at
    // all, so the bug (Enter-on-a-chip falling through to "navigate to the
    // first row") can only surface once a row exists.
    await user.type(await field(), "checked_add");
    await screen.findByTestId("search-result-0");
    const startHash = window.location.hash;
    let guard = 0;
    while (
      document.activeElement?.getAttribute("data-testid") !== "search-chip-regex" &&
      guard < 10
    ) {
      await user.tab();
      guard++;
    }
    expect(document.activeElement?.getAttribute("data-testid")).toBe("search-chip-regex");
    await user.keyboard("{Enter}");
    expect(localStorage.getItem(REGEX_PREF_KEY)).toBe("1");
    expect(window.location.hash).toBe(startHash);
  });

  it("the Uncovered-only toggle keeps only u lines, persists, and the empty state names it", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.click(await screen.findByTestId("search-chip-uncovered"));
    expect(localStorage.getItem(UNCOVERED_PREF_KEY)).toBe("1");
    const input = await field();
    await user.type(input, "checked_add");
    await screen.findByTestId("search-result-0");
    expect(screen.queryByTestId("search-group-product_main.c")).toBeNull();
    expect(screen.getByTestId("search-group-product_utils.c")).toBeTruthy();
    await user.clear(input);
    await user.type(input, "int checked");
    expect((await screen.findByTestId("search-empty")).textContent).toContain("uncovered only");
  });

  it("the Uncovered-only chip has a tooltip explaining its report-wide scope", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByTestId("search-chip-uncovered");
    // react-aria's hover-open gates on its GLOBAL pointer/keyboard modality
    // tracker reading "pointer" (see useTooltipTrigger's onHoverStart), which
    // only flips on a genuine pointermove — arriving, in the real DOM event
    // order, AFTER pointerenter (the event that would open the tooltip). In
    // a fresh jsdom test with no prior interaction, this first hover's own
    // pointerenter still sees the tracker at its unset default and never
    // opens; a throwaway pointer move primes it before the hover that
    // actually matters. Real usage is unaffected — a user has always moved
    // the mouse before hovering any control on the page.
    await user.pointer({ target: document.body });
    await user.hover(screen.getByTestId("search-chip-uncovered"));
    expect((await screen.findByRole("tooltip")).textContent).toContain("across the whole report");
  });

  it("the tooltip stays inside the dialog after a close and reopen", async () => {
    const user = userEvent.setup();
    const index = makeIndex();
    const { rerender } = renderPalette(true, index);
    await screen.findByTestId("search-chip-uncovered");
    rerender(<SearchPalette index={index} open={false} onClose={() => {}} debounceMs={0} />);
    rerender(<SearchPalette index={index} open={true} onClose={() => {}} debounceMs={0} />);
    await screen.findByTestId("search-chip-uncovered");
    // Same modality priming as the hover test above.
    await user.pointer({ target: document.body });
    await user.hover(screen.getByTestId("search-chip-uncovered"));
    expect(
      (await screen.findByRole("tooltip")).closest('[data-testid="search-palette"]'),
    ).not.toBeNull();
  });

  it("@ switches to function mode with a covered/uncovered pill and Enter jumps to the definition", async () => {
    const user = userEvent.setup();
    const { onClose } = renderPalette();
    await user.type(await field(), "@dou");
    const row = await screen.findByTestId("search-result-0");
    expect(row.textContent).toContain("double_it");
    expect(row.textContent).toContain("uncovered");
    await user.keyboard("{Enter}");
    expect(window.location.hash).toBe("#/coverage/product/utils.c?lines=1");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("an orphan function (line 0) links without a lying ?lines=0", async () => {
    const user = userEvent.setup();
    vi.spyOn(dataModule, "loadSymbolsChunk").mockResolvedValue({
      stamp: "stamp-1",
      functions: [
        {
          name: "orphan_fn",
          chunk: "product_main.c",
          path: "product/main.c",
          line: 0,
          end: null,
          hits: {},
        },
      ],
    });
    renderPalette();
    await user.type(await field(), "@orphan");
    const row = await screen.findByTestId("search-result-0");
    expect(row.getAttribute("data-target")).toBe("#/coverage/product/main.c");
  });

  it("Ctrl+Enter navigates and stays open; Enter navigates and closes; a click navigates and closes", async () => {
    const user = userEvent.setup();
    const { onClose } = renderPalette();
    await user.type(await field(), "checked_add");
    await screen.findByTestId("search-result-1");
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(window.location.hash).toBe("#/coverage/product/main.c?lines=1&q=checked_add");
    expect(onClose).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId("search-result-1"));
    expect(window.location.hash).toBe("#/coverage/product/utils.c?lines=2&q=checked_add");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("a pinned ticket scopes results to the files the ticket chunk names", async () => {
    const user = userEvent.setup();
    const ticketChunk: TicketChunk = {
      stamp: "stamp-1",
      id: "PROJ-9",
      files: [
        {
          path: "product/utils.c",
          owned: 1,
          covered: 0,
          missing: [[2, 2]],
          per_tier: {},
          asserted: {},
          asserted_only: 0,
        },
      ],
    };
    vi.spyOn(dataModule, "loadTicketChunk").mockResolvedValue(ticketChunk);
    const index = makeIndex({
      tickets: [
        {
          id: "PROJ-9",
          url: null,
          owned: 1,
          covered: 0,
          uncovered: 1,
          per_tier: {},
          asserted: {},
          chunk: "PROJ-9",
        },
      ],
    });
    window.__OTTO_COV__ = index;
    window.location.hash = "#/coverage?ticket=PROJ-9";
    renderPalette(true, index);
    await user.type(await field(), "checked_add");
    await screen.findByTestId("search-result-0");
    await waitFor(() => expect(screen.queryByTestId("search-group-product_main.c")).toBeNull());
    expect(screen.getByTestId("search-footer").textContent).toContain("in ticket PROJ-9");
  });

  it("a pinned ticket scopes function results (@ mode) too", async () => {
    const user = userEvent.setup();
    const ticketChunk: TicketChunk = {
      stamp: "stamp-1",
      id: "PROJ-9",
      files: [
        {
          path: "product/utils.c",
          owned: 1,
          covered: 0,
          missing: [[2, 2]],
          per_tier: {},
          asserted: {},
          asserted_only: 0,
        },
      ],
    };
    vi.spyOn(dataModule, "loadTicketChunk").mockResolvedValue(ticketChunk);
    const index = makeIndex({
      tickets: [
        {
          id: "PROJ-9",
          url: null,
          owned: 1,
          covered: 0,
          uncovered: 1,
          per_tier: {},
          asserted: {},
          chunk: "PROJ-9",
        },
      ],
    });
    window.__OTTO_COV__ = index;
    window.location.hash = "#/coverage?ticket=PROJ-9";
    renderPalette(true, index);
    await user.type(await field(), "@");
    await screen.findByTestId("search-result-0");
    expect(screen.getByTestId("search-result-0").textContent).toContain("double_it");
    expect(screen.queryByText("checked_add")).toBeNull();
    expect(screen.getByTestId("search-footer").textContent).toContain("in ticket");
  });

  it("bare @ on a report with no functions says so", async () => {
    const user = userEvent.setup();
    vi.spyOn(dataModule, "loadSymbolsChunk").mockResolvedValue({
      stamp: "stamp-1",
      functions: [],
    });
    renderPalette();
    await user.type(await field(), "@");
    expect((await screen.findByTestId("search-empty")).textContent).toContain(
      "No functions in this report",
    );
  });

  it("shows the report-changed message when a chunk's stamp mismatches", async () => {
    vi.spyOn(dataModule, "loadSearchChunk").mockRejectedValue(
      new dataModule.StampMismatchError("search"),
    );
    renderPalette();
    expect((await screen.findByTestId("search-error")).textContent).toContain(
      "report changed on disk",
    );
  });

  // Task 12 fix round 1: every page renders its own <AppShell> (App.tsx
  // returns a different component per route), so navigating remounts the
  // palette from scratch — `query` must survive that remount for the
  // session (spec §3.2), unlike `regex`/`uncovered`, which already persist
  // via localStorage.
  it("a remounted palette restores the session's last query", async () => {
    const user = userEvent.setup();
    const { unmount } = renderPalette();
    const input = await field();
    await user.type(input, "checked_add");
    await screen.findByTestId("search-group-product_main.c");
    unmount();

    renderPalette();
    const reopened = (await field()) as HTMLInputElement;
    expect(reopened.value).toBe("checked_add");
    expect(await screen.findByTestId("search-group-product_main.c")).toBeTruthy();

    // Leave no cross-test leak: clear the field before the module-level
    // session query outlives this test.
    await user.clear(reopened);
  });

  // Task 12 fix round 1: react-aria's SearchField treats Escape-with-content
  // as "clear the field" and stops propagation, so ModalOverlay's
  // dismiss-on-Escape never sees the key — the palette must close itself.
  it("Escape closes the palette with text in the field", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<SearchPalette index={makeIndex()} open={true} onClose={onClose} debounceMs={0} />, {
      wrapper: Providers,
    });
    const input = (await field()) as HTMLInputElement;
    await user.type(input, "ch");
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(input.value).toBe("ch");
  });
});
