// Plan 5b final review, Finding I3: a reconnect must not silently destroy
// the user's view. data/stream.ts's onerror handler resyncs (re-fetches the
// whole snapshot) BEFORE reopening the SSE connection — bootstrapFromServer
// wires that resync callback to reviewStore's resyncMonitorSessions (NOT
// importMonitorSessions, which unconditionally resets range/activeSessionId
// as a fresh Import/first-boot load correctly should). This drives
// bootstrapFromServer itself (not just the reviewStore action in isolation)
// so a regression that re-wires `resync` back to `hydrate`/
// `importMonitorSessions` in bootstrap.ts fails here too.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

let capturedResync: (() => Promise<unknown>) | null = null;
vi.mock("../data/stream", () => ({
  startStream: (opts: { resync?: () => Promise<unknown> }) => {
    capturedResync = opts.resync ?? null;
    return () => {};
  },
}));

// Imported AFTER the mock so bootstrap.ts picks up the mocked module.
const { bootstrapFromServer } = await import("../data/bootstrap");
const { useReviewStore } = await import("../data/reviewStore");

const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");
// A later snapshot of the same session: a real resync would see MORE data
// (a later `end`, an extra sample) — proves the resync actually replaced
// `sessions`, not just a no-op replay, while range/activeSessionId hold.
const GROWN = (() => {
  const doc = JSON.parse(MINIMAL) as {
    sessions: {
      end: string;
      metrics: { timestamp: string; host: string; label: string; value: number }[];
    }[];
  };
  const session = doc.sessions[0];
  session.end = "2026-07-01T09:00:00Z";
  session.metrics.push({
    timestamp: "2026-07-01T09:00:00Z",
    host: "solo",
    label: "CPU %",
    value: 50,
  });
  return JSON.stringify(doc);
})();

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
    editable: false,
    connection: "connecting",
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  resetStore();
  vi.unstubAllGlobals();
  capturedResync = null;
});

describe("bootstrapFromServer's resync (Finding I3)", () => {
  it("a paused reconnect stays paused with the same range and active session, even though sessions data itself refreshes", async () => {
    let calls = 0;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "live", source: null, editable: true }));
      }
      if (url === "/api/monitor_sessions") {
        calls += 1;
        return Promise.resolve(new Response(calls === 1 ? MINIMAL : GROWN, { status: 200 }));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await bootstrapFromServer();
    expect(useReviewStore.getState().mode).toBe("live");
    expect(capturedResync).not.toBeNull();

    // The user pauses (reviewStore's togglePause pins the currently-derived
    // window into an absolute range) and, separately, could have picked a
    // non-default active session — both must survive a reconnect.
    useReviewStore.getState().actions.togglePause();
    const rangeBefore = useReviewStore.getState().range;
    const activeBefore = useReviewStore.getState().activeSessionId;
    expect(rangeBefore).not.toBeNull(); // actually paused
    expect(activeBefore).not.toBeNull();
    const metricsBeforeResync = useReviewStore.getState().sessions[0].metrics.length;

    // Simulate the reconnect: stream.ts calls the resync callback it was
    // handed before reopening the EventSource.
    await capturedResync?.();

    const state = useReviewStore.getState();
    expect(state.range).toEqual(rangeBefore); // still paused, same window
    expect(state.activeSessionId).toBe(activeBefore); // same session selected
    // The resync was NOT a no-op: sessions data itself moved forward.
    expect(state.sessions[0].metrics.length).toBeGreaterThan(metricsBeforeResync);
    expect(state.sessions[0].endMs).toBe(Date.parse("2026-07-01T09:00:00Z"));
  });
});
