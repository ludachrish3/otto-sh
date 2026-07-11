// The import-path contract, tested against the REAL committed fixtures —
// the same files the Playwright specs and manual dev use. Fixture JSON is
// imported directly (vite/vitest resolve JSON imports).
import { describe, expect, it } from "vitest";

import driftDoc from "../../fixtures/drift.json";
import kitchenDoc from "../../fixtures/kitchen-sink.json";
import minimalDoc from "../../fixtures/minimal.json";
import {
  ExportParseError,
  metricsForSubject,
  parseExportDocument,
  presetRange,
  sessionBounds,
  subjectKind,
} from "../data/exportDoc";

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

  it("parses all three committed fixtures without warnings", () => {
    for (const doc of [kitchenDoc, minimalDoc, driftDoc]) {
      const result = parse(doc);
      expect(result.warnings).toEqual([]);
      expect(result.sessions.length).toBeGreaterThan(0);
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
