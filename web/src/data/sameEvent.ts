// web/src/data/sameEvent.ts
// The browser's mirror of event_ops.same_event (src/otto/monitor): the ONE
// "is this the expected state" test for undo/redo. Both sides run the same
// cases (tests/_fixtures/same_event_cases.json). Timestamps compare as
// instants at microsecond precision (observedAtMicros, never Date.parse,
// which collapses a 1 µs difference); style compares in stored form after
// normalizeEventStyle, so `#ABC` equals `#aabbcc` and a legacy "red" is
// itself. `id` is ignored: the caller has already chosen the event by id.
import type { EventRecord } from "../api/export.gen";
import { normalizeEventStyle, storedEventStyle } from "./eventStyle";
import { observedAtMicros } from "./exportDoc";

/** An instant in microseconds; an unparseable stamp keeps its text so two
 * different unparseable values still differ. */
function instant(stamp: string | null | undefined): number | string | null {
  if (stamp == null) return null;
  return observedAtMicros(stamp) ?? stamp;
}

export function sameEvent(a: EventRecord | null, b: EventRecord | null): boolean {
  if (a === null || b === null) return a === b;
  const x = storedEventStyle(normalizeEventStyle(a));
  const y = storedEventStyle(normalizeEventStyle(b));
  return (
    instant(x.timestamp) === instant(y.timestamp) &&
    instant(x.end_timestamp) === instant(y.end_timestamp) &&
    x.label === y.label &&
    x.color === y.color &&
    x.dash === y.dash &&
    x.source === y.source
  );
}
