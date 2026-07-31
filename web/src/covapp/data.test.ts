// data.ts's contract (see task-2 brief): window.__OTTO_COV__ validation +
// guard state, and loadFileChunk's script-injection loader. jsdom does not
// execute injected <script src> tags (no runScripts/resources config), so
// every test here intercepts document.head.appendChild and drives the
// callback/onerror paths by hand — the same technique a real browser
// exercises via the classic script Task 1 emits, just triggered manually.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  _resetForTests,
  dataGuard,
  getIndex,
  loadFileChunk,
  loadTicketChunk,
  StampMismatchError,
} from "./data";
import type { FileChunk, IndexPayload, TicketChunk } from "./types";

function emptyStats() {
  return {
    lines: { total: 0, hit: 0, per_tier: {}, asserted_per_tier: {}, asserted_only: 0 },
    branches: { total: 0, hit: 0, per_tier: {} },
    flags: { stale: 0, aging: 0, excluded: 0 },
    ctx_lines: {},
  };
}

function makeIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return {
    format: 2,
    stamp: "stamp-1",
    generated_at: "2026-07-25 00:00 UTC",
    otto_version: "0.0.0",
    project_name: "otto example product",
    tier_order: [],
    tier_labels: {},
    tier_colors: {},
    state_colors: { uncovered: "#f4a9a8", excluded: "grey", stale: "violet", aging: "tan" },
    thresholds: { high: 80, medium: 70 },
    stat_types: ["line", "branch", "decision"],
    runs: [],
    overrides: [],
    run_contrib: {},
    total_lines: 0,
    tree: { name: "otto example product", dirs: [], files: [], stats: emptyStats() },
    tickets: [],
    tickets_totals: { owned: 0, covered: 0, uncovered: 0, per_tier: {}, asserted: {} },
    ...overrides,
  };
}

function makeChunk(overrides: Partial<FileChunk> = {}): FileChunk {
  return {
    stamp: "stamp-1",
    chunk: "a_b.c",
    path: "a/b.c",
    source: "int main() {}\n",
    lines: {},
    excluded: [],
    ...overrides,
  };
}

beforeEach(() => {
  _resetForTests();
});

afterEach(() => {
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
  _resetForTests();
  vi.restoreAllMocks();
});

describe("dataGuard / getIndex", () => {
  it("is 'missing' when window.__OTTO_COV__ is absent, and getIndex returns null", () => {
    expect(dataGuard()).toBe("missing");
    expect(getIndex()).toBeNull();
  });

  it("is 'format' when the payload's format does not match EXPECTED_DATA_FORMAT", () => {
    window.__OTTO_COV__ = makeIndex({ format: 1 });
    expect(dataGuard()).toBe("format");
  });

  it("is 'ok' for a well-formed payload, and getIndex returns it", () => {
    const index = makeIndex();
    window.__OTTO_COV__ = index;
    expect(dataGuard()).toBe("ok");
    expect(getIndex()).toEqual(index);
  });

  it("tolerates an empty tier_order without crashing (data-less store)", () => {
    window.__OTTO_COV__ = makeIndex({ tier_order: [] });
    expect(dataGuard()).toBe("ok");
    expect(getIndex()?.tier_order).toEqual([]);
  });
});

describe("loadFileChunk", () => {
  it("injects a <script src='./cov_data/files/<chunk>.js'> and resolves via the registered callback", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const promise = loadFileChunk("a_b.c");
    expect(appendSpy).toHaveBeenCalledTimes(1);
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    expect(script.tagName).toBe("SCRIPT");
    // jsdom resolves `.src` to an absolute URL, so assert the path that
    // resolution reflects, not the raw "./..." string data.ts assigns.
    expect(new URL(script.src).pathname.endsWith("/cov_data/files/a_b.c.js")).toBe(true);
    expect(script.getAttribute("src")).toBe("./cov_data/files/a_b.c.js");

    const chunk = makeChunk();
    window.__OTTO_COV_FILE__?.(chunk);

    await expect(promise).resolves.toEqual(chunk);
  });

  it("encodes URL-reserved characters in the chunk id when building the script src", () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const chunk = "product_100%_ready#.c";
    // Deliberately left pending: this case asserts only the src that injection
    // builds, and nothing ever calls __OTTO_COV_FILE__ for this id. `void`
    // marks the non-await (complexity/noVoid is off so it stays writable).
    void loadFileChunk(chunk);

    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    expect(script.getAttribute("src")).toBe(`./cov_data/files/${encodeURIComponent(chunk)}.js`);
  });

  it("caches a resolved chunk — a second call does not inject another script", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const first = loadFileChunk("a_b.c");
    window.__OTTO_COV_FILE__?.(makeChunk());
    await first;

    const second = await loadFileChunk("a_b.c");
    expect(second).toEqual(makeChunk());
    expect(appendSpy).toHaveBeenCalledTimes(1);
  });

  it("rejects with StampMismatchError when the chunk's stamp does not match the index's", async () => {
    window.__OTTO_COV__ = makeIndex({ stamp: "current-stamp" });

    const promise = loadFileChunk("a_b.c");
    window.__OTTO_COV_FILE__?.(makeChunk({ stamp: "stale-stamp" }));

    await expect(promise).rejects.toBeInstanceOf(StampMismatchError);
  });

  it("rejects with a plain Error when the injected script fails to load", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const promise = loadFileChunk("missing_chunk.c");
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    script.onerror?.(new Event("error"));

    await expect(promise).rejects.toBeInstanceOf(Error);
    await expect(promise).rejects.not.toBeInstanceOf(StampMismatchError);
  });

  it("resolves every waiter when two callers request the same in-flight chunk", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const first = loadFileChunk("a_b.c");
    const second = loadFileChunk("a_b.c");
    expect(appendSpy).toHaveBeenCalledTimes(1); // one in-flight load, not two

    const chunk = makeChunk();
    window.__OTTO_COV_FILE__?.(chunk);

    await expect(first).resolves.toEqual(chunk);
    await expect(second).resolves.toEqual(chunk);
  });
});

function makeTicketChunk(overrides: Partial<TicketChunk> = {}): TicketChunk {
  return {
    stamp: "stamp-1",
    id: "PROJ-1",
    files: [],
    ...overrides,
  };
}

// loadTicketChunk (Task 10; stamp handling added in fix round 1):
// loadFileChunk's per-ticket counterpart, same script-injection +
// callback-resolution mechanism against `cov_data/tickets/<chunk>.js` /
// `window.__OTTO_COV_TICKET__` instead, INCLUDING the same stamp-mismatch
// guard (design §5: every data chunk carries the report stamp) — ticket
// chunks didn't carry one until this round.
describe("loadTicketChunk", () => {
  it("injects a <script src='./cov_data/tickets/<chunk>.js'> and resolves via the registered callback", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const promise = loadTicketChunk("PROJ-1");
    expect(appendSpy).toHaveBeenCalledTimes(1);
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    expect(script.tagName).toBe("SCRIPT");
    expect(new URL(script.src).pathname.endsWith("/cov_data/tickets/PROJ-1.js")).toBe(true);
    expect(script.getAttribute("src")).toBe("./cov_data/tickets/PROJ-1.js");

    const chunk = makeTicketChunk();
    window.__OTTO_COV_TICKET__?.("PROJ-1", chunk);

    await expect(promise).resolves.toEqual(chunk);
  });

  it("encodes URL-reserved characters in the chunk id when building the script src", () => {
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const chunk = "product_100%_ready#.c";
    // Deliberately left pending, as in the file-chunk case above: nothing ever
    // calls __OTTO_COV_TICKET__ for this id, and _resetForTests() drops the
    // waiter rather than rejecting it, so no rejection can escape.
    void loadTicketChunk(chunk);

    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    expect(script.getAttribute("src")).toBe(`./cov_data/tickets/${encodeURIComponent(chunk)}.js`);
  });

  it("caches a resolved chunk — a second call does not inject another script", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");
    const chunk = makeTicketChunk();

    const first = loadTicketChunk("PROJ-1");
    window.__OTTO_COV_TICKET__?.("PROJ-1", chunk);
    await first;

    const second = await loadTicketChunk("PROJ-1");
    expect(second).toEqual(chunk);
    expect(appendSpy).toHaveBeenCalledTimes(1);
  });

  it("keys waiters by the callback's chunk-id argument, not TicketChunk.id", async () => {
    // Task 9's emitter names the chunk after mangle_path(ticket_id), which
    // need not equal the ticket id itself (`TicketChunk.id`) — this proves
    // the loader resolves via the callback's FIRST argument, not by reading
    // chunk.id off the payload the way loadFileChunk reads chunk.chunk.
    window.__OTTO_COV__ = makeIndex();
    const promise = loadTicketChunk("mangled_chunk_name");
    const chunk = makeTicketChunk({ id: "PROJ-1" });
    window.__OTTO_COV_TICKET__?.("mangled_chunk_name", chunk);
    await expect(promise).resolves.toEqual(chunk);
  });

  it("rejects with StampMismatchError when the chunk's stamp does not match the index's", async () => {
    window.__OTTO_COV__ = makeIndex({ stamp: "current-stamp" });

    const promise = loadTicketChunk("PROJ-1");
    window.__OTTO_COV_TICKET__?.("PROJ-1", makeTicketChunk({ stamp: "stale-stamp" }));

    await expect(promise).rejects.toBeInstanceOf(StampMismatchError);
  });

  it("rejects with a plain Error when the injected script fails to load", async () => {
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const promise = loadTicketChunk("missing_ticket");
    const script = appendSpy.mock.calls[0][0] as HTMLScriptElement;
    script.onerror?.(new Event("error"));

    await expect(promise).rejects.toBeInstanceOf(Error);
    await expect(promise).rejects.not.toBeInstanceOf(StampMismatchError);
  });

  it("resolves every waiter when two callers request the same in-flight chunk", async () => {
    window.__OTTO_COV__ = makeIndex();
    const appendSpy = vi.spyOn(document.head, "appendChild");

    const first = loadTicketChunk("PROJ-1");
    const second = loadTicketChunk("PROJ-1");
    expect(appendSpy).toHaveBeenCalledTimes(1); // one in-flight load, not two

    const chunk = makeTicketChunk();
    window.__OTTO_COV_TICKET__?.("PROJ-1", chunk);

    await expect(first).resolves.toEqual(chunk);
    await expect(second).resolves.toEqual(chunk);
  });
});
