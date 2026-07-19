// The historical review range picker: ONE Untitled UI-style card that
// replaces the old preset ButtonGroup + two datetime-local inputs + Apply +
// Reset (see ReviewBar.tsx). Deliberately does NOT vendor Untitled UI's own
// `date-range-picker.tsx` — that file is DAY granularity with wall-clock
// presets (Today / This week / Last year / an "All time" starting in the
// year 2000). A review range lives inside one run (minutes to hours), so a
// day-granularity calendar cannot express e.g. a ten-minute session's
// 12:03 -> 12:09. Instead this composes their EXPORTED pieces — RangeCalendar,
// InputDateBase, Button — the same way date-range-picker.tsx does, with our
// own session-relative presets and minute granularity. That is the one
// deliberate exception on this branch (see task-7-brief.md): a functional
// necessity, not a preference, and it is still composition (their pieces,
// unmodified), not a fork (no hand-written Tailwind imitating what they ship).

import type { CalendarDateTime } from "@internationalized/date";
import { Calendar as CalendarIcon } from "@untitledui/icons";
import { useState } from "react";
import { useDateFormatter } from "react-aria";
import {
  DateRangePicker as AriaDateRangePicker,
  Dialog as AriaDialog,
  Group as AriaGroup,
  Popover as AriaPopover,
} from "react-aria-components";

import {
  RangeCalendar,
  RangePresetButton,
} from "@/components/application/date-picker/range-calendar";
import { Button } from "@/components/base/buttons/button";
import { InputDateBase } from "@/components/base/input/input-date";
import { cx } from "@/utils/cx";
import { clampRange, presetRange, type TimeRange } from "../data/exportDoc";
import { calendarDateTimeToMs, msToCalendarDateTime } from "./calendarTime";

// Session-relative — computed from `bounds` at click time, never from
// wall-clock `today()`. Full ("no range") subsumes the old Reset: applying
// it emits `onChange(null)`, same as the retired Reset button. Full stays a
// plain vendored `Button` below rather than `RangePresetButton` — that
// component's active-highlight compares against a `{start, end}` DateValue
// pair, a shape "no range" cannot produce. The relative presets below DO
// produce well-formed `{start, end}` values, so they use `RangePresetButton`
// and get its built-in active-preset highlight for free.
const RELATIVE_PRESETS = [
  { id: "15m", label: "Last 15m", minutes: 15 },
  { id: "1h", label: "Last 1h", minutes: 60 },
] as const;

type PendingRange = { start: CalendarDateTime; end: CalendarDateTime } | null;

// Never returns null (unlike `PendingRange`) -- every call site passes a
// concrete `TimeRange`. RangePresetButton's `value` prop needs that
// narrower, non-nullable type to line up without a cast.
function msRangeToPending(range: TimeRange): { start: CalendarDateTime; end: CalendarDateTime } {
  return { start: msToCalendarDateTime(range.from), end: msToCalendarDateTime(range.to) };
}

// `presetRange` returns null only for the Full preset (`minutes: null`);
// this helper is called exclusively with a concrete relative-preset minutes
// value (15 or 60), so the `?? bounds` fallback is unreachable in practice —
// it exists only to keep the return type non-null without an unsafe cast.
function relativePresetRange(bounds: TimeRange, minutes: number): TimeRange {
  return presetRange(bounds, minutes) ?? bounds;
}

export interface RangePickerProps {
  /** The session's own span — clamps min/max on the calendar and fields, so
   * a range outside the run cannot be chosen. */
  bounds: TimeRange;
  /** `null` means "Full" (no filter). */
  value: TimeRange | null;
  onChange: (range: TimeRange | null) => void;
}

export function RangePicker({ bounds, value, onChange }: RangePickerProps) {
  const [isOpen, setIsOpen] = useState(false);
  // The in-popover draft. Only committed to `onChange` on Apply — Cancel (or
  // dismissing the popover) discards it, matching the old Apply/Reset
  // controls' semantics. Re-seeded from `value` (or the full session bounds,
  // when "Full" — a `null` value — is currently in effect) every time the
  // popover opens (see `onOpenChange` below), so a previously abandoned edit
  // never reappears and the fields never open on blank placeholders.
  const [pending, setPending] = useState<PendingRange>(() => msRangeToPending(value ?? bounds));
  // Whether the user has actually changed anything since the popover last
  // opened (a preset click, a calendar drag, or a date-field edit) — reset
  // to false on every open, alongside the `pending` reseed above. Apply,
  // when untouched, re-emits `value` UNCHANGED rather than recomputing from
  // `pending`. Without this, opening on Full (`value === null`) reseeds
  // `pending` to a concrete `{start, end}` equal to `bounds` (so the fields
  // and calendar have something to show); clicking Apply with zero
  // interaction would then commit() that concrete range instead of
  // re-emitting `null`, silently breaking "null means Full" for any
  // consumer that branches on it (e.g. `useIsPaused` in reviewStore.ts) and
  // flipping the trigger label on what the user experiences as a no-op.
  const [touched, setTouched] = useState(false);

  const formatter = useDateFormatter({
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });

  const minValue = msToCalendarDateTime(bounds.from);
  const maxValue = msToCalendarDateTime(bounds.to);

  // Apply is a no-op while the pending range is inverted or degenerate
  // (from >= to) -- matches the pre-migration control's guard exactly
  // (ReviewBar.applyCustom at 23e40d6: `if (fromMs < toMs) setRange(...)`).
  // react-aria's date fields let the "from" field be dialed or typed past
  // "to" (it only marks them `aria-invalid`; nothing here blocks the edit
  // itself), so `pending` can legitimately hold an inverted range by the
  // time Apply is pressed. Hand-rolled rather than reading react-aria's own
  // `state.isInvalid` (available via AriaDateRangePicker's render props):
  // react-stately's built-in "range reversed" validation is strict
  // (`end.compare(start) < 0`) and does NOT flag an equal from/to as
  // invalid, while the control this replaces did (`fromMs < toMs`, not
  // `<=`) -- matching it exactly means checking here, not trusting that
  // flag. The popover is deliberately left open (not close()'d) on the
  // no-op path so the user can fix the fields immediately; they already see
  // react-aria's own aria-invalid styling on them (InputDateBase's
  // `ring-error_subtle` treatment) as the signal that something's wrong.
  const commit = (close: () => void) => {
    if (!touched) {
      onChange(value);
      close();
      return;
    }
    if (pending === null) {
      onChange(null);
      close();
      return;
    }
    // Guard what we EMIT, not what we read. Clamping happens after the
    // ordering check would, and it can INVERT an already-ordered pair: a
    // pending 9:00->9:05 against a 10:00-11:00 session passes `from < to`
    // raw, then clamps to {from: 10:00, to: 9:05} -- inverted, and an
    // inverted range in the store blanks the dashboard ("0 samples in
    // range", empty charts, every tile no-data). So clamp first and check
    // the clamped values. (The control this replaced had the same hole; it
    // is fixed here rather than carried forward.)
    const clamped = clampRange(
      { from: calendarDateTimeToMs(pending.start), to: calendarDateTimeToMs(pending.end) },
      bounds,
    );
    if (clamped.from >= clamped.to) return;
    onChange(clamped);
    close();
  };

  const triggerLabel =
    value === null
      ? "Full range"
      : `${formatter.format(new Date(value.from))} – ${formatter.format(new Date(value.to))}`;

  return (
    <AriaDateRangePicker
      aria-label="Time range"
      granularity="minute"
      minValue={minValue}
      maxValue={maxValue}
      shouldCloseOnSelect={false}
      value={pending}
      onChange={(next) => {
        setTouched(true);
        setPending(next);
      }}
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (open) {
          setPending(msRangeToPending(value ?? bounds));
          setTouched(false);
        }
        setIsOpen(open);
      }}
      data-testid="range-picker"
    >
      <AriaGroup>
        <Button size="sm" color="secondary" iconLeading={CalendarIcon}>
          {triggerLabel}
        </Button>
      </AriaGroup>
      <AriaPopover
        placement="bottom start"
        offset={8}
        className={({ isEntering, isExiting }) =>
          cx(
            "origin-(--trigger-anchor-point) will-change-transform",
            isEntering &&
              "duration-150 ease-out animate-in fade-in placement-right:slide-in-from-left-0.5 placement-top:slide-in-from-bottom-0.5 placement-bottom:slide-in-from-top-0.5",
            isExiting &&
              "duration-100 ease-in animate-out fade-out placement-right:slide-out-to-left-0.5 placement-top:slide-out-to-bottom-0.5 placement-bottom:slide-out-to-top-0.5",
          )
        }
      >
        <AriaDialog
          aria-label="Time range"
          className="flex rounded-2xl bg-primary shadow-xl ring ring-secondary_alt focus:outline-hidden"
        >
          {({ close }) => (
            <>
              <div className="flex w-36 flex-col gap-0.5 border-r border-secondary p-3">
                <Button
                  size="sm"
                  color="tertiary"
                  className="justify-start"
                  onPress={() => {
                    setTouched(true);
                    setPending(null);
                  }}
                >
                  Full
                </Button>
                {RELATIVE_PRESETS.map((p) => {
                  const presetValue = msRangeToPending(relativePresetRange(bounds, p.minutes));
                  return (
                    <RangePresetButton
                      key={p.id}
                      value={presetValue}
                      onClick={() => {
                        setTouched(true);
                        setPending(presetValue);
                      }}
                    >
                      {p.label}
                    </RangePresetButton>
                  );
                })}
              </div>
              <div className="flex flex-col">
                {/* minValue/maxValue are NOT repeated here — RangeCalendar
                    reads them from AriaDateRangePicker's own RangeCalendarContext
                    (react-aria-components threads calendarProps.minValue/maxValue
                    straight from the props above), same as InputDateBase below
                    reads its slot from DateFieldContext. One source, not two. */}
                <RangeCalendar />
                <div className="flex items-center justify-between gap-3 border-t border-secondary p-4">
                  <div className="flex items-center gap-2">
                    <InputDateBase slot="start" size="sm" />
                    <div className="text-md text-quaternary">–</div>
                    <InputDateBase slot="end" size="sm" />
                  </div>
                  <div className="flex gap-3">
                    <Button size="sm" color="secondary" onPress={close}>
                      Cancel
                    </Button>
                    <Button size="sm" color="primary" onPress={() => commit(close)}>
                      Apply
                    </Button>
                  </div>
                </div>
              </div>
            </>
          )}
        </AriaDialog>
      </AriaPopover>
    </AriaDateRangePicker>
  );
}
