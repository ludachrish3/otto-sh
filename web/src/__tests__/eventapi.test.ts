// web/src/__tests__/eventapi.test.ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json";
import { createEvent, deleteEvent, EventApiError, endEvent, updateEvent } from "../data/eventApi";
import { useReviewStore } from "../data/reviewStore";

// minimal.json's first session id — see the fixture; hydrate once per test so
// the synthetic fragments have a session to land on.
function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

const record = (id: number, label: string) => ({
  id,
  timestamp: "2026-07-18T12:01:00+00:00",
  label,
  source: "manual",
  color: "#888888",
  dash: "dash",
});

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("eventApi", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn()));
  afterEach(() => vi.unstubAllGlobals());

  it("createEvent POSTs and upserts the response into the active session", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(7, "deploy"), 201));
    const before = useReviewStore.getState().sessions[0].events.length;
    await createEvent(sid, { label: "deploy" });
    const events = useReviewStore.getState().sessions[0].events;
    expect(events).toHaveLength(before + 1);
    expect(events.at(-1)).toMatchObject({ id: 7, label: "deploy" });
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("a duplicate SSE echo of the same record is a no-op (upsert by id)", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValue(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    const after = useReviewStore.getState().sessions[0].events.length;
    // the echo the live stream would deliver:
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: sid,
      events: [record(7, "deploy")],
    });
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(after);
  });

  it("updateEvent replaces the row in place", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "renamed")));
    await updateEvent(sid, 7, { label: "renamed" });
    const events = useReviewStore.getState().sessions[0].events;
    expect(events.filter((e) => e.id === 7)).toHaveLength(1);
    expect(events.find((e) => e.id === 7)?.label).toBe("renamed");
  });

  it("deleteEvent removes the row", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteEvent(sid, 7);
    expect(useReviewStore.getState().sessions[0].events.find((e) => e.id === 7)).toBeUndefined();
  });

  it("a non-2xx surfaces the server's error and applies nothing", async () => {
    const sid = hydrate();
    const before = useReviewStore.getState().sessions[0].events.length;
    vi.mocked(fetch).mockResolvedValue(okJson({ error: "archive is locked" }, 409));
    await expect(createEvent(sid, { label: "x" })).rejects.toThrow(EventApiError);
    await expect(
      createEvent(sid, { label: "x" }).catch((e) => Promise.reject(e.message)),
    ).rejects.toMatch("archive is locked");
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(before);
  });

  // The routes' own semantic checks answer `{"error": "..."}` (covered
  // above), but a body that fails FastAPI's own Pydantic validation (before
  // the route ever runs — e.g. a missing required field) answers with
  // FastAPI's own 422 shape instead: `{"detail": [{msg, loc, type}, ...]}`.
  // errorMessage must read that shape too, or a validation failure would
  // fall back to the generic "Request failed (422)" instead of the field
  // error FastAPI actually reported.
  it("a FastAPI body-validation 422 ({detail: [...]}) surfaces its msg", async () => {
    const sid = hydrate();
    const before = useReviewStore.getState().sessions[0].events.length;
    vi.mocked(fetch).mockResolvedValue(
      okJson({ detail: [{ loc: ["body", "label"], msg: "Field required", type: "missing" }] }, 422),
    );
    await expect(
      createEvent(sid, { label: "x" }).catch((e) => Promise.reject(e.message)),
    ).rejects.toMatch("Field required");
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(before);
  });

  it("a network failure surfaces as EventApiError and applies nothing", async () => {
    const sid = hydrate();
    const before = useReviewStore.getState().sessions[0].events.length;
    vi.mocked(fetch).mockRejectedValue(new TypeError("fetch failed"));
    await expect(createEvent(sid, { label: "x" })).rejects.toThrow(EventApiError);
    await expect(
      createEvent(sid, { label: "x" }).catch((e) => Promise.reject(e.message)),
    ).rejects.toMatch("Network error");
    expect(useReviewStore.getState().sessions[0].events).toHaveLength(before);
  });

  it("endEvent POSTs to the .../end path and upserts the response", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    const ended = { ...record(7, "deploy"), end_timestamp: "2026-07-18T12:05:00+00:00" };
    vi.mocked(fetch).mockResolvedValueOnce(okJson(ended));
    await endEvent(sid, 7);
    const events = useReviewStore.getState().sessions[0].events;
    expect(events.filter((e) => e.id === 7)).toHaveLength(1);
    expect(events.find((e) => e.id === 7)?.end_timestamp).toBe("2026-07-18T12:05:00+00:00");
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event/7/end`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});
