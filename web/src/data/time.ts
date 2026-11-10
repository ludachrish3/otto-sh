/** Time helpers for the review UI. All internal times are epoch ms. */
import type { NormalizedSession, TimeRange } from "./exportDoc";

/** A wire or imported ISO-8601 timestamp as epoch ms; NaN when unparsable.
 * The ONE place the browser reads a timestamp string: every server, stream
 * and imported stamp goes through it. otto's convention is that a stamp with
 * no offset means UTC (the server's otto.utils.as_utc), so an offset-less
 * time reads as UTC here too, never as the browser's local time (which is
 * what bare Date.parse does): a legacy capture stored naive then shows the
 * same instants served or imported, in any zone. A stamp that carries `Z` or
 * an offset is read as written. User-typed times are not stamps: the date
 * fields convert their local wall-clock value to ms (ui/calendarTime.ts). */
export function parseTs(iso: string): number {
  return Date.parse(NAIVE_TIME.test(iso) ? `${iso}Z` : iso);
}

/** A date-time whose time part ends with no `Z` or `±hh:mm` offset. */
const NAIVE_TIME = /T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/u;

/** The live window: follow the tail unless the view is pinned. `nowMs` is
 * the reference instant — callers pass `displayEndMs(session)` rather than a
 * raw wall clock, so the window advances exactly when the server reports and
 * never races the browser's clock. */
export function liveRange(nowMs: number, windowMs: number): TimeRange {
  return { from: nowMs - windowMs, to: nowMs };
}

/** The session's display horizon: the later of its last metric (`endMs`)
 * and the server's last observation of it (`observedMs`, from `observed_at`).
 * The ONE derivation behind the live window, Pause and the session's bounds
 * (#866): a mark made while the collector stalls is past `endMs`, and would
 * otherwise wait for the next metric to show. Event timestamps never enter
 * it, so a far-future event stays off-window until server time reaches it.
 * Health and freshness read `endMs` alone (data/health.ts). */
export function displayEndMs(session: NormalizedSession): number {
  return Math.max(session.endMs, session.observedMs);
}

/** A row's clock time, wherever a list shows one (the Events panel, a label
 * cluster's overflow list, a log table's time cell): 24-hour `HH:MM:SS` in
 * local time, matching the chart's x-axis. Padded by hand rather than via
 * toLocaleTimeString, whose `hour12: false` renders midnight as "24" in some
 * engines. Accepts epoch ms or a wire timestamp (read by parseTs). */
export function formatClockTime(t: number | string): string {
  const d = new Date(typeof t === "string" ? parseTs(t) : t);
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
