// A source change is a SOURCE SWITCH (#873, #958): once the tab has moved
// onto another source (a file it loaded during the boot window, or another
// server source it admitted), nothing the old source started before it —
// the SSE stream, a pending boot fetch, an in-flight event mutation — may
// touch the new data, and every source-bound piece of UI state (edit
// rights, the open span, the editor, the sweep, the mark popover) is gone.
// A live or .db source never loads a local file at all (importOffered).
// Boots live through bootstrapFromServer's fetch seam, as bootstrap.test.ts
// and bootstrap.resync.test.ts do, with the stream itself mocked.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonitorSourceState } from "../api/export.gen";
import { snapshotResponse, sourceState, stamp } from "./_sourceState";

interface StreamOpts {
  onOpen?: () => void;
  onFirstError?: () => void;
  onSource?: (s: MonitorSourceState) => void;
}
const stop = vi.fn();
const reconnect = vi.fn();
let streamOpts: StreamOpts | null = null;
// Whether the next stream fails before it ever opens (no otto server).
let streamFails = false;
// The stream opens at once (a microtask later), as a real one does once the
// server subscribes it; a later `streamOpts.onOpen()` is a reconnect.
const startStream = vi.fn((opts: StreamOpts) => {
  streamOpts = opts;
  const fails = streamFails;
  queueMicrotask(() => (fails ? opts.onFirstError?.() : opts.onOpen?.()));
  return { stop, reconnect, lost: vi.fn() };
});
vi.mock("../data/stream", () => ({ startStream }));

// Imported AFTER the mock so bootstrap.ts picks up the mocked module.
const { bootstrapFromServer } = await import("../data/bootstrap");
const { deleteEvent, isStale, reportMutationFailure, updateEvent } = await import(
  "../data/eventApi"
);
const { exportAcknowledgement, useReviewStore } = await import("../data/reviewStore");
const { observedAtMicros } = await import("../data/exportDoc");
const {
  admitSource,
  importLocalFile,
  importOffered,
  requestSourceSwitch,
  resyncOnOpen,
  syncFromServer,
} = await import("../data/source");
const { SOURCE_NAME_HEADER, SOURCE_TOKEN_HEADER, UNSAVED_HEADER } = await import(
  "../data/sourceApi"
);
const { switchPromptText } = await import("../shell/ConfirmSwitchSource");
const { documentFromSessions } = await import("../data/exportDoc");
const { useUndoHistory } = await import("../data/undoHistory");
const { startSpan } = await import("../shell/marking");
const { useUiStore } = await import("../ui/uiStore");
const { useToastStore } = await import("../ui/Toast");

const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");
const KITCHEN = readFileSync(join(__dir, "../../fixtures/kitchen-sink.json"), "utf-8");
const DRIFT = readFileSync(join(__dir, "../../fixtures/drift.json"), "utf-8");

const kitchenSinkFile = () => new File([KITCHEN], "kitchen-sink.json");

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A File-shaped stand-in whose text() the test settles by hand. */
function heldFile(name: string): { file: File; text: Deferred<string> } {
  const text = deferred<string>();
  return { file: { name, text: () => text.promise } as unknown as File, text };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type Route = (init?: RequestInit) => Promise<Response>;

/** Stubs fetch with a route table; a route may be replaced mid-test. */
function stubServer(routes: Record<string, Route>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const route = routes[url];
    if (!route) throw new Error(`unexpected fetch ${url}`);
    return route(init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const liveMode: Route = () => Promise.resolve(jsonResponse(sourceState()));

/** Moves the tab onto another live source (the server restarted under a new
 * token), whose snapshot is kitchen-sink: the source change every piece of
 * the old source's in-flight work must yield to. The transition starts
 * synchronously; the returned promise settles once the new source installed. */
function switchServerSource(routes: Record<string, Route>): Promise<void> {
  routes["/api/monitor_sessions"] = () =>
    Promise.resolve(snapshotResponse(KITCHEN, "other-token", stamp(2)));
  return admitSource(sourceState({ token: "other-token", observed_at: stamp(1) }));
}

/** Boots live and editable against a server whose snapshot is `snapshot`. */
async function bootLive(snapshot = MINIMAL, extra: Record<string, Route> = {}) {
  const routes: Record<string, Route> = {
    "/api/mode": liveMode,
    "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(snapshot)),
    ...extra,
  };
  const fetchMock = stubServer(routes);
  await bootstrapFromServer();
  expect(useReviewStore.getState().mode).toBe("live");
  expect(useReviewStore.getState().editable).toBe(true);
  return { routes, fetchMock };
}

afterEach(() => {
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
  useUiStore.setState({
    eventEditor: null,
    sweepArmed: false,
    openSpan: null,
    markPopover: null,
    switchPrompt: null,
    exportDialogOpen: false,
  });
  useUndoHistory.getState().actions.clear();
  stop.mockClear();
  reconnect.mockClear();
  startStream.mockClear();
  useToastStore.setState({ toast: null });
  streamOpts = null;
  streamFails = false;
  vi.unstubAllGlobals();
});

describe("importLocalFile on a live source", () => {
  it("(a) a file chosen while live is ignored: the live source and its UI state stay", async () => {
    await bootLive();
    const before = useReviewStore.getState();
    const session = before.sessions[0];
    if (!session) throw new Error("live boot loaded no session");
    const ui = useUiStore.getState().actions;
    ui.setOpenSpan({ sessionId: session.id, eventId: 1 });
    ui.armSweep();

    await importLocalFile(kitchenSinkFile());

    expect(stop).not.toHaveBeenCalled();
    const review = useReviewStore.getState();
    expect(review.mode).toBe("live");
    expect(review.sourceToken).toBe("test-token");
    expect(review.editable).toBe(true);
    expect(review.sessions).toBe(before.sessions);
    expect(review.sourceName).toBe(before.sourceName);
    expect(review.importError).toBeNull();
    const after = useUiStore.getState();
    expect(after.switchPrompt).toBeNull();
    expect(after.openSpan).not.toBeNull();
    expect(after.sweepArmed).toBe(true);
  });

  it("a file chosen after a live source installs is ignored", async () => {
    // The picker opened in the boot window, while no source was installed.
    stubServer({
      "/api/mode": liveMode,
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    const { file, text } = heldFile("kitchen-sink.json");
    const importing = importLocalFile(file);
    await bootstrapFromServer();
    expect(useReviewStore.getState().mode).toBe("live");
    const sessions = useReviewStore.getState().sessions;

    text.resolve(KITCHEN);
    await importing;

    const state = useReviewStore.getState();
    expect(state.mode).toBe("live");
    expect(state.sourceToken).toBe("test-token");
    expect(state.sessions).toBe(sessions);
    expect(stop).not.toHaveBeenCalled();
  });

  it("a file chosen while a live source is pending is ignored", async () => {
    // A failed snapshot leaves the live source pending, nothing installed.
    useReviewStore.setState({ source: null, pendingSource: sourceState() });

    await importLocalFile(kitchenSinkFile());

    const state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(0);
    expect(state.sourceName).toBeNull();
    expect(state.pendingSource?.kind).toBe("live");
    expect(useUiStore.getState().switchPrompt).toBeNull();
  });

  it("(c) a read failure surfaces through importError and loads nothing", async () => {
    await bootLive();
    const sessionsBefore = useReviewStore.getState().sessions;
    const { file, text } = heldFile("x.json");
    text.reject(new Error("boom"));

    await importLocalFile(file);

    const state = useReviewStore.getState();
    expect(state.importError).toBe("Import failed: could not read x.json: Error: boom");
    expect(state.sessions).toBe(sessionsBefore);
    expect(state.mode).toBe("live");
    expect(stop).not.toHaveBeenCalled();
  });

  it("(d) the newer of two imports wins even when it reads first", async () => {
    const a = heldFile("a.json");
    const b = heldFile("b.json");
    const importA = importLocalFile(a.file);
    const importB = importLocalFile(b.file);

    b.text.resolve(KITCHEN);
    await importB;
    a.text.resolve(MINIMAL);
    await importA;

    expect(useReviewStore.getState().sourceName).toBe("b.json");
  });

  it("(e) a file chosen while a live mode's snapshot is held is ignored", async () => {
    const held = deferred<Response>();
    const fetchMock = stubServer({
      "/api/mode": liveMode,
      "/api/monitor_sessions": () => held.promise,
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/monitor_sessions"));
    expect(importOffered(useReviewStore.getState())).toBe(false);

    await importLocalFile(kitchenSinkFile());
    expect(useReviewStore.getState().sourceName).toBeNull();
    held.resolve(snapshotResponse(MINIMAL));
    await boot;

    const state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(1);
    expect(state.mode).toBe("live");
    expect(state.sourceToken).toBe("test-token");
    expect(stop).not.toHaveBeenCalled();
  });

  it("(h) a file chosen before /api/mode answers is ignored", async () => {
    const held = deferred<Response>();
    const fetchMock = stubServer({
      "/api/mode": () => held.promise,
      "/api/monitor_sessions": () => Promise.resolve(snapshotResponse(MINIMAL)),
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/mode"));

    await importLocalFile(kitchenSinkFile());
    expect(useReviewStore.getState().sourceName).toBeNull();
    held.resolve(jsonResponse(sourceState()));
    await boot;

    const state = useReviewStore.getState();
    expect(state.mode).toBe("live");
    expect(state.sourceToken).toBe("test-token");
    expect(state.sessions).toHaveLength(1);
    expect(stop).not.toHaveBeenCalled();
  });

  it("a live mode whose snapshot 404s does not offer Import", async () => {
    stubServer({
      "/api/mode": liveMode,
      "/api/monitor_sessions": () => Promise.resolve(new Response(null, { status: 404 })),
    });
    await bootstrapFromServer();

    expect(importOffered(useReviewStore.getState())).toBe(false);
    await importLocalFile(kitchenSinkFile());
    const state = useReviewStore.getState();
    expect(state.sessions).toHaveLength(0);
    expect(state.pendingSource?.kind).toBe("live");
    expect(stop).not.toHaveBeenCalled();
    expect(reconnect).toHaveBeenCalledTimes(1); // the retry is the stream's own
  });

  it("a .json mode whose snapshot is held opens the switch prompt for a file", async () => {
    const held = deferred<Response>();
    const json = sourceState({ mode: "review", kind: "json", source: "a.json", token: "A" });
    const fetchMock = stubServer({
      "/api/mode": () => Promise.resolve(jsonResponse(json)),
      "/api/monitor_sessions": () => held.promise,
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/monitor_sessions"));
    expect(importOffered(useReviewStore.getState())).toBe(true);

    await importLocalFile(kitchenSinkFile());

    expect(useUiStore.getState().switchPrompt?.shown).toEqual({
      token: "A",
      unsaved: 0,
      source: "a.json",
    });
    expect(useReviewStore.getState().sessions).toHaveLength(0);
    held.resolve(snapshotResponse(MINIMAL, "A"));
    await boot;
    expect(useReviewStore.getState().sourceToken).toBe("A");
  });

  it("with no server at all, a file chosen is a local load", async () => {
    streamFails = true;
    const fetchMock = stubServer({});
    await bootstrapFromServer();
    expect(fetchMock).not.toHaveBeenCalled();

    await importLocalFile(kitchenSinkFile());

    const state = useReviewStore.getState();
    expect(state.sourceName).toBe("kitchen-sink.json");
    expect(state.sessions).toHaveLength(1);
    expect(state.mode).toBeNull();
  });
});

describe("(g) a mutation answering after a switch to a source with the same session ID", () => {
  const record = (id: number, label: string) => ({
    id,
    timestamp: "2026-07-01T08:30:00+00:00",
    label,
    source: "manual",
    color: "#888888",
    dash: "dash",
  });

  /** Boots live on kitchen-sink, routes the session's event endpoint `path`
   * to a held response, and returns the session id plus that response. */
  async function bootWithHeldMutation(path: (sid: string) => string) {
    const sid = (JSON.parse(KITCHEN) as { sessions: { id: string }[] }).sessions[0]?.id ?? "";
    const held = deferred<Response>();
    const base = `/api/session/${encodeURIComponent(sid)}/event`;
    const { routes } = await bootLive(KITCHEN, { [path(base)]: () => held.promise });
    return { sid, held, routes };
  }

  const eventsOf = () => useReviewStore.getState().sessions[0]?.events ?? [];

  it("updateEvent applies nothing and reports stale", async () => {
    const { sid, held, routes } = await bootWithHeldMutation((base) => `${base}/1`);
    const pending = updateEvent(sid, 1, { label: "renamed" });
    await switchServerSource(routes);
    const imported = eventsOf();

    held.resolve(jsonResponse(record(1, "renamed")));
    const result = await pending;

    expect(isStale(result)).toBe(true);
    expect(eventsOf()).toBe(imported);
    expect(eventsOf().find((ev) => ev.id === 1)?.label).toBe("config reload");
  });

  it("deleteEvent removes nothing and reports stale", async () => {
    const { sid, held, routes } = await bootWithHeldMutation((base) => `${base}/2`);
    const pending = deleteEvent(sid, 2);
    await switchServerSource(routes);
    const imported = eventsOf();

    held.resolve(
      new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
        status: 200,
      }),
    );
    const result = await pending;

    expect(isStale(result)).toBe(true);
    expect(eventsOf()).toBe(imported);
    expect(eventsOf().some((ev) => ev.id === 2)).toBe(true);
  });

  it("a mutation that fails after the switch reports stale instead of an error", async () => {
    const { sid, held, routes } = await bootWithHeldMutation((base) => `${base}/1`);
    const pending = updateEvent(sid, 1, { label: "renamed" });
    await switchServerSource(routes);

    held.resolve(jsonResponse({ error: "no such event" }, 404));

    expect(isStale(await pending)).toBe(true);
  });

  it("startSpan adds no event and opens no span", async () => {
    const { held, routes } = await bootWithHeldMutation((base) => base);
    const pending = startSpan("late span");
    await switchServerSource(routes);
    const imported = eventsOf();

    held.resolve(jsonResponse(record(99, "late span"), 201));
    await pending;

    expect(eventsOf()).toBe(imported);
    expect(eventsOf().some((ev) => ev.id === 99)).toBe(false);
    expect(useUiStore.getState().openSpan).toBeNull();
  });
});

// Each async step re-checks the source in its OWN continuation: a source
// change that lands after a helper's last check but before its caller
// resumes must still win. These hold the work exactly at those continuation boundaries.
describe("(i) a source change landing between a step's check and its caller's continuation", () => {
  it("startSpan opens no span once a switch lands after its mutation applied", async () => {
    const sid = (JSON.parse(KITCHEN) as { sessions: { id: string }[] }).sessions[0]?.id ?? "";
    const base = `/api/session/${encodeURIComponent(sid)}/event`;
    const created = {
      id: 99,
      timestamp: "2026-07-01T08:30:00+00:00",
      label: "late span",
      source: "manual",
      color: "#888888",
      dash: "dash",
    };
    const { routes } = await bootLive(KITCHEN, {
      [base]: () => Promise.resolve(jsonResponse(created, 201)),
    });
    // Start the switch from inside the mutation's apply step: the mutation's
    // own check has passed, and startSpan's continuation is still to run.
    const actions = useReviewStore.getState().actions;
    const original = actions.appendFragment;
    let switching: Promise<void> | null = null;
    const spy = vi.spyOn(actions, "appendFragment").mockImplementation((frag) => {
      original(frag);
      switching ??= switchServerSource(routes);
    });
    try {
      const result = await startSpan("late span");
      expect(isStale(result)).toBe(true);
    } finally {
      spy.mockRestore();
    }
    await switching;

    expect(useReviewStore.getState().sourceToken).toBe("other-token");
    expect(useUiStore.getState().openSpan).toBeNull();
  });
});

describe("a mutation failure reaching its caller's rejection handler", () => {
  it("is posted as a warning while the source holds", async () => {
    await bootLive();
    const onFailure = reportMutationFailure("End span");

    onFailure(new Error("boom"));

    expect(useReviewStore.getState().warnings).toEqual(["End span failed: boom"]);
  });

  it("is dropped once a source switch lands before the handler runs", async () => {
    const { routes } = await bootLive();
    const onFailure = reportMutationFailure("End span");
    await switchServerSource(routes);
    const warnings = useReviewStore.getState().warnings;

    onFailure(new Error("boom"));

    expect(useReviewStore.getState().warnings).toBe(warnings);
  });
});

// #958: one admission path. A source state arrives on its own (an upload
// response, a stream frame, a refusal) or through a sync; either way a new
// token is installed only once its snapshot has loaded, and an older or
// duplicate state never moves the tab.
describe("source admission (#958)", () => {
  const SID = "2026-07-01T08-00-00-kitchen-sink";
  const json = (token: string, n: number, extra: Partial<MonitorSourceState> = {}) =>
    sourceState({
      mode: "review",
      kind: "json",
      source: `${token.toLowerCase()}.json`,
      token,
      observed_at: stamp(n),
      ...extra,
    });

  interface Server {
    mode: () => Promise<Response>;
    snapshot: () => Promise<Response>;
  }

  /** Boots on `initial`; the test swaps the server's answers through `server`. */
  async function bootOn(initial: MonitorSourceState, text = KITCHEN) {
    const server: Server = {
      mode: () => Promise.resolve(jsonResponse(initial)),
      snapshot: () => Promise.resolve(snapshotResponse(text, initial.token, initial.observed_at)),
    };
    const fetchMock = stubServer({
      "/api/mode": () => server.mode(),
      "/api/monitor_sessions": () => server.snapshot(),
    });
    await bootstrapFromServer();
    expect(useReviewStore.getState().sourceToken).toBe(initial.token);
    return { server, fetchMock };
  }

  const calls = (fetchMock: { mock: { calls: unknown[][] } }, url: string) =>
    fetchMock.mock.calls.filter(([input]) => String(input) === url).length;

  it("boot ends editable", async () => {
    await bootOn(json("A", 10));
    const state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(state.source?.kind).toBe("json");
    expect(state.mode).toBe("review");
  });

  it("a new token installs its snapshot before the tab is editable", async () => {
    const { server } = await bootOn(json("A", 10, { revision: 1 }));
    const held = deferred<Response>();
    server.snapshot = () => held.promise;

    const admitted = admitSource(json("B", 20, { revision: 5 }));
    let state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("B");
    expect(state.editable).toBe(false);
    expect(state.revision).toBe(1);
    expect(state.sourceToken).toBe("A");

    held.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
    await admitted;
    state = useReviewStore.getState();
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(state.revision).toBe(5);
    expect(state.sourceToken).toBe("B");
    expect(state.sourceName).toBe("b.json");
  });

  it("a failed snapshot leaves the tab pending and not editable", async () => {
    const { server } = await bootOn(json("A", 10));
    server.snapshot = () => Promise.resolve(new Response(null, { status: 500 }));

    await admitSource(json("B", 20));

    let state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("B");
    expect(state.editable).toBe(false);
    expect(state.warnings).toContain(
      "Could not load b.json; retrying when the connection returns.",
    );
    // The retry is the stream's own reconnect, forced now.
    expect(reconnect).toHaveBeenCalledTimes(1);

    // The reconnected stream opens: its sync retries and installs.
    server.mode = () => Promise.resolve(jsonResponse(json("B", 30)));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(31)));
    streamOpts?.onOpen?.();
    await vi.waitFor(() => expect(useReviewStore.getState().pendingSource).toBeNull());
    state = useReviewStore.getState();
    expect(state.editable).toBe(true);
    expect(state.sourceToken).toBe("B");
  });

  it("a count frame admitted during a resync survives the resync's older mode state", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    server.mode = () => Promise.resolve(jsonResponse(json("A", 20)));
    const held = deferred<Response>();
    server.snapshot = () => held.promise;
    fetchMock.mockClear();

    const resync = syncFromServer({ fresh: false });
    await vi.waitFor(() => expect(calls(fetchMock, "/api/monitor_sessions")).toBe(1));
    await admitSource(json("A", 30, { unsaved: 2, revision: 2 }));
    expect(useReviewStore.getState().unsaved).toBe(2);

    held.resolve(snapshotResponse(KITCHEN, "A", stamp(21)));
    await resync;
    expect(useReviewStore.getState().unsaved).toBe(2);
    expect(useReviewStore.getState().editable).toBe(true);
  });

  it("a state stamped equal to the installed stamp is ignored", async () => {
    await bootOn(json("A", 10));
    await admitSource(json("A", 10, { unsaved: 3 }));
    expect(useReviewStore.getState().unsaved).toBe(0);
  });

  it("an equal stamp with another token never starts an adoption", async () => {
    const { fetchMock } = await bootOn(json("A", 10));
    fetchMock.mockClear();

    await admitSource(json("B", 10));

    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("A");
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("the notice shows only once the new source has installed", async () => {
    const { server } = await bootOn(json("A", 10));
    server.snapshot = () => Promise.resolve(new Response(null, { status: 500 }));
    await admitSource(json("B", 20));
    expect(useToastStore.getState().toast).toBeNull();

    // A snapshot that does not parse installs nothing either.
    server.snapshot = () => Promise.resolve(snapshotResponse("{not json", "C", stamp(31)));
    await admitSource(json("C", 30));
    expect(useToastStore.getState().toast).toBeNull();

    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "D", stamp(41)));
    await admitSource(json("D", 40));
    expect(useToastStore.getState().toast?.message).toBe("Source changed to d.json");
  });

  it("the first boot shows no notice", async () => {
    await bootOn(json("A", 10));
    expect(useToastStore.getState().toast).toBeNull();
  });

  it("a switch missed while disconnected is announced when the resync finds it", async () => {
    const { server } = await bootOn(json("A", 10));
    // Another tab switched to B; this tab missed the frame.
    server.mode = () => Promise.resolve(jsonResponse(json("B", 20)));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));

    expect(await resyncOnOpen()).toBe(true);

    expect(useReviewStore.getState().sourceToken).toBe("B");
    expect(useToastStore.getState().toast?.message).toBe("Source changed to b.json");
  });

  it("a failed admission announces the switch once, when the reconnect installs it", async () => {
    const { server } = await bootOn(json("A", 10));
    const shown = vi.spyOn(useToastStore.getState(), "showToast");
    server.snapshot = () => Promise.resolve(new Response(null, { status: 500 }));
    streamOpts?.onSource?.(json("B", 20));
    await vi.waitFor(() => expect(reconnect).toHaveBeenCalledTimes(1));
    expect(shown).not.toHaveBeenCalled();

    server.mode = () => Promise.resolve(jsonResponse(json("B", 30)));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(31)));
    expect(await resyncOnOpen()).toBe(true);

    expect(useReviewStore.getState().sourceToken).toBe("B");
    expect(shown.mock.calls).toEqual([["Source changed to b.json"]]);
    shown.mockRestore();
  });

  it("a pending source's pushes are replayed, never applied to the old document", async () => {
    // A and B hold the same session and event ids.
    const { server } = await bootOn(json("A", 10));
    const held = deferred<Response>();
    server.snapshot = () => held.promise;
    const admitted = admitSource(json("B", 20));
    // B's edit of event 1, stamped after B's snapshot capture.
    useReviewStore.getState().actions.appendFragments([
      {
        format: 1,
        session: SID,
        events: [{ id: 1, timestamp: "2026-07-01T08:30:00+00:00", label: "B edit" }],
        observed_at: stamp(25),
      },
    ]);
    const labels = () =>
      documentFromSessions(useReviewStore.getState().sessions).sessions[0]?.events?.map(
        (e) => e.label,
      );
    expect(labels()).not.toContain("B edit");
    expect(useReviewStore.getState().sourceToken).toBe("A");

    held.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
    await admitted;

    expect(useReviewStore.getState().sourceToken).toBe("B");
    expect(labels()).toContain("B edit");
  });

  it("a resync that finds a new token resets the view; a same-token one keeps it", async () => {
    const { server } = await bootOn(json("A", 10), DRIFT);
    const { actions } = useReviewStore.getState();
    const second = useReviewStore.getState().sessions[1]?.id ?? "";
    actions.selectSession(second);
    actions.setRange({ from: 100, to: 200 });

    server.mode = () => Promise.resolve(jsonResponse(json("A", 20)));
    server.snapshot = () => Promise.resolve(snapshotResponse(DRIFT, "A", stamp(21)));
    await syncFromServer({ fresh: false });
    expect(useReviewStore.getState().activeSessionId).toBe(second);
    expect(useReviewStore.getState().range).toEqual({ from: 100, to: 200 });

    // The same session ids under a new token: another source.
    server.mode = () => Promise.resolve(jsonResponse(json("B", 30)));
    server.snapshot = () => Promise.resolve(snapshotResponse(DRIFT, "B", stamp(31)));
    await syncFromServer({ fresh: false });
    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("B");
    expect(state.activeSessionId).toBe(state.sessions[0]?.id);
    expect(state.range).toBeNull();
  });

  it("an older state never starts an adoption", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "C", stamp(31)));
    await admitSource(json("C", 30));
    expect(useReviewStore.getState().sourceToken).toBe("C");
    fetchMock.mockClear();

    // An upload response for B, stamped before C's frame, arrives late.
    await admitSource(json("B", 20));

    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("C");
    expect(state.pendingSource).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a delayed same-token response cannot lower newer counts", async () => {
    await bootOn(json("A", 10));
    await admitSource(json("A", 30, { unsaved: 3, revision: 3 }));
    await admitSource(json("A", 20, { unsaved: 1, revision: 1 }));
    expect(useReviewStore.getState().unsaved).toBe(3);
    expect(useReviewStore.getState().revision).toBe(3);
  });

  it("adopting clears undo history, editor, sweep, popover and open span", async () => {
    const { server } = await bootOn(json("A", 10));
    const entry = (eventId: number) => ({
      verb: "delete" as const,
      sessionId: SID,
      eventId,
      label: "config reload",
      after: null,
      before: null,
      trackedSpan: false,
    });
    const history = useUndoHistory.getState().actions;
    history.record(entry(1));
    history.record(entry(2));
    history.shiftUndoToRedo();
    expect(useUndoHistory.getState().undo).toHaveLength(1);
    expect(useUndoHistory.getState().redo).toHaveLength(1);
    const ui = useUiStore.getState().actions;
    ui.setOpenSpan({ sessionId: SID, eventId: 1 });
    ui.openEventEditor({ kind: "edit", sessionId: SID, eventId: 1 });
    ui.armSweep();
    ui.openMarkPopover("mark");
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));

    await admitSource(json("B", 20));

    expect(useUndoHistory.getState().undo).toHaveLength(0);
    expect(useUndoHistory.getState().redo).toHaveLength(0);
    const after = useUiStore.getState();
    expect(after.openSpan).toBeNull();
    expect(after.eventEditor).toBeNull();
    expect(after.sweepArmed).toBe(false);
    expect(after.markPopover).toBeNull();
  });

  it("re-importing the same ids lands the new document, not a merge", async () => {
    const { server } = await bootOn(json("A", 10));
    // The tab's copy gained an event the edited file does not hold.
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: SID,
      events: [{ id: 99, timestamp: "2026-07-01T08:30:00+00:00", label: "tab only" }],
      observed_at: stamp(15),
    });
    expect(useReviewStore.getState().sessions[0]?.events.some((e) => e.id === 99)).toBe(true);
    // The edited copy: the same session and event ids, event 1 relabelled,
    // event 2 gone.
    const edited = JSON.parse(KITCHEN) as {
      sessions: { events: { id: number; label: string }[] }[];
    };
    const [session] = edited.sessions;
    if (!session) throw new Error("kitchen-sink has no session");
    session.events = session.events
      .filter((e) => e.id !== 2)
      .map((e) => (e.id === 1 ? { ...e, label: "relabelled" } : e));
    const held = deferred<Response>();
    server.snapshot = () => held.promise;

    const admitted = admitSource(json("B", 20, { source: "a-edited.json" }));
    // An old-source push stamped before the capture lands meanwhile.
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: SID,
      events: [{ id: 77, timestamp: "2026-07-01T08:30:00+00:00", label: "old push" }],
      observed_at: stamp(19),
    });
    held.resolve(snapshotResponse(JSON.stringify(edited), "B", stamp(21)));
    await admitted;

    const events = useReviewStore.getState().sessions[0]?.events ?? [];
    const byId = (a: { id?: number | null }, b: { id?: number | null }) =>
      (a.id ?? 0) - (b.id ?? 0);
    expect([...events].sort(byId).map((e) => [e.id, e.label])).toEqual(
      [...session.events].sort(byId).map((e) => [e.id, e.label]),
    );
    expect(useReviewStore.getState().sourceName).toBe("a-edited.json");
  });

  it("a snapshot whose token differs is refused and the mode re-fetched once", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    // A switch lands between the mode fetch and the snapshot fetch.
    let modes = 0;
    server.mode = () => {
      modes += 1;
      return Promise.resolve(jsonResponse(modes === 1 ? json("A", 20) : json("B", 22)));
    };
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
    fetchMock.mockClear();

    await syncFromServer({ fresh: false });

    expect(calls(fetchMock, "/api/mode")).toBe(2);
    expect(calls(fetchMock, "/api/monitor_sessions")).toBe(2);
    expect(useReviewStore.getState().sourceToken).toBe("B");
    expect(useReviewStore.getState().pendingSource).toBeNull();
  });

  it("a second mismatch installs nothing and fetches no third time", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    server.mode = () => Promise.resolve(jsonResponse(json("A", 20)));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
    fetchMock.mockClear();

    await syncFromServer({ fresh: false });

    expect(calls(fetchMock, "/api/mode")).toBe(2);
    expect(calls(fetchMock, "/api/monitor_sessions")).toBe(2);
    expect(useReviewStore.getState().sourceToken).toBe("A");
  });

  // The continuation boundary: fetchMode checked the generation, then B was
  // admitted in the turn before the sync resumed with A's answer. The sync
  // must yield to B, never move the pending identity back to A.
  it("a mode answered for A yields to a B admitted before the sync resumes", async () => {
    const { server } = await bootOn(json("X", 10));
    const body = deferred<unknown>();
    const read = deferred<void>();
    server.mode = () =>
      Promise.resolve({
        ok: true,
        json: () => {
          read.resolve();
          return body.promise;
        },
      } as unknown as Response);
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(31)));

    const sync = syncFromServer({ fresh: false });
    await read.promise; // fetchMode now awaits the body
    let admitted: Promise<void> | null = null;
    void body.promise.then(() => {
      admitted = admitSource(json("B", 30));
    });
    body.resolve(json("A", 20));

    expect(await sync).toBe("stale");
    expect(useReviewStore.getState().pendingSource?.token).toBe("B");
    await admitted;
    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("B");
    expect(state.pendingSource).toBeNull();
    expect(state.lastSourceStamp).toBe(observedAtMicros(stamp(30)));
  });

  // A same-token resync does not move the generation, so two can be in
  // flight at once: only the newer one may install.
  it("of two overlapping same-token resyncs finishing in reverse order, the newer one's document stays", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    const older = deferred<Response>();
    const newer = deferred<Response>();
    fetchMock.mockClear();
    server.mode = () => Promise.resolve(jsonResponse(json("A", 20, { revision: 1 })));
    server.snapshot = () => older.promise;
    const first = syncFromServer({ fresh: false });
    await vi.waitFor(() => expect(calls(fetchMock, "/api/monitor_sessions")).toBe(1));
    server.mode = () => Promise.resolve(jsonResponse(json("A", 30, { revision: 2 })));
    server.snapshot = () => newer.promise;
    const second = syncFromServer({ fresh: false });
    await vi.waitFor(() => expect(calls(fetchMock, "/api/monitor_sessions")).toBe(2));

    newer.resolve(snapshotResponse(MINIMAL, "A", stamp(31)));
    expect(await second).toBe("installed");
    older.resolve(snapshotResponse(DRIFT, "A", stamp(21)));
    expect(await first).toBe("stale");

    const state = useReviewStore.getState();
    const minimalIds = (JSON.parse(MINIMAL) as { sessions: { id: string }[] }).sessions.map(
      (s) => s.id,
    );
    expect(state.sessions.map((s) => s.id)).toEqual(minimalIds);
    expect(state.resyncUnresolved).toBe(false);
    expect(exportAcknowledgement(state)).toEqual({ token: "A", revision: 2 });
  });

  // A sync is current only while it owns the generation AND is the newest
  // begun: an obsolete one acts on nothing, whatever its late answers say.
  it("an older sync whose late mode names a new source adopts nothing; the newer sync installs it", async () => {
    const { server, fetchMock } = await bootOn(json("A", 10));
    fetchMock.mockClear();
    const olderMode = deferred<Response>();
    const newerMode = deferred<Response>();
    server.mode = () => olderMode.promise;
    server.snapshot = () => Promise.resolve(snapshotResponse(MINIMAL, "B", stamp(21)));
    const first = resyncOnOpen();
    server.mode = () => newerMode.promise;
    const second = resyncOnOpen();

    olderMode.resolve(jsonResponse(json("B", 20)));
    await first;
    expect(useReviewStore.getState().pendingSource).toBeNull(); // adopted nothing
    expect(useReviewStore.getState().sourceToken).toBe("A");
    newerMode.resolve(jsonResponse(json("B", 20)));
    expect(await second).toBe(true);

    const state = useReviewStore.getState();
    expect(state.sourceToken).toBe("B");
    expect(state.pendingSource).toBeNull();
    expect(state.editable).toBe(true);
    expect(state.resyncUnresolved).toBe(false);
    expect(calls(fetchMock, "/api/monitor_sessions")).toBe(1); // the newer sync's only
  });

  it("an older sync whose mode names a new source after a newer sync installed retries, never strands it", async () => {
    const { server } = await bootOn(json("A", 10));
    const olderMode = deferred<Response>();
    server.mode = () => olderMode.promise;
    const first = resyncOnOpen();
    server.mode = () => Promise.resolve(jsonResponse(json("A", 20)));
    expect(await resyncOnOpen()).toBe(true); // the newer sync refreshes A
    reconnect.mockClear();

    server.mode = () => Promise.resolve(jsonResponse(json("B", 30)));
    server.snapshot = () => Promise.resolve(snapshotResponse(MINIMAL, "B", stamp(31)));
    olderMode.resolve(jsonResponse(json("B", 30)));
    expect(await first).toBe(false);
    expect(useReviewStore.getState().sourceToken).toBe("A"); // acted on nothing
    expect(useReviewStore.getState().pendingSource).toBeNull();
    expect(reconnect).toHaveBeenCalledTimes(1); // the retry is scheduled

    expect(await resyncOnOpen()).toBe(true); // the retry's open
    expect(useReviewStore.getState().sourceToken).toBe("B");
    expect(useReviewStore.getState().pendingSource).toBeNull();
  });

  // Every way an adoption's sync ends without installing warns alike.
  it("a reconnect adoption whose second mode read fails warns", async () => {
    const { server } = await bootOn(json("A", 10));
    let modes = 0;
    server.mode = () => {
      modes += 1;
      return Promise.resolve(
        modes === 1 ? jsonResponse(json("B", 20)) : new Response(null, { status: 500 }),
      );
    };
    server.snapshot = () => Promise.resolve(snapshotResponse(MINIMAL, "C", stamp(21)));

    expect(await resyncOnOpen()).toBe(false);

    const state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("B");
    expect(state.warnings).toContain(
      "Could not load b.json; retrying when the connection returns.",
    );
  });

  it("a reconnect adoption whose snapshot mismatches twice warns", async () => {
    const { server } = await bootOn(json("A", 10));
    server.mode = () => Promise.resolve(jsonResponse(json("B", 20)));
    server.snapshot = () => Promise.resolve(snapshotResponse(MINIMAL, "C", stamp(21)));

    expect(await resyncOnOpen()).toBe(false);

    const state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("B");
    expect(state.warnings).toContain(
      "Could not load b.json; retrying when the connection returns.",
    );
  });

  // A reconnect that finds another source and cannot read its snapshot says
  // so, the way an admission does, and leaves the tab read-only meanwhile.
  it("a reconnect adoption whose snapshot fails warns as an admission does, once", async () => {
    const { server } = await bootOn(json("A", 10));
    server.mode = () => Promise.resolve(jsonResponse(json("B", 20)));
    server.snapshot = () => Promise.resolve(new Response(null, { status: 500 }));

    expect(await resyncOnOpen()).toBe(false);
    expect(await resyncOnOpen()).toBe(false); // the next retry fails the same way

    const state = useReviewStore.getState();
    expect(state.pendingSource?.token).toBe("B");
    expect(state.editable).toBe(false);
    const notice = "Could not load b.json; retrying when the connection returns.";
    expect(state.warnings.filter((w) => w === notice)).toHaveLength(1);
  });

  // Retrying the pending source is not a source change: what the user opened
  // meanwhile stays open.
  it("a retry of the same pending source keeps the switch prompt and the export dialog open", async () => {
    const { server } = await bootOn(json("A", 10));
    server.snapshot = () => Promise.resolve(new Response(null, { status: 500 }));
    await admitSource(json("B", 20));
    expect(useReviewStore.getState().pendingSource?.token).toBe("B");
    await importLocalFile(new File([KITCHEN], "c.json"));
    useUiStore.getState().actions.openExportDialog();
    expect(useUiStore.getState().switchPrompt).not.toBeNull();

    server.mode = () => Promise.resolve(jsonResponse(json("B", 30)));
    expect(await resyncOnOpen()).toBe(false);

    expect(useReviewStore.getState().pendingSource?.token).toBe("B");
    expect(useUiStore.getState().switchPrompt).not.toBeNull();
    expect(useUiStore.getState().exportDialogOpen).toBe(true);
  });

  it("a source change closes source-bound dialogs: the switch prompt and the export dialog", async () => {
    const { server } = await bootOn(json("A", 10));
    await importLocalFile(new File([KITCHEN], "b.json"));
    useUiStore.getState().actions.openExportDialog();
    expect(useUiStore.getState().switchPrompt).not.toBeNull();
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "C", stamp(21)));

    await admitSource(json("C", 20));

    expect(useUiStore.getState().switchPrompt).toBeNull();
    expect(useUiStore.getState().exportDialogOpen).toBe(false);
  });

  it("a source frame for another source shows the source-changed notice", async () => {
    const { server } = await bootOn(json("A", 10));
    server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));

    streamOpts?.onSource?.(json("B", 20));

    await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("B"));
    expect(useToastStore.getState().toast?.message).toBe("Source changed to b.json");
  });

  describe("Import on a .json source switches it after a prompt", () => {
    type Upload = (init?: RequestInit) => Promise<Response>;

    /** Boots on `initial` with POST /api/source answered by `upload`. */
    async function bootSwitchable(initial: MonitorSourceState, upload: Upload) {
      const booted = await bootOn(initial);
      const uploads: RequestInit[] = [];
      stubServer({
        "/api/mode": () => booted.server.mode(),
        "/api/monitor_sessions": () => booted.server.snapshot(),
        "/api/source": (init?: RequestInit) => {
          uploads.push(init ?? {});
          return upload(init);
        },
      });
      return { ...booted, uploads };
    }

    const noUpload: Upload = () => Promise.reject(new Error("no upload expected"));

    it("importing on a json source with unsaved edits prompts with the count", async () => {
      const { uploads } = await bootSwitchable(json("A", 10, { unsaved: 2 }), noUpload);

      await importLocalFile(new File([KITCHEN], "b.json"));

      const prompt = useUiStore.getState().switchPrompt;
      expect(prompt).toEqual({
        name: "b.json",
        text: KITCHEN,
        shown: { token: "A", unsaved: 2, source: "a.json" },
      });
      expect(prompt && switchPromptText(prompt)).toBe(
        "Opening b.json discards 2 unsaved changes to a.json. Export first to keep them.",
      );
      const one = { name: "b.json", text: "", shown: { token: "A", unsaved: 1, source: "a.json" } };
      expect(switchPromptText(one)).toBe(
        "Opening b.json discards 1 unsaved change to a.json. Export first to keep them.",
      );
      // Nothing has switched: the source, the stream and the server are untouched.
      expect(useReviewStore.getState().sourceToken).toBe("A");
      expect(stop).not.toHaveBeenCalled();
      expect(uploads).toHaveLength(0);
    });

    it('importing with nothing unsaved prompts "Opening b.json closes a.json."', async () => {
      await bootSwitchable(json("A", 10), noUpload);

      await importLocalFile(new File([KITCHEN], "b.json"));

      const prompt = useUiStore.getState().switchPrompt;
      expect(prompt && switchPromptText(prompt)).toBe("Opening b.json closes a.json.");
    });

    it("confirming uploads with token, unsaved and an encoded name", async () => {
      const name = "b ü%.json";
      const { server, uploads } = await bootSwitchable(json("A", 10, { unsaved: 2 }), () =>
        Promise.resolve(jsonResponse(json("B", 20, { source: name }))),
      );
      server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
      await importLocalFile(new File([KITCHEN], name));

      await requestSourceSwitch();

      expect(uploads).toHaveLength(1);
      const [init] = uploads;
      const headers = new Headers(init?.headers);
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe(KITCHEN);
      expect(headers.get(SOURCE_TOKEN_HEADER)).toBe("A");
      expect(headers.get(UNSAVED_HEADER)).toBe("2");
      expect(headers.get(SOURCE_NAME_HEADER)).toBe(encodeURIComponent(name));
      const state = useReviewStore.getState();
      expect(state.sourceToken).toBe("B");
      expect(state.pendingSource).toBeNull();
      expect(state.sourceName).toBe(name);
      expect(useUiStore.getState().switchPrompt).toBeNull();
      // The tab asked for this source: no "source changed" notice.
      expect(useToastStore.getState().toast).toBeNull();
    });

    it("the switch's own source frame shows no notice", async () => {
      const answer = deferred<Response>();
      const { server } = await bootSwitchable(json("A", 10), () => answer.promise);
      server.snapshot = () => Promise.resolve(snapshotResponse(KITCHEN, "B", stamp(21)));
      await importLocalFile(new File([KITCHEN], "b.json"));

      const switching = requestSourceSwitch();
      // The server publishes the new source's frame before it answers.
      streamOpts?.onSource?.(json("B", 20));
      await vi.waitFor(() => expect(useReviewStore.getState().sourceToken).toBe("B"));
      answer.resolve(jsonResponse(json("B", 22)));
      await switching;

      expect(useReviewStore.getState().pendingSource).toBeNull();
      expect(useToastStore.getState().toast).toBeNull();
    });

    it("a changed refusal goes through admission and re-prompts with fresh counts", async () => {
      const fresh = json("A", 20, { unsaved: 3, revision: 3 });
      const { uploads } = await bootSwitchable(json("A", 10, { unsaved: 1 }), () =>
        Promise.resolve(
          jsonResponse({ error: "stale", code: "source_changed", source_state: fresh }, 409),
        ),
      );
      await importLocalFile(new File([KITCHEN], "b.json"));

      await requestSourceSwitch();

      expect(uploads).toHaveLength(1);
      expect(useReviewStore.getState().unsaved).toBe(3);
      expect(useUiStore.getState().switchPrompt).toEqual({
        name: "b.json",
        text: KITCHEN,
        shown: { token: "A", unsaved: 3, source: "a.json" },
      });
    });

    it("a changed refusal carrying a new token transitions before re-prompting", async () => {
      const other = json("C", 20, { unsaved: 4 });
      const { server } = await bootSwitchable(json("A", 10), () =>
        Promise.resolve(
          jsonResponse({ error: "stale", code: "source_changed", source_state: other }, 409),
        ),
      );
      const held = deferred<Response>();
      server.snapshot = () => held.promise;
      await importLocalFile(new File([KITCHEN], "b.json"));

      const switching = requestSourceSwitch();
      await vi.waitFor(() => expect(useReviewStore.getState().pendingSource?.token).toBe("C"));
      // Pending: the prompt waits for the new source to install.
      expect(useUiStore.getState().switchPrompt).toBeNull();
      held.resolve(snapshotResponse(KITCHEN, "C", stamp(21)));
      await switching;

      expect(useReviewStore.getState().sourceToken).toBe("C");
      expect(useUiStore.getState().switchPrompt?.shown).toEqual({
        token: "C",
        unsaved: 4,
        source: "c.json",
      });
    });

    it("a changed refusal does not re-prompt once another import has started", async () => {
      const fresh = json("A", 20, { unsaved: 3, revision: 3 });
      const answer = deferred<Response>();
      await bootSwitchable(json("A", 10, { unsaved: 1 }), () => answer.promise);
      await importLocalFile(new File([KITCHEN], "b.json"));

      const switching = requestSourceSwitch();
      // The user picks another file while the first upload is in flight.
      const later = heldFile("c.json");
      const importing = importLocalFile(later.file);
      answer.resolve(
        jsonResponse({ error: "stale", code: "source_changed", source_state: fresh }, 409),
      );
      await switching;

      expect(useReviewStore.getState().unsaved).toBe(3);
      expect(useUiStore.getState().switchPrompt).toBeNull();
      later.text.resolve(MINIMAL);
      await importing;
      expect(useUiStore.getState().switchPrompt?.name).toBe("c.json");
    });

    it("a 422 leaves the source untouched and shows the error", async () => {
      await bootSwitchable(json("A", 10, { unsaved: 1 }), () =>
        Promise.resolve(jsonResponse({ error: "not a monitor document" }, 422)),
      );
      await importLocalFile(new File([KITCHEN], "b.json"));

      await requestSourceSwitch();

      const state = useReviewStore.getState();
      expect(state.sourceToken).toBe("A");
      expect(state.pendingSource).toBeNull();
      expect(state.unsaved).toBe(1);
      expect(state.editable).toBe(true);
      expect(state.importError).toContain("not a monitor document");
      expect(stop).not.toHaveBeenCalled();
      expect(useUiStore.getState().switchPrompt).toBeNull();
    });

    it("with no server source Import stays a local load", async () => {
      const fetchMock = stubServer({});
      expect(importOffered(useReviewStore.getState())).toBe(true);

      await importLocalFile(new File([KITCHEN], "b.json"));

      expect(fetchMock).not.toHaveBeenCalled();
      expect(useUiStore.getState().switchPrompt).toBeNull();
      expect(useReviewStore.getState().sourceName).toBe("b.json");
      expect(useReviewStore.getState().sessions).toHaveLength(1);
    });

    it("Import is offered with no source and on .json, never on live or .db", () => {
      const none = { source: null, pendingSource: null, sourceKindUnresolved: false };
      const db = sourceState({ mode: "review", kind: "db" });
      expect(importOffered(none)).toBe(true);
      expect(importOffered({ ...none, source: json("A", 1) })).toBe(true);
      expect(importOffered({ ...none, source: sourceState() })).toBe(false);
      expect(importOffered({ ...none, source: db })).toBe(false);
      // A pending source is the one judged: it is what the server serves now.
      expect(importOffered({ ...none, pendingSource: sourceState() })).toBe(false);
      expect(importOffered({ ...none, source: json("A", 1), pendingSource: db })).toBe(false);
      expect(importOffered({ ...none, source: sourceState(), pendingSource: json("B", 2) })).toBe(
        true,
      );
      // Before the boot's first /api/mode answers, nothing is offered.
      expect(importOffered({ ...none, sourceKindUnresolved: true })).toBe(false);
    });
  });
});

describe("resyncOnOpen", () => {
  it("resolves false, never rejects, when the sync throws", async () => {
    const spy = vi
      .spyOn(useReviewStore.getState().actions, "beginResync")
      .mockImplementation(() => {
        throw new Error("boom");
      });
    try {
      await expect(resyncOnOpen()).resolves.toBe(false);
      expect(reconnect).not.toHaveBeenCalled();
    } finally {
      spy.mockRestore();
    }
  });
});
