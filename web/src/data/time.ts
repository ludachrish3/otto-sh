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

/** A row's clock time, wherever a list shows one (the Events panel, a label
 * cluster's overflow list, a log table's time cell): 24-hour `HH:MM:SS` in
 * local time, matching the chart's x-axis. Padded by hand rather than via
 * toLocaleTimeString, whose `hour12: false` renders midnight as "24" in some
 * engines. Accepts epoch ms or anything `Date` parses (a wire timestamp). */
export function formatClockTime(t: number | string): string {
  const d = new Date(t);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

export function formatSpan(fromMs: number, toMs: number): string {
  const mins = Math.round((toMs - fromMs) / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = mins / 60;
  return Number.isInteger(hours) ? `${hours}h` : `${hours.toFixed(1)}h`;
}

/** Outage duration for the unreachable banner. The down threshold is
 * HEALTH_K x cadence — 3s at a 1s interval — so sub-minute outages are
 * reachable and formatSpan's "0m" would be wrong copy. */
export function formatOutage(ms: number): string {
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return formatSpan(0, ms);
}
