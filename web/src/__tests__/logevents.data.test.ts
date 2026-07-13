// Pins src/data/logevents.ts — the Task 12 re-homing of the legacy
// src/logevents.ts's still-needed pieces (SubjectPage.tsx's log-table tabs
// call `groupRowsFromData`/`logKey`/`visibleRows`). Named `logevents.data.test.ts`
// (not `logevents.test.ts`, which pinned the now-deleted legacy module and
// also covered `appendRows` — dropped in the port: the new stack's
// fragment.ts pushes every row onto session.logEvents directly, and nothing
// survives that called the legacy store's incremental capped append).
import { describe, expect, it } from "vitest";

import {
  groupRowsFromData,
  logKey,
  type LogEventRow,
  MAX_TABLE_ROWS,
  visibleRows,
} from "../data/logevents";

function row(n: number, host = "host1", tab = "syslog"): LogEventRow {
  return {
    timestamp: `2026-07-04T12:00:${String(n % 60).padStart(2, "0")}+00:00`,
    host,
    tab,
    fields: { proc: "sshd", message: `row ${n}` },
  };
}

describe("logKey", () => {
  it("joins host and tab", () => {
    expect(logKey("host1", "syslog")).toBe("host1/syslog");
  });
});

describe("groupRowsFromData", () => {
  it("groups a session's log-event rows by (host, tab)", () => {
    const rows = [row(1), row(2, "host2"), row(3)];
    const grouped = groupRowsFromData(rows);
    expect(grouped["host1/syslog"].map((r) => r.fields.message)).toEqual(["row 1", "row 3"]);
    expect(grouped["host2/syslog"]).toHaveLength(1);
  });

  it("caps each key at MAX_TABLE_ROWS, keeping the newest", () => {
    const many = Array.from({ length: MAX_TABLE_ROWS + 20 }, (_, i) => row(i));
    const grouped = groupRowsFromData(many);
    const kept = grouped["host1/syslog"];
    expect(kept).toHaveLength(MAX_TABLE_ROWS);
    expect(kept[kept.length - 1].fields.message).toBe(`row ${MAX_TABLE_ROWS + 19}`);
    expect(kept[0].fields.message).toBe("row 20");
  });
});

describe("visibleRows", () => {
  it("returns newest-first", () => {
    expect(visibleRows([row(1), row(2)], "").map((r) => r.fields.message)).toEqual([
      "row 2",
      "row 1",
    ]);
  });

  it("filters case-insensitively across timestamp and field values", () => {
    const rows = [row(1), { ...row(2), fields: { proc: "cron", message: "JOB ran" } }];
    expect(visibleRows(rows, "job")).toHaveLength(1);
    expect(visibleRows(rows, "sshd")).toHaveLength(1);
    expect(visibleRows(rows, "12:00:01")).toHaveLength(1);
    expect(visibleRows(rows, "nomatch")).toHaveLength(0);
  });
});
