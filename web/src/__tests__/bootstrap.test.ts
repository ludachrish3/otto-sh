// The shell's ONLY boot fetch: /api/mode -> (review mode) /api/monitor_sessions.
// The soft-fail contract is the crux under test — every transport failure
// (rejection, non-200, unexpected shape) must leave the review store
// untouched and must never throw, since the built dist/ is also served by
// dumb static file servers with no /api/* routes at all.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { bootstrapFromServer } from "../data/bootstrap";
import { useReviewStore } from "../data/reviewStore";

const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");

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
    connection: "connecting",
  });
}

// The stream is only exercised (started) in live mode; stub a no-op
// EventSource so startStream's `new EventSource(url)` doesn't blow up in
// jsdom, which doesn't implement it. No message/open/error is ever fired
// here — these tests are about the boot fetch sequence, not the stream
// itself (see stream.test.ts for that).
class FakeEventSource {
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {}
  close() {}
}

afterEach(() => {
  resetStore();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("bootstrapFromServer", () => {
  it("mode fetch rejects: store untouched, no throw", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(useReviewStore.getState().sessions).toEqual([]);
  });

  it("mode 404: store untouched", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(useReviewStore.getState().sessions).toEqual([]);
  });

  // Live mode also hydrates from /api/monitor_sessions (the live server
  // serves a snapshot of the running session there, same endpoint as
  // review mode) and then attaches the SSE stream — a live boot is no
  // longer "mode only, no document fetch" as it was before this module
  // gained live-mode support.
  it('mode "live": hydrates from the snapshot AND starts the stream', async () => {
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "live", source: null }));
      }
      if (url === "/api/monitor_sessions") {
        return Promise.resolve(new Response(MINIMAL, { status: 200 }));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    await bootstrapFromServer();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const state = useReviewStore.getState();
    expect(state.mode).toBe("live");
    expect(state.sessions).toHaveLength(1);
  });

  // Finding 1 (Plan 5b Task 9 review): a live server with nothing recording
  // (e.g. shell_dash's bare harness, or the docs-capture script) answers
  // /api/monitor_sessions with a 404, not a snapshot. Opening an SSE stream
  // against a server with nothing to serve is pointless — it also
  // contradicts shell_dash's "no boot-time API calls" contract — so a
  // failed/404 hydrate in live mode must start NO stream. It must also
  // leave `mode` at its default `null`: the shell is LIVE only once it has
  // actually hydrated a live session, never before — setting mode="live"
  // regardless (the pre-fix behavior) made a shell that got no data claim
  // it was live anyway, which broke every shell_dash-backed dashboard spec
  // (ReviewBar hides itself once mode==="live"; status-text reads
  // "Reconnecting…" instead of "No data").
  it('mode "live": a 404 hydrate starts NO stream and leaves mode null', async () => {
    const EventSourceCtor = vi.fn(function (this: unknown, url: string) {
      throw new Error(`must not construct EventSource(${url}) after a failed hydrate`);
    });
    vi.stubGlobal("EventSource", EventSourceCtor as unknown as typeof EventSource);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "live", source: null }));
      }
      if (url === "/api/monitor_sessions") {
        return Promise.resolve(new Response(null, { status: 404 }));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(EventSourceCtor).not.toHaveBeenCalled();
    const state = useReviewStore.getState();
    expect(state.mode).toBeNull();
    expect(state.sessions).toEqual([]);
  });

  it("mode review + document 200: hydrates the store via importMonitorSessions", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "review", source: "run.sqlite" }));
      }
      if (url === "/api/monitor_sessions") {
        return Promise.resolve(new Response(MINIMAL, { status: 200 }));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    await bootstrapFromServer();
    const state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(1);
    expect(state.sourceName).toBe("run.sqlite");
  });

  // The crux of the soft-fail contract, stated as its NEGATIVE half: a 200
  // /api/monitor_sessions whose body fails validation is NOT a transport failure and
  // must NOT be swallowed. It goes through the same importMonitorSessions path
  // as a user-chosen file, so the store's importError banner surfaces it.
  // Without this test, wrapping the importMonitorSessions call in a try/catch
  // — an easy thing to do while "hardening" the soft-fail path — would
  // silently delete the banner with nothing going red.
  it("mode review + document 200 but INVALID: surfaces importError, no throw", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "review", source: "run.db" }));
      }
      // 200, valid JSON, but not a format:1 document — parseExportDocument
      // rejects it (unsupported format), exactly as it would for a bad file.
      return Promise.resolve(new Response(JSON.stringify({ format: 99 }), { status: 200 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    const state = useReviewStore.getState();
    expect(state.importError).not.toBeNull();
    expect(state.sessions).toEqual([]);
  });

  it("mode review + document 500: store untouched, no throw", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "review", source: "run.sqlite" }));
      }
      return Promise.resolve(new Response(null, { status: 500 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    const state = useReviewStore.getState();
    expect(state.sessions).toEqual([]);
    // The silent half of the contrast above: an HTTP failure must NOT raise
    // the banner — importMonitorSessions is never reached.
    expect(state.importError).toBeNull();
  });
});
