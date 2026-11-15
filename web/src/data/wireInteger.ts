// The ONE rule for each integer on the wire, the dashboard's leg of the
// server's: a JSON integer only, never a bool, a fraction or a numeric string
// (`WireId` / `WireCount` in src/otto/models/monitor.py, #1035).

/** Whether `v` is an event id: an integer from 1 to the largest a browser
 * holds exactly, the range the server refuses a document outside
 * (otto.monitor.review's `check_editable_document`). Never negative, so it
 * never meets the negative row id of a view-only row (exportDoc.ts's
 * `eventRowId`). */
export function isWireId(v: unknown): v is number {
  return Number.isSafeInteger(v) && (v as number) >= 1;
}

/** Whether `v` is a source count (`unsaved`, `revision`): a non-negative
 * integer. */
export function isWireCount(v: unknown): v is number {
  return Number.isSafeInteger(v) && (v as number) >= 0;
}
