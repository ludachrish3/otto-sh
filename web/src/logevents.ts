// Pure log-event table bookkeeping (no DOM, no zustand) — vitest-able in
// isolation, mirroring grouping.ts's role for charts. Rows live per
// (host, tab) key; the backend ring keeps 1000 per key and the DB keeps
// everything, so a 500-row client cap only trims what the table would
// never display anyway.
import type { LogEventRow } from "./api/client";

/** Client store + display cap: the newest rows kept per (host, tab). */
export const MAX_TABLE_ROWS = 500;

/** Store key for one host's one table tab. */
export function logKey(host: string, tab: string): string {
  return `${host}/${tab}`;
}

/** Append a batch for one (host, tab), keeping only the newest MAX_TABLE_ROWS. */
export function appendRows(
  existing: Record<string, LogEventRow[]>,
  host: string,
  tab: string,
  rows: LogEventRow[],
): Record<string, LogEventRow[]> {
  if (rows.length === 0) return existing;
  const key = logKey(host, tab);
  const merged = [...(existing[key] ?? []), ...rows];
  return { ...existing, [key]: merged.slice(-MAX_TABLE_ROWS) };
}

/** Group a /api/data `log_events` snapshot into the per-(host, tab) map. */
export function groupRowsFromData(rows: LogEventRow[]): Record<string, LogEventRow[]> {
  const out: Record<string, LogEventRow[]> = {};
  for (const row of rows) {
    (out[logKey(row.host, row.tab)] ??= []).push(row);
  }
  for (const key of Object.keys(out)) {
    out[key] = out[key].slice(-MAX_TABLE_ROWS);
  }
  return out;
}

/** Newest-first rows whose timestamp or field values contain `filter` (case-insensitive). */
export function visibleRows(rows: LogEventRow[], filter: string): LogEventRow[] {
  const needle = filter.trim().toLowerCase();
  const matched = needle
    ? rows.filter((r) =>
        [r.timestamp, ...Object.values(r.fields)].some((v) => v.toLowerCase().includes(needle)),
      )
    : rows;
  return matched.slice().reverse();
}
