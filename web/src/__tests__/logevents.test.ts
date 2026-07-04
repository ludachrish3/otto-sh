import { describe, expect, it } from "vitest";

import type { LogEventRow } from "../api/client";
import { appendRows, groupRowsFromData, logKey, MAX_TABLE_ROWS, visibleRows } from "../logevents";

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

describe("appendRows", () => {
  it("appends under the (host, tab) key without touching other keys", () => {
    const first = appendRows({}, "host1", "syslog", [row(1)]);
    const second = appendRows(first, "host2", "syslog", [row(2, "host2")]);
    expect(second["host1/syslog"]).toHaveLength(1);
    expect(second["host2/syslog"]).toHaveLength(1);
  });

  it("caps at MAX_TABLE_ROWS keeping the newest", () => {
    const many = Array.from({ length: MAX_TABLE_ROWS + 20 }, (_, i) => row(i));
    const out = appendRows({}, "host1", "syslog", many);
    const kept = out["host1/syslog"];
    expect(kept).toHaveLength(MAX_TABLE_ROWS);
    expect(kept[kept.length - 1].fields.message).toBe(`row ${MAX_TABLE_ROWS + 19}`);
    expect(kept[0].fields.message).toBe("row 20");
  });

  it("returns the same object for an empty batch", () => {
    const existing = { "host1/syslog": [row(1)] };
    expect(appendRows(existing, "host1", "syslog", [])).toBe(existing);
  });
});

describe("groupRowsFromData", () => {
  it("groups a /api/data snapshot by (host, tab) and caps each", () => {
    const rows = [row(1), row(2, "host2"), row(3)];
    const grouped = groupRowsFromData(rows);
    expect(grouped["host1/syslog"].map((r) => r.fields.message)).toEqual(["row 1", "row 3"]);
    expect(grouped["host2/syslog"]).toHaveLength(1);
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
