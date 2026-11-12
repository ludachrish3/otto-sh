// web/src/data/sourceChangedError.ts
// A write refused because it named a source the server no longer serves.
import type { MonitorSourceState } from "../api/export.gen";
import { EventApiError } from "./eventApiError";

/** The server refused the write as `source_changed`: the token it sent is
 * not the served source's. `state` is the source the server serves now,
 * which the tab admits (data/source.ts's admitSource). */
export class SourceChangedError extends EventApiError {
  readonly state: MonitorSourceState;

  constructor(
    message: string,
    state: MonitorSourceState,
    init: { status?: number | null; observed?: string | null },
  ) {
    super(message, init);
    this.state = state;
  }
}
