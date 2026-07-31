// Task 3 wrapped the Task-2 route placeholders in AppShell; Task 4 swaps
// "/coverage" and "/coverage/*" over to the real dispatcher (CoverageRoute
// in App.tsx) — DirectoryPage for a resolved DirNode, the (still-a-
// placeholder-body) file route for a resolved FileNode. This proves the
// wiring survives — GuardScreen still wins when data is missing (Task 2's
// contract, unchanged by this task), and each route reaches the right
// component with real chrome (app bar, crumbs, stats card) derived from
// getIndex().
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import * as dataModule from "./data";
import { emptyStats, makeIndex as makeIndexBase } from "./testUtils";
import type { FileChunk, IndexPayload } from "./types";

function makeIndex(): IndexPayload {
  return makeIndexBase({
    tier_order: ["unit"],
    tier_labels: { unit: "Unit" },
    tier_colors: { unit: "blue" },
    total_lines: 10,
    tree: {
      name: "acme-fw",
      dirs: [],
      files: [
        {
          name: "x.c",
          path: "x.c",
          chunk: "x.c",
          stats: emptyStats({
            lines: {
              total: 3,
              hit: 1,
              per_tier: { unit: 1 },
              asserted_per_tier: {},
              asserted_only: 0,
            },
          }),
        },
      ],
      stats: emptyStats({
        lines: {
          total: 10,
          hit: 7,
          per_tier: { unit: 7 },
          asserted_per_tier: {},
          asserted_only: 0,
        },
        branches: { total: 4, hit: 2, per_tier: { unit: 2 } },
      }),
    },
  });
}

beforeEach(() => {
  window.__OTTO_COV__ = makeIndex();
  window.location.hash = "#/coverage";
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  dataModule._resetForTests();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  window.location.hash = "";
  localStorage.clear();
  document.documentElement.classList.remove("dark-mode");
});

function makeFileChunk(overrides: Partial<FileChunk> = {}): FileChunk {
  return {
    stamp: "stamp-1",
    chunk: "x.c",
    path: "x.c",
    source: "int x;\n",
    lines: {},
    excluded: [],
    ...overrides,
  };
}

describe("App (Task 3 chrome wiring)", () => {
  it("renders GuardScreen instead of chrome when data is missing", () => {
    delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
    render(<App />);
    expect(screen.getByTestId("guard-screen")).toBeTruthy();
    expect(screen.queryByTestId("app-bar")).toBeNull();
  });

  it("#/coverage renders DirectoryPage (the real directory tree) inside AppShell chrome", () => {
    window.location.hash = "#/coverage";
    render(<App />);
    expect(screen.getByTestId("app-bar")).toBeTruthy();
    expect(screen.getByTestId("stats-card")).toBeTruthy();
    expect(screen.getByTestId("stats-row-all")).toBeTruthy();
    expect(screen.getByTestId("directory-tree")).toBeTruthy();
    expect(screen.getByTestId("tree-row-file:x.c")).toBeTruthy();
  });

  it("#/coverage/x.c renders the real FilePage with crumbs + stats resolved via findNode", async () => {
    vi.spyOn(dataModule, "loadFileChunk").mockResolvedValue(makeFileChunk());
    window.location.hash = "#/coverage/x.c";
    render(<App />);

    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("x.c");
    expect(screen.getByTestId("app-bar")).toBeTruthy();
    expect(screen.getByTestId("stats-card")).toBeTruthy();
    expect(await screen.findByTestId("code-row-1")).toBeTruthy();
  });

  it("#/runs renders the real RunsPage (contexts grouped from runs) inside AppShell chrome", () => {
    window.location.hash = "#/runs";
    render(<App />);
    expect(screen.getByTestId("app-bar")).toBeTruthy();
    expect(screen.getByTestId("stats-card")).toBeTruthy();
    expect(screen.getByTestId("stats-row-all")).toBeTruthy();
    expect(screen.getByTestId("runs-card")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Runs & contexts");
  });

  it("#/tickets renders the real TicketsPage inside AppShell chrome", () => {
    window.__OTTO_COV__ = makeIndexBase({
      tier_order: ["unit"],
      tier_labels: { unit: "Unit" },
      tier_colors: { unit: "blue" },
      tickets: [
        {
          id: "PROJ-1",
          url: null,
          owned: 10,
          covered: 4,
          uncovered: 6,
          per_tier: { unit: 4 },
          asserted: { unit: 0 },
          chunk: "PROJ-1",
        },
      ],
    });
    window.location.hash = "#/tickets";
    render(<App />);
    expect(screen.getByTestId("app-bar")).toBeTruthy();
    expect(screen.getByTestId("stats-card")).toBeTruthy();
    expect(screen.getByTestId("tickets-card")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Tickets");
  });

  it("an unknown route renders NotFoundPlaceholder without chrome", () => {
    window.location.hash = "#/nope";
    render(<App />);
    expect(screen.getByTestId("not-found")).toBeTruthy();
    expect(screen.queryByTestId("app-bar")).toBeNull();
  });

  // Regression: a file named "100%.c" is a legal display name but "%" is a
  // reserved URI character. Before this fix, DirectoryPage's onNavigate
  // wrote the raw name into the hash while App.tsx's segmentsFromWildcard
  // decodeURIComponent'd it back out on read — a real filename containing
  // "%" (not followed by two hex digits) threw URIError on the very next
  // render, uncaught by any error boundary, white-screening the whole app.
  // This exercises the FULL round trip through the real production wiring:
  // click (encode) -> hashchange -> route match -> decode -> findNode.
  it("clicking a file whose name contains '%' round-trips through encode/decode without throwing", async () => {
    vi.spyOn(dataModule, "loadFileChunk").mockResolvedValue(
      makeFileChunk({ chunk: "100_.c", path: "100%.c" }),
    );
    window.__OTTO_COV__ = makeIndexBase({
      tier_order: ["unit"],
      tier_labels: { unit: "Unit" },
      tier_colors: { unit: "blue" },
      total_lines: 3,
      tree: {
        name: "acme-fw",
        dirs: [],
        files: [
          {
            name: "100%.c",
            path: "100%.c",
            chunk: "100_.c",
            stats: emptyStats({
              lines: {
                total: 3,
                hit: 1,
                per_tier: { unit: 1 },
                asserted_per_tier: {},
                asserted_only: 0,
              },
            }),
          },
        ],
        stats: emptyStats({
          lines: {
            total: 3,
            hit: 1,
            per_tier: { unit: 1 },
            asserted_per_tier: {},
            asserted_only: 0,
          },
        }),
      },
    });
    window.location.hash = "#/coverage";
    render(<App />);

    expect(() => fireEvent.click(screen.getByTestId("name-file:100%.c"))).not.toThrow();

    expect(window.location.hash).toBe("#/coverage/100%25.c");
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading.textContent).toBe("100%.c");
  });
});
