/** Time helpers for the review UI. All internal times are epoch ms. */

export function parseTs(iso: string): number {
  return Date.parse(iso);
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
