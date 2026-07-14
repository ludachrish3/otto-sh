# Monitor 5b follow-ups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two 5b spec gaps (drilled-in unreachable treatment, a settable live window) and adopt Untitled UI as the shell's component foundation, replacing our hand-rolled primitives rather than wrapping them.

**Architecture:** Untitled UI is copy-in source vendored under `web/src/components/**` (never hand-edited, excluded from coverage). Our own composites live in `web/src/ui/**` (coverage-gated). Their semantic tokens replace the `text-gray-500 dark:text-gray-400` pairs across the app, so the theme flips without `dark:` variants. The two functional features are built on the new primitives, so the foundation lands first.

**Tech Stack:** React 19, TypeScript, Tailwind v4, react-aria-components, Untitled UI (free tier), zustand, ECharts, @xyflow/react, vitest + Playwright.

**Spec:** `docs/superpowers/specs/2026-07-13-monitor-5b-followups-design.md`

## Global Constraints

- **Preserve every existing `data-testid`** unless a task explicitly retires it. The 39-test browser suite is the only safety net for a migration this wide; a green suite is what proves the app still works. When a control is replaced, the replacement carries the old testid.
- **Never hand-edit vendored Untitled UI files** under `src/components/**` and `src/styles/**` — *no exceptions*. Anything we author goes in `src/ui/**`. Untitled UI is copy-in source with no version, no manifest and no lockfile entry, so Dependabot cannot track it (see Task 11); a byte-exact vendored tree is the *only* thing that makes an automated upstream-drift check possible, and one hand-edit destroys it.
- **No `dark:` variants in migrated code.** Use Untitled UI semantic tokens (`text-tertiary`, `bg-primary`, `border-secondary`), which flip on `.dark` by themselves.
- **Keep** `@xyflow/react` (the topology canvas — Untitled UI has no equivalent on any tier) and **ECharts** (Untitled UI's `charts-base` is Recharts; swapping would discard Plan 5b's incremental `setOption` path).
- **Our tokens win:** `--color-brand-*` stays `#7c5cff`-family violet; `--color-status-{live,historical,warn,ok,error}` stay ours. Untitled UI's brand purple must not resolve.
- **`make web-check` before every commit that touches `web/`** — it is the only gate that runs Biome's format check. Plan 5b landed CI red for exactly this reason.
- **For every load-bearing test, prove it can fail.** Mutate the production code, watch the test go red, revert. A guard that cannot fail is worse than no guard. Plan 5b shipped ten of them.
- Interval floor, format:1 payload shape, and the SSE fragment contract are unchanged by this plan.

---

### Task 1: The Untitled UI foundation

Vendor the token layer and wire the aliases. No component migrates yet; the only visible change is the type scale.

**Files:**
- Create: `web/src/styles/theme.css` (vendored), `web/src/utils/cx.ts` (vendored), `web/src/hooks/use-breakpoint.ts` (vendored)
- Modify: `web/package.json`, `web/tsconfig.json`, `web/vite.config.ts`, `web/src/app.css`
- Test: `web/src/__tests__/tokens.test.ts`

**Interfaces:**
- Produces: `cx(...)` from `@/utils/cx`; the full Untitled UI token vocabulary as Tailwind utilities (`bg-primary`, `text-tertiary`, `border-secondary`, …); `@/*` → `src/*` path alias.

- [ ] **Step 1: Vendor via the CLI, then take only what we need**

```bash
cd web
npx untitledui@latest init --yes
```

`init` adds deps, `src/styles/{globals,theme,typography}.css`, `src/utils/cx.ts`, and imports `globals.css` from `main.tsx`. Then **undo the parts we do not want**:

- Delete `src/styles/globals.css` and `src/styles/typography.css`. Revert the `main.tsx` import it added — our single stylesheet stays `app.css`.
- Remove `@tailwindcss/typography` from `package.json` (nothing renders prose).

Keep `theme.css`, `cx.ts`, and these deps: `@internationalized/date`, `react-aria`, `@react-stately/utils`, `@untitledui/icons`, `tailwind-merge`, `tailwindcss-animate`, `tailwindcss-react-aria-components`.

- [ ] **Step 2: Adopt Untitled UI's dark class outright — do not shadow it**

`theme.css` gates its dark tokens on `.dark-mode`. We used to toggle `.dark`.

Do **not** edit the vendored file, and do **not** toggle both classes either — a shadow class is coupling with no payer. Untitled UI's class becomes *the* class:

- `web/src/theme.ts`'s `applyTheme()` toggles **only** `.dark-mode` on `<html>`.
- `app.css`'s variant points at it: `@custom-variant dark (&:where(.dark-mode, .dark-mode *));`. The *variant* is still spelled `dark:` (that is the Tailwind utility prefix, and vendored components may use it) — only the class it resolves against changes. This is not shadowing; it is one class with one name.
- `app.css`'s `--topo-edge-*` dark block moves from `.dark {` to `.dark-mode {`. `LinkEdge.tsx` builds inline SVG style objects and cannot use Tailwind variants, so these custom properties stay — only their selector changes. `topoedge.test.tsx` pins their resolved values and must stay green.
- The Playwright specs that assert `document.documentElement.classList.contains('dark')` (`tests/e2e/monitor/dashboard/test_review_shell.py`, ~4 sites) update to `'dark-mode'`.

`web/src/styles/theme.css` must end up **byte-identical** to what the CLI emits. Verify it: re-vendor into a temp directory and `diff` — zero output, or the task is not done.

- [ ] **Step 3: Wire app.css — their tokens first, ours last**

```css
@import "tailwindcss";
@import "@fontsource-variable/inter";
@import "./styles/theme.css";

@plugin "tailwindcss-animate";
@plugin "tailwindcss-react-aria-components";

@custom-variant dark (&:where(.dark, .dark *));
@custom-variant label (& [data-label]);
@custom-variant focus-input-within (&:has(input:focus));

/* OUR @theme block comes AFTER the import above, so these win over Untitled
   UI's brand purple (#9E77ED). charts/palette.ts reads --color-brand-500, and
   a token collision that resolves the wrong way is invisible to every gate
   except the one in tokens.test.ts. */
@theme {
  --font-sans: "Inter Variable", system-ui, sans-serif;
  /* ... the existing brand-* and status-* block, unchanged ... */
}
```

Keep the existing `:root`/`.dark` topology-edge custom properties and the `body` rule as they are.

- [ ] **Step 4: Path aliases in all three places**

`tsconfig.json` gets `"paths": { "@/*": ["./src/*"] }`. `vite.config.ts` gets `resolve: { alias: { "@": path.resolve(__dirname, "./src") } }` — Vite does not read tsconfig paths, and vitest inherits this same config.

- [ ] **Step 5: Exclude vendored source from coverage**

In `vite.config.ts`'s `test.coverage.exclude`, add `"src/components/**"` with a comment: vendored Untitled UI source, not ours to test. `src/ui/**` stays measured.

- [ ] **Step 6: Write the token guard**

```ts
// web/src/__tests__/tokens.test.ts
import { describe, expect, it } from "vitest";

// A token collision is invisible to lint, typecheck, and the DOM-asserting
// browser suite: Untitled UI's theme.css defines a FULL brand ramp whose 500 is
// #9E77ED (purple), and ours is violet. Which one resolves depends purely on
// @import order in app.css. charts/palette.ts reads this token.
describe("design tokens", () => {
  it("keeps otto's brand violet, not Untitled UI's purple", async () => {
    const css = await import("node:fs/promises").then((fs) =>
      fs.readFile(new URL("../app.css", import.meta.url), "utf8"),
    );
    const themeImport = css.indexOf('@import "./styles/theme.css"');
    const ourBrand = css.indexOf("--color-brand-500: #7c5cff");
    expect(themeImport).toBeGreaterThan(-1);
    expect(ourBrand).toBeGreaterThan(themeImport);
  });
});
```

- [ ] **Step 7: Prove the guard can fail**

Move the `@import "./styles/theme.css"` line below our `@theme` block. Run `npm test -- tokens` — it MUST fail. Restore.

- [ ] **Step 8: Gates**

Run: `npm run build && npx vitest run && make web-check` (from repo root for the last one). Expected: build succeeds, all existing tests pass, Biome clean.

- [ ] **Step 9: Commit**

```bash
git add web/ && git commit -m "build(web): vendor the Untitled UI token foundation"
```

---

### Task 2: Buttons, badges, and the segmented control

**Files:**
- Create (vendored): `web/src/components/base/buttons/button.tsx`, `web/src/components/base/buttons/button-utility.tsx`, `web/src/components/base/button-group/button-group.tsx`, `web/src/components/base/badges/badges.tsx`
- Delete: `web/src/ui/Button.tsx`, `web/src/ui/Badge.tsx`, `web/src/ui/ToggleGroup.tsx`
- Modify: every call site (`shell/AppBar.tsx`, `shell/ReviewBar.tsx`, `shell/ImportExport.tsx`, `shell/EmptyState.tsx`, `pages/OverviewPage.tsx`, `topo/*`)
- Test: existing vitest specs that render these; `tests/e2e/monitor/dashboard/`

**Interfaces:**
- Consumes: `cx`, the token layer (Task 1).
- Produces: `Button` (Untitled UI: `color="primary"|"secondary"|"tertiary"`, `size`, `iconLeading`), `ButtonGroup`/`ButtonGroupItem`, `Badge` (`color`, `type`, `size`).

- [ ] **Step 1: Vendor**

```bash
cd web && npx untitledui@latest add button button-group badges button-utility --yes
```

- [ ] **Step 2: Map the props, preserving testids**

Our `Button`'s `variant="primary"|"secondary"|"ghost"` maps to Untitled UI's `color="primary"|"secondary"|"tertiary"`. Our `ToggleGroup` (a segmented control with `options`/`selectedId`/`onSelect`) becomes `ButtonGroup` with `ButtonGroupItem` children; the group keeps the wrapper's `data-testid` (`view-toggle`, `range-presets`) and each item keeps its own. Our `Badge`'s `tone="historical"` maps to Untitled UI's `color` — pick the closest and keep `data-testid="historical-tag"`.

Every `data-testid` on these controls survives. That is what lets the browser suite tell you whether the migration broke anything.

- [ ] **Step 3: Delete ours**

`rm web/src/ui/{Button,Badge,ToggleGroup}.tsx`. If a call site still imports them, typecheck fails — that is the point.

- [ ] **Step 4: Gates**

Run: `npx vitest run`, `npm run typecheck`, then the browser suite `nox -s dashboard`. Expected: all green. `nox -s dashboard` runs chromium **and** firefox **and** webkit; a bare `pytest tests/e2e/monitor/dashboard` runs chromium only and is NOT the gate.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(web): move buttons, badges and the segmented control to Untitled UI"
```

---

### Task 3: Select, input, dropdown, tooltip

**Files:**
- Create (vendored): `web/src/components/base/select/*`, `web/src/components/base/input/*`, `web/src/components/base/dropdown/*`, `web/src/components/base/tooltip/*`
- Delete: `web/src/ui/Select.tsx`, `web/src/ui/TextInput.tsx`, `web/src/ui/Menu.tsx`
- Modify: `shell/ReviewBar.tsx` (session picker), `shell/AppBar.tsx` (`OverflowMenu` → `Dropdown` + `ButtonUtility` trigger), `pages/SeriesPanel.tsx` (search input), `pages/SubjectPage.tsx` (log-table filter input)
- Test: existing vitest specs; browser suite

**Interfaces:**
- Produces: `Select` (`items`, `selectedKey`, `onSelectionChange` — the react-aria shape our `Select` already mirrors), `Input`, `Dropdown`.

- [ ] **Step 1: Vendor**

```bash
cd web && npx untitledui@latest add select input dropdown tooltip --yes
```

- [ ] **Step 2: Migrate call sites**

`ui/Select`'s API was already react-aria-shaped, so the session picker is close to a drop-in; keep `data-testid="session-picker"`. `OverflowMenu` becomes Untitled UI's `Dropdown` with a `ButtonUtility` trigger, keeping `menu-import` / `menu-export` / `menu-theme` testids and the existing live-mode omission of the export entry. The raw `<input>` in `SubjectPage`'s `LogTable` filter and `SeriesPanel`'s search both become Untitled UI `Input` (keep `log-filter-*` and the search testid).

- [ ] **Step 3: Delete ours; typecheck finds the stragglers**

- [ ] **Step 4: Gates** — `npx vitest run`, `npm run typecheck`, `nox -s dashboard`.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(web): move select, input, dropdown and tooltip to Untitled UI"
```

---

### Task 4: `healthForHost` — one rule, one function

**Files:**
- Modify: `web/src/data/health.ts`
- Test: `web/src/data/health.test.ts` (extend)

**Interfaces:**
- Produces: `healthForHost(session, hostId, range, nowMs?): SubjectHealth`. `healthForHosts` becomes a loop over it and keeps its exact current signature and semantics.

- [ ] **Step 1: Write the failing test — the rule must not fork**

```ts
it("healthForHost agrees with healthForHosts for every host", () => {
  const session = synthSession(); // existing fixture helper
  const now = session.endMs + 60_000;
  const all = healthForHosts(session, null, now);
  for (const host of session.lab.hosts) {
    expect(healthForHost(session, host.id, null, now)).toEqual(all.get(host.id));
  }
});
```

Cover all four statuses (`ok`, `down`, `no-data`, `unknown`) — a fixture where every host reports, one where a host has no series at all, one past the down threshold.

- [ ] **Step 2: Run it — fails, `healthForHost` is not exported**

Run: `npx vitest run health`

- [ ] **Step 3: Extract the per-host body**

Lift the loop body of `healthForHosts` into `healthForHost` verbatim (the binary-search-per-series path, the cadence resolution, the `HEALTH_K × cadence` comparison — none of it changes), then rewrite `healthForHosts` as:

```ts
export function healthForHosts(
  session: NormalizedSession,
  range: TimeRange | null,
  nowMs?: number,
): Map<string, SubjectHealth> {
  const out = new Map<string, SubjectHealth>();
  for (const host of session.lab.hosts) {
    out.set(host.id, healthForHost(session, host.id, range, nowMs));
  }
  return out;
}
```

- [ ] **Step 4: Run — passes.** The existing `health.test.ts` cases must all still pass untouched; they are the proof the extraction was behavior-preserving.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(web): extract healthForHost so the drill-in can ask about one host"
```

---

### Task 5: The unreachable banner

**Files:**
- Create: `web/src/shell/SubjectHealthBanner.tsx`, `web/src/shell/subjecthealthbanner.test.tsx`
- Modify: `web/src/pages/SubjectPage.tsx`, `web/src/data/time.ts` (`formatOutage`)
- Test: `web/src/data/time.test.ts`, `tests/e2e/monitor/dashboard/test_live_shell.py`

**Interfaces:**
- Consumes: `healthForHost` (Task 4), `useNow` (`data/clock.ts`), `useActiveSession`/`useReviewStore`.
- Produces: `<SubjectHealthBanner subjectId={id}>{chartStack}</SubjectHealthBanner>`.

- [ ] **Step 1: `formatOutage` + its test**

```ts
/** Outage duration for the unreachable banner. The down threshold is
 * HEALTH_K x cadence — 3s at a 1s interval — so sub-minute outages are
 * reachable and formatSpan's "0m" would be wrong copy. */
export function formatOutage(ms: number): string {
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return formatSpan(0, ms);
}
```

Test `45s`, `2m`, `1.5h`, and the boundary at exactly 60_000 (`1m`).

- [ ] **Step 2: Write the banner's failing tests**

```tsx
// A host subject: banner + the chart stack is dimmed.
it("dims a host subject and names its outage", () => { /* render, assert
   data-testid="unreachable-banner" text is
   "Unreachable for 2m — showing last-known data" and the stack wrapper
   carries the dim class */ });

// An element subject: banner names the members, and does NOT dim — the
// healthy members' charts are still live and correct.
it("names unreachable members on an element subject without dimming", () => {
  /* two of three members down -> "tech2, tech3 unreachable for 2m — showing
     last-known data", in slot-then-id order; no dim class */
});

it("renders nothing when the subject is healthy", () => { /* ... */ });
```

- [ ] **Step 3: Run — fails (no such module).** Run: `npx vitest run subjecthealthbanner`

- [ ] **Step 4: Implement**

The component subscribes to the clock **itself** and takes the chart stack as `children`:

```tsx
export function SubjectHealthBanner(props: { subjectId: string; children: ReactNode }) {
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  // Only live mode ticks; an archive's "now" is its own endMs (health.ts).
  const tickMs =
    mode === "live" && session?.meta.interval != null ? session.meta.interval * 1000 : null;
  const now = useNow(tickMs);
  // ... healthForHost per member, build the banner, wrap children ...
}
```

`SubjectPage` wraps its chart stack with it and does **not** call `useNow` itself. That is the whole point: React reuses the already-created `children` elements, so a tick re-renders the banner and nothing below it.

- [ ] **Step 5: Run — passes.**

- [ ] **Step 6: The render-count guard, and proof it can fail**

Add to `subjecthealthbanner.test.tsx`: with a live session and a fake timer advancing N intervals with no new data, a render-counting spy on `ChartPanel` must record **zero** additional renders while the banner's text updates.

Then MUTATE: delete the `children` indirection and call `useNow` directly in `SubjectPage`. The guard MUST go red. Restore. If it stays green, the guard is measuring nothing — Plan 5b shipped four guards with exactly this defect.

- [ ] **Step 7: Browser coverage**

Extend `test_live_shell.py`: a host that stops reporting mid-stream shows the banner on its drill-in and its outage grows across ticks, while its charts keep their last-known data.

- [ ] **Step 8: Gates** — `npx vitest run`, `make web-check`, `nox -s dashboard`.

- [ ] **Step 9: Commit**

```bash
git commit -am "feat(web): show the unreachable treatment on the drill-in"
```

---

### Task 6: The live window

**Files:**
- Modify: `web/src/data/reviewStore.ts`, `web/src/shell/AppBar.tsx`
- Test: `web/src/data/reviewstore.test.ts`, `web/src/shell/appbar.test.tsx`, `tests/e2e/monitor/dashboard/test_live_shell.py`

**Interfaces:**
- Consumes: `ButtonGroup` (Task 2).
- Produces: `setWindow(windowMs: number)` on `ReviewActions`.

- [ ] **Step 1: Write the failing store tests**

```ts
it("widens the follow window without pinning the view", () => {
  // live, range === null
  actions.setWindow(3_600_000);
  expect(store.getState().windowMs).toBe(3_600_000);
  expect(store.getState().range).toBeNull(); // STILL FOLLOWING — the spec's word
});

it("re-pins around the frozen instant when paused", () => {
  // live, paused: range = { from: T - 900_000, to: T }
  actions.setWindow(3_600_000);
  const { range } = store.getState();
  expect(range).toEqual({ from: T - 3_600_000, to: T }); // same `to`, wider span
  expect(useIsPaused_equivalent(store.getState())).toBe(true); // still paused
});
```

- [ ] **Step 2: Run — fails, no `setWindow`.**

- [ ] **Step 3: Implement**

```ts
setWindow: (windowMs) => {
  const { range } = get();
  // Following: just resize the derived window (liveRange reads windowMs).
  // Paused: `range` IS the pause (paused is derived, never stored — see
  // togglePause), so keep its `to` and re-pin at the new width. Choosing a
  // window while paused zooms around what you are looking at rather than
  // silently resuming or doing nothing until you do.
  set(range === null ? { windowMs } : { windowMs, range: { from: range.to - windowMs, to: range.to } });
},
```

- [ ] **Step 4: Run — passes.**

- [ ] **Step 5: The AppBar control**

An Untitled UI `ButtonGroup` beside Pause, live mode only: `5m · 15m · 1h`, `data-testid="live-window"`, each item testid'd. Selected item is derived from `windowMs` — do not store it separately (Plan 5b's lesson: a stored copy of a derived value drifts).

- [ ] **Step 6: Browser coverage**

Extend `test_live_shell.py`: choosing `1h` widens the chart's x-axis window (`data-window-to` minus the axis min, or `data-point-count` growth against a replay) while the view keeps following.

- [ ] **Step 7: Gates** — `npx vitest run`, `make web-check`, `nox -s dashboard`.

- [ ] **Step 8: Commit**

```bash
git commit -am "feat(web): let the live window be resized while following or paused"
```

---

### Task 7: The range picker

**Files:**
- Create (vendored): `web/src/components/application/date-picker/{calendar,cell,range-calendar}.tsx`, `web/src/components/base/input/input-date.tsx`
- Create: `web/src/ui/RangePicker.tsx`, `web/src/ui/rangepicker.test.tsx`
- Modify: `web/src/shell/ReviewBar.tsx`
- Delete: the ReviewBar's preset `ButtonGroup`, both `datetime-local` inputs, Apply and Reset
- Test: `tests/e2e/monitor/dashboard/test_review_shell.py` (`test_custom_range_apply_and_reset`, `test_range_presets_change_subject_summary` are rewritten against the new control)

**Interfaces:**
- Consumes: Untitled UI's `RangeCalendar`, `InputDateBase`, `Button`; `@internationalized/date`.
- Produces: `<RangePicker bounds={TimeRange} value={TimeRange | null} onChange={(r: TimeRange | null) => void} />`.

- [ ] **Step 1: Vendor the calendar pieces**

```bash
cd web && npx untitledui@latest add range-calendar date-picker --yes
```

Take `calendar.tsx`, `cell.tsx`, `range-calendar.tsx`, `input-date.tsx`. Do **not** take `date-range-picker.tsx` — its presets are `Today` / `This week` / `Last year` / an `All time` starting in the year 2000, and it is day-granularity. Our range lives inside one run.

- [ ] **Step 2: Write the failing tests**

```tsx
it("maps a preset to a session-relative range", () => {
  // bounds = { from: T-3_600_000, to: T }; choose "Last 15m"
  // -> onChange({ from: T - 900_000, to: T })
});
it("Full clears the range", () => { /* -> onChange(null) */ });
it("keeps minute precision", () => {
  // a 10-minute session: pick 12:03 -> 12:09; the emitted range is exactly that,
  // NOT a day boundary. This is why we did not vendor their picker.
});
it("cannot choose a range outside the session", () => {
  // minValue/maxValue from bounds
});
```

- [ ] **Step 3: Run — fails.**

- [ ] **Step 4: Implement**

Compose Untitled UI's popover + `RangeCalendar` + two `InputDateBase` at `granularity="minute"`, with a preset rail (`Full`, `Last 15m`, `Last 1h`) computed from `bounds`, and `minValue`/`maxValue` clamped to `bounds`. Convert with `@internationalized/date`: ms → `toCalendarDateTime(fromDate(new Date(ms), getLocalTimeZone()))`, and back via `.toDate(getLocalTimeZone()).getTime()`. Match Untitled UI's card layout (preset rail left, inputs and Cancel/Apply bottom-right).

- [ ] **Step 5: Run — passes.**

- [ ] **Step 6: Rewrite the ReviewBar**

`HISTORICAL` badge · source · session picker (when >1) · `<RangePicker>`. `Full` subsumes the old Reset (`setRange(null)`), so `range-reset` is retired. Keep `data-testid="review-bar"`, `historical-tag`, `source-name`, `session-picker`. Give the picker `data-testid="range-picker"`.

- [ ] **Step 7: Rewrite the two browser tests** against the new control (open the popover, choose a preset / type a custom range, Apply). They are the same assertions about the *subject summary* changing — only the driving is different.

- [ ] **Step 8: Gates** — `npx vitest run`, `make web-check`, `nox -s dashboard`.

- [ ] **Step 9: Commit**

```bash
git commit -am "feat(web): replace the range controls with a minute-granularity picker card"
```

---

### Task 8: The remaining surfaces

**Files:**
- Create (vendored): `web/src/components/application/{slideout-menu,empty-state,table,tabs}/*`, `web/src/components/base/{checkbox,tags}/*`
- Delete: `web/src/ui/SlideOver.tsx`, `web/src/shell/EmptyState.tsx` (replaced)
- Modify: `web/src/shell/EventsPanel.tsx`, `web/src/topo/LinkInspector.tsx`, `web/src/pages/SeriesPanel.tsx`, `web/src/pages/SubjectPage.tsx` (`LogTable`, table tabs)
- Test: existing vitest specs; browser suite

- [ ] **Step 1: Vendor**

```bash
cd web && npx untitledui@latest add slideout-menu empty-state table tabs checkbox tags --yes
```

- [ ] **Step 2: Migrate, one surface per commit**

- `ui/SlideOver` → `slideout-menu` (consumers: `EventsPanel`, and `LinkInspector`'s non-modal panel — keep it non-modal; issue #134's occlusion fix must survive, and its browser test is the proof).
- `shell/EmptyState` → `empty-state`.
- `SubjectPage`'s hand-rolled `<table>` → `application/table`; the table-tab strip → `tabs`.
- `SeriesPanel`'s checkboxes → `base/checkbox`; its chips → `base/tags`.
- `ui/Disclosure` has no free-tier equivalent: keep it, restyle onto tokens.

Keep every testid: `events-panel`, `link-inspector`, `empty-state`, `log-table-*`, `log-filter-*`, `series-panel`, and the rest.

- [ ] **Step 3: Gates after each surface** — `npx vitest run`, `npm run typecheck`. After the last: `make web-check` and `nox -s dashboard`.

- [ ] **Step 4: Commit per surface**

---

### Task 9: The token sweep and the visual gate

**Files:**
- Modify: every remaining `.tsx` using `gray-*` / `dark:` (23 files at plan time), `web/src/topo/{TopoLegend,LinkInspector,ImpairPill,EdgeHoverCard,nodes}.tsx`
- Test: `web/src/topo/topoedge.test.tsx` (pins the resolved edge colors — must stay green)

- [ ] **Step 1: Sweep the classes**

`text-gray-500 dark:text-gray-400` → `text-tertiary`, `border-gray-200 dark:border-gray-800` → `border-secondary`, `bg-white dark:bg-gray-950` → `bg-primary`, and so on. When the sweep is done, `grep -r "dark:" web/src --include=*.tsx` should return **nothing** outside vendored `src/components/**`.

The topology edge custom properties in `app.css` (`--topo-edge-*`, flipped by `.dark`) STAY: `LinkEdge.tsx` builds inline SVG style objects and cannot use Tailwind variants. `topoedge.test.tsx` pins their resolved values and is the guard.

- [ ] **Step 2: Restyle the topology chrome** — `TopoLegend`, `LinkInspector`, `ImpairPill`, `EdgeHoverCard` onto Untitled UI tokens and components. The React Flow canvas, its nodes' geometry, and edge routing are untouched.

- [ ] **Step 3: The visual gate (manual, required)**

Build the real bundle (`make web`) and walk every surface in **both themes**: fleet grid, topology (legend, hover card, inspector, minimap), drill-in (charts, series panel, log tables, the new banner), events slide-over, import/export, empty state, and both range controls. The suite asserts DOM, not pixels; the type scale moved app-wide and every surface was restyled, so this is the only thing that can catch a visual regression. Screenshot anything that looks off before changing it.

- [ ] **Step 4: Gates** — `make web-check`, `npx vitest run`, `nox -s dashboard`, `make coverage-hostless`, `make docs`.

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(web): sweep the shell onto Untitled UI semantic tokens"
```

---

### Task 10: Close the follow-ups

**Files:**
- Modify: `todo/monitor-live-streaming-followups.md`, `todo/TODO.md`
- Create: `todo/untitled-ui-adoption-followups.md` (only if anything is left)

- [ ] **Step 1:** Strike items 1 and 2 from `todo/monitor-live-streaming-followups.md` — they are built. Leave items 3–9 (they are still open).
- [ ] **Step 2:** Update `todo/TODO.md`'s "Untitled UI component upgrades" entry: the foundation and the primitives are done; note what remains (`ui/Disclosure` has no free-tier equivalent; PRO components unexplored).
- [ ] **Step 3: Commit**

```bash
git commit -am "docs(todo): close the 5b spec gaps and record the Untitled UI state"
```

---

### Task 11: Tracking Untitled UI upstream (what Dependabot cannot do)

**The problem.** Dependabot already covers the npm packages Untitled UI pulls in —
`.github/dependabot.yml` has `package-ecosystem: "npm", directory: "/web"` on a
weekly schedule, and all seven new deps are ordinary npm packages. But the
**component source is copy-in**: `theme.css`, `cx.ts`, `button.tsx` and the rest
land in our tree as our files, with no version, no manifest, and no lockfile
entry. There is nothing for Dependabot to resolve, so an upstream fix to their
`Select` never reaches us and nothing tells us it exists.

The substitute is a drift check, and it only works because the vendored tree is
byte-exact (Task 1, Step 2 — do not undo that).

**Files:**
- Create: `web/untitledui.lock.json`, `.github/workflows/untitledui-drift.yml`, `scripts/check_untitledui_drift.sh`
- Modify: `todo/untitled-ui-adoption-followups.md`

- [ ] **Step 1: Record provenance**

`npx untitledui@latest` is unpinned, so today's vendor is not reproducible. Write
a manifest we own:

```json
{
  "cli": "untitledui@<the exact version resolved during Task 1 — read it from npx's output or `npm view untitledui version`>",
  "vendoredAt": "2026-07-13",
  "components": ["button", "button-group", "badges", "button-utility", "select", "input", "dropdown", "tooltip", "range-calendar", "date-picker", "slideout-menu", "empty-state", "table", "tabs", "checkbox", "tags"],
  "paths": ["src/components/**", "src/styles/theme.css", "src/utils/cx.ts", "src/hooks/use-breakpoint.ts"],
  "note": "Copy-in source, not an npm dep — Dependabot cannot see it. scripts/check_untitledui_drift.sh re-vendors with the pinned CLI and diffs. Byte-exactness is load-bearing: never hand-edit these files."
}
```

- [ ] **Step 2: The drift script**

`scripts/check_untitledui_drift.sh`: make a temp dir, run the **pinned** CLI
(`npx untitledui@<pinned> add <each component from the manifest> --yes`), and
`diff -r` its output against our vendored paths. Exit non-zero, printing the
diff, when they disagree. Zero output is the pass condition.

- [ ] **Step 3: Prove it detects drift**

Append a single space to a line in `web/src/components/base/buttons/button.tsx`,
run the script — it MUST fail and print that line. Revert. A drift checker that
cannot see drift is the whole failure mode this task exists to prevent.

- [ ] **Step 4: The weekly workflow**

`.github/workflows/untitledui-drift.yml`: `schedule: cron` weekly (match
dependabot's cadence) plus `workflow_dispatch`. Runs the script; on drift, opens
(or updates) an issue titled `Untitled UI upstream drift` with the diff in the
body. It must NOT auto-apply the update — re-vendoring can change class names and
markup, so a human reviews it. This is the deliberate difference from Dependabot's
auto-PR: we get the *notification*, not an unreviewed patch.

- [ ] **Step 5: Commit**

```bash
git commit -am "ci(web): detect Untitled UI upstream drift, which Dependabot cannot see"
```
