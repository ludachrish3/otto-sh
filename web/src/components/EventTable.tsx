// `kind="table"` tab panel: newest-first log-event rows for the selected
// host, client-side substring filter, capped at MAX_TABLE_ROWS by the
// store slice. v1 by design: no sorting, pagination, or virtualization
// (Phase 4 UX territory), and rows keep flowing while charts are paused.
import { useState } from "react";

import type { LogEventRow } from "../api/client";
import type { TabSpec } from "../api/types.gen";
import { logKey, visibleRows } from "../logevents";
import { useMonitorStore } from "../store";

// Stable [] fallback — see TabBar.tsx's EMPTY_TABS comment (React #185).
const EMPTY_ROWS: LogEventRow[] = [];

/** "2026-07-04T12:00:03+00:00" → "12:00:03" (UTC — deterministic for pins). */
function timeCell(timestamp: string): string {
  return timestamp.slice(11, 19);
}

function EventTable({ tab }: { tab: TabSpec }) {
  const selectedHost = useMonitorStore((s) => s.selectedHost);
  const rows = useMonitorStore(
    (s) => s.logEvents[logKey(selectedHost ?? "", tab.id)] ?? EMPTY_ROWS,
  );
  const [filter, setFilter] = useState("");
  const columns = tab.columns ?? [];
  const visible = visibleRows(rows, filter);

  return (
    <div className="event-table">
      <input
        type="search"
        className="event-table-filter"
        placeholder="Filter rows…"
        value={filter}
        onChange={(e) => {
          setFilter(e.target.value);
        }}
      />
      <table>
        <thead>
          <tr>
            <th>Time</th>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map((row, i) => (
            // Index keys are safe: the list is always re-derived whole from
            // the store snapshot (no per-row identity to preserve).
            // biome-ignore lint/suspicious/noArrayIndexKey: rows are re-derived whole from the store snapshot each render; there is no stable per-row identity to key on (see comment above).
            <tr key={i}>
              <td className="event-table-time">{timeCell(row.timestamp)}</td>
              {columns.map((c) => (
                <td key={c}>{row.fields[c] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default EventTable;
