// A reconnect resyncs through the same sync as the boot: the stream
// resubscribes first, then the mode and the snapshot are fetched, in that
// order. The resync must not destroy the user's view (Plan 5b final review,
// Finding I3): it keeps range and activeSessionId, where the boot (a fresh
// load, like Import) resets them. It drives bootstrapFromServer and the real
// stream over a fake EventSource, so a regression that re-wires the resync
// onto a fresh load fails here too.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MonitorSourceState } from "../api/export.gen";
import { bootstrapFromServer } from "../data/bootstrap";
import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { admitSource, syncFromServer } from "../data/source";
import { downloadExport } from "../shell/ImportExport";
import { startSpan } from "../shell/marking";
import { useUiStore } from "../ui/uiStore";
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

let steps: string[] = [];

/** A jsdom EventSource that opens a microtask after it is constructed. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  url: string;
  constructor(url: string) {
    this.url = url;
    steps.push("stream");
    FakeEventSource.instances.push(this);
    queueMicrotask(() => {
      if (!this.closed) {
        steps.push("open");
        this.onopen?.();
      }
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

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve: (v: T) => void = () => {};
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

interface Server {
  mode: () => Promise<Response>;
  snapshot: () => Promise<Response>;
}

/** Boots live on `initial`; the test swaps the server's answers via `server`. */
async function bootOn(initial: MonitorSourceState = sourceState(), text = MINIMAL) {
  const server: Server = {
    mode: () => Promise.resolve(jsonResponse(initial)),
    snapshot: () => Promise.resolve(snapshotResponse(text, initial.token, stamp(1))),
  };
  const extra: Record<string, (init?: RequestInit) => Promise<Response>> = {};
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      steps.push(url);
      if (url === "/api/mode") return server.mode();
      if (url === "/api/monitor_sessions") return server.snapshot();
      const route = extra[url];
      if (route) return route(init);
      throw new Error(`unexpected fetch ${url}`);
    }),
  );
  await bootstrapFromServer();
  expect(useReviewStore.getState().sourceToken).toBe(initial.token);
  return { server, extra };
}

beforeEach(() => {
  steps = [];
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  for (const es of FakeEventSource.instances) es.close();
  resetStore();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the reconnect resync", () => {
  it("a reconnect resubscribes, then fetches mode before the snapshot", async () => {
    fakeStreamTimers();
    await bootOn();
    steps = [];

    lastStream().onerror?.();
    expect(useReviewStore.getState().connection).toBe("disconnected");
    await vi.advanceTimersByTimeAsync(1000); // the first backoff
    await vi.waitFor(() => expect(steps).toContain("/api/monitor_sessions"));

    expect(steps).toEqual(["stream", "open", "/api/mode", "/api/monitor_sessions"]);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("a paused reconnect stays paused with the same range and active session, even though sessions data itself refreshes", async () => {
    const { server } = await bootOn();
    server.snapshot = () => Promise.resolve(snapshotResponse(GROWN, "test-token", stamp(2)));

    useReviewStore.getState().actions.togglePause();
    const rangeBefore = useReviewStore.getState().range;
    const activeBefore = useReviewStore.getState().activeSessionId;
    expect(rangeBefore).not.toBeNull(); // actually paused
    expect(activeBefore).not.toBeNull();
    const metricsBeforeResync = useReviewStore.getState().sessions[0].metrics.length;

    await syncFromServer({ fresh: false });

    const state = useReviewStore.getState();
    expect(state.range).toEqual(rangeBefore); // still paused, same window
    expect(state.activeSessionId).toBe(activeBefore); // same session selected
    // The resync was NOT a no-op: sessions data itself moved forward.
    expect(state.sessions[0].metrics.length).toBeGreaterThan(metricsBeforeResync);
    expect(state.sessions[0].endMs).toBe(Date.parse("2026-07-01T09:00:00Z"));
  });

  it("a write missed across a reconnect on the same source shows after the resync", async () => {
    const review = sourceState({ mode: "review", kind: "json", source: "run.json" });
    const { server } = await bootOn(review);
    // Another tab added event 42 while this one was disconnected: its push
    // never arrived, but the snapshot holds it.
    const doc = JSON.parse(MINIMAL) as { sessions: Record<string, unknown>[] };
    const [first] = doc.sessions;
    if (!first) throw new Error("minimal has no session");
    first["events"] = [{ id: 42, timestamp: "2026-07-01T08:10:00+00:00", label: "missed" }];
    server.mode = () => Promise.resolve(jsonResponse({ ...review, observed_at: stamp(3) }));
    server.snapshot = () =>
      Promise.resolve(snapshotResponse(JSON.stringify(doc), review.token, stamp(4)));

    lastStream().onopen?.(); // the reconnected stream opens
    await vi.waitFor(() =>
      expect(useReviewStore.getState().sessions[0]?.events.map((e) => e.id)).toEqual([42]),
    );
    expect(useReviewStore.getState().sourceToken).toBe(review.token);
  });

  it("a resync that finds a new token adopts it, keeping one replay token", async () => {
    fakeStreamTimers();
    const { server } = await bootOn(sourceState({ token: "A" }));
    server.mode = () =>
      Promise.resolve(jsonResponse(sourceState({ token: "B", observed_at: stamp(10) })));
    const snapshot = deferred<Response>();
    server.snapshot = () => snapshot.promise;
    const beginResync = vi.spyOn(useReviewStore.getState().actions, "beginResync");

    const resync = syncFromServer({ fresh: false });
    await vi.waitFor(() =>
      expect(steps.filter((s) => s === "/api/monitor_sessions")).toHaveLength(2),
    );
    // A push stamped after the capture arrives while the snapshot is in flight.
    lastStream().emit({
      format: 1,
      session: SID,
      events: [{ id: 5, timestamp: "2026-07-01T08:10:00+00:00", label: "after" }],
      observed_at: stamp(12),
    });
    await runStreamTimer(); // the stream's flush
    snapshot.resolve(snapshotResponse(MINIMAL, "B", stamp(11)));
    await resync;

    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("B");
    expect(state.pendingSource).toBeNull();
    expect(state.sessions[0]?.events.map((e) => e.id)).toEqual([5]);
    expect(beginResync).toHaveBeenCalledTimes(1);
  });
});

// #868: a reconnect snapshot is captured some time after its request starts
// but applied when its response lands. Anything applied in between, such as
// a Start span's POST response, is newer than the snapshot: the resync
// replays it onto the snapshot before the one store write, so the new span,
// its tracking and the server-observed horizon all survive.
describe("the resync vs a mutation answered meanwhile (#868)", () => {
  const OBSERVED = "2026-07-01T09:10:00+00:00"; // the POST response's server stamp
  const CAPTURED = "2026-07-01T09:09:59.999999+00:00";
  const span = {
    id: 9,
    timestamp: "2026-07-01T09:09:00+00:00",
    label: "load test",
    source: "manual",
    color: "#888888",
    dash: "dash",
    end_timestamp: null,
  };

  it("a Start span answered while the snapshot is in flight keeps its event, its tracking and the horizon", async () => {
    const { server, extra } = await bootOn();
    const post = deferred<Response>();
    const snapshot = deferred<Response>();
    extra[`/api/session/${encodeURIComponent(SID)}/event`] = () => post.promise;
    server.snapshot = () => snapshot.promise;
    const uninstall = installOpenSpanReconciler();
    try {
      const started = startSpan("load test");
      const resynced = syncFromServer({ fresh: false });
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
      snapshot.resolve(snapshotResponse(MINIMAL, "test-token", CAPTURED));
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

// #958: an export acknowledges only writes the downloaded file holds. A
// reconnect's resync is unresolved until its snapshot installs: a count frame
// that arrives meanwhile may count a write the tab missed while disconnected,
// so an export then downloads but acknowledges nothing.
describe("the export's acknowledgement vs a resync", () => {
  const TOKEN = "J";
  const json = (n: number, revision: number) =>
    sourceState({
      mode: "review",
      kind: "json",
      source: "run.json",
      token: TOKEN,
      unsaved: revision,
      revision,
      observed_at: stamp(n),
    });
  const countFrame = (state: MonitorSourceState) => ({
    format: 1,
    source_state: state,
    observed_at: state.observed_at,
  });
  const eventFrame = (id: number, n: number) => ({
    format: 1,
    session: SID,
    events: [{ id, timestamp: "2026-07-01T08:10:00+00:00", label: `e${id}` }],
    observed_at: stamp(n),
  });
  /** MINIMAL holding the events `ids`. */
  const withEvents = (ids: number[]) => {
    const doc = JSON.parse(MINIMAL) as { sessions: Record<string, unknown>[] };
    const [first] = doc.sessions;
    if (!first) throw new Error("minimal has no session");
    first["events"] = ids.map((id) => ({
      id,
      timestamp: "2026-07-01T08:10:00+00:00",
      label: `e${id}`,
    }));
    return JSON.stringify(doc);
  };

  /** Every acknowledgement body the tab posted. */
  let acks: unknown[];

  /** Boots on the .json source at revision 0, its acknowledgements recorded. */
  async function bootJson() {
    acks = [];
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:mock");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const booted = await bootOn(json(1, 0));
    booted.extra["/api/source/saved"] = (init) => {
      acks.push(JSON.parse(String(init?.body)));
      return Promise.resolve(jsonResponse(json(99, 0)));
    };
    return booted;
  }

  const eventIds = () => useReviewStore.getState().sessions[0]?.events.map((e) => e.id);

  /** Resolves on the next snapshot install: each one writes a new document. */
  const nextInstall = (): Promise<void> =>
    new Promise((resolve) => {
      const unsubscribe = useReviewStore.subscribe((s, prev) => {
        if (s.rawMonitorSessions === prev.rawMonitorSessions) return;
        unsubscribe();
        resolve();
      });
    });

  /** A snapshot route held until the test answers it; `asked` resolves once
   * the tab has requested it. */
  function heldSnapshot(server: Server) {
    const answer = deferred<Response>();
    const asked = deferred<void>();
    server.snapshot = () => {
      asked.resolve();
      return answer.promise;
    };
    return { answer, asked: asked.promise };
  }

  it("with no resync, an export acknowledges the revision its stream delivered", async () => {
    fakeStreamTimers();
    await bootJson();
    lastStream().emit(eventFrame(1, 10));
    lastStream().emit(countFrame(json(11, 1)));
    await runStreamTimer(); // the stream's flush

    downloadExport("run.json");

    expect(eventIds()).toEqual([1]);
    expect(acks).toEqual([{ token: TOKEN, revision: 1 }]);
  });

  it("an export during a held resync acknowledges nothing, then what the installed snapshot holds", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    // Revision 1 lands while this tab is disconnected: it never sees it.
    lastStream().onerror?.();
    const snapshot = deferred<Response>();
    server.mode = () => Promise.resolve(jsonResponse(json(12, 1)));
    server.snapshot = () => snapshot.promise;
    steps = [];
    await runStreamTimer(); // the reconnect's backoff
    await vi.waitFor(() => expect(steps).toContain("/api/monitor_sessions"));
    // Revision 2 arrives on the new connection while the snapshot is held.
    lastStream().emit(eventFrame(2, 20));
    lastStream().emit(countFrame(json(21, 2)));
    await runStreamTimer(); // the stream's flush
    expect(useReviewStore.getState().revision).toBe(2);
    expect(eventIds()).toEqual([2]); // revision 1's event is missing

    downloadExport("run.json");
    expect(acks).toEqual([]);

    snapshot.resolve(snapshotResponse(withEvents([1]), TOKEN, stamp(15)));
    await vi.waitFor(() => expect(eventIds()).toEqual([1, 2]));
    downloadExport("run.json");
    expect(acks).toEqual([{ token: TOKEN, revision: 2 }]);
  });

  // A fragment the tab cannot read is a gap in what it holds: the count that
  // follows it must not be acknowledged, and the forced resync heals the gap.
  it("a dropped fragment forces a resync: its count is never acknowledged, the resync's install is", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    const dropped = lastStream();
    // Revision 1's push arrives malformed (the tab drops it), then its count.
    dropped.emit({ ...eventFrame(1, 10), events: "not an array" });
    dropped.emit(countFrame(json(11, 1)));
    await vi.advanceTimersByTimeAsync(20); // one frame: a flush would have run
    expect(eventIds()).toEqual([]);

    downloadExport("run.json");
    expect(acks).toEqual([]); // never revision 1, and nothing until an install
    expect(dropped.closed).toBe(true); // the drop forced a reconnect

    // The reconnect's resync is held; revision 2 arrives on the new stream.
    const snapshot = deferred<Response>();
    server.mode = () => Promise.resolve(jsonResponse(json(12, 1)));
    server.snapshot = () => snapshot.promise;
    steps = [];
    await runStreamTimer(); // the reconnect's backoff
    await vi.waitFor(() => expect(steps).toContain("/api/monitor_sessions"));
    lastStream().emit(eventFrame(2, 20));
    lastStream().emit(countFrame(json(21, 2)));
    await runStreamTimer(); // the stream's flush
    expect(eventIds()).toEqual([2]);
    acks = [];
    downloadExport("run.json");
    expect(acks).toEqual([]); // the resync is unresolved

    snapshot.resolve(snapshotResponse(withEvents([1]), TOKEN, stamp(15)));
    await vi.waitFor(() => expect(eventIds()).toEqual([1, 2]));
    downloadExport("run.json");
    expect(acks).toEqual([{ token: TOKEN, revision: 2 }]);
  });

  // A fragment in a format the tab does not read is a gap too: a later
  // readable count must not acknowledge past it. Re-reading the same format
  // would not help, so the tab does not reconnect for it; an install (here
  // a reconnect for another reason) covers again.
  it("an unreadable fragment and its count withhold the acknowledgement until an install", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    const stream = lastStream();
    stream.emit({ ...eventFrame(1, 10), format: 2 });
    stream.emit({ ...countFrame(json(11, 1)), format: 2 });
    stream.emit(eventFrame(2, 20));
    stream.emit(countFrame(json(21, 2)));
    await runStreamTimer(); // the stream's flush
    expect(eventIds()).toEqual([2]);
    expect(useReviewStore.getState().revision).toBe(2);

    downloadExport("run.json");
    expect(acks).toEqual([]);
    expect(stream.closed).toBe(false); // no reconnect for an unreadable format

    server.mode = () => Promise.resolve(jsonResponse(json(22, 2)));
    server.snapshot = () => Promise.resolve(snapshotResponse(withEvents([1, 2]), TOKEN, stamp(23)));
    const installed = nextInstall();
    stream.onerror?.();
    await runStreamTimer(); // the reconnect's backoff
    await installed;
    expect(useReviewStore.getState().resyncUnresolved).toBe(false);
    expect(eventIds()).toEqual([1, 2]);
    downloadExport("run.json");
    expect(acks).toEqual([{ token: TOKEN, revision: 2 }]);
  });

  // A fragment for a session the tab does not hold is not applied either.
  it("a fragment for a session the tab does not hold withholds the acknowledgement and forces a resync", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    const dropped = lastStream();
    dropped.emit({ ...eventFrame(1, 10), session: "not-held" });
    dropped.emit(countFrame(json(11, 1)));
    await runStreamTimer(); // the stream's flush

    downloadExport("run.json");
    expect(acks).toEqual([]);
    expect(dropped.closed).toBe(true); // the drop forced a reconnect

    server.mode = () => Promise.resolve(jsonResponse(json(12, 1)));
    server.snapshot = () => Promise.resolve(snapshotResponse(withEvents([1]), TOKEN, stamp(13)));
    const installed = nextInstall();
    await runStreamTimer(); // the reconnect's backoff
    await installed;
    expect(eventIds()).toEqual([1]);
    downloadExport("run.json");
    expect(acks).toEqual([{ token: TOKEN, revision: 1 }]);
  });

  // A sync in flight when a fragment is lost may have captured its snapshot
  // before it: its install does not cover past the gap; a later one does.
  it("a fragment lost while a resync is in flight withholds past that resync's install", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    lastStream().onerror?.();
    server.mode = () => Promise.resolve(jsonResponse(json(12, 0)));
    const snapshot = heldSnapshot(server);
    await runStreamTimer(); // the reconnect's backoff
    await snapshot.asked;
    const stream = lastStream();
    stream.emit({ ...eventFrame(1, 20), format: 2 });
    stream.emit({ ...countFrame(json(21, 1)), format: 2 });
    stream.emit(countFrame(json(22, 1)));
    await runStreamTimer(); // the stream's flush

    // The snapshot holds event 7, so its install shows.
    const installed = nextInstall();
    snapshot.answer.resolve(snapshotResponse(withEvents([7]), TOKEN, stamp(15)));
    await installed;
    expect(eventIds()).toEqual([7]);
    downloadExport("run.json");
    expect(acks).toEqual([]);
  });

  // Only the current sync's log keeps a fragment for its replay: an obsolete
  // sync never installs, so what only its log holds is lost.
  it("a fragment for an unheld session that only an obsolete sync logged is lost", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    const olderMode = deferred<Response>();
    server.mode = () => olderMode.promise;
    const older = syncFromServer({ fresh: false });
    server.mode = () => Promise.resolve(jsonResponse(json(5, 0)));
    server.snapshot = () => Promise.resolve(snapshotResponse(withEvents([]), TOKEN, stamp(6)));
    expect(await syncFromServer({ fresh: false })).toBe("installed");
    const stream = lastStream();
    stream.emit({ ...eventFrame(1, 10), session: "not-held" });
    stream.emit(countFrame(json(11, 1)));
    await runStreamTimer(); // the stream's flush

    downloadExport("run.json");
    expect(acks).toEqual([]);
    expect(stream.closed).toBe(true); // the loss forced a reconnect
    olderMode.resolve(jsonResponse(json(12, 1)));
    expect(await older).toBe("stale");
    downloadExport("run.json");
    expect(acks).toEqual([]);
  });

  // The current sync's install replays what it logged; a logged fragment for
  // a session its snapshot does not hold cannot apply, and is lost there.
  it("a logged fragment the installing snapshot cannot apply is lost: withheld, and a resync follows", async () => {
    fakeStreamTimers();
    const { server } = await bootJson();
    lastStream().onerror?.();
    server.mode = () => Promise.resolve(jsonResponse(json(12, 0)));
    const snapshot = heldSnapshot(server);
    await runStreamTimer(); // the reconnect's backoff
    await snapshot.asked;
    const stream = lastStream();
    stream.emit({ ...eventFrame(1, 20), session: "not-held" });
    await runStreamTimer(); // the stream's flush
    expect(stream.closed).toBe(false); // logged for the current sync's replay

    const installed = nextInstall();
    snapshot.answer.resolve(snapshotResponse(withEvents([]), TOKEN, stamp(15)));
    await installed;
    expect(useReviewStore.getState().resyncUnresolved).toBe(true);
    expect(stream.closed).toBe(true); // the replay's loss forced a resync
    downloadExport("run.json");
    expect(acks).toEqual([]);
  });

  it("a same-token count that arrives outside the stream is not acknowledged ahead of it", async () => {
    fakeStreamTimers();
    await bootJson();
    // A refusal's fresh counts (revision 3) arrive before the stream has
    // delivered the writes they count.
    await admitSource(json(30, 3));
    expect(useReviewStore.getState().revision).toBe(3);

    downloadExport("run.json");
    expect(acks).toEqual([{ token: TOKEN, revision: 0 }]);
  });
});
