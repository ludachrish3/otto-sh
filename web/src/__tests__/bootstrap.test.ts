// The shell's boot: the stream opens first, then /api/mode, then
// /api/monitor_sessions. The soft-fail contract is the crux under test —
// every transport failure (rejection, non-200, unexpected shape, a stream
// that cannot open) must leave the review store untouched and must never
// throw, since the built dist/ is also served by dumb static file servers
// with no /api/* routes at all.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MonitorSourceState } from "../api/export.gen";
import { bootstrapFromServer } from "../data/bootstrap";
import { useReviewStore } from "../data/reviewStore";
import { importOffered } from "../data/source";
import {
  fakeStreamTimers,
  runStreamTimer,
  snapshotResponse,
  sourceState,
  stamp,
} from "./_sourceState";

const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");
const SID = "2026-07-01T08-00-00-minimal";

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
    sourceToken: null,
    source: null,
    pendingSource: null,
    lastSourceStamp: null,
    sourceKindUnresolved: false,
    resyncUnresolved: false,
    covered: null,
    connection: "connecting",
  });
}

/** Every step of the boot, in order: each stream constructed, each fetch. */
let steps: string[] = [];

/** A jsdom stand-in for EventSource. By default it opens a microtask after
 * it is constructed, as a real one does once the server subscribed it;
 * `FakeEventSource.next` makes the next one fail before opening instead (a
 * static server with no /api/stream) or wait for the test to open it. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static next: "open" | "error" | "manual" = "open";
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  url: string;
  constructor(url: string) {
    this.url = url;
    steps.push("stream");
    FakeEventSource.instances.push(this);
    const how = FakeEventSource.next;
    queueMicrotask(() => {
      if (how === "open") this.onopen?.();
      else if (how === "error") this.onerror?.();
    });
  }
  close() {
    this.closed = true;
  }
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}

const lastStream = (): FakeEventSource => {
  const last = FakeEventSource.instances.at(-1);
  if (!last) throw new Error("no stream was opened");
  return last;
};

beforeEach(() => {
  steps = [];
  FakeEventSource.instances = [];
  FakeEventSource.next = "open";
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  for (const es of FakeEventSource.instances) es.close();
  resetStore();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Route = () => Promise<Response>;

/** Stubs fetch with a route table, recording each fetch in `steps`. */
function stubServer(routes: Record<string, Route>) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    steps.push(url);
    const route = routes[url];
    if (!route) throw new Error(`unexpected fetch ${url}`);
    return route();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe("bootstrapFromServer", () => {
  // The stream opened, so an otto server is there: a mode that fails is
  // retried through the stream's reconnect, never the end of the boot.
  it("mode fetch rejects: store untouched, no throw, retried through a reconnect", async () => {
    fakeStreamTimers();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(useReviewStore.getState().sessions).toEqual([]);
    expect(importOffered(useReviewStore.getState())).toBe(false);
    expect(lastStream().closed).toBe(true); // the forced reconnect dropped it
    await runStreamTimer(); // the reconnect's backoff
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("mode 404: store untouched", async () => {
    fakeStreamTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 404 })));
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(useReviewStore.getState().sessions).toEqual([]);
  });

  it('mode "live": hydrates from the snapshot over the open stream', async () => {
    const fetchMock = stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(sourceState())),
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    await bootstrapFromServer();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const state = useReviewStore.getState();
    expect(state.mode).toBe("live");
    expect(state.sessions).toHaveLength(1);
    expect(state.editable).toBe(true);
    expect(lastStream().closed).toBe(false);
  });

  it("the stream opens before the mode and the snapshot are fetched", async () => {
    FakeEventSource.next = "manual";
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(sourceState())),
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    const boot = bootstrapFromServer();
    await Promise.resolve();
    expect(steps).toEqual(["stream"]); // nothing fetched until the stream is subscribed
    lastStream().onopen?.();
    await boot;
    expect(steps).toEqual(["stream", "/api/mode", "/api/monitor_sessions"]);
  });

  // A live server with nothing recording yet answers /api/mode but 404s the
  // snapshot. The mode named the URL's source, so that identity holds: it
  // stays pending (Import is not offered on a live URL), and the sync is
  // retried through the stream's own reconnect, never a second timer.
  it("a live mode whose snapshot 404s keeps the live identity and retries through a reconnect", async () => {
    fakeStreamTimers();
    let snapshot = () => Promise.resolve(new Response(null, { status: 404 }));
    const fetchMock = stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(sourceState())),
      "/api/monitor_sessions": () => snapshot(),
    });
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    let state = useReviewStore.getState();
    expect(state.mode).toBeNull();
    expect(state.sessions).toEqual([]);
    expect(state.pendingSource?.kind).toBe("live");
    expect(importOffered(state)).toBe(false);
    expect(lastStream().closed).toBe(true); // the forced reconnect dropped it
    expect(fetchMock).toHaveBeenCalledTimes(2);

    snapshot = () => Promise.resolve(snapshotResponse(MINIMAL));
    await runStreamTimer(); // the reconnect's backoff
    await vi.waitFor(() => expect(useReviewStore.getState().mode).toBe("live"));
    state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(state.sessions).toHaveLength(1);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(lastStream().closed).toBe(false);
  });

  it("Import is withheld until the first /api/mode answers", async () => {
    const mode = deferred<Response>();
    stubServer({
      "/api/mode": () => mode.promise,
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    const boot = bootstrapFromServer();
    expect(importOffered(useReviewStore.getState())).toBe(false);
    await vi.waitFor(() => expect(steps).toContain("/api/mode"));
    expect(importOffered(useReviewStore.getState())).toBe(false);
    mode.resolve(jsonResponse(sourceState({ mode: "review", kind: "json", source: "a.json" })));
    await boot;
    expect(importOffered(useReviewStore.getState())).toBe(true);
  });

  // No otto server answered at all: the stream could not open, nothing was
  // fetched, and Import loads a file in the tab.
  it("a boot with no server behind it offers Import", async () => {
    FakeEventSource.next = "error";
    const fetchMock = stubServer({});
    await bootstrapFromServer();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(lastStream().closed).toBe(true);
    expect(importOffered(useReviewStore.getState())).toBe(true);
  });

  it("a stream that opened whose first mode fails stays open, retries and recovers", async () => {
    fakeStreamTimers();
    let mode = () => Promise.resolve(new Response(null, { status: 503 }));
    stubServer({
      "/api/mode": () => mode(),
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    await bootstrapFromServer();
    let state = useReviewStore.getState();
    expect(importOffered(state)).toBe(false);
    expect(state.sourceKindUnresolved).toBe(true);
    expect(lastStream().closed).toBe(true); // the forced reconnect dropped it

    mode = () => Promise.resolve(jsonResponse(sourceState()));
    await runStreamTimer(); // the reconnect's backoff
    await vi.waitFor(() => expect(useReviewStore.getState().mode).toBe("live"));
    state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(1);
    expect(importOffered(state)).toBe(false); // judged by the live source now
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(lastStream().closed).toBe(false);
  });

  it("a static server's stream fails before opening and is stopped with no reconnect", async () => {
    fakeStreamTimers();
    FakeEventSource.next = "error";
    const fetchMock = stubServer({});
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(lastStream().closed).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useReviewStore.getState().mode).toBeNull();
  });

  it("boot stores the source token", async () => {
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(sourceState({ token: "served-source" }))),
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL, "served-source")),
    });
    await bootstrapFromServer();
    expect(useReviewStore.getState().sourceToken).toBe("served-source");
  });

  it("a /api/mode body that is not a source state leaves the store untouched", async () => {
    fakeStreamTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ mode: "live", source: null, editable: true }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const state = useReviewStore.getState();
    expect(state.mode).toBeNull();
    expect(state.sourceToken).toBeNull();
    expect(lastStream().closed).toBe(true);
  });

  it("mode review + document 200: hydrates the store", async () => {
    stubServer({
      "/api/mode": () =>
        Promise.resolve(
          jsonResponse(sourceState({ mode: "review", source: "run.sqlite", editable: true })),
        ),
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    await bootstrapFromServer();
    const state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(1);
    expect(state.sourceName).toBe("run.sqlite");
    expect(state.mode).toBe("review");
    expect(state.editable).toBe(true);
  });

  // The crux of the soft-fail contract, stated as its NEGATIVE half: a 200
  // /api/monitor_sessions whose body fails validation is NOT a transport
  // failure and must NOT be swallowed. It goes through the same load path as
  // a user-chosen file, so the store's importError banner surfaces it.
  it("mode review + document 200 but INVALID: surfaces importError, no throw", async () => {
    stubServer({
      "/api/mode": () =>
        Promise.resolve(
          jsonResponse(sourceState({ mode: "review", source: "run.db", editable: false })),
        ),
      // 200, valid JSON, but not a format:1 document — parseExportDocument
      // rejects it (unsupported format), exactly as it would for a bad file.
      "/api/monitor_sessions": () =>
        Promise.resolve(snapshotResponse(JSON.stringify({ format: 99 }))),
    });
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    const state = useReviewStore.getState();
    expect(state.importError).not.toBeNull();
    expect(state.sessions).toEqual([]);
  });

  // An unparsable snapshot from a server that answered its mode leaves the
  // source pending, and the stream open: the next reconnect resyncs it.
  it("a boot that ends invalid keeps the stream open and recovers through it", async () => {
    fakeStreamTimers();
    const json = sourceState({ mode: "review", kind: "json", source: "run.json" });
    let snapshot = () => Promise.resolve(snapshotResponse(JSON.stringify({ format: 99 })));
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(json)),
      "/api/monitor_sessions": () => snapshot(),
    });
    await bootstrapFromServer();
    expect(useReviewStore.getState().importError).not.toBeNull();
    expect(useReviewStore.getState().pendingSource?.token).toBe(json.token);
    expect(lastStream().closed).toBe(false);

    snapshot = () => Promise.resolve(snapshotResponse(MINIMAL));
    lastStream().onerror?.(); // the connection drops
    await runStreamTimer(); // its reconnect
    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe(json.token));
    expect(useReviewStore.getState().pendingSource).toBeNull();
    expect(useReviewStore.getState().sessions).toHaveLength(1);
  });

  it("mode review + document 500: store untouched, no throw", async () => {
    stubServer({
      "/api/mode": () =>
        Promise.resolve(
          jsonResponse(sourceState({ mode: "review", source: "run.sqlite", editable: false })),
        ),
      "/api/monitor_sessions": () => Promise.resolve(new Response(null, { status: 500 })),
    });
    await expect(bootstrapFromServer()).resolves.toBeUndefined();
    const state = useReviewStore.getState();
    expect(state.sessions).toEqual([]);
    // The silent half of the contrast above: an HTTP failure must NOT raise
    // the banner.
    expect(state.importError).toBeNull();
    expect(state.warnings).toEqual([]);
  });
});

// Source frames through the real wiring: stream -> onSource -> admitSource.
// A snapshot that cannot install retries through the stream's own reconnect
// (backoff 1 s), whose open resyncs: no second retry loop.
describe("the boot vs source frames and retries", () => {
  const stateOf = (token: string, n: number) =>
    sourceState({
      mode: "review",
      kind: "json",
      source: `${token}.json`,
      token,
      observed_at: stamp(n),
    });
  const frame = (state: MonitorSourceState) => ({
    format: 1,
    source_state: state,
    observed_at: state.observed_at,
  });

  it("a source frame admitted during the boot sync installs and keeps the stream open", async () => {
    const firstSnapshot = deferred<Response>();
    const admittedSnapshot = deferred<Response>();
    let snapshots = 0;
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(stateOf("A", 10))),
      "/api/monitor_sessions": () => {
        snapshots += 1;
        return snapshots === 1 ? firstSnapshot.promise : admittedSnapshot.promise;
      },
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(snapshots).toBe(1));
    lastStream().emit(frame(stateOf("C", 20)));
    await vi.waitFor(() => expect(snapshots).toBe(2));
    // The boot sync's answer lands first, stale: C is only pending.
    firstSnapshot.resolve(snapshotResponse(MINIMAL, "A", stamp(11)));
    await boot;
    expect(lastStream().closed).toBe(false);
    admittedSnapshot.resolve(snapshotResponse(MINIMAL, "C", stamp(21)));

    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("C"));
    const state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(lastStream().closed).toBe(false);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("a transient snapshot failure after an admission recovers with the stream open", async () => {
    fakeStreamTimers();
    let mode = stateOf("A", 10);
    let snapshot: () => Promise<Response> = () =>
      Promise.resolve(snapshotResponse(MINIMAL, "A", stamp(11)));
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(mode)),
      "/api/monitor_sessions": () => snapshot(),
    });
    await bootstrapFromServer();
    expect(useReviewStore.getState().sourceToken).toBe("A");

    // Another tab switched to B; B's first snapshot read fails.
    mode = stateOf("B", 30);
    snapshot = () => Promise.resolve(new Response(null, { status: 503 }));
    lastStream().emit(frame(stateOf("B", 20)));
    await vi.waitFor(() =>
      expect(useReviewStore.getState().warnings).toContain(
        "Could not load B.json; retrying when the connection returns.",
      ),
    );
    expect(useReviewStore.getState().editable).toBe(false);
    snapshot = () => Promise.resolve(snapshotResponse(MINIMAL, "B", stamp(31)));

    await runStreamTimer(); // the forced reconnect's backoff
    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("B"));
    const state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2); // one forced reconnect
    expect(lastStream().closed).toBe(false);
    expect(state.connection).toBe("live");
  });

  // The first mode named A, so a server is there: a second mode that fails
  // (after a snapshot of B) is retried, never the end of the boot.
  it("a boot whose second mode fails after a mismatch retries through a reconnect and recovers", async () => {
    fakeStreamTimers();
    const modes: (() => Promise<Response>)[] = [
      () => Promise.resolve(jsonResponse(stateOf("A", 10))),
      () => Promise.resolve(new Response(null, { status: 503 })),
    ];
    const later = () => Promise.resolve(jsonResponse(stateOf("A", 30)));
    let snapshotToken = "B";
    stubServer({
      "/api/mode": () => (modes.shift() ?? later)(),
      "/api/monitor_sessions": () =>
        Promise.resolve(snapshotResponse(MINIMAL, snapshotToken, stamp(31))),
    });
    await bootstrapFromServer();
    let state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("A");
    expect(importOffered(state)).toBe(true); // judged by A, a .json source
    expect(lastStream().closed).toBe(true); // the forced reconnect dropped it
    snapshotToken = "A";

    await runStreamTimer(); // the forced reconnect's backoff
    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("A"));
    state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(lastStream().closed).toBe(false);
  });

  it("a boot whose snapshot names another source twice retries through a reconnect", async () => {
    fakeStreamTimers();
    let snapshotToken = "B";
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(stateOf("A", 10))),
      "/api/monitor_sessions": () =>
        Promise.resolve(snapshotResponse(MINIMAL, snapshotToken, stamp(11))),
    });
    await bootstrapFromServer();
    expect(useReviewStore.getState().source).toBeNull();
    expect(lastStream().closed).toBe(true); // the forced reconnect dropped it
    snapshotToken = "A";

    await runStreamTimer(); // the forced reconnect's backoff
    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("A"));
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(lastStream().closed).toBe(false);
  });
});

// The stream is open while the snapshot is in flight, so a push can land
// before the snapshot it is newer than. The snapshot's capture stamp
// (X-Otto-Observed-At) is the line: a push stamped after it is replayed onto
// the snapshot, one stamped at or before it is already in it.
describe.each([
  ["live", sourceState()],
  ["review", sourceState({ mode: "review", kind: "json", source: "run.json" })],
] as [string, MonitorSourceState][])("the boot snapshot vs pushes (%s)", (_mode, mode) => {
  const CAPTURED = stamp(500);
  const event = (id: number) => ({
    id,
    timestamp: "2026-07-01T08:10:00+00:00",
    label: `e${id}`,
    source: "manual",
    color: "#888888",
    dash: "dash",
  });

  async function bootWithHeldSnapshot() {
    const snapshot = deferred<Response>();
    stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(mode)),
      "/api/monitor_sessions": () => snapshot.promise,
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(steps).toContain("/api/monitor_sessions"));
    return { boot, snapshot };
  }

  it("a push stamped after the capture arriving before the snapshot is replayed; one stamped at or before it is dropped", async () => {
    fakeStreamTimers();
    const { boot, snapshot } = await bootWithHeldSnapshot();
    const append = vi.spyOn(useReviewStore.getState().actions, "appendFragments");
    const es = lastStream();
    es.emit({ format: 1, session: SID, events: [event(6)], observed_at: stamp(499) });
    es.emit({ format: 1, session: SID, events: [event(8)], observed_at: CAPTURED });
    es.emit({ format: 1, session: SID, events: [event(7)], observed_at: stamp(501) });
    await runStreamTimer(); // the stream's flush
    // The pushes reached the store before the snapshot resolved: the replay
    // is what puts event 7 onto the snapshot.
    const appended = append.mock.calls.flatMap(([frags]) => frags.map((f) => f.observed_at));
    expect(appended).toEqual([stamp(499), CAPTURED, stamp(501)]);
    expect(useReviewStore.getState().sessions).toEqual([]);

    snapshot.resolve(snapshotResponse(MINIMAL, mode.token, CAPTURED));
    await boot;

    const events = useReviewStore.getState().sessions[0]?.events ?? [];
    expect(events.map((e) => e.id)).toEqual([7]);
  });

  it("a metric fragment stamped at or before the capture is dropped; one after it is appended exactly once", async () => {
    fakeStreamTimers();
    const { boot, snapshot } = await bootWithHeldSnapshot();
    const es = lastStream();
    const point = (minute: number) => ({
      timestamp: `2026-07-01T08:${minute}:00Z`,
      host: "solo",
      label: "CPU %",
      value: minute,
    });
    es.emit({ format: 1, session: SID, metrics: [point(40)], observed_at: CAPTURED });
    es.emit({ format: 1, session: SID, metrics: [point(41)], observed_at: stamp(501) });
    await runStreamTimer(); // the stream's flush

    snapshot.resolve(snapshotResponse(MINIMAL, mode.token, CAPTURED));
    await boot;

    const session = useReviewStore.getState().sessions[0];
    const inSnapshot = (JSON.parse(MINIMAL) as { sessions: { metrics: unknown[] }[] }).sessions[0]
      ?.metrics.length;
    expect(session?.metrics).toHaveLength((inSnapshot ?? 0) + 1);
    const stamps = session?.metrics.map((m) => m.timestamp) ?? [];
    expect(stamps.filter((t) => t === "2026-07-01T08:41:00Z")).toHaveLength(1);
    expect(stamps).not.toContain("2026-07-01T08:40:00Z");
  });
});
