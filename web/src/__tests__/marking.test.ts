// web/src/__tests__/marking.test.ts
// Fetch-mocked against the real stores, following eventapi.test.ts's idiom:
// marking.ts is a thin imperative wrapper over eventApi.ts + the ui/review
// stores, so the store IS the assertion surface rather than a mock.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json";
import { useReviewStore } from "../data/reviewStore";
import { blankDraft, endOpenSpan, markNow, startSpan } from "../shell/marking";
import { useUiStore } from "../ui/uiStore";

// minimal.json's first session id — see the fixture; hydrate once per test so
// the synthetic fragments (and requireActiveSessionId) have a session to
// land on. Mirrors eventapi.test.ts's hydrate() exactly.
function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

const record = (id: number, label: string, endTimestamp: string | null = null) => ({
  id,
  timestamp: "2026-07-18T12:01:00+00:00",
  label,
  source: "manual",
  color: "#888888",
  dash: "dash",
  end_timestamp: endTimestamp,
});

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function lastRequestBody(): unknown {
  const calls = vi.mocked(fetch).mock.calls;
  const init = calls.at(-1)?.[1] as RequestInit;
  return JSON.parse(init.body as string);
}

describe("marking", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => {
    vi.unstubAllGlobals();
    useReviewStore.setState({
      sessions: [],
      rawMonitorSessions: null,
      sourceName: null,
      warnings: [],
      importError: null,
      activeSessionId: null,
      range: null,
    });
    useUiStore.setState({ openSpan: null });
  });

  it("startSpan creates the event and sets openSpan to the created id", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(9, "soak"), 201));
    await startSpan("soak");
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 });
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(lastRequestBody()).toEqual({ label: "soak" });
  });

  it("endOpenSpan calls the /end route for the open span's id and clears openSpan", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(9, "soak"), 201));
    await startSpan("soak");
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(9, "soak", "2026-07-18T12:05:00+00:00")));
    await endOpenSpan();
    expect(useUiStore.getState().openSpan).toBeNull();
    expect(vi.mocked(fetch)).toHaveBeenLastCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event/9/end`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("endOpenSpan is a no-op with no open span", async () => {
    await endOpenSpan();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("markNow posts with no timestamp field", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(11, "checkpoint"), 201));
    await markNow("checkpoint");
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event`,
      expect.objectContaining({ method: "POST" }),
    );
    const body = lastRequestBody() as Record<string, unknown>;
    expect(body).toEqual({ label: "checkpoint" });
    expect("timestamp" in body).toBe(false);
  });

  it("markNow/startSpan throw when there is no active session", async () => {
    useReviewStore.setState({ activeSessionId: null });
    await expect(markNow("x")).rejects.toThrow("no active monitor session");
    await expect(startSpan("x")).rejects.toThrow("no active monitor session");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("a failed createEvent leaves openSpan untouched and rejects EventApiError upward", async () => {
    hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson({ error: "archive is locked" }, 409));
    await expect(startSpan("soak")).rejects.toThrow("archive is locked");
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("blankDraft anchors a point draft to the session's end with the default styling", () => {
    const session = { id: "s9", endMs: 12_345 } as Parameters<typeof blankDraft>[0];
    expect(blankDraft(session)).toEqual({
      sessionId: "s9",
      timestampMs: 12_345,
      endTimestampMs: null,
      label: "",
      color: "#888888",
      dash: "dash",
    });
  });
});
