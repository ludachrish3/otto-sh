# Monitor 5b follow-ups — drilled-in unreachable, the live window, and Untitled UI as the foundation

Three items. The first two close spec gaps left open by Plan 5b
(`todo/monitor-live-streaming-followups.md` items 1 and 2, both flagged there as
"needs a ruling"). The third adopts Untitled UI as the shell's component
foundation — not as a wrapper around what we have, but as a replacement for it.

The functional items are built **on** the new foundation, so the order below is
also the build order: foundation, primitives, then the two features, then the
remaining surfaces.

---

## A. The drilled-in unreachable treatment

**The gap.** `SubjectPage` computes no health at all, so a drilled-in dead host
renders its charts normally with no indication it stopped reporting. The fleet
grid and the topology both dim correctly; only the drill-in is missing. The 5b
spec's own words: "A drilled-in unreachable host shows last-known data, frozen
and dimmed, with the *Unreachable for 2m — showing last-known data* banner."

**One rule, one function.** `healthForHosts()` already owns the down rule (`gap >
HEALTH_K × cadence`) and is the *only* place it may live. Add:

```ts
export function healthForHost(
  session: NormalizedSession,
  hostId: string,
  range: TimeRange | null,
  nowMs?: number,
): SubjectHealth
```

and rewrite `healthForHosts` as a loop over it. The drill-in must not pay for the
whole fleet's health to learn about one host, and the rule must not be forked to
achieve that.

**Where it renders.** A host subject shows the banner and dims its chart stack. An
element subject shows a banner naming its unreachable members and does **not**
dim — its healthy members' charts are still live and correct, and dimming them
would lie. Members are named in slot-then-id order, the order `elementRollup`
already uses.

| Subject | Members down | Banner | Dim |
| --- | --- | --- | --- |
| host | — (itself) | `Unreachable for 2m — showing last-known data` | yes |
| element | some or all | `tech2, tech3 unreachable for 2m — showing last-known data` | no |
| either | none | none | no |

**The clock must not wake the page.** A silent host emits nothing, so only a tick
can make the banner appear — but `SubjectPage` deliberately does not subscribe to
`data/clock.ts` (a tick would re-render every chart). The banner therefore lives
in its own component, `SubjectHealthBanner`, which subscribes to the clock itself
and takes the chart stack as `children`. React reuses a parent's already-created
child elements, so a tick re-renders the banner alone and never the chart stack.
A render-count guard pins this: N ticks with no new data must not re-render
`ChartPanel`.

The banner is derived in **both** modes, from the same function. In review, "now"
is the session's own end (an archive's meaning of now), so a host that died
before the run ended reads as unreachable there too — which is exactly what the
fleet grid already says about it in review.

**Duration wording.** `formatOutage(ms)`: `45s` under a minute, else `formatSpan`'s
existing `2m` / `1.5h`. The down threshold is `HEALTH_K × cadence` — 3s at a 1s
interval — so seconds are reachable, and `0m` (what `formatSpan` alone would
print) is not acceptable copy.

There is no free-tier Untitled UI alert component, so the banner is built from
their tokens and `FeaturedIcon`-style markup rather than lifted wholesale.

---

## B. The live window

**The gap.** The 5b spec's state table says "preset chosen → sets `windowMs`; still
following". What shipped: `windowMs` is a `900_000` literal with **no setter**, and
the only presets in the app (`ReviewBar`'s) call `setRange`, which *pins* the view
— i.e. pauses. Live mode has no way to change its 15-minute window.

**The control.** An Untitled UI `ButtonGroup` in the `AppBar`, beside Pause,
rendered only in live mode — the established home for live-only chrome. `5m · 15m
· 1h`, default 15m. The historical `ReviewBar` is unchanged in behavior: its
presets go on pinning ranges, because a pinned range and a follow-window are
different things and the two rows must not disagree (the reason `ReviewBar` is
hidden in live mode at all).

**The action.**

```ts
setWindow: (windowMs: number) => void
```

Following (`range === null`): sets `windowMs`; the derived `liveRange` widens or
narrows on the next render. Still following — the spec's requirement.

Paused (`range !== null`): sets `windowMs` **and** re-pins `range` to that width
ending at the pinned `range.to`. Choosing "1h" while paused zooms around what you
are looking at, instead of silently resuming or silently doing nothing until you
resume. Pause is not disturbed: `paused` is derived from `range !== null`, and
`range` stays non-null.

---

## C. Untitled UI as the component foundation

**The decision.** Untitled UI replaces our hand-rolled primitives wherever it has
an equivalent. `web/src/ui/*` is **deleted**, not wrapped — a wrapper layer would
preserve exactly the maintenance burden this adopts Untitled UI to remove. Call
sites import the vendored Untitled UI components directly.

Untitled UI is copy-in source (`npx untitledui add <name>`), not a package
dependency: the files land in our tree and we own them.

### What we take

| Ours (deleted) | Untitled UI (free tier) |
| --- | --- |
| `ui/Button.tsx` | `base/buttons/button` |
| `ui/Badge.tsx` | `base/badges` |
| `ui/Select.tsx` | `base/select` |
| `ui/TextInput.tsx` | `base/input` |
| `ui/ToggleGroup.tsx` | `base/button-group` |
| `ui/Menu.tsx` (`OverflowMenu`) | `base/dropdown` + `base/buttons/button-utility` |
| `ui/SlideOver.tsx` | `application/slideout-menu` |
| `ui/Disclosure.tsx` | *(no free equivalent)* — kept, restyled onto tokens |
| `shell/EmptyState.tsx` | `application/empty-state` |
| `SubjectPage`'s `LogTable` | `application/table` |
| `SeriesPanel` checkboxes / search / chips | `base/checkbox`, `base/input`, `base/tags` |
| table-tab strip | `application/tabs` |

Plus `base/tooltip` (already pulled in by the date-picker family).

### What stays, and why

- **`@xyflow/react`** — the topology canvas. Untitled UI has no node-graph or flow
  component on any tier; it is a component library, not a canvas. Its *chrome*
  (`TopoLegend`, `LinkInspector`, `ImpairPill`, `EdgeHoverCard`) moves onto
  Untitled UI tokens and components; the canvas itself is untouched.
- **ECharts** — Untitled UI's `charts-base` is Recharts. Swapping would throw away
  Plan 5b's incremental `setOption` merge-patch path, which is the reason a live
  tick doesn't rebuild every chart. Not a candidate.
- **`--color-status-*`** — live/historical/warn/ok/error. These are monitor
  semantics fixed by the UX spec and read by `charts/palette.ts`. They stay ours.

### Tokens replace the `dark:` sprawl

Untitled UI's semantic tokens flip with the theme by themselves, so the
`text-gray-500 dark:text-gray-400` pairs that appear across 23 files collapse to
`text-tertiary`. This is the maintenance win, and it is why the migration is a
class sweep rather than a component-by-component reskin.

### Dependencies added (7)

`@internationalized/date`, `react-aria`, `@react-stately/utils`,
`@untitledui/icons`, `tailwind-merge`, and the two Tailwind plugins their class
names require: `tailwindcss-animate` and `tailwindcss-react-aria-components`.
`react-aria-components` is already a dependency. We skip `typography.css` and
`@tailwindcss/typography`: nothing here renders prose.

`@/*` → `src/*` path aliases go in `tsconfig.json` **and** `vite.config.ts` (Vite
does not read tsconfig paths) **and** `vitest`'s resolve config.

### Two collisions, resolved explicitly

Both are the kind that pass every gate and show up only on screen:

1. **Dark mode.** Untitled UI keys dark tokens on `.dark-mode`; otto historically
   keyed on `.dark`. Resolution: **Untitled UI's class wins outright** — `theme.ts`
   toggles only `.dark-mode`, and our `@custom-variant dark` resolves against it
   (`&:where(.dark-mode, .dark-mode *)`). The Tailwind *variant* is still spelled
   `dark:` — that is the utility prefix, not a class — so nothing else changes.

   We do **not** edit the vendored `theme.css` to say `.dark`, and we do **not**
   toggle both classes: a shadow class is coupling with nobody paying for it, and
   any hand-edit to vendored source forfeits the byte-exactness that the
   upstream-drift check depends on (Untitled UI is copy-in — no version, no
   manifest, nothing Dependabot can resolve; see the drift-check task).

   The class name is a *string*, and nothing type-checks it: besides `theme.ts`,
   `app.css`'s `--topo-edge-*` block, `charts/useIsDark.ts` (which drives ECharts'
   theme and React Flow's `colorMode`), and four Playwright assertions all read it.
   Every one must move together or dark mode silently half-works.

2. **Brand.** Untitled UI's `--color-brand-500` is `#9E77ED` (purple); ours is
   `#7c5cff` (violet), chosen to match the default event color, and `charts/`
   reads it. Resolution: `app.css` imports `theme.css` **first**, then its own
   `@theme` block, so our brand values win and Untitled UI's extra shades
   (25/100/200/400/800/900/950) remain available. A test asserts the *resolved*
   value of `--color-brand-500`, because a token collision that resolves the wrong
   way is invisible to every other gate.

**Accepted, not accidental:** their `@theme` also redefines `--text-xs--line-height`
(1.125rem vs Tailwind's 1rem) and `--text-xl--line-height` (1.875rem vs 1.75rem).
Adopting their type scale is the point of adopting the design system, so we take
it; every `text-xs` in the app gains 2px of line-height. They do **not** redefine
`--color-gray-*`, `--color-neutral-*`, or `--spacing`.

### The range picker — custom, by necessity

We do **not** vendor Untitled UI's `date-range-picker.tsx`. It is built for a SaaS
analytics page: day granularity, presets hardcoded to `Today` / `This week` /
`Last year` / an `All time` that starts in the year 2000. Our range lives *inside a
single run* — minutes to hours, clamped to `sessionBounds`. A day-granularity
calendar cannot express `12:03 → 12:09` of a ten-minute run.

We compose their `range-calendar`, `input-date`, `button` and popover pieces into
`web/src/ui/RangePicker.tsx`, keeping their look and their card layout.

This fixes the directory convention for the whole migration: **`src/components/**`
is vendored Untitled UI source** — never hand-edited (bar the documented
`.dark-mode` fix), and excluded from vitest coverage, because third-party source
must not be measured as if we wrote it. **`src/ui/**` is ours** — components we
author *on top of* Untitled UI, and still coverage-gated. The hand-rolled
primitives currently in `src/ui/` are deleted; what remains there is composition.

```text
[ Jul 13, 12:03 – 12:41  ▾ ]
         │
   ┌─────┴──────────────────────────────┐
   │ Full        │  From [13 Jul 12:03] │
   │ Last 15m    │  To   [13 Jul 12:41] │
   │ Last 1h     │                      │
   │             │   [Cancel]  [Apply]  │
   └────────────────────────────────────┘
```

- **Granularity `minute`** — the whole point.
- **Clamped** with `minValue`/`maxValue` from `sessionBounds`, so a range outside
  the run cannot be chosen (today `clampRange` repairs it after the fact).
- **Presets** are session-relative — `Full` (which is `range = null`, subsuming the
  old Reset), `Last 15m`, `Last 1h` — computed from the session's bounds, not from
  wall-clock `today()`.
- Historical only, like the rest of `ReviewBar`. The live window (B) is a separate
  concept with a separate control.

It replaces the `ReviewBar`'s four loose controls (preset toggle group, two
`datetime-local` inputs, Apply, Reset) with one trigger and the card.

---

## Testing

- **vitest** — `healthForHost` agrees with `healthForHosts` for every host in a
  fixture (the rule is not forked); the banner's copy and member list; the
  dim/no-dim split between host and element subjects; `setWindow` while following
  and while paused; the range picker's preset → range mapping and bound clamping;
  the resolved `--color-brand-500`.
- **render-count guard** — with the clock ticking and no new data, `ChartPanel`
  re-renders zero times while the banner re-renders. This guard must be proven
  able to fail: delete the `children` indirection (subscribe `SubjectPage` to the
  clock directly) and it must go red.
- **browser (`nox -s dashboard`, all three engines)** — a host that stops reporting
  mid-stream grows its banner on the drill-in while the fleet grid dims it; the
  window group widens the live x-axis; the range picker pins a sub-minute range on
  an archive. The existing suite's `data-testid`s are preserved through the
  migration wherever the control survives in spirit, so a green suite means the
  migration kept the app working — the primary safety net for a change this wide.
- **visual** — the type scale moves app-wide and every surface is restyled. The
  dashboard suite asserts DOM, not pixels, so a built-bundle walkthrough of every
  surface (fleet, topology, drill-in, events, import/export, both themes) is a
  required manual gate before merge.
- **`make web-check`** — lint + format + typecheck + coverage. Plan 5b landed CI
  red because no hostless gate runs Biome's format check; this branch touches
  `web/` in almost every commit, so it runs before every push.

## Out of scope

- Persisting the chosen live window across reloads.
- `windowMs` in review mode (there is no follow window to size).
- Untitled UI PRO components. Everything above is on the free tier.
