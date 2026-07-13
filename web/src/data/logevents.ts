// Pure log-event table bookkeeping (no DOM, no zustand) — re-homed against
// the format:1 stack (Task 12) from the legacy src/logevents.ts. Only the
// pieces SubjectPage.tsx's log-table tabs still use survive the port:
// `appendRows` (the legacy zustand store's incremental SSE-batch append) and
// `MAX_TABLE_ROWS`-as-append-cap had no caller left once src/store.ts was
// deleted — the new stack's fragment.ts just pushes every row onto
// session.logEvents and SubjectPage re-derives the per-(host, tab) table
// fresh each render via `groupRowsFromData`, which applies the same cap.

/** One log-event row, normalized from a format:1 `LogEventRecord` (whose
 * host/tab/fields are optional on the wire — see data/exportDoc.ts). */
export interface LogEventRow {
  timestamp: string;
  host: string;
  tab: string;
  fields: Record<string, string>;
}

/** Client-side display cap: the newest rows kept per (host, tab). */
export const MAX_TABLE_ROWS = 500;

/** Store key for one host's one table tab. */
export function logKey(host: string, tab: string): string {
  return `${host}/${tab}`;
}

/** Group a session's log-event rows into the per-(host, tab) map, newest
 * MAX_TABLE_ROWS kept per key. */
export function groupRowsFromData(rows: LogEventRow[]): Record<string, LogEventRow[]> {
  const out: Record<string, LogEventRow[]> = {};
  for (const row of rows) {
    const key = logKey(row.host, row.tab);
    out[key] ??= [];
    out[key].push(row);
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
