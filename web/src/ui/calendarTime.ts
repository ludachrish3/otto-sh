// Epoch-ms <-> react-aria CalendarDateTime conversions, shared by any date
// field driven off a plain ms number (RangePicker's popover fields, the
// EventEditor's start/end fields). Extracted from RangePicker.tsx (Task 7)
// verbatim -- a pure move, not a rewrite: both call sites still resolve the
// local timezone via `getLocalTimeZone()`, so a ms value round-trips through
// a CalendarDateTime and back to the exact same instant.
import {
  type CalendarDateTime,
  fromDate,
  getLocalTimeZone,
  toCalendarDateTime,
} from "@internationalized/date";
import type { DateValue } from "react-aria-components";

export function msToCalendarDateTime(ms: number): CalendarDateTime {
  return toCalendarDateTime(fromDate(new Date(ms), getLocalTimeZone()));
}

export function calendarDateTimeToMs(value: DateValue): number {
  return value.toDate(getLocalTimeZone()).getTime();
}
