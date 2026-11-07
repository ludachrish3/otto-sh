// A file import is a SOURCE SWITCH (#873): once importLocalFile has loaded a
// file, nothing the live server started before it — the SSE stream, a
// pending boot fetch, a reconnect resync, an in-flight event mutation — may
// touch the imported data, and every source-bound piece of UI state (edit
// rights, the open span, the editor, the sweep, the mark popover) is gone.
// Boots live through bootstrapFromServer's fetch seam, as bootstrap.test.ts
// and bootstrap.resync.test.ts do, with the stream itself mocked.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

const stop = vi.fn();
let capturedResync: (() => Promise<unknown>) | null = null;
const startStream = vi.fn((opts: { resync?: () => Promise<unknown> }) => {
  capturedResync = opts.resync ?? null;
  return stop;
});
vi.mock("../data/stream", () => ({ startStream }));

// Imported AFTER the mock so bootstrap.ts picks up the mocked module.
const { bootstrapFromServer } = await import("../data/bootstrap");
const { deleteEvent, isStale, reportMutationFailure, updateEvent } = await import(
  "../data/eventApi"
);
const { useReviewStore } = await import("../data/reviewStore");
const { importLocalFile } = await import("../data/source");
const { startSpan } = await import("../shell/marking");
const { useUiStore } = await import("../ui/uiStore");

const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");
const KITCHEN = readFileSync(join(__dir, "../../fixtures/kitchen-sink.json"), "utf-8");

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

const liveMode: Route = () =>
  Promise.resolve(jsonResponse({ mode: "live", source: null, editable: true }));

/** Boots live and editable against a server whose snapshot is `snapshot`. */
async function bootLive(snapshot = MINIMAL, extra: Record<string, Route> = {}) {
  const routes: Record<string, Route> = {
    "/api/mode": liveMode,
    "/api/monitor_sessions": () => Promise.resolve(new Response(snapshot, { status: 200 })),
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
    connection: "connecting",
  });
  useUiStore.setState({ eventEditor: null, sweepArmed: false, openSpan: null, markPopover: null });
  stop.mockClear();
  startStream.mockClear();
  capturedResync = null;
});

describe("importLocalFile while live (#873)", () => {
  it("(a) leaves live mode, stops the stream and clears every source-bound UI state", async () => {
    await bootLive();
    const session = useReviewStore.getState().sessions[0];
    if (!session) throw new Error("live boot loaded no session");
    const ui = useUiStore.getState().actions;
    ui.setOpenSpan({ sessionId: session.id, eventId: 1 });
    ui.openEventEditor({ kind: "edit", sessionId: session.id, eventId: 1 });
    ui.armSweep();
    ui.openMarkPopover("mark");
    useReviewStore.getState().actions.setRange({ from: session.startMs, to: session.endMs });
    expect(useReviewStore.getState().range).not.toBeNull();

    await importLocalFile(kitchenSinkFile());

    expect(stop).toHaveBeenCalledTimes(1);
    const review = useReviewStore.getState();
    expect(review.mode).toBe("review");
    expect(review.editable).toBe(false);
    expect(review.range).toBeNull();
    expect(review.sourceName).toBe("kitchen-sink.json");
    const after = useUiStore.getState();
    expect(after.openSpan).toBeNull();
    expect(after.eventEditor).toBeNull();
    expect(after.sweepArmed).toBe(false);
    expect(after.markPopover).toBeNull();
  });

  it("(b) a file that fails to parse leaves the live source untouched", async () => {
    await bootLive();

    await importLocalFile(new File(["{not json"], "bad.json"));

    expect(stop).not.toHaveBeenCalled();
    expect(useReviewStore.getState().mode).toBe("live");
    expect(useReviewStore.getState().importError).not.toBeNull();
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

  it("(e) a boot hydrate that answers after the import is dropped", async () => {
    const held = deferred<Response>();
    const fetchMock = stubServer({
      "/api/mode": liveMode,
      "/api/monitor_sessions": () => held.promise,
    });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/monitor_sessions"));

    await importLocalFile(kitchenSinkFile());
    held.resolve(new Response(MINIMAL, { status: 200 }));
    await boot;

    const state = useReviewStore.getState();
    expect(state.sourceName).toBe("kitchen-sink.json");
    expect(state.mode).toBe("review");
    expect(startStream).not.toHaveBeenCalled();
  });

  it("(f) a reconnect resync that answers after the import is dropped", async () => {
    const { routes } = await bootLive();
    expect(capturedResync).not.toBeNull();
    const held = deferred<Response>();
    routes["/api/monitor_sessions"] = () => held.promise;

    const resync = capturedResync?.();
    await importLocalFile(kitchenSinkFile());
    const imported = useReviewStore.getState().sessions;
    held.resolve(new Response(MINIMAL, { status: 200 }));
    await resync;

    const state = useReviewStore.getState();
    expect(state.sessions).toBe(imported);
    expect(state.sourceName).toBe("kitchen-sink.json");
  });

  it("(h) a /api/mode answer that arrives after the import is dropped", async () => {
    const held = deferred<Response>();
    const fetchMock = stubServer({ "/api/mode": () => held.promise });
    const boot = bootstrapFromServer();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/mode"));

    await importLocalFile(kitchenSinkFile());
    held.resolve(jsonResponse({ mode: "live", source: null, editable: true }));
    await boot;

    const state = useReviewStore.getState();
    expect(state.mode).toBe("review");
    expect(state.editable).toBe(false);
    expect(state.sourceName).toBe("kitchen-sink.json");
  });
});

describe("(g) a mutation answering after an import of the same session ID", () => {
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
    await bootLive(KITCHEN, { [path(base)]: () => held.promise });
    return { sid, held };
  }

  const eventsOf = () => useReviewStore.getState().sessions[0]?.events ?? [];

  it("updateEvent applies nothing and reports stale", async () => {
    const { sid, held } = await bootWithHeldMutation((base) => `${base}/1`);
    const pending = updateEvent(sid, 1, { label: "renamed" });
    await importLocalFile(kitchenSinkFile());
    const imported = eventsOf();

    held.resolve(jsonResponse(record(1, "renamed")));
    const result = await pending;

    expect(isStale(result)).toBe(true);
    expect(eventsOf()).toBe(imported);
    expect(eventsOf().find((ev) => ev.id === 1)?.label).toBe("config reload");
  });

  it("deleteEvent removes nothing and reports stale", async () => {
    const { sid, held } = await bootWithHeldMutation((base) => `${base}/2`);
    const pending = deleteEvent(sid, 2);
    await importLocalFile(kitchenSinkFile());
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

  it("a mutation that fails after the import reports stale instead of an error", async () => {
    const { sid, held } = await bootWithHeldMutation((base) => `${base}/1`);
    const pending = updateEvent(sid, 1, { label: "renamed" });
    await importLocalFile(kitchenSinkFile());

    held.resolve(jsonResponse({ error: "no such event" }, 404));

    expect(isStale(await pending)).toBe(true);
  });

  it("startSpan adds no event and opens no span", async () => {
    const { held } = await bootWithHeldMutation((base) => base);
    const pending = startSpan("late span");
    await importLocalFile(kitchenSinkFile());
    const imported = eventsOf();

    held.resolve(jsonResponse(record(99, "late span"), 201));
    await pending;

    expect(eventsOf()).toBe(imported);
    expect(eventsOf().some((ev) => ev.id === 99)).toBe(false);
    expect(useUiStore.getState().openSpan).toBeNull();
  });
});

// Each async step re-checks the source in its OWN continuation: an import
// that lands after a helper's last check but before its caller resumes must
// still win. These hold the work exactly at those continuation boundaries.
describe("(i) an import landing between a step's check and its caller's continuation", () => {
  /** A Response stand-in whose body read the test settles by hand. */
  function heldBody(): { response: Response; body: Deferred<string>; read: Deferred<void> } {
    const body = deferred<string>();
    const read = deferred<void>();
    const response = {
      ok: true,
      status: 200,
      text: () => {
        read.resolve();
        return body.promise;
      },
    } as unknown as Response;
    return { response, body, read };
  }

  it("the boot hydrate does not overwrite the file", async () => {
    const held = heldBody();
    stubServer({
      "/api/mode": liveMode,
      "/api/monitor_sessions": () => Promise.resolve(held.response),
    });
    const boot = bootstrapFromServer();
    await held.read.promise;
    const file = heldFile("kitchen-sink.json");
    const importing = importLocalFile(file.file);
    // Same turn, body first: fetchDocumentText's continuation runs (its own
    // check passes), then the import's, then hydrate's.
    held.body.resolve(MINIMAL);
    file.text.resolve(KITCHEN);
    await importing;
    await boot;

    const state = useReviewStore.getState();
    expect(state.sourceName).toBe("kitchen-sink.json");
    expect(state.mode).toBe("review");
    expect(startStream).not.toHaveBeenCalled();
  });

  it("a reconnect resync does not overwrite the file", async () => {
    const { routes } = await bootLive();
    const held = heldBody();
    routes["/api/monitor_sessions"] = () => Promise.resolve(held.response);

    const resync = capturedResync?.();
    await held.read.promise;
    const file = heldFile("kitchen-sink.json");
    const importing = importLocalFile(file.file);
    held.body.resolve(MINIMAL);
    file.text.resolve(KITCHEN);
    await importing;
    await resync;

    expect(useReviewStore.getState().sourceName).toBe("kitchen-sink.json");
  });

  it("startSpan opens no span once the import lands after its mutation applied", async () => {
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
    await bootLive(KITCHEN, { [base]: () => Promise.resolve(jsonResponse(created, 201)) });
    const file = heldFile("kitchen-sink.json");
    const importing = importLocalFile(file.file);
    // Settle the file read from inside the mutation's apply step: the
    // mutation's own check has passed, and the import's continuation is
    // queued ahead of startSpan's.
    const actions = useReviewStore.getState().actions;
    const original = actions.appendFragment;
    const spy = vi.spyOn(actions, "appendFragment").mockImplementation((frag) => {
      original(frag);
      file.text.resolve(KITCHEN);
    });
    try {
      const result = await startSpan("late span");
      await importing;
      expect(isStale(result)).toBe(true);
    } finally {
      spy.mockRestore();
    }

    expect(useReviewStore.getState().sourceName).toBe("kitchen-sink.json");
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

  it("is dropped once an import lands before the handler runs", async () => {
    await bootLive();
    const onFailure = reportMutationFailure("End span");
    await importLocalFile(kitchenSinkFile());
    const warnings = useReviewStore.getState().warnings;

    onFailure(new Error("boom"));

    expect(useReviewStore.getState().warnings).toBe(warnings);
  });
});
