import { describe, expect, it } from "vitest";
import { applyFragment } from "../data/fragment";
import { seriesKey } from "../data/seriesIndex";
import { synthSession } from "./_synth";
import type { MonitorSessionFragment } from "../api/export.gen";

const frag = (over: Partial<MonitorSessionFragment>): MonitorSessionFragment =>
  ({
    format: 1,
    session: "synth",
    metrics: [],
    events: [],
    log_events: [],
    deleted_event_ids: [],
    chart_map: {},
    meta: null,
    ...over,
  }) as MonitorSessionFragment;

describe("applyFragment", () => {
  const base = () => synthSession({ hosts: 1, seriesPerHost: 1, ticks: 2, intervalS: 5 });

  it("appends metrics and extends the session end", () => {
    const s = base();
    const before = s.metrics.length;
    const ts = new Date(s.endMs + 5000).toISOString();
    const next = applyFragment(
      s,
      frag({ metrics: [{ host: "h0", label: "m0", timestamp: ts, value: 42 }] as never }),
    );
    expect(next).not.toBe(s); // new object -> zustand re-renders
    expect(next.metrics.length).toBe(before + 1);
    expect(next.endMs).toBe(Date.parse(ts));
  });

  it("mutates the metrics and touched-series index arrays IN PLACE — copying would restore the O(total) cost this design removes", () => {
    const s = synthSession({ hosts: 2, seriesPerHost: 1, ticks: 2, intervalS: 5 });
    const metricsBefore = s.metrics;
    const touchedKey = seriesKey("h0", "m0");
    const untouchedKey = seriesKey("h1", "m0");
    const recsBefore = s.index.recs.get(touchedKey);
    const touchedRevBefore = s.index.rev.get(touchedKey);
    const untouchedRevBefore = s.index.rev.get(untouchedKey);
    const ts = new Date(s.endMs + 5000).toISOString();

    const next = applyFragment(
      s,
      frag({ metrics: [{ host: "h0", label: "m0", timestamp: ts, value: 42 }] as never }),
    );

    // Same array reference as before the call: appended IN PLACE, not copied.
    // `[...session.metrics, ...metrics]` would pass every other assertion in this
    // file while silently reintroducing the O(total run length) cost per tick.
    expect(next.metrics).toBe(metricsBefore);
    expect(next.index.recs.get(touchedKey)).toBe(recsBefore);
    expect(next.index.rev.get(touchedKey)).toBe((touchedRevBefore ?? 0) + 1);
    expect(next.index.rev.get(untouchedKey)).toBe(untouchedRevBefore);
  });

  it("upserts events by id — an edited event is just an event", () => {
    let s = base();
    s = applyFragment(
      s,
      frag({ events: [{ id: 1, timestamp: "2026-07-12T00:00:01Z", label: "boot" }] as never }),
    );
    expect(s.events).toHaveLength(1);
    s = applyFragment(
      s,
      frag({ events: [{ id: 1, timestamp: "2026-07-12T00:00:01Z", label: "boot2" }] as never }),
    );
    expect(s.events).toHaveLength(1);
    expect(s.events[0].label).toBe("boot2");
  });

  it("keeps id-less events from separate fragments — upsert-by-id must not collapse them onto one key", () => {
    let s = base();
    s = applyFragment(
      s,
      frag({ events: [{ timestamp: "2026-07-12T00:00:01Z", label: "first" }] as never }),
    );
    s = applyFragment(
      s,
      frag({ events: [{ timestamp: "2026-07-12T00:00:02Z", label: "second" }] as never }),
    );
    expect(s.events).toHaveLength(2);
    expect(s.events.map((e) => e.label)).toEqual(expect.arrayContaining(["first", "second"]));
  });

  it("drops deleted event ids", () => {
    let s = base();
    s = applyFragment(
      s,
      frag({ events: [{ id: 7, timestamp: "2026-07-12T00:00:01Z", label: "x" }] as never }),
    );
    s = applyFragment(s, frag({ deleted_event_ids: [7] }));
    expect(s.events).toHaveLength(0);
  });

  it("merges chart_map and replaces meta when present", () => {
    const s = base();
    const next = applyFragment(
      s,
      frag({
        chart_map: { newlabel: "cpu" },
        meta: {
          interval: 5,
          charts: [{ label: "newlabel", y_title: "y", unit: "%", command: "c", chart: "cpu" }],
          tabs: [],
        },
      } as never),
    );
    expect(next.meta.charts.map((c) => c.label)).toContain("newlabel");
    expect(next.chartMap.newlabel).toBe("cpu");
  });

  it("ignores a fragment addressed to a different session", () => {
    const s = base();
    const next = applyFragment(
      s,
      frag({
        session: "someone-else",
        metrics: [
          { host: "h0", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 1 },
        ] as never,
      }),
    );
    expect(next).toBe(s);
  });

  it("returns the SAME object for a heartbeat/no-op fragment (right session, nothing set)", () => {
    const s = base();
    const next = applyFragment(s, frag({}));
    expect(next).toBe(s);
  });

  // Follow-up: the live path had the same NaN exposure the import path
  // (exportDoc.ts's dropInvalidTimestamps) was just closed for. stream.ts's
  // isFragment() checks structure only (metrics IS an array), never each
  // row's own timestamp — so a malformed metric among good ones used to
  // flow straight through applyFragment into appendToIndex and land in the
  // series index's `tsMs`, which every reader (health.ts's
  // lastIndexAtOrBefore, seriesIndex.ts's own lowerBound) binary-searches
  // assuming ASCENDING order. `tsMs[mid] <= t` is always false for NaN, so
  // one bad sample silently broke every search over that series from then
  // on — wrong slicing, not a crash.
  describe("invalid metric timestamps (live-path NaN exposure)", () => {
    it("drops the bad row, keeps the good ones, leaves tsMs finite and ascending, and surfaces a warning", () => {
      const s = base();
      const metricsBefore = s.metrics.length;
      const goodTs = new Date(s.endMs + 5000).toISOString();
      const warnings: string[] = [];

      const next = applyFragment(
        s,
        frag({
          metrics: [
            { host: "h0", label: "m0", timestamp: goodTs, value: 1 },
            { host: "h0", label: "m0", timestamp: "not-a-timestamp", value: 2 },
          ] as never,
        }),
        warnings,
      );

      // Only the good row landed.
      expect(next.metrics).toHaveLength(metricsBefore + 1);
      expect(next.metrics.every((m) => Number.isFinite(Date.parse(m.timestamp)))).toBe(true);

      // tsMs for the touched series is finite and strictly ascending — the
      // invariant every binary-search reader depends on.
      const key = seriesKey("h0", "m0");
      // biome-ignore lint/style/noNonNullAssertion: the touched series always gets an index entry
      const tsMs = next.index.tsMs.get(key)!;
      expect(tsMs.every((t) => Number.isFinite(t))).toBe(true);
      for (let i = 1; i < tsMs.length; i++) {
        expect(tsMs[i]).toBeGreaterThanOrEqual(tsMs[i - 1]);
      }

      // Surfaced once, not swallowed.
      expect(warnings).toHaveLength(1);
      expect(warnings[0]).toMatch(/dropped 1 metric.*invalid timestamp/);
    });

    it("a fragment whose ONLY content is an invalid-timestamp metric is still a session no-op — but still warns", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({
          metrics: [{ host: "h0", label: "m0", timestamp: "garbage", value: 1 }] as never,
        }),
        warnings,
      );
      // Nothing survived filtering, so this is exactly a heartbeat fragment
      // from the merge's point of view: same object, no store write.
      expect(next).toBe(s);
      expect(warnings).toHaveLength(1);
    });

    it("drops multiple bad rows in one fragment and reports the count", () => {
      const s = base();
      const warnings: string[] = [];
      applyFragment(
        s,
        frag({
          metrics: [
            { host: "h0", label: "m0", timestamp: "not-a-timestamp-a", value: 1 },
            { host: "h0", label: "m0", timestamp: "not-a-timestamp-b", value: 2 },
          ] as never,
        }),
        warnings,
      );
      expect(warnings).toHaveLength(1);
      expect(warnings[0]).toMatch(/dropped 2 metrics.*invalid timestamp/);
    });
  });

  // Finding [2] (5b final follow-ups review): the import path
  // (exportDoc.ts's normalizeSession) already drops bad-timestamp
  // events/log_events, but applyFragment appended `frag.events`/
  // `frag.log_events` UNVALIDATED — an SSE row with a garbage timestamp
  // landed in `session.events`/`logEvents`, rendered "Invalid Date" in
  // EventsPanel, was invisible on charts (NaN fails eventMarkers' overlap
  // comparisons), and then silently disappeared on the next resync (the
  // same payload re-parsed via the import path, which DOES drop it) — the
  // same payload, two behaviours. Fixed by routing both through the same
  // `dropInvalidTimestamps` the metrics test above already covers.
  describe("invalid event/log_event timestamps (live-path parity with the import path)", () => {
    it("drops a bad-timestamp event, keeps the good one, and surfaces a warning", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({
          events: [
            { id: 1, timestamp: "2026-07-12T00:00:01Z", label: "good" },
            { id: 2, timestamp: "not-a-timestamp", label: "bad" },
          ] as never,
        }),
        warnings,
      );
      expect(next.events).toHaveLength(1);
      expect(next.events[0].label).toBe("good");
      expect(warnings).toHaveLength(1);
      expect(warnings[0]).toMatch(/dropped 1 event.*invalid timestamp/);
    });

    it("drops a bad-timestamp log_event and surfaces a warning", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({
          log_events: [
            { timestamp: "2026-07-12T00:00:01Z", host: "h0", tab: "kernel" },
            { timestamp: "not-a-timestamp", host: "h0", tab: "kernel" },
          ] as never,
        }),
        warnings,
      );
      expect(next.logEvents).toHaveLength(1);
      expect(warnings).toHaveLength(1);
      expect(warnings[0]).toMatch(/dropped 1 log event.*invalid timestamp/);
    });

    it("a fragment whose ONLY content is an invalid-timestamp event is still a session no-op — but still warns", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({ events: [{ timestamp: "garbage", label: "bad" }] as never }),
        warnings,
      );
      expect(next).toBe(s);
      expect(warnings).toHaveLength(1);
    });

    it("a fragment whose ONLY content is an invalid-timestamp log_event is still a session no-op — but still warns", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({ log_events: [{ timestamp: "garbage", host: "h0" }] as never }),
        warnings,
      );
      expect(next).toBe(s);
      expect(warnings).toHaveLength(1);
    });
  });

  // Finding [3] (5b final follow-ups review): neither boundary validated
  // `EventRecord.end_timestamp` (a SPAN event's end) — only `timestamp`. A
  // span with a valid start and a malformed end passed both boundaries,
  // then produced a NaN `toMs` wherever end_timestamp is read
  // (charts/options.ts's eventMarkers), silently vanishing from every chart
  // with no warning at all.
  describe("invalid end_timestamp (live-path span-event validation, Finding [3])", () => {
    it("drops an event whose start is valid but whose end_timestamp is malformed", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({
          events: [
            {
              id: 1,
              timestamp: "2026-07-12T00:00:01Z",
              end_timestamp: "not-a-real-end",
              label: "broken span",
            },
          ] as never,
        }),
        warnings,
      );
      expect(next.events).toHaveLength(0);
      expect(warnings).toHaveLength(1);
      expect(warnings[0]).toMatch(/dropped 1 event.*invalid timestamp/);
    });

    it("keeps a span event whose start AND end_timestamp both parse", () => {
      const s = base();
      const warnings: string[] = [];
      const next = applyFragment(
        s,
        frag({
          events: [
            {
              id: 1,
              timestamp: "2026-07-12T00:00:01Z",
              end_timestamp: "2026-07-12T00:00:05Z",
              label: "real span",
            },
          ] as never,
        }),
        warnings,
      );
      expect(next.events).toHaveLength(1);
      expect(warnings).toHaveLength(0);
    });
  });
});
