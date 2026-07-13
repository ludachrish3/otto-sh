// Plan 5b final review, Finding I2: a live export must be truthful about
// everything streamed in, not just metrics (which happen to survive only by
// accidental array aliasing between the raw boot document and the
// NormalizedSession — see exportDoc.ts's sessionToRecord). This drives the
// REAL exportLoadedDocument() call site (not just the pure data-layer
// helper), so a regression back to serializing the stale rawMonitorSessions
// document fails here even if sessionToRecord itself still exists unused.
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MonitorSessionFragment } from "../api/export.gen";
import { useReviewStore } from "../data/reviewStore";
import { exportLoadedDocument } from "../shell/ImportExport";
import { synthSession } from "./_synth";

interface ExportedSessionDoc {
  format: number;
  sessions: {
    id: string;
    chart_map?: Record<string, string>;
    meta?: { charts?: { label: string }[] };
    events?: { label?: string }[];
  }[];
}

function captureExportedDocument(): () => Promise<ExportedSessionDoc> {
  let captured: Blob | null = null;
  vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
    captured = blob as Blob;
    return "blob:mock";
  });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  return async () => {
    if (captured === null) throw new Error("exportLoadedDocument never called URL.createObjectURL");
    const text = await (captured as Blob).text();
    return JSON.parse(text) as ExportedSessionDoc;
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    activeSessionId: null,
    mode: null,
  });
});

describe("live export truthfulness", () => {
  it("a live export taken after a fragment carrying new chart_map/meta/events contains them", async () => {
    const bootSession = {
      ...synthSession({ hosts: 1, seriesPerHost: 1, ticks: 1, intervalS: 5 }),
      id: "s",
    };
    // Boot-time raw document (what /api/monitor_sessions served before any
    // streaming happened) — deliberately stale, matching bootstrap.ts's real
    // behavior: rawMonitorSessions is set once at import/hydrate time and
    // never touched again.
    useReviewStore.setState({
      sessions: [bootSession],
      rawMonitorSessions: { format: 1, sessions: [{ id: "s", start: "2026-07-12T00:00:00Z" }] },
      sourceName: "live",
      activeSessionId: "s",
      mode: "live",
    });

    const frag: MonitorSessionFragment = {
      format: 1,
      session: "s",
      metrics: [],
      events: [{ id: 1, timestamp: "2026-07-12T00:05:00Z", label: "boot" }] as never,
      log_events: [],
      deleted_event_ids: [],
      chart_map: { "proc/999": "CPU" },
      meta: {
        interval: 5,
        charts: [
          { label: "proc/999", y_title: "%", unit: "%", command: "top", chart: "CPU" },
        ] as never,
        tabs: [],
      },
    };
    useReviewStore.getState().actions.appendFragment(frag);

    const readExported = captureExportedDocument();
    exportLoadedDocument();
    const doc = await readExported();

    expect(doc.format).toBe(1);
    const exported = doc.sessions.find((s) => s.id === "s");
    expect(exported).toBeDefined();
    expect(exported?.chart_map?.["proc/999"]).toBe("CPU");
    expect(exported?.meta?.charts?.some((c) => c.label === "proc/999")).toBe(true);
    expect(exported?.events?.some((e) => e.label === "boot")).toBe(true);
  });
});
