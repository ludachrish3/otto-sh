// web/src/__tests__/eventapi.test.ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json" with { type: "json" };
import { createEvent, deleteEvent, endEvent, revertEvent, updateEvent } from "../data/eventApi";
import { EventApiError } from "../data/eventApiError";
import { EventChangedError } from "../data/eventChangedError";
import { useReviewStore } from "../data/reviewStore";
import { importLocalFile } from "../data/source";
import { displayEndMs } from "../data/time";
import { useUndoHistory } from "../data/undoHistory";

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
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy")));
    await deleteEvent(sid, 7);
    expect(useReviewStore.getState().sessions[0].events.find((e) => e.id === 7)).toBeUndefined();
  });

  it("deleteEvent resolves to the deleted record", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy")));
    expect(await deleteEvent(sid, 7)).toEqual(record(7, "deploy"));
  });

  it("a 409 changed throws EventChangedError with current and the observed header", async () => {
    const sid = hydrate();
    const res = new Response(
      JSON.stringify({ error: "changed", code: "changed", current: record(7, "other") }),
      { status: 409, headers: { "X-Otto-Observed-At": "2026-07-18T12:05:00.000001+00:00" } },
    );
    vi.mocked(fetch).mockResolvedValueOnce(res);
    const err = await revertEvent(sid, 7, record(7, "a"), record(7, "b")).catch((e) => e);
    expect(err).toBeInstanceOf(EventChangedError);
    expect(err.status).toBe(409);
    expect(err.current).toEqual(record(7, "other"));
    expect(err.observed).toBe("2026-07-18T12:05:00.000001+00:00");
  });

  it("a 409 changed with a deleted event carries current null", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(
      okJson({ error: "changed", code: "changed", current: null }, 409),
    );
    const err = await revertEvent(sid, 7, record(7, "a"), null).catch((e) => e);
    expect(err).toBeInstanceOf(EventChangedError);
    expect(err.current).toBeNull();
  });

  // A conflict's `current` passes the same check as a success's record: an
  // undo recovering a lost response applies it under its id.
  it("a 409 changed whose current has no id is refused, not an EventChangedError", async () => {
    const sid = hydrate();
    const { id: _id, ...anonymous } = record(7, "other");
    vi.mocked(fetch).mockResolvedValueOnce(
      okJson({ error: "changed", code: "changed", current: anonymous }, 409),
    );
    const err = await revertEvent(sid, 7, record(7, "a"), record(7, "b")).catch((e) => e);
    expect(err).toBeInstanceOf(EventApiError);
    expect(err).not.toBeInstanceOf(EventChangedError);
    expect(err.message).toMatch("no event id");
  });

  it("a 409 changed without a current key is malformed, not a deletion", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson({ error: "changed", code: "changed" }, 409));
    const err = await revertEvent(sid, 7, record(7, "a"), null).catch((e) => e);
    expect(err).toBeInstanceOf(EventApiError);
    expect(err).not.toBeInstanceOf(EventChangedError);
    expect(err.status).toBe(409);
  });

  it("a 409 busy is a plain EventApiError", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson({ error: "locked", code: "busy" }, 409));
    const err = await revertEvent(sid, 7, record(7, "a"), null).catch((e) => e);
    expect(err).toBeInstanceOf(EventApiError);
    expect(err).not.toBeInstanceOf(EventChangedError);
    expect(err.status).toBe(409);
  });

  it("a network error has status null", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("down"));
    const err = await deleteEvent(sid, 7).catch((e) => e);
    expect(err).toBeInstanceOf(EventApiError);
    expect(err.status).toBeNull();
    expect(err.observed).toBeNull();
  });

  it("revertEvent with write null applies a deletion", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(okJson(null));
    expect(await revertEvent(sid, 7, record(7, "deploy"), null)).toBeNull();
    expect(useReviewStore.getState().sessions[0].events.find((e) => e.id === 7)).toBeUndefined();
    const [path, init] = vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
    expect(path).toBe(`/api/session/${encodeURIComponent(sid)}/event/7/revert`);
    expect(JSON.parse(init.body as string)).toEqual({ expect: record(7, "deploy"), write: null });
  });

  it("revertEvent applies the record with the observed stamp", async () => {
    const sid = hydrate();
    const later = "2026-07-18T12:10:00+00:00";
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(record(7, "back")), {
        status: 200,
        headers: { "X-Otto-Observed-At": later },
      }),
    );
    expect(await revertEvent(sid, 7, null, record(7, "back"))).toEqual(record(7, "back"));
    const s = useReviewStore.getState().sessions[0];
    expect(s.events.find((e) => e.id === 7)?.label).toBe("back");
    expect(displayEndMs(s)).toBe(Date.parse(later));
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

  // otto never invents identity: a success response is the server's record,
  // and the server always names its id. One without fails loud instead of
  // filing the event (and its history entry) under a made-up id.
  it.each([
    ["createEvent", (sid: string) => createEvent(sid, { label: "x" })],
    ["updateEvent", (sid: string) => updateEvent(sid, 7, { label: "x" })],
    ["endEvent", (sid: string) => endEvent(sid, 7)],
    ["deleteEvent", (sid: string) => deleteEvent(sid, 7)],
    ["revertEvent", (sid: string) => revertEvent(sid, 7, null, record(7, "x"))],
  ])("%s: a response record without an id rejects and applies nothing", async (_, call) => {
    const sid = hydrate();
    const before = useReviewStore.getState().sessions[0].events;
    const { id: _id, ...anonymous } = record(7, "x");
    vi.mocked(fetch).mockResolvedValueOnce(okJson(anonymous));
    const err = await call(sid).catch((e) => e);
    expect(err).toBeInstanceOf(EventApiError);
    expect(err.message).toMatch("no event id");
    expect(useReviewStore.getState().sessions[0].events).toEqual(before);
    expect(useUndoHistory.getState().undo).toEqual([]);
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

  // #992: a write refused as `changed` carries the event as the server holds
  // it; the shared mutation path applies it to the tab, so the tab shows the
  // saved event instead of the stale one it sent from.
  describe("a 409 changed applies current (#992)", () => {
    const changed = (current: unknown) =>
      okJson({ error: "the event was changed elsewhere", code: "changed", current }, 409);
    const stored = (id: number) =>
      useReviewStore.getState().sessions[0]?.events.find((e) => e.id === id);
    function seed(sid: string): void {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: sid,
        events: [record(7, "stale")],
      });
    }

    it.each([
      ["updateEvent", (sid: string) => updateEvent(sid, 7, { color: "#2ca02c" })],
      ["endEvent", (sid: string) => endEvent(sid, 7)],
      ["deleteEvent", (sid: string) => deleteEvent(sid, 7)],
    ])("%s: current is applied to the review store", async (_, call) => {
      const sid = hydrate();
      seed(sid);
      vi.mocked(fetch).mockResolvedValueOnce(changed(record(7, "saved elsewhere")));
      const err = await call(sid).catch((e) => e);
      expect(err).toBeInstanceOf(EventChangedError);
      expect(stored(7)?.label).toBe("saved elsewhere");
      expect(useUndoHistory.getState().undo).toEqual([]);
    });

    it("a delete answered with current null drops the event", async () => {
      const sid = hydrate();
      seed(sid);
      vi.mocked(fetch).mockResolvedValueOnce(changed(null));
      const err = await deleteEvent(sid, 7).catch((e) => e);
      expect(err).toBeInstanceOf(EventChangedError);
      expect(stored(7)).toBeUndefined();
    });

    it("a 409 that lands after a source switch applies nothing", async () => {
      const sid = hydrate();
      seed(sid);
      let release!: (r: Response) => void;
      vi.mocked(fetch).mockReturnValueOnce(
        new Promise<Response>((res) => {
          release = res;
        }),
      );
      const pending = updateEvent(sid, 7, { color: "#2ca02c" });
      // The imported file carries a session with the same id.
      await importLocalFile(new File([JSON.stringify(minimal)], "minimal.json"));
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: sid,
        events: [record(7, "imported")],
      });
      release(changed(record(7, "saved elsewhere")));
      expect(await pending).toEqual({ stale: true });
      expect(stored(7)?.label).toBe("imported");
    });
  });

  // #866: the response header carries the server's time, so the mutation's
  // own applied fragment raises the horizon even with the SSE echo withheld.
  describe("X-Otto-Observed-At", () => {
    // minimal.json's last sample; the collector has stalled since.
    const T = Date.parse("2026-07-01T08:30:00Z");
    const later = new Date(T + 600_000).toISOString();
    const stamped = (body: unknown, status: number) =>
      new Response(body === null ? null : JSON.stringify(body), {
        status,
        headers: { "X-Otto-Observed-At": later },
      });

    it("HTTP-only: with SSE withheld, a successful Mark on a stalled collector is visible at once", async () => {
      const sid = hydrate();
      const mark = { ...record(7, "mark"), timestamp: later };
      vi.mocked(fetch).mockResolvedValueOnce(stamped(mark, 201));
      await createEvent(sid, { label: "mark" });
      const s = useReviewStore.getState().sessions[0];
      expect(s.endMs).toBe(T); // metric-only, untouched
      expect(displayEndMs(s)).toBe(T + 600_000);
      const windowMs = 300_000;
      const ts = Date.parse(mark.timestamp);
      expect(ts).toBeGreaterThanOrEqual(displayEndMs(s) - windowMs);
      expect(ts).toBeLessThanOrEqual(displayEndMs(s));
    });

    it("deleteEvent applies the header too", async () => {
      const sid = hydrate();
      vi.mocked(fetch).mockResolvedValueOnce(stamped(record(7, "deploy"), 200));
      await deleteEvent(sid, 7);
      expect(displayEndMs(useReviewStore.getState().sessions[0])).toBe(T + 600_000);
    });

    it("a response without the header leaves the horizon alone", async () => {
      const sid = hydrate();
      vi.mocked(fetch).mockResolvedValueOnce(okJson(record(7, "deploy"), 201));
      await createEvent(sid, { label: "deploy" });
      expect(displayEndMs(useReviewStore.getState().sessions[0])).toBe(T);
    });
  });
});
