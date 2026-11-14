# Coverage report: choose the source view's columns (#888)

**Issue:** #888 (covapp layout at narrow desktop widths). This design replaces the issue's remaining "phones" item.
**Status:** approved in conversation by Chris on 2026-11-14.

## Why

People read coverage reports in narrow desktop windows. That means a laptop screen split in half (about 680–720 px), or a browser window someone shrinks below half. Chris ruled that phones are out of scope.

#921 fixed the header and the runs table from 820 px up. Below that, the source view (`FilePage` rendering `CodeView`) is where the report fails. The columns beside the code keep their fixed widths while the source column, the grid's `1fr` track, shrinks:

- the ticket gutter (72 px when the file has tickets);
- the line number (46 px);
- one hit-count column per tier (40 px each, four in the fixture);
- the branches column (96 px).

At 600 px the source reads `#include <stdi`. At 480 px it is two characters.

No single set of columns suits every reader, so the reader chooses. The defaults depend on the window's width.

## Goals

- In a narrow desktop window, the source column gets enough room to read code.
- The reader can show or hide each column beside the code, and choose per-tier counts or one total count.
- The choice holds on every page of the report open in that tab.

## Non-goals

- **Phones.** No touch-specific layout and no compact header menu.
- **Other tables.** The file tree (`DirectoryPage`) and runs table (`RunsPage`) keep their columns and keep scrolling sideways. #921 chose this deliberately for runs, and Chris kept the scope to source views.
- **Hiding individual tiers.** Per tier shows every tier, and Total shows their sum.
- **Remembering choices across reports.** A newly opened report starts from the defaults. Chris's reason: the reader's window width may differ next time.

## Anti-goals

- A hand-rolled menu beside the vendored `Dropdown`, or column-visibility logic restated in more than one builder.
- A choice the reader made being overridden by a resize.
- Hidden columns leaving gaps. (The expanded context panels are already safe: they span the full row with a fixed `pl-[60px]`, `CodeView.tsx:245-249`, independent of the columns.)

## What the reader sees

### The Columns menu

The source card's header gets a **Columns** button, next to **Expand contexts**. It opens a menu built on the vendored `Dropdown` (`web/src/components/base/dropdown/dropdown.tsx`), the same component as the header's ⋮ menu. The menu has three sections.

- **Show columns.** A multiple-selection section (`selectionMode="multiple"`, checkbox indicator) with **Tickets**, **Line numbers**, **Branches** and **Hit counts**.
  - The Tickets item is present when the file *has* tickets: `fileHasTickets`, computed from the data before any visibility choice, with today's past-EOF exclusion (`FilePage.tsx:696`). Hiding Tickets never removes its own item.
- **Hit counts as.** A single-selection section (`selectionMode="single"`) with **Per tier** and **Total**, directly under the Hit counts item. Chris chose (2026-11-14) to keep option A's look: the two items are **styled as a two-segment switch**, side by side, with the selected one filled. Underneath they stay real menu radio items, so arrow keys move between them and screen readers announce the selection. A real switch control nested inside a menu item would break the menu's keyboard and screen-reader behaviour, and react-aria forbids it. The builder checks the side-by-side styling still keeps arrow-key order (Hit counts, Per tier, Total, Reset columns).
  - While Hit counts is off, both items are `isDisabled` (the vendored styling greys them out) and keep their selection.
  - With one tier in the report (or none), the section is disabled and the effective mode is always Per tier at every width, because Per tier and Total would differ only in the header.
- **Reset columns.** An action item that clears every chosen setting (see below) and closes the menu.

Toggling a selection item keeps the menu open, so a reader can change several settings at once (`shouldCloseOnSelect={false}` on the selection sections; Reset columns closes). Both mechanisms are verified in the installed react-aria-components 1.21.1. A `MenuSection` with its own `selectionMode` gets its own selection manager (`Menu.mjs:341`), and `shouldCloseOnSelect` is read per section and item (`Menu.mjs:342,366`). No `Dropdown` in the repo uses selection yet, so this is the first. The vendored `Dropdown.Section` is the bare `AriaMenuSection` (`dropdown.tsx:187`) and passes these props through.

### Defaults

| Window width | Hit counts | Line numbers | Tickets | Branches |
|---|---|---|---|---|
| ≥ 820 px | on, Per tier (today's view) | on | on | on |
| < 820 px | on, **Total** | on | **off** | **off** |

Chris ruled (2026-11-14) that a narrow window shows the bare minimum: the line number, the line's total hit count, and the source. The row's colour already says whether and how a line is covered. Tickets and branches are one click away in the menu.

820 px is #921's boundary. No `web/src` file names it today; the covapp browser suite pins it (`tests/e2e/cov/report_browser/test_spa_layout.py`). The builder adds ONE named constant in `web/src` and uses it everywhere this feature decides "narrow". The browser test checks 819 px and 820 px, so the constant and the Python pin cannot drift apart silently.

### Total mode

Total replaces the per-tier columns with a single **Hits** column: each line's tier counts added together.

- **One resolver for the numbers.** Today `buildCells` (`FilePage.tsx:284-308`) and `buildCellsFocused` (`:320-342`) return React nodes, so their values cannot be summed, and both restate the line-number cell and the tier loop. The builder extracts the numbers first:
  - two small data functions, unfocused and focused, each returning one `{value: number | null, asserted: boolean}` per tier. They keep today's `hideAsserted` zeroing (unfocused), the per-run aggregation (focused), and the bare-product focus's all-null result;
  - ONE renderer that turns those entries plus the settings into cells: the line-number cell, the Per tier cells or the Total cell (hit cells through `HitCell`), and the branches cell;
  - `buildColumns` reading the same settings, so `columns[i]` ↔ `cells[i]` holds by construction.
- **The sum.** If every entry's value is `null`, the total is `null` ("·"). Otherwise null entries count as 0. `HitCell` already renders 0 and null as "·".
- **The asserted marker** (my call). Per tier, `HitCell`'s dashed "asserted" pill means "this number comes only from overrides". The total keeps that meaning. It shows the pill only when every entry with `value > 0` is asserted, and a total that includes any real hits shows plain. With `hideAsserted` on, asserted entries are already 0, so no pill appears. The focused view never marks entries asserted (`:338`), so focused totals never show it. Readers who need the split switch to Per tier.
- The column's header reads **Hits**.

### How long choices last

- **Choices are tracked per setting** (my call). Each setting, whether a column's visibility, Hit counts on/off, or Per tier/Total, is either *chosen* by the reader or follows its *default*.
  - A default follows the window live: crossing 820 px switches the hit-count mode, and shows or hides Tickets and Branches, for each of those settings the reader has not chosen.
  - A chosen setting holds at every width, and a resize never overrides it.
  - Example: a reader who only hides Branches at 1000 px and then narrows to 700 px still gets Total. A snapshot of every setting would bring back the squeeze for a reader who only touched an unrelated item.
  - **Turning Hit counts off fixes the mode.** The mode in effect at that moment becomes a chosen setting, so "keeps its setting" holds across a resize. Consequence: once counts have been turned off and on, the mode no longer follows the width until Reset columns. Sequence to pin in one test: open at 1000 px, turn counts off, resize to 700 px, turn counts on (the result is Per tier), resize back to 1000 px (still Per tier), then Reset columns at 700 px (the result is Total).
- **Reset columns** clears every chosen setting. The defaults then follow the width again.
- Choices apply to every source page of the report open in that tab, and survive moving between pages.
- **Choices live only in memory.** They are not kept across a reload, a new tab, or a different report, and nothing is written to `localStorage`, `sessionStorage` or the URL. This is exactly Chris's scope, and it avoids `sessionStorage`'s copying into duplicated and opened tabs.

## Constraints

- **One place decides visibility.** The settings plus the width resolve once into the effective visibility. `buildColumns`, the cell renderer, and the ticket gutter width (`fileHasTickets && showTickets ? "72px" : "0px"`) all read that one result. Hiding is done by not emitting a column, never by CSS that leaves an empty track. The ticket gutter is the exception: it collapses to its existing `"0px"` width, and its chips are not passed while it is hidden.
- **One settings home** (my call on where). The state lives in ONE small provider mounted once in `App.tsx` beside `FocusProvider`, and the source view reads and writes it only through its hook.
  - It is not added to `focus.tsx`. That module owns *pinned, shareable* values (hash query, `localStorage` by stamp, Back/Forward stamping); these settings have the opposite lifecycle and hold no storage at all.
  - The provider mounts outside the data guard (`App.tsx:122-128`), so it must work before the index loads, as `FocusProvider` does.
- **Accessibility.** The menu uses react-aria selection semantics, so screen readers announce each item's checked or selected state. Keyboard behaviour is the vendored Dropdown's.
- **Docs.** `docs/cli/cov/index.md`, which already carries the report screenshots, explains the Columns menu and the narrow-width default, following docs/contributing.md. Screenshots are clipped to the element.

## Left to the builder

- The menu's exact styling within the Untitled UI set (the mockups are wireframes).
- Whether the width check uses `matchMedia` directly or a hook. The vendored `use-breakpoint.ts` only knows Tailwind's named screens, so 820 px needs the new constant either way.
- Test layout. The existing `FilePage.test.tsx` and the covapp browser suite (`tests/e2e/cov/report_browser/`) are the natural homes.

## What "room back" means (my call)

At 600 px on a file with tickets, today's side columns take about 440 px: ticket 72, line 46, four tiers at 40, branches 96, plus `CodeView`'s 66 px expander track. The narrow defaults keep only line 46, Hits 40 and the 66 px expander track, about 152 px. That is roughly 288 px more for the code. The card's own margins and the source cell's padding take some of the rest, so the readable width is less than the raw track.

The narrow-width browser test pins this, at 600 px on the fixture report, measuring the source cell's border box (its grid track, padding included):
- with the narrow defaults, the source column is at least 270 px wider than with every column shown in Per tier mode (today's layout);
- with every column hidden from the menu, the source cell's border box spans the row's width minus the expander track and the row's 3 px left border (`CodeView.tsx:126`). Measuring the border box keeps the cell's `pl-3.5 pr-3.5` padding (`CodeView.tsx:229`) out of the arithmetic.

Shrinking the 66 px expander track is out of scope.
