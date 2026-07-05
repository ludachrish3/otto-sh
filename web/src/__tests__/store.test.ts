// Reducer tests for store.ts. Each block cites the dashboard.js behavior it
// pins so a future change that breaks parity fails loudly here rather than
// only in the (much slower) Playwright suite.
import { beforeEach, describe, expect, it } from "vitest";

import type { MonitorEvent } from "../api/client";
import type { MonitorDashboardApiMetaPayload } from "../api/types.gen";
import { seriesKey, statusDotClass, statusText, useMonitorStore } from "../store";

function resetStore(): void {
  useMonitorStore.setState({
    meta: null,
    series: {},
    events: [],
    chartMap: {},
    logEvents: {},
    activeTab: null,
    selectedHost: null,
    paused: false,
    connection: "connecting",
    spanStartId: null,
    editingEventId: null,
    popoverAnchor: null,
    everLive: false,
    chartsInitialized: false,
    lastMetric: null,
  });
}

function makeEvent(overrides: Partial<MonitorEvent> = {}): MonitorEvent {
  return {
    id: 1,
    timestamp: "2026-07-02T00:00:00Z",
    label: "Reboot",
    source: "manual",
    color: "#888888",
    dash: "dash",
    end_timestamp: null,
    ...overrides,
  };
}

function makeMeta(
  overrides: Partial<MonitorDashboardApiMetaPayload> = {},
): MonitorDashboardApiMetaPayload {
  return { hosts: [], live: true, metrics: [], tabs: [], interval: null, ...overrides };
}

beforeEach(() => {
  resetStore();
});

describe("metricMsg (dashboard.js appendMetricPoint)", () => {
  it("appends a point under its series key", () => {
    useMonitorStore.getState().actions.metricMsg({
      type: "metric",
      key: "host1/Overall CPU",
      host: "host1",
      label: "Overall CPU",
      chart: "cpu",
      y_title: "%",
      unit: "%",
      ts: "2026-07-02T00:00:00Z",
      value: 42,
    });
    expect(useMonitorStore.getState().series["host1/Overall CPU"]).toEqual([
      { ts: "2026-07-02T00:00:00Z", value: 42, meta: null },
    ]);
  });

  it("appends to an existing series without disturbing prior points", () => {
    const { metricMsg } = useMonitorStore.getState().actions;
    metricMsg({
      type: "metric",
      key: "k",
      host: "h",
      label: "l",
      chart: "c",
      y_title: "",
      unit: "",
      ts: "t1",
      value: 1,
    });
    metricMsg({
      type: "metric",
      key: "k",
      host: "h",
      label: "l",
      chart: "c",
      y_title: "",
      unit: "",
      ts: "t2",
      value: 2,
    });
    expect(useMonitorStore.getState().series.k).toEqual([
      { ts: "t1", value: 1, meta: null },
      { ts: "t2", value: 2, meta: null },
    ]);
  });

  it("appends even while paused — pause freezes chart rendering, never the data (dashboard.js: push happens before the `if (state.paused) return;` gate)", () => {
    useMonitorStore.setState({ paused: true });
    const { metricMsg } = useMonitorStore.getState().actions;
    metricMsg({
      type: "metric",
      key: "k",
      host: "h",
      label: "l",
      chart: "c",
      y_title: "",
      unit: "",
      ts: "t1",
      value: 1,
    });
    metricMsg({
      type: "metric",
      key: "k",
      host: "h",
      label: "l",
      chart: "c",
      y_title: "",
      unit: "",
      ts: "t2",
      value: 2,
    });
    expect(useMonitorStore.getState().series.k).toHaveLength(2);
    expect(useMonitorStore.getState().paused).toBe(true);
  });

  it("carries point meta through (hover text source)", () => {
    const { metricMsg } = useMonitorStore.getState().actions;
    metricMsg({
      type: "metric",
      key: "k",
      host: "h",
      label: "l",
      chart: "c",
      y_title: "",
      unit: "",
      ts: "t",
      value: 1,
      meta: { pid: "7" },
    });
    expect(useMonitorStore.getState().series.k[0].meta).toEqual({ pid: "7" });
  });

  it("sets `lastMetric` to the raw message — ChartGrid's live-append notification channel (chart/y_title/unit aren't derivable from `series` alone)", () => {
    const msg = {
      type: "metric",
      key: "host1/Overall CPU",
      host: "host1",
      label: "Overall CPU",
      chart: "cpu",
      y_title: "%",
      unit: "%",
      ts: "2026-07-02T00:00:00Z",
      value: 42,
    } as const;
    useMonitorStore.getState().actions.metricMsg(msg);
    expect(useMonitorStore.getState().lastMetric).toEqual(msg);
  });
});

describe("event lifecycle (dashboard.js addEventToPlot / event_updated / event_deleted)", () => {
  it("eventMsg appends a new event even while paused (data is never gated on pause)", () => {
    useMonitorStore.setState({ paused: true });
    useMonitorStore.getState().actions.eventMsg(makeEvent());
    expect(useMonitorStore.getState().events).toEqual([makeEvent()]);
  });

  it("eventUpdated replaces the event with a matching id in place", () => {
    useMonitorStore.setState({
      events: [makeEvent({ id: 5, label: "old" }), makeEvent({ id: 6 })],
    });
    useMonitorStore.getState().actions.eventUpdated(makeEvent({ id: 5, label: "new" }));
    const { events } = useMonitorStore.getState();
    expect(events[0].label).toBe("new");
    expect(events[1].id).toBe(6);
  });

  it("eventUpdated is a no-op for an unknown id", () => {
    useMonitorStore.setState({ events: [makeEvent({ id: 5 })] });
    useMonitorStore.getState().actions.eventUpdated(makeEvent({ id: 99, label: "ghost" }));
    expect(useMonitorStore.getState().events).toEqual([makeEvent({ id: 5 })]);
  });

  it("eventDeleted removes only the matching id", () => {
    useMonitorStore.setState({ events: [makeEvent({ id: 1 }), makeEvent({ id: 2 })] });
    useMonitorStore.getState().actions.eventDeleted(1);
    expect(useMonitorStore.getState().events.map((e) => e.id)).toEqual([2]);
  });

  it("eventDeleted closes the popover when the deleted event is the one being edited (dashboard.js: `if (state.editingEventId === msg.id) hidePopover();`)", () => {
    useMonitorStore.setState({
      events: [makeEvent({ id: 1 })],
      editingEventId: 1,
      popoverAnchor: { x: 10, y: 20 },
    });
    useMonitorStore.getState().actions.eventDeleted(1);
    const state = useMonitorStore.getState();
    expect(state.editingEventId).toBeNull();
    expect(state.popoverAnchor).toBeNull();
  });

  it("eventDeleted leaves an unrelated open popover alone", () => {
    useMonitorStore.setState({
      events: [makeEvent({ id: 1 }), makeEvent({ id: 2 })],
      editingEventId: 2,
      popoverAnchor: { x: 10, y: 20 },
    });
    useMonitorStore.getState().actions.eventDeleted(1);
    const state = useMonitorStore.getState();
    expect(state.editingEventId).toBe(2);
    expect(state.popoverAnchor).toEqual({ x: 10, y: 20 });
  });
});

describe("popover open/close (dashboard.js openPopover/hidePopover)", () => {
  it("openPopover records the editing id and click anchor", () => {
    useMonitorStore.getState().actions.openPopover(7, 100, 200);
    const state = useMonitorStore.getState();
    expect(state.editingEventId).toBe(7);
    expect(state.popoverAnchor).toEqual({ x: 100, y: 200 });
  });

  it("closePopover clears both", () => {
    useMonitorStore.setState({ editingEventId: 7, popoverAnchor: { x: 1, y: 2 } });
    useMonitorStore.getState().actions.closePopover();
    const state = useMonitorStore.getState();
    expect(state.editingEventId).toBeNull();
    expect(state.popoverAnchor).toBeNull();
  });
});

describe("chartsReady (dashboard.js initTabCharts()'s clear-events-btn.disabled = false)", () => {
  it("flips chartsInitialized on", () => {
    expect(useMonitorStore.getState().chartsInitialized).toBe(false);
    useMonitorStore.getState().actions.chartsReady();
    expect(useMonitorStore.getState().chartsInitialized).toBe(true);
  });
});

describe("disconnect transition (dashboard.js src.onerror)", () => {
  it("moves live -> disconnected, clears paused, and abandons an open span", () => {
    useMonitorStore.setState({ connection: "live", paused: true, spanStartId: 42 });
    useMonitorStore.getState().actions.sseErrored();
    const state = useMonitorStore.getState();
    expect(state.connection).toBe("disconnected");
    expect(state.paused).toBe(false);
    expect(state.spanStartId).toBeNull();
  });

  it("falls back to historical when the stream errors before ever reaching live (mirrors the legacy `meta.live && isLive` gate)", () => {
    useMonitorStore.setState({ connection: "connecting", meta: makeMeta({ live: true }) });
    useMonitorStore.getState().actions.sseErrored();
    expect(useMonitorStore.getState().connection).toBe("historical");
  });

  it("stays historical on error in historical mode", () => {
    useMonitorStore.setState({ connection: "historical", meta: makeMeta({ live: false }) });
    useMonitorStore.getState().actions.sseErrored();
    expect(useMonitorStore.getState().connection).toBe("historical");
  });

  it("does not clear `everLive` — the mark-event button stays enabled through a disconnect (dashboard.js's `src.onerror` never touches `markEventBox`)", () => {
    useMonitorStore.setState({ connection: "live", everLive: true });
    useMonitorStore.getState().actions.sseErrored();
    expect(useMonitorStore.getState().everLive).toBe(true);
  });
});

describe("sseOpened (dashboard.js src.onopen)", () => {
  it("resolves to live when meta.live is true", () => {
    useMonitorStore.setState({ meta: makeMeta({ live: true, hosts: ["host1"] }) });
    useMonitorStore.getState().actions.sseOpened();
    expect(useMonitorStore.getState().connection).toBe("live");
  });

  it("resolves to historical when meta.live is false", () => {
    useMonitorStore.setState({ meta: makeMeta({ live: false }) });
    useMonitorStore.getState().actions.sseOpened();
    expect(useMonitorStore.getState().connection).toBe("historical");
  });

  it("sets `everLive` once meta.live resolves live — dashboard.js's `state.isLive = true`, which (unlike `connection`) is never reset", () => {
    useMonitorStore.setState({ meta: makeMeta({ live: true }) });
    useMonitorStore.getState().actions.sseOpened();
    expect(useMonitorStore.getState().everLive).toBe(true);
  });

  it("leaves `everLive` false when meta.live is false", () => {
    useMonitorStore.setState({ meta: makeMeta({ live: false }) });
    useMonitorStore.getState().actions.sseOpened();
    expect(useMonitorStore.getState().everLive).toBe(false);
  });

  it("`everLive` stays true across a later historical-resolving open (sticky, mirrors dashboard.js never resetting `state.isLive`)", () => {
    useMonitorStore.setState({ everLive: true, meta: makeMeta({ live: false }) });
    useMonitorStore.getState().actions.sseOpened();
    expect(useMonitorStore.getState().everLive).toBe(true);
  });
});

describe("logEventMsg", () => {
  it("appends batch rows under host/tab, tagging each row", () => {
    const { actions } = useMonitorStore.getState();
    actions.logEventMsg({
      type: "log_event",
      host: "host1",
      tab: "syslog",
      rows: [{ ts: "2026-07-04T12:00:00+00:00", fields: { message: "hi" } }],
    });
    const rows = useMonitorStore.getState().logEvents["host1/syslog"];
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      timestamp: "2026-07-04T12:00:00+00:00",
      host: "host1",
      tab: "syslog",
      fields: { message: "hi" },
    });
  });
});

describe("applyData with log_events", () => {
  it("hydrates the logEvents slice from the snapshot", () => {
    const { actions } = useMonitorStore.getState();
    actions.applyData({
      series: {},
      events: [],
      chart_map: {},
      log_events: [
        { timestamp: "2026-07-04T12:00:00+00:00", host: "h", tab: "t", fields: { m: "x" } },
      ],
    });
    expect(useMonitorStore.getState().logEvents["h/t"]).toHaveLength(1);
  });
});

describe("seriesKey (host-scoped key resolution)", () => {
  it("prefixes the selected host", () => {
    expect(seriesKey("host1", "Overall CPU")).toBe("host1/Overall CPU");
  });

  it("falls back to the bare label with no host selected (historical data)", () => {
    expect(seriesKey(null, "Overall CPU")).toBe("Overall CPU");
  });
});

describe("statusText/statusDotClass (Header parity)", () => {
  it.each([
    ["connecting", false, "Connecting…", ""],
    ["live", false, "Live", "live"],
    ["live", true, "Paused", "live paused"],
    ["historical", false, "Historical", "history"],
    ["disconnected", false, "Disconnected", "disconnected"],
  ] as const)("connection=%s paused=%s", (connection, paused, text, dotClass) => {
    expect(statusText(connection, paused)).toBe(text);
    expect(statusDotClass(connection, paused)).toBe(dotClass);
  });
});
