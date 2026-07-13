/** Time helpers for the review UI. All internal times are epoch ms. */
import type { TimeRange } from "./exportDoc";

export function parseTs(iso: string): number {
  return Date.parse(iso);
}

/** The live window: follow the tail unless the view is pinned. `nowMs` is
 * the reference instant — callers pass the active session's `endMs` (the
 * latest ingested sample) rather than a raw wall clock, so the window
 * advances exactly when new data arrives and never races a real clock. */
export function liveRange(nowMs: number, windowMs: number): TimeRange {
  return { from: nowMs - windowMs, to: nowMs };
}

/** ms → the value a <input type="datetime-local"> wants, in LOCAL time. */
export function msToLocalInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

export function localInputToMs(value: string): number | null {
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

export function formatSpan(fromMs: number, toMs: number): string {
  const mins = Math.round((toMs - fromMs) / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = mins / 60;
  return Number.isInteger(hours) ? `${hours}h` : `${hours.toFixed(1)}h`;
}
