// data.ts's contract (see task-2 brief): window.__OTTO_COV__ validation +
// guard state, and loadFileChunk's script-injection loader. jsdom does not
// execute injected <script src> tags (no runScripts/resources config), so
// every test here intercepts document.head.appendChild and drives the
// callback/onerror paths by hand — the same technique a real browser
// exercises via the classic script Task 1 emits, just triggered manually.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetForTests, dataGuard, getIndex, loadFileChunk, StampMismatchError } from "./data";
import type { FileChunk, IndexPayload } from "./types";

function emptyStats() {
  return {
    lines: { total: 0, hit: 0, per_tier: {} },
    branches: { total: 0, hit: 0, per_tier: {} },
    flags: { stale: 0, aging: 0, excluded: 0 },
    ctx_lines: {},
  };
}

function makeIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return {
    format: 1,
    stamp: "stamp-1",
    generated_at: "2026-07-25 00:00 UTC",
    otto_version: "0.0.0",
    project_name: "otto example product",
    tier_order: [],
    tier_labels: {},
    tier_colors: {},
    state_colors: {},
    thresholds: { high: 80, medium: 70 },
    stat_types: ["line", "branch", "decision"],
    runs: [],
    run_contrib: {},
    total_lines: 0,
    tree: { name: "otto example product", dirs: [], files: [], stats: emptyStats() },
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
    window.__OTTO_COV__ = makeIndex({ format: 2 });
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
    loadFileChunk(chunk);

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
