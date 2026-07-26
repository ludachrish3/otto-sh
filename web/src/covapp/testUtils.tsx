// Shared vitest fixtures for covapp tests (Task 4 cleanup, ledger-sanctioned
// deferred-minor from Task 3): App.test.tsx and AppShell.test.tsx each
// hand-rolled their own emptyStats()/makeIndex()/CSS.escape polyfill —
// identical boilerplate, duplicated. This module is the one copy; every
// covapp test file that needs a fixture IndexPayload imports from here
// instead of re-declaring it.
import type { ReactNode } from "react";

import { FocusProvider } from "./focus";
import { ToastProvider } from "./Toast";
import type { IndexPayload, RunJson, Stats } from "./types";

// jsdom (pinned for this project) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection utilities call unconditionally when a Menu autofocuses or
// scrolls a selected/focused item into view — same polyfill
// web/src/__tests__/shell.test.tsx installs for the vendored Dropdown.
// Side effect runs at import time, so importing this module is enough.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

/** All-zero `Stats`, the common case for a fixture node nobody's exercising
 * numbers on. Pass `overrides` for a scenario that needs specific counts —
 * `per_tier`/`ctx_lines` overrides replace the whole sub-object, matching
 * how callers already built ad hoc stats fixtures before this migration. */
export function emptyStats(overrides: Partial<Stats> = {}): Stats {
  return {
    lines: { total: 0, hit: 0, per_tier: {} },
    branches: { total: 0, hit: 0, per_tier: {} },
    flags: { stale: 0, aging: 0, excluded: 0 },
    ctx_lines: {},
    ...overrides,
  };
}

/** A minimal-but-valid `RunJson` (Task 6 migration: contexts.test.ts and
 * RunsPage.test.tsx both build several of these per test — DirectoryPage.
 * test.tsx hand-rolled two before this, unshared). `id` defaults to 1 so a
 * single-call site doesn't need to think about it; give every run in a
 * multi-run fixture an explicit `id` (it's also the `run_contrib` lookup
 * key). Pass `overrides` to replace any field wholesale. */
export function makeRun(overrides: Partial<RunJson> = {}): RunJson {
  return {
    id: 1,
    tier: "system",
    label: "nightly-full",
    board: "stm32h7-rev3",
    host: "router-a",
    labs: [],
    captured_at: "2026-07-21",
    tester: null,
    ticket: null,
    note: null,
    base_commit: "8f3c21abcdef",
    dirty_remap: false,
    aging: false,
    ...overrides,
  };
}

/** A minimal-but-valid `IndexPayload` (empty tree, no runs). Pass
 * `overrides` to replace any top-level key wholesale — the same pattern
 * AppShell.test.tsx used before this migration. */
export function makeIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return {
    format: 1,
    stamp: "stamp-1",
    generated_at: "2026-07-25 00:00 UTC",
    otto_version: "0.0.0",
    project_name: "acme-fw",
    tier_order: ["system", "unit"],
    tier_labels: { system: "System (e2e)", unit: "Unit" },
    tier_colors: { system: "green", unit: "blue" },
    state_colors: { uncovered: "#f4a9a8", excluded: "grey", stale: "violet", aging: "tan" },
    thresholds: { high: 80, medium: 70 },
    stat_types: ["line", "branch", "decision"],
    runs: [],
    run_contrib: {},
    total_lines: 0,
    tree: { name: "acme-fw", dirs: [], files: [], stats: emptyStats() },
    ...overrides,
  };
}

/** Task 7 migration: `useFocus()` now requires a `FocusProvider` ancestor
 * (same "throws without a provider" contract `Toast.tsx`'s `useToast()`
 * already has), and `FocusProvider` itself calls `useToast()` — so every
 * covapp test that renders a component reachable from `AppShell` (i.e. all
 * of them) needs both providers mounted above it. Pass as RTL's `render`
 * `wrapper` option (`render(ui, { wrapper: Providers })`) rather than
 * hand-wrapping JSX at each call site. */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <ToastProvider>
      <FocusProvider>{children}</FocusProvider>
    </ToastProvider>
  );
}
