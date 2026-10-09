// Dump spec §13.5 for the browser: every declared monitor-export read version
// has a frozen, populated sample (tests/_fixtures/formats/monitor-export/,
// shared with the Python readers) that parseExportDocument must read for
// meaning, and every declared write version is what documentFromSessions
// stamps. The versions come from exportFormat.ts, which
// tests/unit/models/test_browser_format_mirrors.py holds equal to the Python lists.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  documentFromSessions,
  ExportParseError,
  type NormalizedSession,
  parseExportDocument,
} from "../data/exportDoc";
import { MONITOR_EXPORT_READ_VERSIONS, MONITOR_EXPORT_WRITE_VERSIONS } from "../data/exportFormat";

const SAMPLES = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../../tests/_fixtures/formats/monitor-export",
);
const sample = (version: number) => readFileSync(join(SAMPLES, `${version}.json`), "utf-8");

const MEANING: Record<number, (s: NormalizedSession) => void> = {
  1: (s) => {
    expect(s.id).toBe("2026-07-01T08-00-00-sample");
    expect(s.note).toBe("frozen monitor-export v1 sample");
    expect(s.endMs - s.startMs).toBe(60_000);
    expect(s.lab.hosts.map((h) => h.id)).toEqual(["rack1-a", "rack1-b"]);
    expect(s.lab.links[0]?.endpoints.map((e) => e.host)).toEqual(["rack1-a", "rack1-b"]);
    expect(s.elements.map((e) => [e.id, e.type, e.hostIds])).toEqual([
      ["rack1", "physical", ["rack1-a", "rack1-b"]],
    ]);
    expect(s.meta.charts.map((c) => c.chart)).toEqual(["cpu"]);
    expect(s.metrics.map((m) => m.value)).toEqual([12.5, 25, 50]);
    expect(s.events.map((e) => e.label)).toEqual(["deploy", "outage"]);
    expect(s.logEvents[0]?.fields).toEqual({ level: "error", message: "link flap" });
    expect(s.chartMap).toEqual({ "CPU %": "cpu" });
    expect(s.tunnels[0]?.hops).toEqual(["rack1-a", "rack1-b"]);
  },
};

describe("monitor-export versions (browser)", () => {
  it.each([...MONITOR_EXPORT_READ_VERSIONS])("reads the frozen v%s sample", (version) => {
    const check = MEANING[version];
    if (check === undefined) throw new Error(`no expectation for read version ${version}`);
    const { sessions, warnings } = parseExportDocument(sample(version));
    expect(warnings).toEqual([]);
    expect(sessions).toHaveLength(1);
    check(sessions[0] as NormalizedSession);
  });

  it("refuses a version outside the declared reads", () => {
    const next = Math.max(...MONITOR_EXPORT_READ_VERSIONS) + 1;
    expect(() => parseExportDocument(JSON.stringify({ format: next, sessions: [] }))).toThrow(
      ExportParseError,
    );
  });

  // Today's verdicts on odd input, kept as they are. A raw JSON 1.0 parses to
  // the number 1, so it reads as version 1, as it does in Python. The raw
  // text is spelled out because JSON.stringify(1.0) is "1". A boolean `true`
  // is refused here, while Python accepts it as 1. Aligning that is a later,
  // marked change.
  it("reads a raw JSON 1.0 as version 1", () => {
    const { sessions, warnings } = parseExportDocument('{"format": 1.0, "sessions": []}');
    expect(sessions).toEqual([]);
    expect(warnings).toEqual([]);
  });

  it.each([true, "1", 1.5])("keeps refusing format %s", (bad) => {
    expect(() => parseExportDocument(JSON.stringify({ format: bad, sessions: [] }))).toThrow(
      ExportParseError,
    );
  });

  it.each([...MONITOR_EXPORT_WRITE_VERSIONS])("emits write version %s", (version) => {
    const newest = Math.max(...MONITOR_EXPORT_READ_VERSIONS);
    const doc = documentFromSessions(parseExportDocument(sample(newest)).sessions);
    expect(doc.format).toBe(version);
    expect(parseExportDocument(JSON.stringify(doc)).sessions[0]?.metrics).toHaveLength(3);
  });
});
