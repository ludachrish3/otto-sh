// web/src/data/eventChangedError.ts
// A revert refused because the event is no longer what it expected.
import type { EventRecord } from "../api/export.gen";
import { EventApiError } from "./eventApiError";

/** A revert's `expect` no longer holds. `current` is the event as the server
 * has it now: a record, or null when it is gone; never undefined, so a
 * caller hands it straight to sameEvent. */
export class EventChangedError extends EventApiError {
  readonly current: EventRecord | null;

  constructor(
    message: string,
    current: EventRecord | null,
    init: { status?: number | null; observed?: string | null },
  ) {
    super(message, init);
    this.current = current;
  }
}
