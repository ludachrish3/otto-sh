import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it } from "vitest";

import type { MonitorSessionFragment } from "../api/export.gen";
import { observedAtMicros } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { displayEndMs } from "../data/time";

const __dir = dirname(fileURLToPath(import.meta.url));
const DRIFT = readFileSync(join(__dir, "../../fixtures/drift.json"), "utf-8");
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");

function reset() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    windowMs: 900_000,
    editable: false,
  });
}

describe("reviewStore", () => {
  beforeEach(reset);

  it("importMonitorSessions loads sessions and activates the first", () => {
    const ok = useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    expect(ok).toBe(true);
    const s = useReviewStore.getState();
    expect(s.sessions).toHaveLength(3);
    expect(s.activeSessionId).toBe(s.sessions[0].id);
    expect(s.sourceName).toBe("drift.json");
    expect(s.importError).toBeNull();
  });

  it("importMonitorSessions reports errors without clobbering loaded data", () => {
    useReviewStore.getState().actions.importMonitorSessions(MINIMAL, "minimal.json");
    const ok = useReviewStore.getState().actions.importMonitorSessions("{}", "bad.json");
    expect(ok).toBe(false);
    const s = useReviewStore.getState();
    expect(s.importError).toMatch(/format|unversioned/iu);
    expect(s.sessions).toHaveLength(1); // minimal still loaded
    expect(s.sourceName).toBe("minimal.json");
  });

  // Plan 5b follow-ups #3: resyncMonitorSessions (the stream's reconnect
  // path, data/stream.ts's onerror -> resync -> reopen) must preserve a
  // paused view across a transient blip, but NOT strand the shell forever
  // when the monitor server actually restarted mid-tab and handed back a
  // wholly different session set — the old id resolves to nothing, and
  // nothing else re-derives a valid selection.
  describe("resyncMonitorSessions", () => {
    it("id still resolves: range and activeSessionId survive untouched", () => {
      useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
      const s2 = useReviewStore.getState().sessions[1].id;
      useReviewStore.getState().actions.selectSession(s2);
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });

      // A resync of the SAME document: every id (including s2) still
      // resolves in the new snapshot.
      const ok = useReviewStore.getState().actions.resyncMonitorSessions(DRIFT, "drift.json");

      expect(ok).toBe(true);
      const s = useReviewStore.getState();
      expect(s.activeSessionId).toBe(s2); // untouched
      expect(s.range).toEqual({ from: 100, to: 200 }); // untouched
      expect(s.sessions).toHaveLength(3); // sessions data itself DID refresh
    });

    it("id is gone from the new snapshot: falls back to sessions[0] with range null", () => {
      useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
      const s2 = useReviewStore.getState().sessions[1].id;
      useReviewStore.getState().actions.selectSession(s2);
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });
      expect(useReviewStore.getState().activeSessionId).toBe(s2); // sanity: actually selected+paused

      // A resync against MINIMAL: a server restart's fresh snapshot with a
      // completely different (single) session id — s2 resolves to nothing.
      const ok = useReviewStore.getState().actions.resyncMonitorSessions(MINIMAL, "minimal.json");

      expect(ok).toBe(true);
      const s = useReviewStore.getState();
      expect(s.sessions).toHaveLength(1);
      expect(s.activeSessionId).toBe(s.sessions[0].id); // fell back, not stranded on s2
      expect(s.range).toBeNull(); // follow/whole-session, not a dangling pinned range
    });

    it("id is gone AND there are zero sessions in the new snapshot: activeSessionId becomes null, not stale", () => {
      useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
      const s2 = useReviewStore.getState().sessions[1].id;
      useReviewStore.getState().actions.selectSession(s2);
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });

      const empty = JSON.stringify({ format: 1, sessions: [] });
      const ok = useReviewStore.getState().actions.resyncMonitorSessions(empty, "empty.json");

      expect(ok).toBe(true);
      const s = useReviewStore.getState();
      expect(s.sessions).toHaveLength(0);
      expect(s.activeSessionId).toBeNull();
      expect(s.range).toBeNull();
    });

    // #868: what a resync replays onto its snapshot, and what it must not.
    describe("with a token from beginResync", () => {
      const event = (id: number) => ({
        id,
        timestamp: "2026-07-01T08:10:00Z",
        label: `e${id}`,
        source: "manual",
        color: "#888888",
        dash: "dash",
      });
      const withEvents = (events: unknown[]) => {
        const doc = JSON.parse(MINIMAL) as { sessions: Record<string, unknown>[] };
        doc.sessions[0]["events"] = events;
        return JSON.stringify(doc);
      };
      const metric = { timestamp: "2026-07-01T08:30:30Z", host: "solo", label: "CPU %", value: 9 };

      it("does not re-apply a fragment that landed before the resync began", () => {
        const { actions } = useReviewStore.getState();
        actions.importMonitorSessions(MINIMAL, "live");
        const sid = useReviewStore.getState().sessions[0].id;
        actions.appendFragment({ format: 1, session: sid, events: [event(1)], metrics: [metric] });
        const token = actions.beginResync();
        // The snapshot already holds what landed before its request.
        const doc = JSON.parse(withEvents([event(1)])) as {
          sessions: { metrics: unknown[] }[];
        };
        doc.sessions[0].metrics.push(metric);
        const expected = doc.sessions[0].metrics.length;
        expect(actions.resyncMonitorSessions(JSON.stringify(doc), "live", token)).toBe(true);
        const s = useReviewStore.getState().sessions[0];
        expect(s.events.map((e) => e.id)).toEqual([1]);
        expect(s.metrics).toHaveLength(expected);
      });

      it("replays event upserts and deletes, never metric rows, and the log ends with the apply", () => {
        const { actions } = useReviewStore.getState();
        actions.importMonitorSessions(withEvents([event(1), event(2)]), "live");
        const sid = useReviewStore.getState().sessions[0].id;
        const token = actions.beginResync();
        // Server stamps: the snapshot's capture, then the three mutations after it.
        const capturedAt = "2026-07-01T08:31:00.000001+00:00";
        const after = "2026-07-01T08:31:00.000002+00:00";
        actions.appendFragment({ format: 1, session: sid, events: [event(3)], observed_at: after });
        actions.appendFragment({
          format: 1,
          session: sid,
          deleted_event_ids: [2],
          observed_at: after,
        });
        actions.appendFragment({ format: 1, session: sid, metrics: [metric], observed_at: after });
        // The snapshot was captured before all three; it already has event 2.
        const doc = JSON.parse(withEvents([event(1), event(2)])) as {
          sessions: Record<string, unknown>[];
        };
        for (const session of doc.sessions) session["observed_at"] = capturedAt;
        const snapshot = JSON.stringify(doc);
        const metricsInSnapshot = (JSON.parse(snapshot) as { sessions: { metrics: unknown[] }[] })
          .sessions[0].metrics.length;
        expect(actions.resyncMonitorSessions(snapshot, "live", token)).toBe(true);
        let s = useReviewStore.getState().sessions[0];
        expect(s.events.map((e) => e.id).sort()).toEqual([1, 3]);
        expect(s.metrics).toHaveLength(metricsInSnapshot);
        // The token is spent: a later resync with it replays nothing.
        expect(actions.resyncMonitorSessions(snapshot, "live", token)).toBe(true);
        s = useReviewStore.getState().sessions[0];
        expect(s.events.map((e) => e.id).sort()).toEqual([1, 2]);
      });

      it("resync reseeds stamps from the snapshot", () => {
        const { actions } = useReviewStore.getState();
        actions.importMonitorSessions(withEvents([event(1)]), "live");
        const sid = useReviewStore.getState().sessions[0].id;
        // A tombstone far ahead of the snapshot, from before the resync began.
        actions.appendFragment({
          format: 1,
          session: sid,
          deleted_event_ids: [1],
          observed_at: "2026-07-01T08:39:00.000000+00:00",
        });
        const token = actions.beginResync();
        const capturedAt = "2026-07-01T08:31:00.000001+00:00";
        const doc = JSON.parse(withEvents([event(1)])) as { sessions: Record<string, unknown>[] };
        for (const session of doc.sessions) session["observed_at"] = capturedAt;
        expect(actions.resyncMonitorSessions(JSON.stringify(doc), "live", token)).toBe(true);
        let s = useReviewStore.getState().sessions[0];
        const floor = observedAtMicros(capturedAt);
        expect(s.eventFloor).toBe(floor);
        expect([...s.eventStamps]).toEqual([[1, floor]]);
        // The snapshot is the authority now: a change after it applies, though
        // it is older than the tombstone the store held before.
        actions.appendFragment({
          format: 1,
          session: sid,
          events: [{ ...event(1), label: "edited" }],
          observed_at: "2026-07-01T08:32:00.000000+00:00",
        });
        s = useReviewStore.getState().sessions[0];
        expect(s.events.map((e) => e.label)).toEqual(["edited"]);
        // And one older than the snapshot does not.
        actions.appendFragment({
          format: 1,
          session: sid,
          deleted_event_ids: [1],
          observed_at: "2026-07-01T08:31:00.000000+00:00",
        });
        expect(useReviewStore.getState().sessions[0].events.map((e) => e.id)).toEqual([1]);
      });

      it("never lowers a kept session's observedMs", () => {
        const { actions } = useReviewStore.getState();
        actions.importMonitorSessions(MINIMAL, "live");
        const sid = useReviewStore.getState().sessions[0].id;
        const later = "2026-07-01T09:30:00Z";
        actions.appendFragment({ format: 1, session: sid, observed_at: later });
        expect(actions.resyncMonitorSessions(MINIMAL, "live")).toBe(true);
        expect(useReviewStore.getState().sessions[0].observedMs).toBe(Date.parse(later));
      });
    });
  });

  it("selectSession switches and resets the range", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const s2 = useReviewStore.getState().sessions[1].id;
    useReviewStore.getState().actions.setRange({ from: 1, to: 2 });
    useReviewStore.getState().actions.selectSession(s2);
    expect(useReviewStore.getState().activeSessionId).toBe(s2);
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("appendFragment is a no-op for a session we don't hold", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const before = useReviewStore.getState().sessions;
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: "no-such-session",
      metrics: [
        { host: "chassis-a-lc1-1", label: "CPU %", timestamp: "2026-03-01T08:20:30Z", value: 1 },
      ],
      events: [],
      log_events: [],
      deleted_event_ids: [],
      chart_map: {},
      meta: null,
    } as MonitorSessionFragment);
    // Same `sessions` array reference: no set() call happened, so zustand does not re-render.
    expect(useReviewStore.getState().sessions).toBe(before);
  });

  it("appendFragment is a no-op for a heartbeat fragment on a session we DO hold", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const active = useReviewStore.getState().activeSessionId;
    if (active === null) throw new Error("expected an active session after import");
    const before = useReviewStore.getState().sessions;
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: active,
      metrics: [],
      events: [],
      log_events: [],
      deleted_event_ids: [],
      chart_map: {},
      meta: null,
    } as MonitorSessionFragment);
    // Same `sessions` array reference: the fragment addresses a session we
    // hold but carries nothing to apply, so applyFragment (fragment.ts)
    // returns that session's SAME object, mergeFragments never copies the
    // array, and no set() call happens — no re-render for a no-op tick.
    expect(useReviewStore.getState().sessions).toBe(before);
  });

  it("appendFragment replaces the addressed session with a NEW object so zustand re-renders", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const active = useReviewStore.getState().activeSessionId;
    if (active === null) throw new Error("expected an active session after import");
    const sessionsBefore = useReviewStore.getState().sessions;
    // biome-ignore lint/style/noNonNullAssertion: active session id is guaranteed to be in sessions
    const sessionBefore = sessionsBefore.find((s) => s.id === active)!;
    const metricsBefore = sessionBefore.metrics.length;

    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: active,
      metrics: [
        { host: "chassis-a-lc1-1", label: "CPU %", timestamp: "2026-03-01T08:20:30Z", value: 1 },
      ],
      events: [],
      log_events: [],
      deleted_event_ids: [],
      chart_map: {},
      meta: null,
    } as MonitorSessionFragment);

    const sessionsAfter = useReviewStore.getState().sessions;
    expect(sessionsAfter).not.toBe(sessionsBefore); // sessions array itself is a fresh copy
    // biome-ignore lint/style/noNonNullAssertion: same session id, still present after append
    const sessionAfter = sessionsAfter.find((s) => s.id === active)!;
    expect(sessionAfter).not.toBe(sessionBefore); // new session object -> zustand re-renders
    expect(sessionAfter.metrics.length).toBe(metricsBefore + 1);
  });

  // Follow-up: the live path's NaN exposure closed in fragment.ts's
  // applyFragment (dropInvalidTimestamps, shared with the import path) must
  // actually reach the store's `warnings` channel — the spec's "drop it,
  // count it, surface once in the warnings channel" — not just be an inert
  // optional parameter applyFragment's own unit tests happen to pass.
  it("appendFragment drops a bad-timestamp metric and surfaces the drop in the store's warnings channel", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const active = useReviewStore.getState().activeSessionId;
    if (active === null) throw new Error("expected an active session after import");
    const warningsBefore = useReviewStore.getState().warnings.length;
    const metricsBefore =
      useReviewStore.getState().sessions.find((x) => x.id === active)?.metrics.length ?? 0;

    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: active,
      metrics: [
        { host: "chassis-a-lc1-1", label: "CPU %", timestamp: "2026-03-01T08:20:30Z", value: 1 },
        { host: "chassis-a-lc1-1", label: "CPU %", timestamp: "not-a-timestamp", value: 2 },
      ],
      events: [],
      log_events: [],
      deleted_event_ids: [],
      chart_map: {},
      meta: null,
    } as MonitorSessionFragment);

    const s = useReviewStore.getState();
    // The good row still landed...
    const session = s.sessions.find((x) => x.id === active);
    expect(session?.metrics.length).toBe(metricsBefore + 1);
    // ...and the bad one is a single warning, not silence.
    expect(s.warnings.length).toBe(warningsBefore + 1);
    expect(s.warnings.at(-1)).toMatch(/dropped 1 metric.*invalid timestamp/u);
  });

  it("appendFragments (batched) folds warnings from multiple fragments into ONE store write", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const active = useReviewStore.getState().activeSessionId;
    if (active === null) throw new Error("expected an active session after import");
    const warningsBefore = useReviewStore.getState().warnings.length;

    const badFrag = (ts: string): MonitorSessionFragment =>
      ({
        format: 1,
        session: active,
        metrics: [{ host: "chassis-a-lc1-1", label: "CPU %", timestamp: ts, value: 1 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      }) as MonitorSessionFragment;

    useReviewStore.getState().actions.appendFragments([badFrag("nope-a"), badFrag("nope-b")]);

    const s = useReviewStore.getState();
    expect(s.warnings.length).toBe(warningsBefore + 2); // one summary warning per fragment
  });

  // Plan 5b follow-up: setRange is the ONE door every range enters through
  // (reviewStore.ts's doc comment on the action) — an inverted or empty
  // range must never reach the store from ANY caller, not just a guarded
  // one (RangePicker's Apply guards its own raw pending values; the chart
  // drag-zoom and the events-panel jump do not — see SubjectPage.tsx's
  // onZoom and EventsPanel.tsx's jump, both of which route a `clampRange`
  // result straight into `setRange`).
  describe("setRange", () => {
    it("refuses an inverted range (from > to): the store's range is left unchanged", () => {
      useReviewStore.getState().actions.setRange({ from: 10, to: 5 });
      const s = useReviewStore.getState();
      expect(s.range).toBeNull(); // unchanged from reset()'s null, NOT {from: 10, to: 5}
    });

    it("refuses an empty range (from === to): it selects nothing, same as inverted", () => {
      useReviewStore.getState().actions.setRange({ from: 5, to: 5 });
      expect(useReviewStore.getState().range).toBeNull();
    });

    it("refusing an inverted range leaves a PRE-EXISTING range untouched, not just null", () => {
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });
      useReviewStore.getState().actions.setRange({ from: 300, to: 250 }); // inverted -- refused
      expect(useReviewStore.getState().range).toEqual({ from: 100, to: 200 });
    });

    it("still accepts a well-ordered range", () => {
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });
      expect(useReviewStore.getState().range).toEqual({ from: 100, to: 200 });
    });

    it("setRange(null) still clears the range (means: follow / whole session)", () => {
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });
      useReviewStore.getState().actions.setRange(null);
      expect(useReviewStore.getState().range).toBeNull();
    });

    // Follow-up: `from >= to` is a NaN blind spot — `NaN >= NaN` is `false`,
    // so a NaN range walks straight past the inverted/empty check above and
    // reaches the store. A malformed event timestamp (parseTs -> Date.parse
    // -> NaN, see data/time.ts) reaching the events-panel jump is a real way
    // in, not a hypothetical. `Number.isFinite` on both edges must catch
    // this the same as an inverted/empty range does.
    it("refuses a fully-NaN range: the store's range is left unchanged", () => {
      useReviewStore.getState().actions.setRange({ from: Number.NaN, to: Number.NaN });
      expect(useReviewStore.getState().range).toBeNull();
    });

    it("refuses a range with only one NaN edge (from): one bad edge is enough", () => {
      useReviewStore.getState().actions.setRange({ from: 100, to: Number.NaN });
      expect(useReviewStore.getState().range).toBeNull();
    });

    it("refuses a range with only one NaN edge (to): one bad edge is enough", () => {
      useReviewStore.getState().actions.setRange({ from: Number.NaN, to: 200 });
      expect(useReviewStore.getState().range).toBeNull();
    });

    it("refuses an infinite range: +/-Infinity is as useless as NaN", () => {
      useReviewStore
        .getState()
        .actions.setRange({ from: Number.NEGATIVE_INFINITY, to: Number.POSITIVE_INFINITY });
      expect(useReviewStore.getState().range).toBeNull();
    });

    it("refusing a NaN range leaves a PRE-EXISTING range untouched, not just null", () => {
      useReviewStore.getState().actions.setRange({ from: 100, to: 200 });
      useReviewStore.getState().actions.setRange({ from: Number.NaN, to: Number.NaN });
      expect(useReviewStore.getState().range).toEqual({ from: 100, to: 200 });
    });
  });

  // Task 6 (Plan 5b follow-ups): setWindow's two cases, keyed on `range` —
  // the same "paused is derived from range, never stored" state `togglePause`
  // owns (see reviewStore.ts's interface doc comment on both).
  describe("setWindow", () => {
    it("widens the follow window without pinning the view (still following)", () => {
      expect(useReviewStore.getState().range).toBeNull(); // following, per reset()
      useReviewStore.getState().actions.setWindow(3_600_000);
      const s = useReviewStore.getState();
      expect(s.windowMs).toBe(3_600_000);
      expect(s.range).toBeNull(); // STILL FOLLOWING — the spec's word
    });

    it("re-pins around the frozen instant when paused, same `to`, wider span", () => {
      const to = 2_000_000_000;
      useReviewStore.getState().actions.setRange({ from: to - 900_000, to });
      useReviewStore.getState().actions.setWindow(3_600_000);
      const s = useReviewStore.getState();
      expect(s.windowMs).toBe(3_600_000);
      expect(s.range).toEqual({ from: to - 3_600_000, to });
      // Still paused: `range` stays non-null (useIsPaused, reviewStore.ts)
      // — a stored `paused` flag would have to be checked separately and
      // could drift; deriving it from `range` makes that impossible.
      expect(s.range).not.toBeNull();
    });
  });

  // Plan 5c: /api/mode's editable flag and the mutation-failure warnings
  // channel both land directly on the store (bootstrap.ts and eventApi.ts
  // are the real callers; this is the store-level unit case for each).
  describe("setEditable / addWarning (Plan 5c)", () => {
    it("setEditable stores the flag verbatim", () => {
      expect(useReviewStore.getState().editable).toBe(false); // reset()'s default
      useReviewStore.getState().actions.setEditable(true);
      expect(useReviewStore.getState().editable).toBe(true);
      useReviewStore.getState().actions.setEditable(false);
      expect(useReviewStore.getState().editable).toBe(false);
    });

    it("addWarning appends to the existing warnings channel without clobbering it", () => {
      useReviewStore.setState({ warnings: ["earlier warning"] });
      useReviewStore.getState().actions.addWarning("mutation failed: archive is locked");
      expect(useReviewStore.getState().warnings).toEqual([
        "earlier warning",
        "mutation failed: archive is locked",
      ]);
    });
  });

  // #866: the live window follows the server's observation, so a mark made
  // while the collector stalls survives a reload, a reconnect and a Pause.
  describe("server-observed horizon", () => {
    const T = Date.parse("2026-07-01T08:30:00Z"); // minimal.json's last sample
    const at = T + 600_000;
    const markEvent = {
      id: 7,
      timestamp: new Date(at).toISOString(),
      label: "mark",
      source: "manual",
      color: "#888888",
      dash: "dash",
    };
    const snapshot = () => {
      const doc = JSON.parse(MINIMAL) as { sessions: Record<string, unknown>[] };
      const [first] = doc.sessions;
      first["end"] = null; // a live session
      first["events"] = [markEvent];
      first["observed_at"] = new Date(at).toISOString();
      return JSON.stringify(doc);
    };
    const visible = () => {
      const s = useReviewStore.getState().sessions[0];
      const to = displayEndMs(s);
      return at >= to - useReviewStore.getState().windowMs && at <= to;
    };

    it("reload and reconnect keep the mark visible while metrics are stalled", () => {
      const { actions } = useReviewStore.getState();
      expect(actions.importMonitorSessions(snapshot(), "live")).toBe(true);
      expect(useReviewStore.getState().sessions[0].endMs).toBe(T);
      expect(visible()).toBe(true);
      expect(actions.resyncMonitorSessions(snapshot(), "live")).toBe(true);
      expect(visible()).toBe(true);
    });

    it("pause after a mark keeps it visible, and resume follows again", () => {
      const { actions } = useReviewStore.getState();
      actions.importMonitorSessions(MINIMAL, "live");
      actions.setMode("live");
      const sid = useReviewStore.getState().sessions[0].id;
      actions.appendFragment({
        format: 1,
        session: sid,
        events: [markEvent],
        observed_at: new Date(at).toISOString(),
      });
      actions.togglePause();
      const paused = useReviewStore.getState().range;
      expect(paused?.to).toBe(at);
      expect(at).toBeGreaterThanOrEqual(paused?.from ?? Number.POSITIVE_INFINITY);
      actions.togglePause();
      expect(useReviewStore.getState().range).toBeNull();
      actions.appendFragment({
        format: 1,
        session: sid,
        observed_at: new Date(at + 60_000).toISOString(),
      });
      expect(displayEndMs(useReviewStore.getState().sessions[0])).toBe(at + 60_000);
    });
  });
});
