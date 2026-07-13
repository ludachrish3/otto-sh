import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it } from "vitest";

import type { MonitorSessionFragment } from "../api/export.gen";
import { useReviewStore } from "../data/reviewStore";

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
    expect(s.importError).toMatch(/format|unversioned/i);
    expect(s.sessions).toHaveLength(1); // minimal still loaded
    expect(s.sourceName).toBe("minimal.json");
  });

  it("selectSession switches and resets the range", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const s2 = useReviewStore.getState().sessions[1].id;
    useReviewStore.getState().actions.setRange({ from: 1, to: 2 });
    useReviewStore.getState().actions.selectSession(s2);
    expect(useReviewStore.getState().activeSessionId).toBe(s2);
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("resetView restores first session + full range", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const first = useReviewStore.getState().sessions[0].id;
    useReviewStore.getState().actions.selectSession(useReviewStore.getState().sessions[2].id);
    useReviewStore.getState().actions.setRange({ from: 1, to: 2 });
    useReviewStore.getState().actions.resetView();
    expect(useReviewStore.getState().activeSessionId).toBe(first);
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("appendFragment is a no-op for a session we don't hold", () => {
    useReviewStore.getState().actions.importMonitorSessions(DRIFT, "drift.json");
    const before = useReviewStore.getState().sessions;
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: "no-such-session",
      metrics: [
        { host: "chassis-a_lc1", label: "CPU %", timestamp: "2026-03-01T08:20:30Z", value: 1 },
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
        { host: "chassis-a_lc1", label: "CPU %", timestamp: "2026-03-01T08:20:30Z", value: 1 },
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
});
