# Untitled UI deepening: button-border tabs, icon-advanced menu, command palette + keyboard shortcuts

**Date:** 2026-07-17
**Status:** Approved (brainstorm session with Chris)
**Builds on:** `2026-07-17-topology-default-view-docs-hero-design.md` — this
design assumes that spec's routes (`/` = topology, `/hosts` = fleet grid) and
lands after (or together with) it.

## Goal

Lean harder into the Untitled UI look and feel across the dashboard chrome:

1. The topology/grid view switcher becomes the horizontal **button-border
   tabs** style.
2. The AppBar `⋯` overflow menu becomes the **icon-advanced** dropdown
   composition (sections, icons, right-side shortcut hints).
3. A **⌘K command menu** with global keyboard shortcuts, doubling as the
   discoverable shortcut reference.
4. The series search box shows a **keycap shortcut hint** (the
   settings-page-example affordance), and the AppBar gains a search-style
   ⌘K trigger with the same keycap.

## Constraints discovered up front

- **The Untitled UI command menu is PRO-tier** — the pinned free-tier CLI
  (`web/untitledui.lock.json`) refuses it (`pro_access_required`). Decision:
  **build our own** in `web/src/ui/**` on react-aria-components primitives
  (`ModalOverlay` + `Dialog` + `Autocomplete` + `Menu`, all present in the
  installed v1.19.0 — the same stack Untitled UI's own command menu uses),
  styled with the vendored theme tokens.
- **No new vendored components are needed at all.** The vendored `tabs.tsx`
  already ships `type="button-border"` horizontal; `dropdown.tsx` already
  supports `icon`, `addon`, and `Section`; `input.tsx`'s `InputBase` already
  renders a `shortcut` keycap. The lock file's component list and the drift
  check are untouched, and the never-hand-edit rule holds: every new file is
  authored under `web/src/ui/**` (fully lint/format/coverage-gated).

## Decisions (made in the brainstorm)

1. Command palette built in-house (PRO constraint above) — not `cmdk`, not a
   hand-rolled listbox.
2. Builds on the topology-default-view routes; the tabs read
   **Topology | Hosts** and navigate `/` ↔ `/hosts`.
3. Palette contents: navigation + actions + live-window presets, and the
   palette **is** the shortcut reference (every bound row shows its keycap;
   no separate "?" overlay).
4. **Action shortcuts are Cmd/Ctrl chords; search is bare `/`** (Chris: a
   bare letter typed into the wrong window mangles the page — but starting
   a search is harmless, and `/` is the natural search key for
   CLI-focused engineers, the typical user; it is the one bare-key
   binding, applied to *every* search surface). Browsers reserve
   `⌘T`/`Ctrl+T`, `⌘N`, `⌘W`, `⌘⇧T` (and macOS owns `⌘H`) at a level page
   JS cannot intercept, so Topology/Hosts get **no direct chord** — they
   are the top rows of `⌘K` plus the visible tabs. Shipped set (Cmd on
   mac, Ctrl elsewhere): `⌘K` palette · `/` focus search (opens the
   palette on pages without one) · `⌘I` import · `⌘S` export (Chris's
   pick — export is save-like; shadows the browser's save-page, which is
   interceptable) · `⌘L` theme (Chris's pick — interceptable; deliberately
   overrides the browser's focus-address-bar while the dashboard has
   focus) · `⌘.` pause/resume. `⌘D`/`⌘P` (bookmark, print) stay
   untouched.
5. Tabs stay at the top of each page (per-page, as today) — not promoted
   into the AppBar.
6. Theme control in the overflow menu stays a **single toggle row**
   ("Switch to dark/light mode"), not Light/Dark radio rows.
7. The AppBar gets a **search-style button** trigger for the palette
   (input lookalike: magnifier icon, "Search…" placeholder, keycap).
8. Export becomes **always present** in the overflow menu (disabled when no
   data). The old live-mode omission existed to avoid a second click path;
   the palette duplicates every action anyway, so a mode-stable menu wins.
9. **AppBar rework (second review round):** the status text + dot are
   removed — "Reviewing"/"Historical"/"No data" are redundant (ReviewBar
   badge, EmptyState), and live connection loss moves to a new
   **Reconnecting banner** (the status cluster was the *only* render site
   for the store's `connection` state; deleting it without a replacement
   would leave a state nothing displays). Pause and Export become
   **icon-only glyph buttons** (`ButtonUtility`); the search trigger gets
   a permanent left-anchored home beside the brand (third review round —
   it never moves with mode) while every other control groups on the
   right; and the overflow trigger becomes **`DotsVertical` (⋮)** instead
   of `DotsHorizontal`.
10. **Live-window presets leave the AppBar (fourth review round):** the
    `5m·15m·1h` ButtonGroup renders only on the **subject page** (per-host
    stats view), in its title row, live mode only. `windowMs` drives the
    per-host chart windows (`liveRange(endMs, windowMs)` — SubjectPage);
    on topology/hosts views the control was dead chrome. The palette's
    "Live window" rows stay — global reach without standing chrome.
11. **The Events badge joins the per-host chrome (same round):** the
    Events button + count pill (and the EventsPanel slideout it opens)
    move from the AppBar to the subject page's title row beside the
    presets. The AppBar right group is just pause · export · `⋮`. The
    slideout still lists session-wide events — only the entry point
    moves.

## UI surfaces

### View switcher (TopologyPage + hosts grid page)

- Replace the `ButtonGroup` with vendored `Tabs`
  (`components/application/tabs`), horizontal, `type="button-border"`,
  size sm; items **Topology | Hosts**.
- Selected tab is **derived from the current route**, never stored (the
  `selectedWindowId` lesson); `onSelectionChange` navigates via wouter.
- Keeps `data-testid="view-toggle"`. The ARIA shape changes
  (group/buttons → tablist/tabs); every vitest/Playwright assertion that
  clicks or queries the old roles is updated in the same change.

### AppBar

Left: brand, then the **search trigger** — a permanent, mode-independent
home (left-anchored rather than centered, which would fight the right
group on narrower windows). Right group, all controls together:
pause/resume glyph (live only) · export glyph (live only) · `⋮` menu.
No status text, no status dot, no live-window presets, no Events badge —
presets and Events move to the subject page (decisions 10/11). Mode
changes only ever add/remove entries in the right group; the search bar
never moves.

- Pause/resume and Export are `ButtonUtility` icon glyphs (pause/play,
  download icons) with `aria-label`s and tooltips; the old text "Pause"
  button and text "Export" button are gone. Existing testids
  (`pause-toggle`) are kept.
- `ui/SearchTrigger` (name at implementer's discretion): a `<button>`
  styled as an input lookalike — magnifier icon, "Search…" placeholder
  text, right-aligned `/` keycap (every search surface advertises `/`,
  decision 4). Opens the palette; `⌘K` also opens it and stays surfaced
  on the menu's "Keyboard shortcuts…" addon.
- Chord hint text is platform-aware (`⌘I` on macOS, `Ctrl I` elsewhere) —
  detected once in the shortcut module and shared by every hint site.
- Overflow trigger icon is `DotsVertical`.

### Reconnecting banner (new)

The AppBar status cluster was the only reader of the store's `connection`
state. Its replacement: a slim banner (same pattern/placement as the
import-error banner: `border-b`, `bg-status-warn/10`, `text-status-warn`)
rendered only while `mode === "live"` and `connection !== "live"`, reading
"Reconnecting…". It disappears on reconnect; a healthy live session shows
no connection chrome at all. Review/historical modes never show it.

### Overflow menu (icon-advanced composition)

Same vendored `Dropdown`, recomposed with sections/icons/addons:

- **Data section:** Import… (upload icon, addon `⌘I`) · Export (download
  icon, addon `⌘S`, `isDisabled` when no data; present in all modes per
  decision 8).
- **Appearance section:** Switch to dark/light mode (moon/sun icon,
  addon `⌘L`).
- **Help section:** Keyboard shortcuts… (addon `⌘K`/`Ctrl K`) — opens the
  palette.

Addon strings use the platform-aware formatter (`⌘I` mac / `Ctrl I`
elsewhere). Trigger is `ButtonUtility` + `DotsVertical` (decision 9).
Existing `menu-import`/`menu-export`/`menu-theme` testids are kept.

### Per-host chrome on the subject page (decisions 10/11)

The existing `live-window` ButtonGroup (testids `live-window`,
`live-window-5m/15m/1h`, selection derived from `windowMs`) and the Events
button (`events-button`/`events-count`, with the `EventsPanel` slideout it
opens) move verbatim from the AppBar into the subject page's title row
(right-aligned beside the `subject-title` heading). Presets render only
when `mode === "live"`; Events keeps its existing gate (session has
events). E2e consequences: shell-level "is live" waits can no longer use
`live-window`, and events/preset-driving tests navigate to a host page
first — the pause glyph is the AppBar's live signal.

### Series search keycap

- `ui/TextInput.tsx` gains an optional `shortcut` prop passed through to
  the vendored `InputBase` (which owns the keycap rendering); `SeriesPanel`
  passes `"/"` (platform-independent). The vendored styling hides the
  keycap below the `md` breakpoint — accepted as-is.

## Command layer

### Registry — one source of truth, three consumers

`ui/commands.ts` exports `useCommands(): Command[]` deriving from the
review store; shape `{ id, label, section, icon?, binding?, enabled,
run() }`. The palette renders commands, the shortcut layer executes the
bound ones, and every visible hint (palette keycaps, dropdown addons,
AppBar keycap) formats the same binding objects — nothing is stored twice,
so hints cannot drift from handlers.

- **Navigation:** Topology (→ `/`), Hosts (→ `/hosts`), one row per host
  (label = host id + board·slot, → `/host/:id`), one row per element
  (→ `/topology/:elementId`). All navigation rows are chord-less (the
  browser-reserved-key constraint, decision 4) — reachable by typing.
- **Actions:** Import… (`⌘I`), Export (`⌘S`, disabled without data),
  Switch to dark/light mode (`⌘L`), Pause/Resume (`⌘.`, live mode only).
- **Live window:** 5m / 15m / 1h preset rows, live mode only; the active
  preset is check-marked (derived from `windowMs`).

Live-only rows (Pause/Resume, the presets) are **omitted** outside live
mode, not disabled; Export is the one disabled-not-hidden row (its
disablement communicates "nothing loaded yet", matching the menu item).

### Palette (`ui/CommandMenu.tsx`)

react-aria-components `ModalOverlay` (dimmed backdrop; panel near the top,
~20vh) → `Dialog` → `Autocomplete` (contains-filter via `useFilter`)
wrapping a search field composed from vendored `InputBase` plus a sectioned
`Menu` of command rows: icon · label · right-aligned keycap rendered by a
small authored `Kbd` component that mirrors `InputBase`'s keycap classes.
Enter runs and closes; Esc closes; empty filter state shows a "No results"
row. Styling uses theme-token classes only, so it sits next to the vendored
dropdown indistinguishably in both light and dark mode.

### Global shortcuts (`ui/useGlobalShortcuts.ts`)

One document-level keydown listener. Chords match on `metaKey` (mac) /
`ctrlKey` (elsewhere) + `key`, call `preventDefault()` on match, and fire
from anywhere — chords never type characters, inputs included. The one
bare key, `/`, gets a narrow guard: it fires only when focus is not in an
input/textarea/contentEditable and no overlay is open (typing a literal
slash into the series search or palette filter must stay a slash); that
predicate is unit-tested on its own.

- `⌘K`/`Ctrl+K` toggles the palette.
- `/` focuses the series search on subject pages (SeriesPanel registers
  its input element via a tiny focus-registry module in `ui/`); on pages
  without a search box it opens the palette.
- `⌘I` / `⌘S` / `⌘L` / `⌘.` run their commands directly (each is a
  no-op where its command is absent or disabled — e.g. `⌘.` outside live
  mode). `⌘S` and `⌘L` intentionally shadow the browser's save-page and
  focus-address-bar while the dashboard has focus (decision 4) —
  `preventDefault()` on the matched chord is what keeps the browser's
  save dialog from also opening.
- Esc is untouched — overlays and the link inspector already handle it.
- Reserved-key rule for future bindings: never `⌘T`/`⌘N`/`⌘W`/`⌘⇧T`
  (browser-owned, uninterceptable) or `⌘H`/`⌘M`/`⌘Q` (macOS-owned), and
  avoid interceptable-but-sacred `⌘D`/`⌘P` (bookmark, print).

### Wiring

AppBar renders outside the wouter `<Router>`, but navigation commands need
router context. Palette open/close state therefore lives in a tiny
`ui/`-layer zustand store (the AppBar trigger and dropdown item just call
its `open()`), while `CommandMenu` + the shortcut layer mount inside the
Router branch of `App.tsx`. Consequence, accepted: shortcuts and palette
are active only once data is loaded — the EmptyState import screen keeps
its own explicit buttons.

## Testing

- **Vitest (all new code coverage-gated):** registry derivation per mode
  (live vs review vs import-only); the chord matcher (fires from inside an
  input, `preventDefault` called, wrong-modifier and bare-letter presses
  ignored) and the `/` guard (suppressed while typing or with an overlay
  open, fires otherwise); mac/win binding labels; palette filter → run
  flow (jsdom);
  tabs navigate-on-select; dropdown composition (icons/addons/sections
  render, actions still fire); `TextInput` shortcut pass-through; the
  reconnecting banner (renders only for `mode === "live"` +
  `connection !== "live"`, disappears on reconnect; AppBar renders no
  status text/dot in any mode).
- **Playwright e2e:** palette open → type host id → land on subject page;
  chord smoke test (`Ctrl+L` toggles theme); restyled overflow menu
  still imports/exports/toggles theme; `view-toggle` assertions updated
  for tab roles; existing assertions on `status-text`/`status-dot`
  (e.g. `test_review_shell`'s "Live"/"Historical" waits) replaced with
  mode-appropriate signals (empty-state/ReviewBar presence; the pause
  glyph as the live signal); preset- and events-driving tests navigate to
  a subject page first (decisions 10/11).
- **Gates:** `nox -s dashboard` (full three-engine matrix — bare pytest is
  Chromium-only and must not be called green), `make web-check`, and the
  standard `make coverage` per-task gate. Web dist rebuilt (`make web`)
  before any browser verification — the stale-bundle conftest guard
  applies.

## Out of scope

- No new vendored components; no changes to `untitledui.lock.json`, the
  drift script, or any file under the vendored paths.
- No System theme option (explicit light/dark persistence unchanged).
- No shortcuts on the EmptyState screen.
- Docs screenshots: the topology-default-view spec owns the docs hero;
  nothing here changes docs media (the restyled switcher will simply
  appear in the next capture run).
