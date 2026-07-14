// The import-path contract, tested against the REAL committed fixtures —
// the same files the Playwright specs and manual dev use.
//
// The "every committed fixture" test below reads the DIRECTORY rather than a
// hand-written list of imports, so its name is true by construction: a new
// fixture is covered the moment it is committed. (node:fs, not Vite's
// import.meta.glob — tsconfig has no `vite/client` in `types`, and readFileSync
// is already the idiom in topology.test.ts and pages.test.tsx.)
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import kitchenDoc from "../../fixtures/kitchen-sink.json";
import {
  ExportParseError,
  metricsForSubject,
  parseExportDocument,
  presetRange,
  sessionBounds,
  subjectKind,
} from "../data/exportDoc";

function metric(host: string, label: string, timestamp: string, value: number) {
  return { host, label, timestamp, value };
}

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), "../../fixtures");

const parse = (doc: unknown) => parseExportDocument(JSON.stringify(doc));

describe("parseExportDocument", () => {
  it("rejects non-JSON", () => {
    expect(() => parseExportDocument("not json")).toThrow(ExportParseError);
  });

  it("rejects a legacy document without a format field", () => {
    expect(() => parse({ metrics: [], events: [] })).toThrow(/unversioned|format/i);
  });

  it("rejects unknown format versions", () => {
    expect(() => parse({ format: 2, sessions: [] })).toThrow(/format 2/);
  });

  it("parses every committed fixture without warnings", () => {
    const files = readdirSync(FIXTURES).filter((name) => name.endsWith(".json"));
    // Guard the guard: an empty glob would make every assertion below vacuous.
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const result = parseExportDocument(readFileSync(join(FIXTURES, file), "utf-8"));
      expect(result.warnings, file).toEqual([]);
      expect(result.sessions.length, file).toBeGreaterThan(0);
    }
  });

  it("normalizes omitted optional sections to empty defaults", () => {
    const result = parse({
      format: 1,
      sessions: [{ id: "bare", start: "2026-07-01T08:00:00Z" }],
    });
    const s = result.sessions[0];
    expect(s.lab.hosts).toEqual([]);
    expect(s.metrics).toEqual([]);
    expect(s.elements).toEqual([]);
    expect(s.endMs).toBe(s.startMs); // open session with no samples
  });

  it("warns on duplicate session ids", () => {
    const dup = { id: "s", start: "2026-07-01T08:00:00Z" };
    const result = parse({ format: 1, sessions: [dup, dup] });
    expect(result.warnings.some((w) => w.includes("duplicate session id"))).toBe(true);
  });
});

describe("element derivation (kitchen-sink)", () => {
  const session = parse(kitchenDoc).sessions[0];
  const byId = new Map(session.elements.map((e) => [e.id, e]));

  it("derives grouped elements from hosts", () => {
    expect(byId.get("chassis-a")?.hostIds).toHaveLength(3);
    expect(byId.get("chassis-a")?.type).toBe("physical"); // members carry slots
    expect(byId.get("workers")?.type).toBe("logical"); // explicit entry
    expect(byId.get("workers")?.explicit).toBe(true);
  });

  it("includes the explicit zero-host element (empty chassis)", () => {
    expect(byId.get("spare-chassis")?.hostIds).toEqual([]);
    expect(byId.get("spare-chassis")?.type).toBe("physical");
  });

  it("marks single-host elements singleton", () => {
    expect(byId.get("db-01")?.singleton).toBe(true);
    expect(byId.get("chassis-a")?.singleton).toBe(false);
  });

  it("resolves subject kinds host-first", () => {
    expect(subjectKind(session, "chassis-a_lc1")).toBe("host");
    expect(subjectKind(session, "chassis-a")).toBe("element");
    expect(subjectKind(session, "nope")).toBeNull();
  });
});

describe("ranges", () => {
  const session = parse(kitchenDoc).sessions[0];
  const bounds = sessionBounds(session);

  it("bounds span the session", () => {
    expect(bounds.to - bounds.from).toBe(2 * 3600 * 1000);
  });

  it("presets compute from the bounds end", () => {
    const last15 = presetRange(bounds, 15);
    expect(last15).not.toBeNull();
    expect(last15?.to).toBe(bounds.to);
    expect(last15 ? last15.to - last15.from : 0).toBe(15 * 60 * 1000);
    expect(presetRange(bounds, null)).toBeNull(); // Full = no range filter
  });

  it("metricsForSubject filters by subject and range", () => {
    const all = metricsForSubject(session, "workers_w2", null);
    const last15 = metricsForSubject(session, "workers_w2", presetRange(bounds, 15));
    expect(all.length).toBeGreaterThan(last15.length);
    expect(last15.length).toBeGreaterThan(0);
    // element-targeted series resolve through the element id
    expect(metricsForSubject(session, "chassis-a", null).length).toBeGreaterThan(0);
  });
});

describe("meta densification", () => {
  const base = {
    format: 1,
    sessions: [
      {
        id: "s1",
        start: "2026-07-01T08:00:00Z",
        end: "2026-07-01T09:00:00Z",
        meta: { interval: 15.0 }, // present but PARTIAL: no charts/tabs keys
      },
    ],
  };

  it("densifies a present-but-partial meta (follow-up #1)", () => {
    const { sessions } = parseExportDocument(JSON.stringify(base));
    expect(sessions[0].meta.interval).toBe(15.0);
    expect(sessions[0].meta.charts).toEqual([]);
    expect(sessions[0].meta.tabs).toEqual([]);
  });

  it("densifies an absent meta", () => {
    const doc = structuredClone(base);
    delete (doc.sessions[0] as Record<string, unknown>).meta;
    const { sessions } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].meta).toEqual({ interval: null, charts: [], tabs: [] });
  });
});

// Plan 5b follow-ups #5 (the important one): a single malformed timestamp
// must never silently poison a whole session. `parseTs` is `Date.parse`,
// which returns NaN for any malformed/non-ISO string, and nothing validated
// it before this fix — a NaN `endMs` then flowed into sessionBounds/
// clampRange/presetRange, and a NaN entry in seriesIndex.ts's `tsMs` arrays
// silently broke the binary search's ascending-order assumption (wrong
// slicing, not a crash). The fix: a session's OWN anchor (start, and end
// when present) is fail-loud (ExportParseError); an individual metric/
// event/log row is dropped with a warning, matching the file's own
// "duplicate id warns, not fails" philosophy.
describe("timestamp validation (follow-up #5)", () => {
  it("a malformed session start throws ExportParseError", () => {
    const doc = {
      format: 1,
      sessions: [{ id: "bad-start", start: "not-a-timestamp" }],
    };
    expect(() => parseExportDocument(JSON.stringify(doc))).toThrow(ExportParseError);
    expect(() => parseExportDocument(JSON.stringify(doc))).toThrow(/start/i);
  });

  it("a malformed session end (when present) throws ExportParseError", () => {
    const doc = {
      format: 1,
      sessions: [{ id: "bad-end", start: "2026-07-01T08:00:00Z", end: "also-not-a-timestamp" }],
    };
    expect(() => parseExportDocument(JSON.stringify(doc))).toThrow(ExportParseError);
    expect(() => parseExportDocument(JSON.stringify(doc))).toThrow(/end/i);
  });

  it("one malformed sample among many loads fine, drops that row, and warns with the count", () => {
    const good = Array.from({ length: 20 }, (_, i) =>
      metric("solo", "CPU %", `2026-07-01T08:00:${String(i).padStart(2, "0")}Z`, i),
    );
    const withBad = [
      ...good.slice(0, 10),
      metric("solo", "CPU %", "garbage-timestamp", 999),
      ...good.slice(10),
    ];
    const doc = {
      format: 1,
      sessions: [{ id: "s1", start: "2026-07-01T08:00:00Z", metrics: withBad }],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions).toHaveLength(1);
    expect(sessions[0].metrics).toHaveLength(20); // the bad row dropped, all 20 good ones kept
    expect(sessions[0].metrics.some((m) => m.timestamp === "garbage-timestamp")).toBe(false);
    expect(warnings.some((w) => /dropped 1 metric/i.test(w) && w.includes("s1"))).toBe(true);
  });

  it("endMs is finite when the bad sample is the chronologically last one (no explicit end)", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          metrics: [
            metric("solo", "CPU %", "2026-07-01T08:00:00Z", 1),
            metric("solo", "CPU %", "2026-07-01T08:00:30Z", 2),
            metric("solo", "CPU %", "not-a-real-timestamp", 999), // would-be "last" sample
          ],
        },
      ],
    };
    const { sessions } = parseExportDocument(JSON.stringify(doc));
    expect(Number.isFinite(sessions[0].endMs)).toBe(true);
    expect(sessions[0].endMs).toBe(Date.parse("2026-07-01T08:00:30Z"));
  });

  it("endMs is finite (falls back to startMs) when EVERY sample is malformed", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          metrics: [metric("solo", "CPU %", "nope", 1), metric("solo", "CPU %", "still-nope", 2)],
        },
      ],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].metrics).toHaveLength(0);
    expect(Number.isFinite(sessions[0].endMs)).toBe(true);
    expect(sessions[0].endMs).toBe(sessions[0].startMs);
    expect(warnings.some((w) => /dropped 2 metrics/i.test(w))).toBe(true);
  });

  it("the series index contains no NaN after a bad sample is dropped", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          metrics: [
            metric("solo", "CPU %", "2026-07-01T08:00:00Z", 1),
            metric("solo", "CPU %", "not-a-real-timestamp", 999),
            metric("solo", "CPU %", "2026-07-01T08:00:30Z", 2),
          ],
        },
      ],
    };
    const { sessions } = parseExportDocument(JSON.stringify(doc));
    for (const tsArray of sessions[0].index.tsMs.values()) {
      for (const t of tsArray) expect(Number.isNaN(t)).toBe(false);
    }
  });

  it("a malformed event timestamp is dropped with a warning, not fatal", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          events: [
            { timestamp: "2026-07-01T08:00:00Z", label: "good" },
            { timestamp: "nonsense", label: "bad" },
          ],
        },
      ],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].events).toHaveLength(1);
    expect(sessions[0].events[0].label).toBe("good");
    expect(warnings.some((w) => /dropped 1 event/i.test(w))).toBe(true);
  });

  it("a malformed log_event timestamp is dropped with a warning, not fatal", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          log_events: [
            { timestamp: "2026-07-01T08:00:00Z", host: "solo" },
            { timestamp: "nonsense", host: "solo" },
          ],
        },
      ],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].logEvents).toHaveLength(1);
    expect(warnings.some((w) => /dropped 1 log event/i.test(w))).toBe(true);
  });

  // Finding [3] (5b final follow-ups review): `dropInvalidTimestamps`
  // previously validated only `timestamp`, never `EventRecord.end_timestamp`
  // (a SPAN event's end). A span whose start parses fine but whose end
  // doesn't used to sail through both boundaries untouched, then produce a
  // NaN `toMs` wherever `end_timestamp` is read (charts/options.ts's
  // `eventMarkers`, EventsPanel.tsx) — NaN fails every overlap comparison,
  // so the event silently vanished from every chart, un-warned.
  it("a valid start with a malformed end_timestamp is dropped (not just kept half-broken)", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          events: [
            { timestamp: "2026-07-01T08:00:00Z", label: "good instant" },
            {
              timestamp: "2026-07-01T08:05:00Z",
              end_timestamp: "not-a-real-end",
              label: "broken span",
            },
          ],
        },
      ],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].events).toHaveLength(1);
    expect(sessions[0].events[0].label).toBe("good instant");
    expect(warnings.some((w) => /dropped 1 event/i.test(w))).toBe(true);
  });

  it("a span event with both a valid start and a valid end_timestamp is kept", () => {
    const doc = {
      format: 1,
      sessions: [
        {
          id: "s1",
          start: "2026-07-01T08:00:00Z",
          events: [
            {
              timestamp: "2026-07-01T08:05:00Z",
              end_timestamp: "2026-07-01T08:10:00Z",
              label: "real span",
            },
          ],
        },
      ],
    };
    const { sessions, warnings } = parseExportDocument(JSON.stringify(doc));
    expect(sessions[0].events).toHaveLength(1);
    expect(warnings).toHaveLength(0);
  });
});
