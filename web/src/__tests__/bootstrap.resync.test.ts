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
const { installOpenSpanReconciler } = await import("../data/openSpan");
const { startSpan } = await import("../shell/marking");
const { useUiStore } = await import("../ui/uiStore");

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

// #868: a reconnect snapshot is captured when its request starts but applied
// when its response lands. Anything applied in between, such as a Start
// span's POST response, is newer than the snapshot: the resync replays it
// onto the snapshot before the one store write, so the new span, its
// tracking and the server-observed horizon all survive.
describe("bootstrapFromServer's resync vs a mutation answered meanwhile (#868)", () => {
  const SID = "2026-07-01T08-00-00-minimal";
  const OBSERVED = "2026-07-01T09:10:00+00:00"; // the POST response's server stamp
  const CAPTURED_BEFORE = (() => {
    const doc = JSON.parse(MINIMAL) as { sessions: Record<string, unknown>[] };
    for (const session of doc.sessions) session["observed_at"] = "2026-07-01T09:09:59.999999+00:00";
    return JSON.stringify(doc);
  })();
  const span = {
    id: 9,
    timestamp: "2026-07-01T09:09:00+00:00",
    label: "load test",
    source: "manual",
    color: "#888888",
    dash: "dash",
    end_timestamp: null,
  };

  function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
    let resolve: (v: T) => void = () => {};
    const promise = new Promise<T>((r) => {
      resolve = r;
    });
    return { promise, resolve };
  }

  it("a Start span answered while the snapshot is in flight keeps its event, its tracking and the horizon", async () => {
    const post = deferred<Response>();
    const snapshot = deferred<Response>();
    let snapshots = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/mode") {
          return Promise.resolve(jsonResponse({ mode: "live", source: null, editable: true }));
        }
        if (url === "/api/monitor_sessions") {
          snapshots += 1;
          return snapshots === 1
            ? Promise.resolve(new Response(MINIMAL, { status: 200 }))
            : snapshot.promise;
        }
        if (url === `/api/session/${encodeURIComponent(SID)}/event`) return post.promise;
        throw new Error(`unexpected fetch ${url}`);
      }),
    );
    await bootstrapFromServer();
    const uninstall = installOpenSpanReconciler();
    try {
      const started = startSpan("load test");
      const resynced = capturedResync?.();
      post.resolve(
        new Response(JSON.stringify(span), {
          status: 201,
          headers: { "content-type": "application/json", "X-Otto-Observed-At": OBSERVED },
        }),
      );
      await started;
      expect(useUiStore.getState().openSpan).toEqual({ sessionId: SID, eventId: 9 });

      // The snapshot was captured before the POST: it holds no event 9, and
      // its server stamp is earlier than the POST response's.
      snapshot.resolve(new Response(CAPTURED_BEFORE, { status: 200 }));
      await resynced;

      const session = useReviewStore.getState().sessions[0];
      expect(session.events.map((e) => e.id)).toEqual([9]);
      expect(useUiStore.getState().openSpan).toEqual({ sessionId: SID, eventId: 9 });
      expect(session.observedMs).toBe(Date.parse(OBSERVED));
    } finally {
      uninstall();
      useUiStore.setState({ openSpan: null });
    }
  });
});
