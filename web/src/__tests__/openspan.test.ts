// web/src/__tests__/openspan.test.ts
// The tracked open span clears whenever the STORED state says it ended or
// vanished (#868), whichever door the change came through: one store
// subscription, so every case below drives a different store write.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import minimal from "../../fixtures/minimal.json" with { type: "json" };
import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { useUiStore } from "../ui/uiStore";

const START = "2026-07-18T12:01:00+00:00";
const END = "2026-07-18T12:05:00+00:00";

const record = (id: number, endTimestamp: string | null = null) => ({
  id,
  timestamp: START,
  label: `span ${id}`,
  source: "manual",
  color: "#888888",
  dash: "dash",
  end_timestamp: endTimestamp,
});

let uninstall: () => void;

function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

/** Track an open span (id 9) in the stored session, as Start span does. */
function openSpan(): string {
  const sid = hydrate();
  useReviewStore.getState().actions.appendFragment({
    format: 1,
    session: sid,
    events: [record(9)],
  });
  useUiStore.getState().actions.setOpenSpan({ sessionId: sid, eventId: 9 });
  return sid;
}

/** The export document with the stored session's events replaced by `events`;
 * `keepSession: false` renames the session, so the stored one goes missing. */
function docWith(sid: string, events: unknown[], keepSession = true): string {
  const doc = JSON.parse(JSON.stringify(minimal));
  const session = doc.sessions.find((s: { id: string }) => s.id === sid);
  session.events = events;
  if (!keepSession) session.id = "some-other-session";
  return JSON.stringify(doc);
}

beforeEach(() => {
  uninstall = installOpenSpanReconciler();
});

afterEach(() => {
  uninstall();
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

describe("open-span reconciler", () => {
  it("an HTTP end of the open span clears it", () => {
    const sid = openSpan();
    useReviewStore
      .getState()
      .actions.appendFragment({ format: 1, session: sid, events: [record(9, END)] });
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("deleting it clears it", () => {
    const sid = openSpan();
    useReviewStore
      .getState()
      .actions.appendFragment({ format: 1, session: sid, deleted_event_ids: [9] });
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("a change to another event or session leaves it", () => {
    const sid = openSpan();
    const { actions } = useReviewStore.getState();
    actions.appendFragment({ format: 1, session: sid, events: [record(10, END)] });
    actions.appendFragment({ format: 1, session: sid, events: [record(9)] });
    actions.appendFragment({ format: 1, session: "elsewhere", events: [record(9, END)] });
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 });
  });

  it("an SSE batch ending the open span clears it", () => {
    const sid = openSpan();
    useReviewStore.getState().actions.appendFragments([
      { format: 1, session: sid, events: [record(10)] },
      { format: 1, session: sid, events: [record(9, END)] },
    ]);
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("a reconnect resync where it ended clears it", () => {
    const sid = openSpan();
    useReviewStore.getState().actions.resyncMonitorSessions(docWith(sid, [record(9, END)]), "live");
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("a reconnect resync where it was deleted clears it", () => {
    const sid = openSpan();
    useReviewStore.getState().actions.resyncMonitorSessions(docWith(sid, []), "live");
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("a reconnect resync where its session is missing clears it", () => {
    const sid = openSpan();
    useReviewStore
      .getState()
      .actions.resyncMonitorSessions(docWith(sid, [record(9)], false), "live");
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("a reconnect resync where it is still open leaves it", () => {
    const sid = openSpan();
    useReviewStore.getState().actions.resyncMonitorSessions(docWith(sid, [record(9)]), "live");
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 });
  });

  it("a fragment whose event row is dropped as invalid does not clear it", () => {
    const sid = openSpan();
    const before = useReviewStore.getState().sessions;
    // Paired with a valid change to another event in the same call, so
    // `sessions` really changes and the reconciler runs on it.
    useReviewStore.getState().actions.appendFragments([
      { format: 1, session: sid, events: [record(9, "not a time")] },
      { format: 1, session: sid, events: [record(10)] },
    ]);
    expect(useReviewStore.getState().sessions).not.toBe(before);
    expect(useReviewStore.getState().warnings.length).toBeGreaterThan(0);
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 });
  });
});

// #868, fix round 2: a resync replays a logged mutation onto its snapshot
// ONLY when the server ordered the mutation after the snapshot's capture,
// read from the two observed_at stamps (strictly increasing, microsecond
// resolution, event_ops.utc_now). A snapshot that already reflects the
// mutation, or a later remote change to the same event, wins.
describe("open-span reconciler across a resync that raced a Start span", () => {
  const STARTED_AT = "2026-07-18T12:01:00.000100+00:00"; // our Start's response stamp
  const CAPTURED_AT = "2026-07-18T12:01:00.000200+00:00"; // 100 us later

  /** Start span answered (and logged) while a resync is in flight. */
  function startDuringResync(observed: string): { sid: string; token: number } {
    const sid = hydrate();
    const { actions } = useReviewStore.getState();
    const token = actions.beginResync();
    actions.appendFragment({ format: 1, session: sid, events: [record(9)], observed_at: observed });
    useUiStore.getState().actions.setOpenSpan({ sessionId: sid, eventId: 9 });
    return { sid, token };
  }

  function snapshot(sid: string, events: unknown[], observedAt: string | null): string {
    const doc = JSON.parse(docWith(sid, events));
    const session = doc.sessions.find((s: { id: string }) => s.id === sid);
    if (observedAt === null) delete session.observed_at;
    else session.observed_at = observedAt;
    return JSON.stringify(doc);
  }

  const ids = () => useReviewStore.getState().sessions[0]?.events.map((e) => e.id);

  it("(a) a remote end before capture: the snapshot's end stands and the span clears", () => {
    const { sid, token } = startDuringResync(STARTED_AT);
    const text = snapshot(sid, [record(9, END)], CAPTURED_AT);
    useReviewStore.getState().actions.resyncMonitorSessions(text, "live", token);
    const ev = useReviewStore.getState().sessions[0]?.events.find((e) => e.id === 9);
    expect(ev?.end_timestamp).toBe(END);
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("(b) a remote delete before capture: the event stays gone and the span clears", () => {
    const { sid, token } = startDuringResync(STARTED_AT);
    const text = snapshot(sid, [], CAPTURED_AT);
    useReviewStore.getState().actions.resyncMonitorSessions(text, "live", token);
    expect(ids()).not.toContain(9);
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("(c) a Start ordered after capture is replayed, and the span is kept", () => {
    const { sid, token } = startDuringResync(CAPTURED_AT);
    const text = snapshot(sid, [], STARTED_AT);
    useReviewStore.getState().actions.resyncMonitorSessions(text, "live", token);
    expect(ids()).toContain(9);
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 });
  });

  it.each([
    ["a tie", STARTED_AT, STARTED_AT],
    ["no snapshot stamp", STARTED_AT, null],
  ])("(d) %s: the snapshot wins", (_name, fragStamp, snapStamp) => {
    const { sid, token } = startDuringResync(fragStamp);
    const text = snapshot(sid, [], snapStamp);
    useReviewStore.getState().actions.resyncMonitorSessions(text, "live", token);
    expect(ids()).not.toContain(9);
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("(d) a logged fragment with no stamp: the snapshot wins", () => {
    const sid = hydrate();
    const { actions } = useReviewStore.getState();
    const token = actions.beginResync();
    actions.appendFragment({ format: 1, session: sid, events: [record(9)] });
    actions.resyncMonitorSessions(snapshot(sid, [], CAPTURED_AT), "live", token);
    expect(ids()).not.toContain(9);
  });
});
