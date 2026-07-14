// RangePicker composes Untitled UI's RangeCalendar + InputDateBase (minute
// granularity) + Button into a session-relative time range picker — the
// deliberate exception to "always vendor their own file": Untitled UI's own
// date-range-picker.tsx is day-granularity with wall-clock presets (Today /
// This week / Last year / an "All time" starting in the year 2000), which
// cannot express e.g. a ten-minute session's 12:03 -> 12:09. See
// RangePicker.tsx's header for the full rationale.
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TimeRange } from "../data/exportDoc";
import { RangePicker } from "./RangePicker";

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection/focus-scroll utilities call unconditionally — without this,
// opening the calendar or the session's date fields throws. Same polyfill
// as reviewbar.test.tsx / shell.test.tsx.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

// jsdom doesn't implement `matchMedia` either — the vendored RangeCalendar
// uses `useBreakpoint` (src/hooks/use-breakpoint.ts) to decide 1 vs 2 visible
// months, AND to decide whether it renders its own EXTRA pair of mobile-only
// InputDateBase fields alongside ours (`!isDesktop &&`). This dashboard is
// desktop-only software, so `matches: true` (report every breakpoint as met)
// keeps the calendar off that mobile branch — otherwise there would be four
// `[data-type="minute"]` segments in the DOM (two of ours + RangeCalendar's
// own duplicate pair), not two.
if (typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) never kicks in —
  // without this, a popover portal from one test's render() lingers for the
  // next test's queries (see reviewbar.test.tsx for the same rationale).
  cleanup();
});

// A one-hour session well clear of any DST boundary in any timezone. July 15
// is comfortably mid-month, so the calendar days immediately before/after
// stay in the same visible month grid.
const BOUNDS: TimeRange = {
  from: new Date("2026-07-15T10:00:00").getTime(),
  to: new Date("2026-07-15T11:00:00").getTime(),
};

function triggerButton() {
  // AriaDateRangePicker (react-aria-components) puts `data-testid` on the
  // root wrapper it renders itself, not on the inner trigger `<button>` —
  // same "wrapper, not leaf" situation as the session picker Select
  // documents in reviewbar.test.tsx. Scope to it and query by role.
  return within(screen.getByTestId("range-picker")).getByRole("button");
}

async function openPicker(user: ReturnType<typeof userEvent.setup>) {
  // react-aria's popovers are driven by real pointer events — fireEvent.click
  // does not open them in jsdom. userEvent synthesizes the full
  // pointerdown/pointerup/click sequence react-aria's usePress listens for.
  await user.click(triggerButton());
}

describe("RangePicker", () => {
  it("maps a preset to a session-relative range", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RangePicker bounds={BOUNDS} value={null} onChange={onChange} />);
    await openPicker(user);
    await user.click(screen.getByRole("button", { name: "Last 15m" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledWith({ from: BOUNDS.to - 15 * 60_000, to: BOUNDS.to });
  });

  it("Full clears the range", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const value: TimeRange = { from: BOUNDS.from + 5 * 60_000, to: BOUNDS.to - 5 * 60_000 };
    render(<RangePicker bounds={BOUNDS} value={value} onChange={onChange} />);
    await openPicker(user);
    await user.click(screen.getByRole("button", { name: "Full" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("Apply with zero interaction re-emits null, not the reseeded bounds", async () => {
    // Opening on `value={null}` ("Full") reseeds the in-popover draft to the
    // session's own `bounds` so the calendar/fields have something to show
    // (see RangePicker.tsx's `pending` doc comment) -- but that reseed must
    // stay presentation-only. Apply, with no preset click and no calendar/
    // field interaction in between, is a no-op from the user's point of
    // view and must re-emit the ORIGINAL `null`, not the concrete reseeded
    // range. Either "emits null" or "never calls onChange" would honor that
    // no-op contract; this pins the former, which is what RangePicker.tsx
    // actually implements (commit() re-emits `value` unchanged when
    // `!touched`).
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RangePicker bounds={BOUNDS} value={null} onChange={onChange} />);
    await openPicker(user);
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("highlights the active relative preset via RangePresetButton's own isSelected state", async () => {
    // `Last 15m`/`Last 1h` are rendered with Untitled UI's own
    // `RangePresetButton` (not a plain vendored `Button`, unlike `Full` --
    // see RangePicker.tsx's PRESETS comment): it derives an `isSelected`
    // highlight (a `bg-secondary` class) by comparing its own `value` prop
    // against the in-popover draft it reads off `RangeCalendarContext`. A
    // plain `Button` has no such behavior -- this pins that the highlight
    // actually fires when the currently-open value equals a preset exactly,
    // which only `RangePresetButton` can produce.
    const user = userEvent.setup();
    const value: TimeRange = { from: BOUNDS.to - 15 * 60_000, to: BOUNDS.to }; // exactly "Last 15m"
    render(<RangePicker bounds={BOUNDS} value={value} onChange={vi.fn()} />);
    await openPicker(user);
    const last15m = screen.getByRole("button", { name: "Last 15m" });
    const last1h = screen.getByRole("button", { name: "Last 1h" });
    expect(last15m.className).toContain("bg-secondary");
    expect(last1h.className).not.toContain("bg-secondary");
  });

  it("keeps minute precision -- not a day boundary", async () => {
    // This is the entire reason we did not vendor Untitled UI's own
    // date-range-picker.tsx: a day-granularity calendar cannot express a
    // sub-hour session window. Start from an explicit (non-null) value so
    // the date fields open showing real digits, not empty placeholders,
    // making the arrow-key increment below land on a known minute.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RangePicker bounds={BOUNDS} value={BOUNDS} onChange={onChange} />);
    await openPicker(user);
    // The popover's Dialog portals outside the render() container (a direct
    // child of <body>, not nested under it), so this must query the whole
    // document rather than the narrow `container` render() returns.
    const minuteSegments = document.body.querySelectorAll('[data-type="minute"]');
    // Two minute segments (the "from" field's and the "to" field's) prove
    // granularity="minute" actually produced minute segments in the DOM —
    // if it regressed to day granularity, these would not exist and this
    // assertion fails before we ever get to typing anything.
    expect(minuteSegments.length).toBe(2);
    const fromMinute = minuteSegments[0] as HTMLElement;
    await user.click(fromMinute);
    for (let i = 0; i < 7; i++) {
      await user.keyboard("{ArrowUp}");
    }
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    const applied = onChange.mock.calls[0]?.[0] as TimeRange;
    // BOUNDS.from is exactly on the hour (minute 0); +7 minutes is neither a
    // day boundary nor rounded back to 0 — the emitted range is exactly what
    // was dialed in on the minute segment.
    expect(applied.from).toBe(BOUNDS.from + 7 * 60_000);
    expect(applied.to).toBe(BOUNDS.to);
  });

  it("refuses to emit an inverted range -- Apply is a no-op when 'from' is dialed past 'to'", async () => {
    // Regression (whole-branch review, Important 1): RangePicker.commit()
    // had no cross-field ordering guard. react-aria permits dialing the
    // "from" date field past "to" -- it only marks the fields
    // aria-invalid, it does not block the edit -- so Apply would call
    // onChange with `{ from > to }`, inverting the store's range and
    // silently blanking the whole dashboard (subject page reads "0 series
    // - 0 samples in range"). Mirrors ReviewBar.applyCustom's
    // pre-migration guard at 23e40d6 (`fromMs < toMs`): Apply is a no-op
    // on inversion, same as here.
    const user = userEvent.setup();
    const onChange = vi.fn();
    // A five-minute sub-range well inside BOUNDS (10:00 -> 10:05 of a
    // 10:00-11:00 session) -- small enough that a handful of ArrowUp
    // presses on the "from" minute segment pushes it past "to" without
    // ever leaving [minValue, maxValue].
    const value: TimeRange = { from: BOUNDS.from, to: BOUNDS.from + 5 * 60_000 };
    render(<RangePicker bounds={BOUNDS} value={value} onChange={onChange} />);
    await openPicker(user);
    const minuteSegments = document.body.querySelectorAll('[data-type="minute"]');
    const fromMinute = minuteSegments[0] as HTMLElement;
    await user.click(fromMinute);
    // :00 -> :06, past "to"'s :05: pending is now { from: 10:06, to: 10:05 }.
    for (let i = 0; i < 6; i++) {
      await user.keyboard("{ArrowUp}");
    }
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refuses to emit an inverted range -- Apply is a no-op when clamping (not the raw fields) inverts the pair", async () => {
    // Regression (final whole-branch review, in the PREVIOUS fix's own blind
    // spot): the guard above checks the RAW pending values, but commit()
    // emits `clampRange(pending, bounds)` -- and clampRange (exportDoc.ts)
    // clamps `from` UP to bounds.from and `to` DOWN to bounds.to
    // independently, which can invert an already-ordered pair instead of
    // just narrowing it. Here `pending` is 9:00 -> 9:05, entirely BEFORE the
    // 10:00-11:00 session on the SAME side: raw `from < to` holds (9:00 <
    // 9:05), so the raw-values guard alone would wave it through, then
    // clampRange produces {from: 10:00 (clamped up), to: 9:05 (untouched,
    // already <= bounds.to)} -- inverted, same store-blanking failure mode
    // as the raw-inversion case above. The fix clamps FIRST and checks the
    // CLAMPED pair, so this must no-op exactly like the raw case.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RangePicker bounds={BOUNDS} value={BOUNDS} onChange={onChange} />);
    await openPicker(user);
    // hour/minute segments appear twice each, in document order: [from, to].
    const hourSegments = document.body.querySelectorAll('[data-type="hour"]');
    const minuteSegments = document.body.querySelectorAll('[data-type="minute"]');
    const fromHour = hourSegments[0] as HTMLElement;
    const toHour = hourSegments[1] as HTMLElement;
    const toMinute = minuteSegments[1] as HTMLElement;
    // "from": 10:00 -> 9:00 (one hour segment decrement).
    await user.click(fromHour);
    await user.keyboard("{ArrowDown}");
    // "to": 11:00 -> 9:05 (two hour decrements, five minute increments).
    await user.click(toHour);
    await user.keyboard("{ArrowDown}{ArrowDown}");
    await user.click(toMinute);
    for (let i = 0; i < 5; i++) {
      await user.keyboard("{ArrowUp}");
    }
    // pending is now { from: 9:00, to: 9:05 } -- well-ordered raw, both
    // entirely outside [10:00, 11:00] on the early side.
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("cannot choose a range outside the session (minValue/maxValue applied)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<RangePicker bounds={BOUNDS} value={null} onChange={onChange} />);
    await openPicker(user);
    // BOUNDS spans 10:00-11:00 on July 15 -- the 14th and 16th are entirely
    // outside [minValue, maxValue] and must be disabled for selection.
    const dayBefore = screen.getByRole("button", { name: /July 14, 2026/ });
    const dayAfter = screen.getByRole("button", { name: /July 16, 2026/ });
    expect(dayBefore.getAttribute("aria-disabled")).toBe("true");
    expect(dayAfter.getAttribute("aria-disabled")).toBe("true");
  });
});
