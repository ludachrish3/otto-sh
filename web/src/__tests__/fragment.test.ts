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
});
