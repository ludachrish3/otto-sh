// The shell's ONLY boot fetch: /api/mode -> (review mode) /api/document.
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
    rawDocument: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
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

  it('mode "live": no document fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ mode: "live", source: null }));
    vi.stubGlobal("fetch", fetchMock);
    await bootstrapFromServer();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(useReviewStore.getState().sessions).toEqual([]);
  });

  it("mode review + document 200: hydrates the store via importText", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/mode") {
        return Promise.resolve(jsonResponse({ mode: "review", source: "run.sqlite" }));
      }
      if (url === "/api/document") {
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
  // /api/document whose body fails validation is NOT a transport failure and
  // must NOT be swallowed. It goes through the same importText path as a
  // user-chosen file, so the store's importError banner surfaces it. Without
  // this test, wrapping the importText call in a try/catch — an easy thing to
  // do while "hardening" the soft-fail path — would silently delete the
  // banner with nothing going red.
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
    // the banner — importText is never reached.
    expect(state.importError).toBeNull();
  });
});
