# Untitled UI Command Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Button-border view-switcher tabs, an icon-advanced overflow menu, an in-house ⌘K command palette with chord shortcuts, a reworked AppBar (search trigger left, glyphs right, no status cluster), and a reconnecting banner — per `docs/superpowers/specs/2026-07-17-untitledui-command-layer-design.md`.

**Architecture:** All new code lives in `web/src/ui/**` (authored, fully gated) — vendored `web/src/components/**` is NEVER edited. One command registry (`ui/commands.ts`) drives the palette rows, the global shortcut handlers, and every visible keycap hint. The palette is built on react-aria-components (`ModalOverlay + Dialog + Autocomplete + Menu`, v1.19.0 — already installed), styled with the vendored theme tokens. Palette open/close + theme live in a tiny ui-layer zustand store so the AppBar (outside the wouter Router) and the command layer (inside it) share state.

**Tech Stack:** React 18 + TypeScript, react-aria-components 1.19.0, zustand, wouter (hash routing), Tailwind v4 theme tokens, vitest + @testing-library/react (jsdom), Playwright e2e via pytest.

## Global Constraints

- **PREREQUISITE:** the topology-default-view route change must be implemented first (`/` = `TopologyPage`, `/hosts` = `OverviewPage`; spec `2026-07-17-topology-default-view-docs-hero-design.md`). Task 10's tabs navigate `/` ↔ `/hosts`. Do not start Task 10 if `App.tsx` still routes `/` to `OverviewPage`; every other task is route-independent.
- **Never hand-edit vendored files** (`web/src/components/**`, `web/src/styles/theme.css`, `web/src/utils/cx.ts`, `web/src/utils/is-react-component.ts`, `web/src/hooks/use-breakpoint.ts`, `web/src/hooks/use-resize-observer.ts`). No changes to `web/untitledui.lock.json` or `scripts/check_untitledui_drift.sh`. If a vendored file seems to need a change, the reconciliation happens on OUR side (see `web/README.md`).
- **Binding set (spec decision 4, exact):** `⌘K`/`Ctrl+K` toggle palette · bare `/` focus search (palette on pages without one) · `⌘I` import · `⌘S` export · `⌘L` theme · `⌘.` pause/resume. Topology/Hosts have NO chord. Never bind `⌘T`/`⌘N`/`⌘W`/`⌘⇧T` (browser-owned, uninterceptable), `⌘H`/`⌘M`/`⌘Q` (macOS-owned), `⌘D`/`⌘P` (bookmark/print — sacred).
- **Per-task web gate:** from `web/`: `npm run check && npm run typecheck && npx vitest run <task's test files>` (`make web-check` runs the whole thing including coverage — run it at Tasks 11 and 12). After ANY Python edit (Task 12): `nox -s lint`.
- **Browser lane:** bare `pytest tests/e2e/monitor/dashboard` is Chromium-only. Only `nox -s dashboard` (three-engine matrix) may be called green. `make web` MUST rebuild the dist before any e2e run (stale-bundle guard will otherwise fail the suite).
- **Testids preserved:** `view-toggle`, `pause-toggle`, `export-button`, `overflow-menu`, `menu-import`, `menu-export`, `menu-theme`, `series-search`, `brand`. Testids removed: `status-text`, `status-dot`. Testids relocated: `live-window` + `live-window-5m/15m/1h` and `events-button` + `events-count` move from the AppBar to SubjectPage's title row (decisions 10/11).
- **Commits:** this plan executes on a worktree branch (create via superpowers:using-git-worktrees) — self-commit per task with a conventional prefix and an `Assisted-by: Claude Fable 5` trailer.

## File Structure

```
web/src/ui/
  shortcuts.ts          (NEW)  platform detection, Binding type, format/match, / guard, binding constants
  shortcuts.test.ts     (NEW)
  uiStore.ts            (NEW)  zustand: paletteOpen + theme + actions
  uistore.test.ts       (NEW)
  commands.ts           (NEW)  useCommands(): Command[] — the one registry
  commands.test.tsx     (NEW)
  searchFocus.ts        (NEW)  module-level search-input focus registry
  useGlobalShortcuts.ts (NEW)  document keydown listener
  useglobalshortcuts.test.tsx (NEW)
  Kbd.tsx               (NEW)  keycap chip (mirrors InputBase's keycap classes)
  CommandMenu.tsx       (NEW)  the palette (ModalOverlay+Dialog+Autocomplete+Menu)
  commandmenu.test.tsx  (NEW)
  CommandLayer.tsx      (NEW)  mounts useGlobalShortcuts + CommandMenu inside Router
  SearchTrigger.tsx     (NEW)  input-lookalike AppBar button
  searchtrigger.test.tsx (NEW)
  ViewSwitcher.tsx      (NEW)  button-border Tabs, route-derived selection
  viewswitcher.test.tsx (NEW)
  TextInput.tsx         (MOD)  + shortcut / inputRef pass-through
web/src/shell/
  AppBar.tsx            (MOD)  layout rework, glyphs, menu recomposition, no status
  ReconnectingBanner.tsx (NEW)
  reconnectingbanner.test.tsx (NEW)
  ImportExport.tsx      (MOD)  delete ExportButton (superseded by AppBar glyph)
web/src/App.tsx         (MOD)  mount CommandLayer + ReconnectingBanner inside Router
web/src/pages/OverviewPage.tsx (MOD)  ButtonGroup -> ViewSwitcher
web/src/pages/SeriesPanel.tsx  (MOD)  shortcut="/" + registerSearchInput
web/src/topo/TopologyPage.tsx  (MOD)  ButtonGroup -> ViewSwitcher
web/src/__tests__/shell.test.tsx (MOD)  status-text waits replaced
web/src/__tests__/seriespanel.test.tsx (MOD)  keycap assertion
tests/e2e/monitor/dashboard/test_review_shell.py (MOD)
tests/e2e/monitor/dashboard/test_live_shell.py   (MOD)
tests/e2e/monitor/dashboard/test_command_palette.py (NEW)
```

---

### Task 1: `ui/shortcuts.ts` — bindings, platform detection, slash guard

**Files:**
- Create: `web/src/ui/shortcuts.ts`
- Test: `web/src/ui/shortcuts.test.ts`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces (later tasks import these exact names):
  - `interface Binding { key: string; mod?: boolean }`
  - `detectMac(platform: string): boolean`, `isMac(): boolean` (cached `detectMac(navigator.platform)`)
  - `formatBinding(b: Binding): string` — `{key:"i",mod:true}` → `"⌘I"` (mac) / `"Ctrl I"`; `{key:"."}` variants keep the literal; `{key:"/"}` → `"/"`
  - `matchesBinding(e: KeyboardEvent, b: Binding): boolean`
  - `shouldSuppressSlash(target: EventTarget | null, overlayOpen: boolean): boolean`
  - Constants: `PALETTE_BINDING: Binding = { key: "k", mod: true }`, `SEARCH_BINDING: Binding = { key: "/" }`, `IMPORT_BINDING = { key: "i", mod: true }`, `EXPORT_BINDING = { key: "s", mod: true }`, `THEME_BINDING = { key: "l", mod: true }`, `PAUSE_BINDING = { key: ".", mod: true }`

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/ui/shortcuts.test.ts
import { describe, expect, it } from "vitest";

import {
  type Binding,
  detectMac,
  formatBindingFor,
  matchesBindingFor,
  shouldSuppressSlash,
} from "./shortcuts";

const MOD_I: Binding = { key: "i", mod: true };
const SLASH: Binding = { key: "/" };

function keyEvent(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", init);
}

describe("detectMac", () => {
  it("recognizes mac-family platforms", () => {
    expect(detectMac("MacIntel")).toBe(true);
    expect(detectMac("iPhone")).toBe(true);
    expect(detectMac("Win32")).toBe(false);
    expect(detectMac("Linux x86_64")).toBe(false);
    expect(detectMac("")).toBe(false);
  });
});

describe("formatBindingFor", () => {
  it("formats chords per platform", () => {
    expect(formatBindingFor(MOD_I, true)).toBe("⌘I");
    expect(formatBindingFor(MOD_I, false)).toBe("Ctrl I");
    expect(formatBindingFor({ key: ".", mod: true }, true)).toBe("⌘.");
    expect(formatBindingFor({ key: ".", mod: true }, false)).toBe("Ctrl .");
  });
  it("formats the bare slash identically everywhere", () => {
    expect(formatBindingFor(SLASH, true)).toBe("/");
    expect(formatBindingFor(SLASH, false)).toBe("/");
  });
});

describe("matchesBindingFor", () => {
  it("matches ctrl chords on non-mac and rejects meta", () => {
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true }), MOD_I, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "I", ctrlKey: true }), MOD_I, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "i", metaKey: true }), MOD_I, false)).toBe(false);
  });
  it("matches meta chords on mac and rejects ctrl", () => {
    expect(matchesBindingFor(keyEvent({ key: "i", metaKey: true }), MOD_I, true)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true }), MOD_I, true)).toBe(false);
  });
  it("rejects bare letters, extra modifiers, and wrong keys", () => {
    expect(matchesBindingFor(keyEvent({ key: "i" }), MOD_I, false)).toBe(false);
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true, shiftKey: true }), MOD_I, false)).toBe(false);
    expect(matchesBindingFor(keyEvent({ key: "i", ctrlKey: true, altKey: true }), MOD_I, false)).toBe(false);
    expect(matchesBindingFor(keyEvent({ key: "s", ctrlKey: true }), MOD_I, false)).toBe(false);
  });
  it("matches the bare slash only without modifiers", () => {
    expect(matchesBindingFor(keyEvent({ key: "/" }), SLASH, false)).toBe(true);
    expect(matchesBindingFor(keyEvent({ key: "/", ctrlKey: true }), SLASH, false)).toBe(false);
  });
});

describe("shouldSuppressSlash", () => {
  it("suppresses inside editable targets", () => {
    const input = document.createElement("input");
    const textarea = document.createElement("textarea");
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "true");
    expect(shouldSuppressSlash(input, false)).toBe(true);
    expect(shouldSuppressSlash(textarea, false)).toBe(true);
    expect(shouldSuppressSlash(editable, false)).toBe(true);
  });
  it("suppresses inside an open dialog or menu subtree", () => {
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const child = document.createElement("span");
    dialog.appendChild(child);
    document.body.appendChild(dialog);
    expect(shouldSuppressSlash(child, false)).toBe(true);
    dialog.remove();
  });
  it("suppresses while the palette overlay is open regardless of target", () => {
    expect(shouldSuppressSlash(document.body, true)).toBe(true);
  });
  it("fires on a plain body target", () => {
    expect(shouldSuppressSlash(document.body, false)).toBe(false);
    expect(shouldSuppressSlash(null, false)).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/shortcuts.test.ts`
Expected: FAIL — `Cannot find module './shortcuts'` (or missing exports).

- [ ] **Step 3: Write the implementation**

```ts
// web/src/ui/shortcuts.ts
// The single source of keyboard-binding truth (spec §Command layer): the
// registry (commands.ts), the shortcut layer (useGlobalShortcuts.ts), and
// every visible hint (palette keycaps, dropdown addons, AppBar/search
// keycaps) all format and match THESE objects — nothing binding-shaped is
// ever stored twice, so a hint cannot drift from its handler.
//
// Reserved-key rule for future bindings (spec decision 4): never ⌘T/⌘N/⌘W/
// ⌘⇧T (browser-owned, uninterceptable) or ⌘H/⌘M/⌘Q (macOS-owned); avoid
// ⌘D/⌘P (bookmark/print — interceptable but sacred). ⌘S and ⌘L below
// intentionally shadow save-page and focus-address-bar while the dashboard
// has focus.

export interface Binding {
  /** KeyboardEvent.key, lowercase. */
  key: string;
  /** true = Cmd on mac / Ctrl elsewhere. Absent = bare key (only "/"). */
  mod?: boolean;
}

export function detectMac(platform: string): boolean {
  return /Mac|iPhone|iPad|iPod/.test(platform);
}

let cachedIsMac: boolean | null = null;
export function isMac(): boolean {
  cachedIsMac ??= detectMac(navigator.platform ?? "");
  return cachedIsMac;
}

/** Pure core, platform injected — what the unit tests exercise. */
export function formatBindingFor(binding: Binding, mac: boolean): string {
  const keyLabel = binding.key.length === 1 ? binding.key.toUpperCase() : binding.key;
  if (!binding.mod) return keyLabel;
  return mac ? `⌘${keyLabel}` : `Ctrl ${keyLabel}`;
}

export function formatBinding(binding: Binding): string {
  return formatBindingFor(binding, isMac());
}

/** Pure core, platform injected — what the unit tests exercise. */
export function matchesBindingFor(e: KeyboardEvent, binding: Binding, mac: boolean): boolean {
  if (e.altKey || e.shiftKey) return false;
  if (e.key.toLowerCase() !== binding.key) return false;
  if (!binding.mod) return !e.ctrlKey && !e.metaKey;
  return mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

export function matchesBinding(e: KeyboardEvent, binding: Binding): boolean {
  return matchesBindingFor(e, binding, isMac());
}

/** The bare "/" guard (spec §Global shortcuts): a literal slash typed into
 * any field (series search, palette filter, a react-aria popover) must stay
 * a slash. Chords need no guard — they never type characters. */
export function shouldSuppressSlash(target: EventTarget | null, overlayOpen: boolean): boolean {
  if (overlayOpen) return true;
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"], [role="menu"], [role="listbox"]') !==
    null
  );
}

export const PALETTE_BINDING: Binding = { key: "k", mod: true };
export const SEARCH_BINDING: Binding = { key: "/" };
export const IMPORT_BINDING: Binding = { key: "i", mod: true };
export const EXPORT_BINDING: Binding = { key: "s", mod: true };
export const THEME_BINDING: Binding = { key: "l", mod: true };
export const PAUSE_BINDING: Binding = { key: ".", mod: true };
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/shortcuts.test.ts`
Expected: PASS (all describe blocks).

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`
Expected: clean.

```bash
git add web/src/ui/shortcuts.ts web/src/ui/shortcuts.test.ts
git commit -m "feat(monitor-web): shortcut binding module — platform-aware chords + slash guard

Assisted-by: Claude Fable 5"
```

---

### Task 2: `ui/uiStore.ts` — palette open/close + theme state

**Files:**
- Create: `web/src/ui/uiStore.ts`
- Test: `web/src/ui/uistore.test.ts`

**Interfaces:**
- Consumes: `loadTheme`, `saveTheme`, `type Theme` from `web/src/theme.ts` (existing: `loadTheme(): Theme`, `saveTheme(theme: Theme): void` persists AND applies the html class).
- Produces:
  - `useUiStore` (zustand hook) with state `{ paletteOpen: boolean; theme: Theme; actions: { openPalette(): void; closePalette(): void; togglePalette(): void; toggleTheme(): void } }`

The AppBar currently holds theme in a local `useState` — once the palette can also toggle theme, a component-local copy goes stale. This store becomes the ONE reactive owner; `theme.ts` stays the persistence layer.

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/ui/uistore.test.ts
import { afterEach, describe, expect, it } from "vitest";

import { useUiStore } from "./uiStore";

afterEach(() => {
  useUiStore.setState({ paletteOpen: false, theme: "light" });
  localStorage.clear();
  document.documentElement.classList.remove("dark-mode");
});

describe("uiStore palette state", () => {
  it("opens, closes, and toggles", () => {
    const { openPalette, closePalette, togglePalette } = useUiStore.getState().actions;
    expect(useUiStore.getState().paletteOpen).toBe(false);
    openPalette();
    expect(useUiStore.getState().paletteOpen).toBe(true);
    closePalette();
    expect(useUiStore.getState().paletteOpen).toBe(false);
    togglePalette();
    expect(useUiStore.getState().paletteOpen).toBe(true);
    togglePalette();
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });
});

describe("uiStore theme", () => {
  it("toggleTheme flips state, persists, and applies the html class", () => {
    useUiStore.setState({ theme: "light" });
    useUiStore.getState().actions.toggleTheme();
    expect(useUiStore.getState().theme).toBe("dark");
    expect(localStorage.getItem("otto-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(true);
    useUiStore.getState().actions.toggleTheme();
    expect(useUiStore.getState().theme).toBe("light");
    expect(localStorage.getItem("otto-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark-mode")).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/uistore.test.ts`
Expected: FAIL — `Cannot find module './uiStore'`.

- [ ] **Step 3: Write the implementation**

```ts
// web/src/ui/uiStore.ts
// UI-chrome state that must be shared across the Router boundary: the
// AppBar (outside wouter's <Router> — see App.tsx) opens the palette; the
// palette itself + the shortcut layer mount inside the Router (they need
// navigation). A store, not context, so both sides reach it without a
// provider spanning that boundary. Theme lives here too (not in AppBar
// useState) because the palette's ⌘L command must flip the same reactive
// value the menu label reads.
import { create } from "zustand";

import { loadTheme, saveTheme, type Theme } from "../theme";

interface UiState {
  paletteOpen: boolean;
  theme: Theme;
  actions: {
    openPalette: () => void;
    closePalette: () => void;
    togglePalette: () => void;
    toggleTheme: () => void;
  };
}

export const useUiStore = create<UiState>()((set, get) => ({
  paletteOpen: false,
  theme: loadTheme(),
  actions: {
    openPalette: () => set({ paletteOpen: true }),
    closePalette: () => set({ paletteOpen: false }),
    togglePalette: () => set({ paletteOpen: !get().paletteOpen }),
    toggleTheme: () => {
      const next: Theme = get().theme === "dark" ? "light" : "dark";
      saveTheme(next);
      set({ theme: next });
    },
  },
}));
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/uistore.test.ts`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/uiStore.ts web/src/ui/uistore.test.ts
git commit -m "feat(monitor-web): ui store — palette open state + reactive theme owner

Assisted-by: Claude Fable 5"
```

---

### Task 3: `ui/commands.ts` — the command registry

**Files:**
- Create: `web/src/ui/commands.ts`
- Test: `web/src/ui/commands.test.tsx`

**Interfaces:**
- Consumes: `useReviewStore`, `useActiveSession`, `useIsPaused` from `../data/reviewStore`; `openImportPicker`, `exportLoadedDocument` from `../shell/ImportExport`; `useUiStore` from `./uiStore`; bindings from `./shortcuts`; `useLocation` from `wouter`; icons from `@untitledui/icons` (verified to exist: `Dataflow03`, `Grid01`, `Monitor01`, `Upload01`, `Download01`, `Moon01`, `Sun`, `PauseCircle`, `Play`, `Clock`).
- Produces:
  - `type CommandSection = "Navigation" | "Actions" | "Live window"`
  - `interface Command { id: string; label: string; section: CommandSection; sublabel?: string; icon: FC<{ className?: string }>; binding?: Binding; enabled: boolean; checked?: boolean; run(): void }`
  - `useCommands(): Command[]` — memoized; MUST be called under wouter's `<Router>`.
  - `LIVE_WINDOW_PRESETS` moves here from `AppBar.tsx` (exported: `{ id: "5m" | "15m" | "1h"; label: string; ms: number }[]`) so presets exist once. AppBar re-imports it in Task 7.

Registry contents (spec §Registry): Navigation = Topology (`/`), Hosts (`/hosts`), one row per host (`sublabel` = `board · slot N` pieces that exist), one row per element (`/topology/:elementId`) — all chord-less. Actions = Import… (`IMPORT_BINDING`), Export (`EXPORT_BINDING`, `enabled: hasData`), theme toggle (`THEME_BINDING`, label from current theme), Pause/Resume (`PAUSE_BINDING`, live only). Live window = presets, live only, active one `checked`. Live-only rows are OMITTED outside live mode; Export is the one disabled-not-hidden row.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/ui/commands.test.tsx
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { useUiStore } from "./uiStore";
import { useCommands } from "./commands";

// Minimal session shape the registry reads: lab.hosts (id/board/slot),
// elements (id), plus store mode/windowMs. Matches NormalizedSession's
// fields used by OverviewPage (hosts/elements) — see data/reviewStore.
const SESSION = {
  id: "s1",
  label: null,
  note: null,
  startMs: 0,
  endMs: 60_000,
  meta: { interval: 5 },
  lab: { hosts: [{ id: "test1", element: "rack", board: "qemu-x86", slot: 1 }] },
  elements: [{ id: "rack", type: "physical", description: null, hostIds: ["test1"] }],
  elementIds: new Set(["rack"]),
  charts: [],
  events: [],
  logEvents: [],
};

function seedStore(mode: "live" | null): void {
  useReviewStore.setState({
    // biome-ignore lint/suspicious/noExplicitAny: test seeds the minimal shape the registry reads
    sessions: [SESSION as any],
    activeSessionId: "s1",
    rawMonitorSessions: {} as never,
    mode,
    windowMs: 900_000,
    range: null,
  });
}

afterEach(() => {
  useReviewStore.setState({
    sessions: [],
    activeSessionId: null,
    rawMonitorSessions: null,
    mode: null,
    range: null,
    windowMs: 900_000,
  });
  useUiStore.setState({ paletteOpen: false, theme: "light" });
  window.location.hash = "";
});

describe("useCommands — review/import mode", () => {
  it("has navigation rows for views, hosts, and elements — all chord-less", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    const nav = result.current.filter((c) => c.section === "Navigation");
    const ids = nav.map((c) => c.id);
    expect(ids).toContain("nav-topology");
    expect(ids).toContain("nav-hosts");
    expect(ids).toContain("nav-host-test1");
    expect(ids).toContain("nav-element-rack");
    for (const c of nav) expect(c.binding).toBeUndefined();
    const host = nav.find((c) => c.id === "nav-host-test1");
    expect(host?.sublabel).toBe("qemu-x86 · slot 1");
  });

  it("omits live-only rows outside live mode but keeps Export (enabled with data)", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    const ids = result.current.map((c) => c.id);
    expect(ids).not.toContain("action-pause");
    expect(ids.filter((id) => id.startsWith("window-"))).toEqual([]);
    const exp = result.current.find((c) => c.id === "action-export");
    expect(exp?.enabled).toBe(true);
    expect(exp?.binding).toEqual({ key: "s", mod: true });
  });

  it("labels the theme toggle from the current theme", () => {
    seedStore(null);
    useUiStore.setState({ theme: "light" });
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-theme")?.label).toBe("Switch to dark mode");
    useUiStore.setState({ theme: "dark" });
    const { result: r2 } = renderHook(() => useCommands());
    expect(r2.current.find((c) => c.id === "action-theme")?.label).toBe("Switch to light mode");
  });

  it("disables Export with no data loaded", () => {
    useReviewStore.setState({ rawMonitorSessions: null });
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-export")?.enabled).toBe(false);
  });
});

describe("useCommands — live mode", () => {
  it("adds Pause and check-marks the active window preset", () => {
    seedStore("live");
    const { result } = renderHook(() => useCommands());
    const pause = result.current.find((c) => c.id === "action-pause");
    expect(pause?.binding).toEqual({ key: ".", mod: true });
    const windows = result.current.filter((c) => c.section === "Live window");
    expect(windows.map((c) => c.id)).toEqual(["window-5m", "window-15m", "window-1h"]);
    expect(windows.find((c) => c.id === "window-15m")?.checked).toBe(true);
    expect(windows.find((c) => c.id === "window-5m")?.checked).toBe(false);
  });

  it("running a navigation command changes the hash route", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    result.current.find((c) => c.id === "nav-host-test1")?.run();
    expect(window.location.hash).toBe("#/host/test1");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/commands.test.tsx`
Expected: FAIL — `Cannot find module './commands'`.

- [ ] **Step 3: Write the implementation**

Note on routing: `useLocation` from `wouter` returns `[location, navigate]`. The registry hook is mounted under the app's `<Router hook={useHashLocation}>` (Task 5's CommandLayer); in tests it falls back to wouter's default browser hook, and hash-style paths still land in `window.location.hash` because tests run the default router with `navigate("/host/test1")` — assert via the hash as above after wiring `useHashLocation`? No: keep the test deterministic by having `useCommands` use `useHashLocation`'s navigate directly. Implement with `import { useHashLocation } from "wouter/use-hash-location"` — the SAME hook App.tsx hands to `<Router>` — so navigation behaves identically inside the app and in bare renderHook tests.

```ts
// web/src/ui/commands.ts
// The command registry (spec §Registry): ONE derivation feeding three
// consumers — palette rows (CommandMenu), bound-chord handlers
// (useGlobalShortcuts), and every visible keycap hint. Navigation rows are
// deliberately chord-less: browsers reserve ⌘T/⌘N/⌘W/⌘⇧T uninterceptably
// and macOS owns ⌘H (spec decision 4), so views ride the palette + tabs.
import {
  Dataflow03,
  Download01,
  Grid01,
  Monitor01,
  Moon01,
  PauseCircle,
  Play,
  Sun,
  Upload01,
  Clock,
} from "@untitledui/icons";
import type { FC } from "react";
import { useMemo } from "react";
import { useHashLocation } from "wouter/use-hash-location";

import { useIsPaused, useReviewStore, useActiveSession } from "../data/reviewStore";
import { exportLoadedDocument, openImportPicker } from "../shell/ImportExport";
import {
  type Binding,
  EXPORT_BINDING,
  IMPORT_BINDING,
  PAUSE_BINDING,
  THEME_BINDING,
} from "./shortcuts";
import { useUiStore } from "./uiStore";

export type CommandSection = "Navigation" | "Actions" | "Live window";

export interface Command {
  id: string;
  label: string;
  section: CommandSection;
  /** Secondary text (host board · slot). */
  sublabel?: string;
  icon: FC<{ className?: string }>;
  /** Absent on navigation rows (decision 4) and preset rows. */
  binding?: Binding;
  /** false renders a disabled row (Export without data) — still listed. */
  enabled: boolean;
  /** Live-window rows: the active preset. */
  checked?: boolean;
  run: () => void;
}

// The follow-window presets, moved here from AppBar (Task 7 re-imports) so
// the palette rows and the AppBar ButtonGroup share one definition.
export const LIVE_WINDOW_PRESETS = [
  { id: "5m", label: "5m", ms: 300_000 },
  { id: "15m", label: "15m", ms: 900_000 },
  { id: "1h", label: "1h", ms: 3_600_000 },
] as const;

export function useCommands(): Command[] {
  const [, navigate] = useHashLocation();
  const session = useActiveSession();
  const mode = useReviewStore((s) => s.mode);
  const windowMs = useReviewStore((s) => s.windowMs);
  const hasData = useReviewStore((s) => s.rawMonitorSessions !== null);
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const setWindow = useReviewStore((s) => s.actions.setWindow);
  const paused = useIsPaused();
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.actions.toggleTheme);

  return useMemo(() => {
    const commands: Command[] = [
      {
        id: "nav-topology",
        label: "Topology",
        section: "Navigation",
        icon: Dataflow03,
        enabled: true,
        run: () => navigate("/"),
      },
      {
        id: "nav-hosts",
        label: "Hosts",
        section: "Navigation",
        icon: Grid01,
        enabled: true,
        run: () => navigate("/hosts"),
      },
    ];
    for (const host of session?.lab.hosts ?? []) {
      const pieces = [host.board, host.slot != null ? `slot ${host.slot}` : null].filter(
        (p): p is string => p != null,
      );
      commands.push({
        id: `nav-host-${host.id}`,
        label: host.id,
        section: "Navigation",
        sublabel: pieces.length > 0 ? pieces.join(" · ") : undefined,
        icon: Monitor01,
        enabled: true,
        run: () => navigate(`/host/${host.id}`),
      });
    }
    for (const el of session?.elements ?? []) {
      commands.push({
        id: `nav-element-${el.id}`,
        label: el.id,
        section: "Navigation",
        sublabel: "element",
        icon: Dataflow03,
        enabled: true,
        run: () => navigate(`/topology/${el.id}`),
      });
    }
    commands.push(
      {
        id: "action-import",
        label: "Import…",
        section: "Actions",
        icon: Upload01,
        binding: IMPORT_BINDING,
        enabled: true,
        run: openImportPicker,
      },
      {
        id: "action-export",
        label: "Export",
        section: "Actions",
        icon: Download01,
        binding: EXPORT_BINDING,
        enabled: hasData,
        run: exportLoadedDocument,
      },
      {
        id: "action-theme",
        label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
        section: "Actions",
        icon: theme === "dark" ? Sun : Moon01,
        binding: THEME_BINDING,
        enabled: true,
        run: toggleTheme,
      },
    );
    if (mode === "live") {
      commands.push({
        id: "action-pause",
        label: paused ? "Resume" : "Pause",
        section: "Actions",
        icon: paused ? Play : PauseCircle,
        binding: PAUSE_BINDING,
        enabled: true,
        run: togglePause,
      });
      for (const preset of LIVE_WINDOW_PRESETS) {
        commands.push({
          id: `window-${preset.id}`,
          label: `Follow ${preset.label}`,
          section: "Live window",
          icon: Clock,
          enabled: true,
          checked: windowMs === preset.ms,
          run: () => setWindow(preset.ms),
        });
      }
    }
    return commands;
  }, [navigate, session, mode, windowMs, hasData, paused, theme, togglePause, setWindow, toggleTheme]);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/commands.test.tsx`
Expected: PASS. If the `SESSION as any` seed trips on a missing field read by `useActiveSession`, extend the seed object — do NOT loosen the registry.

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/commands.ts web/src/ui/commands.test.tsx
git commit -m "feat(monitor-web): command registry — one derivation for palette, chords, and hints

Assisted-by: Claude Fable 5"
```

---

### Task 4: `ui/searchFocus.ts` + `ui/useGlobalShortcuts.ts`

**Files:**
- Create: `web/src/ui/searchFocus.ts`
- Create: `web/src/ui/useGlobalShortcuts.ts`
- Test: `web/src/ui/useglobalshortcuts.test.tsx`

**Interfaces:**
- Consumes: `Command` from `./commands`; `matchesBinding`, `shouldSuppressSlash`, `PALETTE_BINDING`, `SEARCH_BINDING` from `./shortcuts`; `useUiStore` from `./uiStore`.
- Produces:
  - `registerSearchInput(el: HTMLInputElement | null): void` and `focusSearchInput(): boolean` (false when nothing registered/connected) in `searchFocus.ts`
  - `useGlobalShortcuts(commands: Command[]): void` in `useGlobalShortcuts.ts`

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/ui/useglobalshortcuts.test.tsx
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Command } from "./commands";
import { registerSearchInput } from "./searchFocus";
import { useGlobalShortcuts } from "./useGlobalShortcuts";
import { useUiStore } from "./uiStore";

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
  useUiStore.setState({ paletteOpen: false, theme: "light" });
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/useglobalshortcuts.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write the implementations**

```ts
// web/src/ui/searchFocus.ts
// Module-level focus registry (spec §Global shortcuts): SeriesPanel
// registers its real <input> on mount; "/" asks here first. Same
// context-free registration pattern as ImportExport's picker.
let target: HTMLInputElement | null = null;

export function registerSearchInput(el: HTMLInputElement | null): void {
  target = el;
}

/** Focus the registered search input. False = nothing usable registered
 * (caller falls back to opening the palette — the palette IS a search). */
export function focusSearchInput(): boolean {
  if (target === null || !target.isConnected) return false;
  target.focus();
  return true;
}
```

```ts
// web/src/ui/useGlobalShortcuts.ts
// The one document-level keydown listener (spec §Global shortcuts). Chords
// fire from anywhere — they never type characters — and preventDefault on
// match is load-bearing: it is what keeps ⌘S from ALSO opening the
// browser's save dialog and ⌘L from focusing the address bar. The bare
// "/" is the only guarded key (shouldSuppressSlash).
import { useEffect } from "react";

import type { Command } from "./commands";
import {
  matchesBinding,
  PALETTE_BINDING,
  SEARCH_BINDING,
  shouldSuppressSlash,
} from "./shortcuts";
import { focusSearchInput } from "./searchFocus";
import { useUiStore } from "./uiStore";

export function useGlobalShortcuts(commands: Command[]): void {
  const actions = useUiStore((s) => s.actions);
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (matchesBinding(e, PALETTE_BINDING)) {
        e.preventDefault();
        actions.togglePalette();
        return;
      }
      if (matchesBinding(e, SEARCH_BINDING)) {
        if (shouldSuppressSlash(e.target, useUiStore.getState().paletteOpen)) return;
        e.preventDefault();
        if (!focusSearchInput()) actions.openPalette();
        return;
      }
      for (const command of commands) {
        if (command.binding && command.enabled && matchesBinding(e, command.binding)) {
          e.preventDefault();
          command.run();
          return;
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [commands, actions]);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/useglobalshortcuts.test.tsx`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/searchFocus.ts web/src/ui/useGlobalShortcuts.ts web/src/ui/useglobalshortcuts.test.tsx
git commit -m "feat(monitor-web): global shortcut layer — chords + guarded bare slash

Assisted-by: Claude Fable 5"
```

---

### Task 5: `ui/Kbd.tsx`, `ui/CommandMenu.tsx`, `ui/CommandLayer.tsx` + App wiring

**Files:**
- Create: `web/src/ui/Kbd.tsx`, `web/src/ui/CommandMenu.tsx`, `web/src/ui/CommandLayer.tsx`
- Modify: `web/src/App.tsx` (mount `<CommandLayer />` inside the Router branch)
- Test: `web/src/ui/commandmenu.test.tsx`

**Interfaces:**
- Consumes: `useCommands`, `Command`, `CommandSection` (Task 3); `useGlobalShortcuts` (Task 4); `useUiStore` (Task 2); `formatBinding` (Task 1); react-aria-components `ModalOverlay, Modal, Dialog, Autocomplete, useFilter, SearchField, Input, Menu, MenuItem, MenuSection, Header`; `Check`, `SearchLg` from `@untitledui/icons`.
- Produces:
  - `Kbd({ children }: { children: string })` — keycap chip
  - `CommandMenu({ commands }: { commands: Command[] })` — reads/writes `useUiStore` open state
  - `CommandLayer()` — `const commands = useCommands(); useGlobalShortcuts(commands); return <CommandMenu commands={commands} />;`
  - Testids: `command-menu` (the dialog), `command-input`, `command-item-<id>`, `command-empty`.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/ui/commandmenu.test.tsx
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Command } from "./commands";
import { CommandMenu } from "./CommandMenu";
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
  cmd({ id: "nav-host-test1", label: "test1", section: "Navigation", sublabel: "qemu-x86 · slot 1" }),
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
    expect(screen.getByTestId("command-item-nav-host-test1").textContent).toContain("qemu-x86 · slot 1");
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
    render(
      <CommandMenu commands={[cmd({ id: "action-import", label: "Import…", run })]} />,
    );
    await user.click(await screen.findByTestId("command-item-action-import"));
    expect(run).toHaveBeenCalledOnce();
    expect(useUiStore.getState().paletteOpen).toBe(false);
  });

  it("a disabled row is aria-disabled and does not run", async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    useUiStore.setState({ paletteOpen: true });
    render(
      <CommandMenu commands={[cmd({ id: "action-export", label: "Export", enabled: false, run })]} />,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/commandmenu.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write `Kbd.tsx`**

```tsx
// web/src/ui/Kbd.tsx
// The keycap chip. Class list mirrors the vendored InputBase shortcut
// keycap (components/base/input/input.tsx — rounded / px-1 py-px / text-xs
// font-medium text-quaternary / inset ring-secondary) so authored hint
// sites are pixel-identical to the vendored one; duplicated here because
// the vendored file cannot be edited to export it.
export function Kbd({ children }: { children: string }) {
  return (
    <kbd
      aria-hidden
      className="rounded px-1 py-px font-sans text-xs font-medium text-quaternary ring-1
        ring-secondary select-none ring-inset"
    >
      {children}
    </kbd>
  );
}
```

- [ ] **Step 4: Write `CommandMenu.tsx`**

```tsx
// web/src/ui/CommandMenu.tsx
// The in-house command palette (spec §Palette). Untitled UI's own command
// menu is PRO-tier, so this is the same react-aria stack it uses —
// ModalOverlay + Dialog + Autocomplete + Menu — styled with the vendored
// theme tokens only. Enter/click runs and closes; Esc closes; the filter
// is a plain case/diacritic-insensitive contains over label + sublabel.
import { Check, SearchLg } from "@untitledui/icons";
import {
  Autocomplete,
  Dialog,
  Header,
  Input,
  Menu,
  MenuItem,
  MenuSection,
  Modal,
  ModalOverlay,
  SearchField,
  useFilter,
} from "react-aria-components";

import type { Command, CommandSection } from "./commands";
import { formatBinding } from "./shortcuts";
import { Kbd } from "./Kbd";
import { useUiStore } from "./uiStore";

const SECTION_ORDER: CommandSection[] = ["Navigation", "Actions", "Live window"];

export function CommandMenu({ commands }: { commands: Command[] }) {
  const open = useUiStore((s) => s.paletteOpen);
  const { openPalette, closePalette } = useUiStore((s) => s.actions);
  const { contains } = useFilter({ sensitivity: "base" });

  const sections = SECTION_ORDER.map((section) => ({
    section,
    items: commands.filter((c) => c.section === section),
  })).filter((s) => s.items.length > 0);

  const byId = new Map(commands.map((c) => [c.id, c]));

  return (
    <ModalOverlay
      isOpen={open}
      onOpenChange={(next) => (next ? openPalette() : closePalette())}
      isDismissable
      className="fixed inset-0 z-50 flex justify-center bg-black/45 pt-[20vh]
        dark:bg-black/60"
    >
      <Modal className="w-full max-w-140 px-4">
        <Dialog
          aria-label="Command menu"
          data-testid="command-menu"
          className="overflow-hidden rounded-xl bg-primary shadow-2xl ring-1 ring-secondary_alt
            outline-hidden"
        >
          <Autocomplete
            filter={(textValue, inputValue) => contains(textValue, inputValue)}
          >
            <SearchField aria-label="Search commands" autoFocus className="group flex">
              <div className="flex w-full items-center gap-2.5 border-b border-secondary px-4">
                <SearchLg aria-hidden className="size-4 shrink-0 text-fg-quaternary" />
                <Input
                  data-testid="command-input"
                  placeholder="Type a command or search hosts…"
                  className="h-12 w-full bg-transparent text-md text-primary outline-hidden
                    placeholder:text-placeholder"
                />
              </div>
            </SearchField>
            <Menu
              className="max-h-80 overflow-y-auto py-1.5 outline-hidden"
              renderEmptyState={() => (
                <div data-testid="command-empty" className="px-4 py-6 text-sm text-quaternary">
                  No results
                </div>
              )}
              onAction={(key) => {
                const command = byId.get(String(key));
                if (!command || !command.enabled) return;
                closePalette();
                command.run();
              }}
            >
              {sections.map(({ section, items }) => (
                <MenuSection key={section} className="pb-1">
                  <Header className="px-4 pt-2 pb-1 text-xs font-medium text-quaternary">
                    {section}
                  </Header>
                  {items.map((command) => {
                    const Icon = command.icon;
                    return (
                      <MenuItem
                        key={command.id}
                        id={command.id}
                        textValue={`${command.label} ${command.sublabel ?? ""}`}
                        isDisabled={!command.enabled}
                        data-testid={`command-item-${command.id}`}
                        className="group mx-1.5 flex cursor-pointer items-center rounded-md px-2.5
                          py-2 outline-hidden transition duration-100 ease-linear
                          data-focused:bg-primary_hover data-disabled:cursor-not-allowed
                          data-disabled:opacity-50"
                      >
                        <Icon
                          aria-hidden
                          className="mr-2.5 size-4 shrink-0 stroke-[2.25px] text-fg-quaternary"
                        />
                        <span className="truncate text-sm font-medium text-secondary">
                          {command.label}
                        </span>
                        {command.sublabel && (
                          <span className="ml-2 truncate text-xs text-quaternary">
                            {command.sublabel}
                          </span>
                        )}
                        <span className="ml-auto flex items-center pl-3">
                          {command.checked && (
                            <Check
                              aria-hidden
                              className="size-4 stroke-[2.25px] text-fg-brand-primary"
                            />
                          )}
                          {command.binding && <Kbd>{formatBinding(command.binding)}</Kbd>}
                        </span>
                      </MenuItem>
                    );
                  })}
                </MenuSection>
              ))}
            </Menu>
          </Autocomplete>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
```

- [ ] **Step 5: Write `CommandLayer.tsx` and mount it in `App.tsx`**

```tsx
// web/src/ui/CommandLayer.tsx
// Mounted INSIDE App's <Router> (spec §Wiring): the registry needs
// navigation, so shortcuts + palette exist only once data is loaded —
// the EmptyState import screen keeps its own explicit buttons.
import { useCommands } from "./commands";
import { CommandMenu } from "./CommandMenu";
import { useGlobalShortcuts } from "./useGlobalShortcuts";

export function CommandLayer() {
  const commands = useCommands();
  useGlobalShortcuts(commands);
  return <CommandMenu commands={commands} />;
}
```

In `web/src/App.tsx`, add the import and mount it directly under `<ReviewBar />` (inside `<Router hook={useHashLocation}>`):

```tsx
import { CommandLayer } from "./ui/CommandLayer";
// ... inside the hasData branch:
          <Router hook={useHashLocation}>
            <CommandLayer />
            <ReviewBar />
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/commandmenu.test.tsx src/__tests__/shell.test.tsx`
Expected: commandmenu PASS; shell.test.tsx still PASS (CommandLayer must not disturb existing shell behavior). If react-aria logs a controlled-Autocomplete warning, wrap the input value via `Autocomplete`'s uncontrolled default — do not suppress the warning.

- [ ] **Step 7: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/Kbd.tsx web/src/ui/CommandMenu.tsx web/src/ui/CommandLayer.tsx web/src/ui/commandmenu.test.tsx web/src/App.tsx
git commit -m "feat(monitor-web): command palette — ModalOverlay+Autocomplete+Menu on theme tokens

Assisted-by: Claude Fable 5"
```

---

### Task 6: `ui/SearchTrigger.tsx` + AppBar left cluster

**Files:**
- Create: `web/src/ui/SearchTrigger.tsx`
- Test: `web/src/ui/searchtrigger.test.tsx`
- Modify: `web/src/shell/AppBar.tsx` (left cluster only — brand + trigger)

**Interfaces:**
- Consumes: `useUiStore` (Task 2), `Kbd` (Task 5), `SearchLg` from `@untitledui/icons`.
- Produces: `SearchTrigger()` — testid `search-trigger`; clicking calls `useUiStore.getState().actions.openPalette()`. Keycap text is the literal `"/"` (decision 4 — every search surface advertises `/`).

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/ui/searchtrigger.test.tsx
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { SearchTrigger } from "./SearchTrigger";
import { useUiStore } from "./uiStore";

afterEach(() => {
  cleanup();
  useUiStore.setState({ paletteOpen: false, theme: "light" });
});

describe("SearchTrigger", () => {
  it("renders the input-lookalike with placeholder text and the / keycap", () => {
    render(<SearchTrigger />);
    const trigger = screen.getByTestId("search-trigger");
    expect(trigger.textContent).toContain("Search…");
    expect(trigger.textContent).toContain("/");
  });

  it("opens the palette on click", async () => {
    const user = userEvent.setup();
    render(<SearchTrigger />);
    await user.click(screen.getByTestId("search-trigger"));
    expect(useUiStore.getState().paletteOpen).toBe(true);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/searchtrigger.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
// web/src/ui/SearchTrigger.tsx
// The AppBar's palette trigger (spec decision 7/9): a <button> DRESSED as
// the vendored sm input (wrapper classes mirror InputBase's AriaGroup —
// rounded-lg bg-primary shadow-xs inset ring-primary) with the "/" keycap.
// It is not a real input on purpose: focusing it must not start text
// entry, it opens the palette, which owns the real filter field.
import { SearchLg } from "@untitledui/icons";

import { Kbd } from "./Kbd";
import { useUiStore } from "./uiStore";

export function SearchTrigger() {
  const openPalette = useUiStore((s) => s.actions.openPalette);
  return (
    <button
      type="button"
      data-testid="search-trigger"
      aria-label="Search (press / or the command menu)"
      onClick={openPalette}
      className="flex w-50 cursor-pointer items-center gap-2 rounded-lg bg-primary py-1 pr-1.5
        pl-2.5 text-sm text-quaternary shadow-xs ring-1 ring-primary outline-focus-ring
        transition duration-100 ease-linear ring-inset hover:bg-primary_hover
        focus-visible:outline-2 focus-visible:-outline-offset-2"
    >
      <SearchLg aria-hidden className="size-4 shrink-0 text-fg-quaternary" />
      <span className="grow text-left text-placeholder">Search…</span>
      <Kbd>/</Kbd>
    </button>
  );
}
```

- [ ] **Step 4: Place it in the AppBar left cluster**

In `web/src/shell/AppBar.tsx`, the left cluster becomes brand + trigger (Task 7 turns Pause into a right-cluster glyph and moves the live-window ButtonGroup to SubjectPage — in THIS task only insert the trigger after the brand `<div>`):

```tsx
import { SearchTrigger } from "../ui/SearchTrigger";
// inside the left <div className="flex items-center gap-3">, directly after
// the brand div:
        <SearchTrigger />
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/searchtrigger.test.tsx src/__tests__/shell.test.tsx`
Expected: PASS both.

- [ ] **Step 6: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/SearchTrigger.tsx web/src/ui/searchtrigger.test.tsx web/src/shell/AppBar.tsx
git commit -m "feat(monitor-web): search-style palette trigger, left-anchored beside the brand

Assisted-by: Claude Fable 5"
```

---

### Task 7: AppBar right cluster — glyphs, grouping, status removal, presets → SubjectPage

**Files:**
- Modify: `web/src/shell/AppBar.tsx`
- Modify: `web/src/pages/SubjectPage.tsx` (receives the live-window ButtonGroup — spec decision 10)
- Modify: `web/src/shell/ImportExport.tsx` (delete `ExportButton`; keep `exportLoadedDocument`, `openImportPicker`, `ImportProvider`, `useImportFile`)
- Modify: `web/src/__tests__/shell.test.tsx`

**Interfaces:**
- Consumes: `ButtonUtility` (vendored — props `icon`, `color`, `size`, `tooltip`, plus DOM props); icons `PauseCircle`, `Play`, `Download01`; `LIVE_WINDOW_PRESETS` from `ui/commands` (Task 3 — delete AppBar's local copy; SubjectPage imports it now); `useUiStore` theme (replaces AppBar's `useState<Theme>`); `exportLoadedDocument` from `./ImportExport`.
- Produces: AppBar layout contract for later tasks/e2e — left: `brand`, `search-trigger`; right, in order: `pause-toggle` glyph (live only, `aria-label` "Pause"/"Resume") · `export-button` glyph (live only, `aria-label` "Export") · `overflow-menu`. NO `status-text`, NO `status-dot`, NO `live-window`, NO `events-button` in the AppBar. The `live-window` ButtonGroup (testids `live-window`, `live-window-5m/15m/1h`) AND the Events button (`events-button`/`events-count` + the `EventsPanel` slideout) render in SubjectPage's title row (decisions 10/11) — presets live-only, Events gated on `session.events.length > 0` exactly as before. The pause glyph is now the AppBar's "is live" signal for tests.

- [ ] **Step 1: Update shell.test.tsx expectations first (failing tests)**

In `web/src/__tests__/shell.test.tsx`:

Replace `importMinimal`'s wait (the `status-text` testid is being deleted):

```tsx
async function importMinimal() {
  const file = new File([MINIMAL], "minimal.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  // status-text is gone (spec decision 9) — loaded-historical chrome is the
  // ReviewBar, which renders only once a session with bounds exists.
  await waitFor(() => expect(screen.getByTestId("review-bar")).toBeTruthy());
}
```

Replace the boot test's status assertion and add the no-status contract test:

```tsx
  it("boots to the empty review state with no backend fetches", () => {
    render(<App />);
    expect(screen.getByTestId("empty-review")).toBeTruthy();
    expect(screen.queryByTestId("status-text")).toBeNull();
  });

  it("renders no status text or dot in any mode (spec decision 9)", async () => {
    render(<App />);
    expect(screen.queryByTestId("status-text")).toBeNull();
    expect(screen.queryByTestId("status-dot")).toBeNull();
    await importMinimal();
    expect(screen.queryByTestId("status-text")).toBeNull();
    expect(screen.queryByTestId("status-dot")).toBeNull();
  });
```

In the import-error test and the data-warnings test, replace both
`await waitFor(() => expect(screen.getByTestId("status-text").textContent).toBe("Historical"));`
lines with
`await waitFor(() => expect(screen.getByTestId("review-bar")).toBeTruthy());`
and the import-error test's mid-assertion `expect(screen.getByTestId("status-text").textContent).toBe("Historical");` with `expect(screen.getByTestId("review-bar")).toBeTruthy();`.

- [ ] **Step 2: Run tests to verify the new expectations fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/__tests__/shell.test.tsx`
Expected: the two edited/new tests FAIL (status-text still renders); others pass.

- [ ] **Step 3: Rework AppBar.tsx**

Full replacement for `web/src/shell/AppBar.tsx`:

```tsx
// Global chrome (UX spec §7, reworked per spec 2026-07-17 decision 9):
// left = brand + the permanent search trigger (never moves with mode);
// right = every control, grouped: live-window presets · pause glyph ·
// export glyph (all live-only) · events · ⋮ menu. The status text/dot are
// GONE — "Historical"/"No data" were redundant (ReviewBar badge,
// EmptyState) and live connection loss now renders as the Reconnecting
// banner (ReconnectingBanner.tsx), which replaced the status cluster as
// the `connection` state's one render site.
import { Download01, DotsVertical, Moon01, PauseCircle, Play, Sun, Command } from "@untitledui/icons";

import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useIsPaused, useReviewStore } from "../data/reviewStore";
import { SearchTrigger } from "../ui/SearchTrigger";
import { useUiStore } from "../ui/uiStore";
import { exportLoadedDocument, openImportPicker } from "./ImportExport";

export function AppBar() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const mode = useReviewStore((s) => s.mode);
  const paused = useIsPaused();
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const theme = useUiStore((s) => s.theme);
  const { toggleTheme, openPalette } = useUiStore((s) => s.actions);

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between gap-3 border-b border-secondary px-4"
    >
      <div className="flex items-center gap-3">
        <div data-testid="brand" className="flex items-center gap-2 text-sm font-semibold">
          <span aria-hidden className="text-brand-500">
            ⬡
          </span>
          otto monitor
        </div>
        <SearchTrigger />
      </div>
      <div className="flex items-center gap-2">
        {mode === "live" && (
          <ButtonUtility
            aria-label={paused ? "Resume" : "Pause"}
            tooltip={paused ? "Resume" : "Pause"}
            data-testid="pause-toggle"
            icon={paused ? Play : PauseCircle}
            color="tertiary"
            size="sm"
            onClick={togglePause}
          />
        )}
        {mode === "live" && (
          <ButtonUtility
            aria-label="Export"
            tooltip="Export"
            data-testid="export-button"
            icon={Download01}
            color="tertiary"
            size="sm"
            isDisabled={!hasData}
            onClick={exportLoadedDocument}
          />
        )}
        <Dropdown.Root>
          <ButtonUtility
            aria-label="More actions"
            data-testid="overflow-menu"
            icon={DotsVertical}
            color="tertiary"
          />
          <Dropdown.Popover>
            <Dropdown.Menu>
              {/* Task 8 recomposes this menu icon-advanced-style; this task
                  only keeps behavior alive under the new trigger icon. */}
              <Dropdown.Item
                id="import"
                label="Import…"
                onAction={openImportPicker}
                data-testid="menu-import"
              />
              <Dropdown.Item
                id="export"
                label="Export"
                onAction={exportLoadedDocument}
                isDisabled={!hasData}
                data-testid="menu-export"
              />
              <Dropdown.Item
                id="theme"
                label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                onAction={toggleTheme}
                data-testid="menu-theme"
              />
              <Dropdown.Item
                id="shortcuts"
                label="Keyboard shortcuts…"
                onAction={openPalette}
                data-testid="menu-shortcuts"
              />
            </Dropdown.Menu>
          </Dropdown.Popover>
        </Dropdown.Root>
      </div>
    </header>
  );
}
```

Notes for the implementer:
- `ButtonUtility` — check its prop for disabled state in the vendored file (it wraps react-aria; the prop is `isDisabled` if it renders `AriaButton`, `disabled` if plain DOM). Read `web/src/components/base/buttons/button-utility.tsx` and use whichever it actually forwards; the e2e contract is only that the button is non-interactive without data.
- The Export menu item is now ALWAYS present (decision 8) — the old `mode !== "live"` conditional is deleted.
- The unused `Moon01`/`Sun`/`Command` imports belong to Task 8's recomposition; if Biome flags them in this task, add them in Task 8 instead.
- Delete the `LIVE_WINDOW_PRESETS` local constant + `loadTheme`/`saveTheme` imports/state — theme now reads `useUiStore` (menu label test must keep passing since `toggleTheme` persists identically).

In `web/src/shell/ImportExport.tsx`: delete the entire `ExportButton` function (lines 35–58) and its doc comment. `exportLoadedDocument` and everything else stays.

- [ ] **Step 4: Move the live-window presets + Events button into SubjectPage (spec decisions 10/11)**

In `web/src/pages/SubjectPage.tsx`, the title row gains the per-host chrome
the AppBar just lost — right-aligned via `ml-auto`: the live-window
ButtonGroup (live mode only; the presets only affect this page's chart
windows) and the Events button with its EventsPanel slideout (same
`session.events.length > 0` gate as before — the panel still lists
session-wide events, only the entry point moved). Change the `<h1>` block
to:

```tsx
      <h1 data-testid="subject-title" className="flex items-center gap-2 text-lg font-semibold">
        {id}
        <span className="text-sm font-normal text-quaternary">
          {kind}
          {host?.board ? ` · ${host.board}` : ""}
          {host?.slot != null ? ` · slot ${host.slot}` : ""}
          {host?.hop ? ` · via ${host.hop}` : ""}
        </span>
        <span className="ml-auto flex items-center gap-2 text-sm font-normal">
          {mode === "live" && (
            <ButtonGroup
              aria-label="Live window"
              data-testid="live-window"
              size="sm"
              selectedKeys={new Set([selectedWindowId(windowMs)])}
              disallowEmptySelection
              onSelectionChange={(keys) => {
                const selected = [...keys][0];
                const preset = LIVE_WINDOW_PRESETS.find((p) => p.id === selected);
                if (preset) setWindow(preset.ms);
              }}
            >
              {LIVE_WINDOW_PRESETS.map((p) => (
                <ButtonGroupItem key={p.id} id={p.id} data-testid={`live-window-${p.id}`}>
                  {p.label}
                </ButtonGroupItem>
              ))}
            </ButtonGroup>
          )}
          {session.events.length > 0 && (
            <button
              type="button"
              data-testid="events-button"
              onClick={() => setEventsOpen(true)}
              className="cursor-pointer rounded-md px-2 py-1 text-sm text-tertiary
                hover:bg-primary_hover"
            >
              Events{" "}
              <span data-testid="events-count" className="rounded-full bg-tertiary px-1.5 text-xs">
                {session.events.length}
              </span>
            </button>
          )}
        </span>
      </h1>
```

plus, anywhere in SubjectPage's returned JSX (sibling of the `<h1>`):

```tsx
      <EventsPanel isOpen={eventsOpen} onClose={() => setEventsOpen(false)} />
```

with these additions to SubjectPage:

```tsx
import { ButtonGroup, ButtonGroupItem } from "@/components/base/button-group/button-group";
import { EventsPanel } from "../shell/EventsPanel";
import { LIVE_WINDOW_PRESETS } from "../ui/commands";
// inside the component:
const [eventsOpen, setEventsOpen] = useState(false);

function selectedWindowId(windowMs: number): string {
  return LIVE_WINDOW_PRESETS.find((p) => p.ms === windowMs)?.id ?? "15m";
}
```

(SubjectPage already reads `mode`, `windowMs`, `setWindow`, and the active
`session` from the review store for its chart-window logic — reuse those
existing selectors; add whichever it does not already subscribe to. It
already imports `useState`.)

- [ ] **Step 5: Run the web test suite**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run`
Expected: PASS, including the two rewritten shell tests. Grep for leftovers: `grep -rn "ExportButton\|status-text\|status-dot" web/src --include='*.tsx' --include='*.ts'` → only test files asserting absence may match. `grep -rn "live-window" web/src` → SubjectPage (+ its tests) only, never AppBar.

- [ ] **Step 6: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/shell/AppBar.tsx web/src/pages/SubjectPage.tsx web/src/shell/ImportExport.tsx web/src/__tests__/shell.test.tsx
git commit -m "feat(monitor-web)!: AppBar rework — glyphs grouped right, status cluster removed, presets to subject page

Assisted-by: Claude Fable 5"
```

---

### Task 8: Overflow menu — icon-advanced composition

**Files:**
- Modify: `web/src/shell/AppBar.tsx` (menu body only)
- Modify: `web/src/__tests__/shell.test.tsx` (add composition assertions)

**Interfaces:**
- Consumes: `Dropdown.Section`/`Dropdown.Separator` (vendored exports), `Dropdown.Item`'s `icon`/`addon` props; `formatBinding`, `IMPORT_BINDING`, `EXPORT_BINDING`, `THEME_BINDING`, `PALETTE_BINDING` from `../ui/shortcuts`; icons `Upload01`, `Download01`, `Moon01`, `Sun`, `Command`.
- Produces: the final menu — Data section (Import…/Export), separator, Appearance (theme toggle), separator, Help (Keyboard shortcuts…). Testids `menu-import`/`menu-export`/`menu-theme`/`menu-shortcuts` unchanged.

- [ ] **Step 1: Add failing composition tests to shell.test.tsx**

```tsx
  it("overflow menu is icon-advanced: chord addons on every action row", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByTestId("overflow-menu"));
    // jsdom is non-mac -> Ctrl-form labels (shortcuts.ts formatBinding).
    expect((await screen.findByTestId("menu-import")).textContent).toContain("Ctrl I");
    expect(screen.getByTestId("menu-export").textContent).toContain("Ctrl S");
    expect(screen.getByTestId("menu-theme").textContent).toContain("Ctrl L");
    expect(screen.getByTestId("menu-shortcuts").textContent).toContain("Ctrl K");
  });

  it("Keyboard shortcuts… menu item opens the command palette", async () => {
    const user = userEvent.setup();
    render(<App />);
    await importMinimal();
    await user.click(screen.getByTestId("overflow-menu"));
    await user.click(await screen.findByTestId("menu-shortcuts"));
    expect(useUiStore.getState().paletteOpen).toBe(true);
  });
```

Add the import at the top of shell.test.tsx: `import { useUiStore } from "../ui/uiStore";` and reset it in `afterEach`: `useUiStore.setState({ paletteOpen: false, theme: "light" });`

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/__tests__/shell.test.tsx`
Expected: the two new tests FAIL (no addons yet); rest pass.

- [ ] **Step 3: Recompose the menu**

Replace the `<Dropdown.Menu>` body in `AppBar.tsx` with:

```tsx
            <Dropdown.Menu>
              <Dropdown.Section>
                <Dropdown.Item
                  id="import"
                  label="Import…"
                  icon={Upload01}
                  addon={formatBinding(IMPORT_BINDING)}
                  onAction={openImportPicker}
                  data-testid="menu-import"
                />
                <Dropdown.Item
                  id="export"
                  label="Export"
                  icon={Download01}
                  addon={formatBinding(EXPORT_BINDING)}
                  onAction={exportLoadedDocument}
                  isDisabled={!hasData}
                  data-testid="menu-export"
                />
              </Dropdown.Section>
              <Dropdown.Separator />
              <Dropdown.Section>
                <Dropdown.Item
                  id="theme"
                  label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                  icon={theme === "dark" ? Sun : Moon01}
                  addon={formatBinding(THEME_BINDING)}
                  onAction={toggleTheme}
                  data-testid="menu-theme"
                />
              </Dropdown.Section>
              <Dropdown.Separator />
              <Dropdown.Section>
                <Dropdown.Item
                  id="shortcuts"
                  label="Keyboard shortcuts…"
                  icon={Command}
                  addon={formatBinding(PALETTE_BINDING)}
                  onAction={openPalette}
                  data-testid="menu-shortcuts"
                />
              </Dropdown.Section>
            </Dropdown.Menu>
```

with the new imports: `import { formatBinding, EXPORT_BINDING, IMPORT_BINDING, PALETTE_BINDING, THEME_BINDING } from "../ui/shortcuts";` and the icon imports from Task 7's note.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/__tests__/shell.test.tsx`
Expected: PASS — including the pre-existing menu-import/menu-export/menu-theme behavior tests (testids and actions unchanged).

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/shell/AppBar.tsx web/src/__tests__/shell.test.tsx
git commit -m "feat(monitor-web): icon-advanced overflow menu — sections, icons, chord addons

Assisted-by: Claude Fable 5"
```

---

### Task 9: Reconnecting banner

**Files:**
- Create: `web/src/shell/ReconnectingBanner.tsx`
- Test: `web/src/shell/reconnectingbanner.test.tsx`
- Modify: `web/src/App.tsx` (mount under AppBar, OUTSIDE the hasData branch? No — see below)

**Interfaces:**
- Consumes: `useReviewStore` (`mode`, `connection`).
- Produces: `ReconnectingBanner()` — testid `reconnecting-banner`; renders `null` unless `mode === "live" && connection !== "live"`.

Placement: mount directly after `<AppBar />` in `App.tsx` (before the `hasData` conditional) — a live server that drops its stream mid-session must keep the banner even if a resync empties sessions; the component's own mode gate keeps it silent everywhere else.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/shell/reconnectingbanner.test.tsx
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { ReconnectingBanner } from "./ReconnectingBanner";

afterEach(() => {
  cleanup();
  useReviewStore.setState({ mode: null, connection: "connecting" });
});

describe("ReconnectingBanner", () => {
  it("renders only in live mode with a non-live connection", () => {
    useReviewStore.setState({ mode: "live", connection: "disconnected" });
    render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
  });

  it("disappears when the connection recovers", () => {
    useReviewStore.setState({ mode: "live", connection: "live" });
    render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });

  it("never renders outside live mode, whatever the connection says", () => {
    useReviewStore.setState({ mode: null, connection: "disconnected" });
    const { rerender } = render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
    useReviewStore.setState({ mode: "review", connection: "connecting" });
    rerender(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/shell/reconnectingbanner.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation and mount it**

```tsx
// web/src/shell/ReconnectingBanner.tsx
// The `connection` state's ONE render site (spec §Reconnecting banner).
// The AppBar status text/dot it replaces were deleted in the same spec —
// deleting them without this banner would have left stream.ts's
// connecting/disconnected states with no reader (the render-site rule:
// guard what you emit). Same pattern as App.tsx's import-error banner.
import { AlertTriangle } from "@untitledui/icons";

import { useReviewStore } from "../data/reviewStore";

export function ReconnectingBanner() {
  const mode = useReviewStore((s) => s.mode);
  const connection = useReviewStore((s) => s.connection);
  if (mode !== "live" || connection === "live") return null;
  return (
    <div
      data-testid="reconnecting-banner"
      className="flex items-center gap-2 border-b border-status-warn/30 bg-status-warn/10 px-4
        py-2 text-sm font-medium text-status-warn dark:bg-status-warn/15"
    >
      <AlertTriangle aria-hidden className="size-4 shrink-0" />
      Reconnecting…
    </div>
  );
}
```

In `web/src/App.tsx`:

```tsx
import { ReconnectingBanner } from "./shell/ReconnectingBanner";
// ... in the tree:
        <AppBar />
        <ReconnectingBanner />
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/shell/reconnectingbanner.test.tsx src/__tests__/shell.test.tsx`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/shell/ReconnectingBanner.tsx web/src/shell/reconnectingbanner.test.tsx web/src/App.tsx
git commit -m "feat(monitor-web): reconnecting banner — connection state's render site after status removal

Assisted-by: Claude Fable 5"
```

---

### Task 10: `ui/ViewSwitcher.tsx` — button-border tabs on both pages

**PREREQUISITE GATE:** verify `App.tsx` routes `/` → `TopologyPage` and `/hosts` → `OverviewPage` (the topology-default-view plan). If not, STOP this task and flag it.

**Files:**
- Create: `web/src/ui/ViewSwitcher.tsx`
- Test: `web/src/ui/viewswitcher.test.tsx`
- Modify: `web/src/pages/OverviewPage.tsx`, `web/src/topo/TopologyPage.tsx` (swap ButtonGroup → ViewSwitcher; drop now-unused ButtonGroup imports)

**Interfaces:**
- Consumes: vendored `Tabs`, `TabList`, `Tab` from `@/components/application/tabs/tabs`; `useHashLocation` from `wouter/use-hash-location`.
- Produces: `ViewSwitcher({ active }: { active: "topology" | "hosts" })` — testid `view-toggle`, role `tablist` with two `tab`s "Topology"/"Hosts". `active` is passed by the hosting page (each page knows what it is — no route parsing), satisfying "derived from the route, never stored".

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/ui/viewswitcher.test.tsx
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ViewSwitcher } from "./ViewSwitcher";

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

describe("ViewSwitcher", () => {
  it("renders button-border tabs with the active view selected", () => {
    render(<ViewSwitcher active="topology" />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Topology", "Hosts"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].getAttribute("aria-selected")).toBe("false");
  });

  it("selecting the other tab navigates the hash route", async () => {
    const user = userEvent.setup();
    render(<ViewSwitcher active="topology" />);
    await user.click(screen.getByRole("tab", { name: "Hosts" }));
    expect(window.location.hash).toBe("#/hosts");
  });

  it("navigates to / for topology", async () => {
    const user = userEvent.setup();
    window.location.hash = "#/hosts";
    render(<ViewSwitcher active="hosts" />);
    await user.click(screen.getByRole("tab", { name: "Topology" }));
    expect(window.location.hash).toBe("#/");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/ui/viewswitcher.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
// web/src/ui/ViewSwitcher.tsx
// The Topology|Hosts switcher (spec §View switcher): vendored button-border
// tabs. `active` comes from the HOSTING page, not internal state — the
// selectedWindowId lesson: a stored copy of a route-derived value drifts.
import { Tab, TabList, Tabs } from "@/components/application/tabs/tabs";
import { useHashLocation } from "wouter/use-hash-location";

const ROUTES = { topology: "/", hosts: "/hosts" } as const;

export function ViewSwitcher({ active }: { active: keyof typeof ROUTES }) {
  const [, navigate] = useHashLocation();
  return (
    <Tabs
      data-testid="view-toggle"
      selectedKey={active}
      onSelectionChange={(key) => {
        if (key !== active) navigate(ROUTES[key as keyof typeof ROUTES]);
      }}
    >
      <TabList aria-label="View" type="button-border" size="sm">
        <Tab id="topology">Topology</Tab>
        <Tab id="hosts">Hosts</Tab>
      </TabList>
    </Tabs>
  );
}
```

- [ ] **Step 4: Swap it into both pages**

`web/src/pages/OverviewPage.tsx` — replace the `ButtonGroup` block (the `<div className="flex items-center gap-3">…</div>` wrapping it) with:

```tsx
      <div className="flex items-center gap-3">
        <ViewSwitcher active="hosts" />
      </div>
```

deleting the `ButtonGroup, ButtonGroupItem` import and the `useLocation` navigate IF nothing else uses it (OverviewPage's `navigate` was only for the switcher — verify with grep before deleting), adding `import { ViewSwitcher } from "../ui/ViewSwitcher";`.

`web/src/topo/TopologyPage.tsx` — replace its `ButtonGroup` block (keeping the sibling `topo-breadcrumb` nav and `sources-toggle` button in the same flex row) with `<ViewSwitcher active="topology" />`, same import/cleanup treatment.

- [ ] **Step 5: Run the full web test suite**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run`
Expected: PASS. Any existing test that clicked the old `view-toggle` buttons by role=button must be updated to `getByRole("tab", { name: … })` — find them with `grep -rn "view-toggle" web/src`.

- [ ] **Step 6: Gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck`

```bash
git add web/src/ui/ViewSwitcher.tsx web/src/ui/viewswitcher.test.tsx web/src/pages/OverviewPage.tsx web/src/topo/TopologyPage.tsx
git commit -m "feat(monitor-web): button-border view-switcher tabs on topology + hosts pages

Assisted-by: Claude Fable 5"
```

---

### Task 11: Series search keycap + focus registration

**Files:**
- Modify: `web/src/ui/TextInput.tsx`, `web/src/pages/SeriesPanel.tsx`
- Modify: `web/src/__tests__/seriespanel.test.tsx`

**Interfaces:**
- Consumes: vendored `InputBase`'s existing `shortcut?: string | boolean` prop and its `ref` forwarding; `registerSearchInput` (Task 4).
- Produces: `TextInput` gains `shortcut?: string` and `inputRef?: (el: HTMLInputElement | null) => void` props.

- [ ] **Step 1: Add failing assertions to seriespanel.test.tsx**

Locate the existing render of `SeriesPanel` in `web/src/__tests__/seriespanel.test.tsx` and add:

```tsx
  it("series search shows the / keycap and registers itself for the / shortcut", () => {
    renderPanel(); // the file's existing render helper — reuse it
    const input = screen.getByTestId("series-search") as HTMLInputElement;
    // The keycap is aria-hidden decoration NEXT to the input, inside the
    // same InputBase wrapper group.
    const wrapper = input.closest("div[class*='ring-1']");
    expect(wrapper?.textContent).toContain("/");
    // Registration: the / shortcut focuses this exact input.
    expect(document.activeElement).not.toBe(input);
    expect(focusSearchInput()).toBe(true);
    expect(document.activeElement).toBe(input);
  });
```

with imports `import { focusSearchInput, registerSearchInput } from "../ui/searchFocus";` and an `afterEach` addition: `registerSearchInput(null);`. (If the file has no shared `renderPanel` helper, replicate the props the file's first test passes.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/__tests__/seriespanel.test.tsx`
Expected: the new test FAILS (no keycap, `focusSearchInput()` returns false).

- [ ] **Step 3: Extend TextInput and wire SeriesPanel**

`web/src/ui/TextInput.tsx` — add the two props and pass them through (keep the existing header comment; InputBase spreads unrecognized props onto the `<input>` and forwards `ref` — the whitelist problem its comment describes applies to the high-level `Input`, not `InputBase`):

```tsx
export function TextInput({
  label,
  type = "text",
  value,
  onChange,
  testId,
  shortcut,
  inputRef,
}: {
  label: string;
  type?: string;
  value: string;
  onChange: (value: string) => void;
  testId?: string;
  /** Keycap hint rendered by the vendored InputBase (e.g. "/"). */
  shortcut?: string;
  /** Ref callback to the real <input> (searchFocus registration). */
  inputRef?: (el: HTMLInputElement | null) => void;
}) {
  return (
    <TextField value={value} onChange={onChange} className="inline-flex items-center gap-1.5">
      <Label className="text-xs text-tertiary">{label}</Label>
      <InputBase
        type={type}
        size="sm"
        data-testid={testId}
        wrapperClassName="w-auto"
        shortcut={shortcut}
        ref={inputRef}
      />
    </TextField>
  );
}
```

`web/src/pages/SeriesPanel.tsx` — the search line becomes:

```tsx
      <TextInput
        label="Search"
        value={search}
        onChange={onSearch}
        testId="series-search"
        shortcut="/"
        inputRef={registerSearchInput}
      />
```

with `import { registerSearchInput } from "../ui/searchFocus";`. React calls a ref callback with `null` on unmount, which unregisters automatically.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/vagrant/otto-sh/web && npx vitest run src/__tests__/seriespanel.test.tsx`
Expected: PASS. If InputBase's `ref` prop type rejects a callback ref, check the vendored signature and adapt on OUR side (e.g. `useCallback` wrapper) — never edit the vendored file.

- [ ] **Step 5: Full web gate and commit**

Run: `cd /home/vagrant/otto-sh/web && npm run check && npm run typecheck && make -C /home/vagrant/otto-sh web-check`
Expected: all clean, coverage thresholds hold (every new ui/ module carries its own tests).

```bash
git add web/src/ui/TextInput.tsx web/src/pages/SeriesPanel.tsx web/src/__tests__/seriespanel.test.tsx
git commit -m "feat(monitor-web): series search advertises and receives the / shortcut

Assisted-by: Claude Fable 5"
```

---

### Task 12: E2e updates + full verification

**Files:**
- Modify: `tests/e2e/monitor/dashboard/test_review_shell.py` (lines 248/250 area)
- Modify: `tests/e2e/monitor/dashboard/test_live_shell.py` (lines 72/84, 279/291 area)
- Create: `tests/e2e/monitor/dashboard/test_command_palette.py`

**Interfaces:**
- Consumes: the shipped testids — `command-menu`, `command-input`, `command-item-<id>`, `search-trigger`, `menu-shortcuts`, `reconnecting-banner`, `pause-toggle` (aria-label contract), tab roles under `view-toggle`.

- [ ] **Step 1: Update the stale assertions**

`test_review_shell.py` — the boot/import spec around lines 248–250: replace
`assert page.locator('[data-testid="status-text"]').inner_text() == "No data"` with
`page.locator('[data-testid="empty-review"]').wait_for()` and
`assert page.locator('[data-testid="status-text"]').inner_text() == "Historical"` with
`page.locator('[data-testid="review-bar"]').wait_for()`.
Read the surrounding test to keep its intent (it is asserting the before/after of an import — the empty-state → review-bar transition expresses the same contract without the deleted testid).

`test_live_shell.py` — replace both `expect(_tid(page, "status-text")).to_have_text("Live", ignore_case=True)` (lines ~72/84) with `expect(_tid(page, "pause-toggle")).to_be_visible()` — the pause glyph renders only in live mode and lives in the AppBar on every page, so it is the shell-level "is live" signal now (`live-window` is NOT: decision 10 moved it to the subject page). Replace `expect(_tid(page, "pause-toggle")).to_have_text("Pause")` (lines ~279/291) with `expect(_tid(page, "pause-toggle")).to_have_attribute("aria-label", "Pause")` — the control is a glyph now. The test around line 281 that clicks `live-window-1h` must first navigate to a subject page (click any `subject-link-*` tile from `#/hosts`, or `page.goto` the `#/host/<id>` route the test's session serves) before driving the presets — read that test's setup and keep its assertions intact.

`test_review_shell.py` — the events spec around line 630 (asserts `events-count`, clicks `events-button`, drives `events-panel` rows): the badge now lives on the subject page (decision 11), so after its `_import_fixture(page, "kitchen-sink.json")` add a navigation to a host page before the first events assertion — click any `[data-testid^="subject-link-"]` tile from the hosts grid (navigate to `#/hosts` first if the import lands elsewhere). Keep every existing assertion in that test intact; only the entry point moved.

Also grep the whole dashboard suite for other uses of the deleted ids: `grep -rn "status-text\|status-dot" tests/e2e/` → must come back empty after this task.

- [ ] **Step 2: Write the new palette e2e**

```python
# tests/e2e/monitor/dashboard/test_command_palette.py
"""Command layer e2e (spec 2026-07-17): palette open -> filter -> navigate,
one chord smoke test, and the / search-focus routing. data-testid contract
only. Fixtures import through the client-side front door, zero backend."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def _tid(page, testid: str):
    return page.locator(f'[data-testid="{testid}"]')


def test_palette_opens_and_navigates_to_a_host(page, dashboard_url) -> None:
    """Ctrl+K opens the palette; typing a host id + Enter lands on its
    subject page (spec: palette flow). The host id is read off the loaded
    hosts grid rather than pinned to fixture contents."""
    page.goto(dashboard_url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{dashboard_url}#/hosts")
    first_tile = page.locator('[data-testid^="subject-link-"]').first
    first_tile.wait_for()
    host_id = first_tile.get_attribute("data-testid").removeprefix("subject-link-")

    page.keyboard.press("Control+KeyK")
    expect(_tid(page, "command-menu")).to_be_visible()
    _tid(page, "command-input").fill(host_id)
    expect(_tid(page, f"command-item-nav-host-{host_id}")).to_be_visible()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(_tid(page, "command-menu")).not_to_be_visible()
    page.wait_for_url(f"**/#/host/{host_id}")


def test_search_trigger_opens_palette(page, dashboard_url) -> None:
    page.goto(dashboard_url)
    _import_fixture(page, "kitchen-sink.json")
    _tid(page, "search-trigger").click()
    expect(_tid(page, "command-menu")).to_be_visible()
    page.keyboard.press("Escape")
    expect(_tid(page, "command-menu")).not_to_be_visible()


def test_theme_chord_toggles_dark_mode(page, dashboard_url) -> None:
    """Ctrl+L flips the html dark-mode class (chord smoke test — proves the
    chord path end-to-end in a real browser, preventDefault included)."""
    page.goto(dashboard_url)
    _import_fixture(page, "kitchen-sink.json")
    before = page.evaluate("document.documentElement.classList.contains('dark-mode')")
    page.keyboard.press("Control+KeyL")
    page.wait_for_function(
        f"document.documentElement.classList.contains('dark-mode') !== {str(before).lower()}"
    )


def test_slash_opens_palette_off_subject_pages(page, dashboard_url) -> None:
    page.goto(dashboard_url)
    _import_fixture(page, "kitchen-sink.json")
    page.keyboard.press("Slash")
    expect(_tid(page, "command-menu")).to_be_visible()
```

Adapt the harness boilerplate to the suite's real conventions before running: the `page`/`dashboard_url` fixtures above are stand-ins — the dashboard suite shares a conftest and `test_review_shell.py` shows the actual page-setup pattern (there may be no `dashboard_url` fixture; mirror that file's setup EXACTLY, including how it reaches the served dashboard origin and the hash routes).

- [ ] **Step 3: Python lint gate**

Run: `nox -s lint`
Expected: clean (ruff + format). Fix anything it reports before proceeding.

- [ ] **Step 4: Rebuild the dist and run the Chromium lane for fast iteration**

Run: `make web && pytest tests/e2e/monitor/dashboard/test_command_palette.py tests/e2e/monitor/dashboard/test_review_shell.py tests/e2e/monitor/dashboard/test_live_shell.py -x -q`
Expected: PASS (Chromium only — NOT the green signal yet). Verify the run reports a non-zero test count (xdist/bad-node-id trap: 0 collected = wrong paths, not success).

- [ ] **Step 5: The real browser gate**

Run: `nox -s dashboard`
Expected: PASS across Chromium + Firefox + WebKit. This is the only command that may be called green for the browser lane.

- [ ] **Step 6: Full repo gates**

Run: `make web-check && make coverage`
Expected: both clean; junit triage on any failure via `uv run python scripts/junit_failures.py` (never `make coverage | tail` — it eats the exit code).

- [ ] **Step 7: Commit**

```bash
git add tests/e2e/monitor/dashboard/test_command_palette.py tests/e2e/monitor/dashboard/test_review_shell.py tests/e2e/monitor/dashboard/test_live_shell.py
git commit -m "test(monitor-e2e): command palette + chord flows; status-text assertions replaced

Assisted-by: Claude Fable 5"
```

---

## Final verification checklist (after Task 12)

- [ ] `grep -rn "status-text\|status-dot\|ExportButton" web/src tests/e2e` → only absence-assertions remain.
- [ ] `scripts/check_untitledui_drift.sh` untouched; `git diff --stat main -- web/src/components web/src/styles web/untitledui.lock.json` → empty (vendored tree byte-identical).
- [ ] `nox -s dashboard` green (all three engines), `make web-check` green, `make coverage` green.
- [ ] Manual smoke in a real browser (`make web-dev`): ⌘K/Ctrl+K opens palette · `/` focuses series search on a subject page, opens palette elsewhere · Ctrl+S downloads the export (no browser save dialog) · Ctrl+L flips theme · Ctrl+. pauses live (live server needed: `otto monitor --live` against the playground VM) · tabs switch views · live-window presets AND the Events badge appear ONLY on a subject page (title row), absent from the AppBar on topology/hosts · reconnecting banner appears when the live server is stopped mid-session.
