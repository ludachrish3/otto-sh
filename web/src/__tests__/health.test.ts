import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { NormalizedSession, TimeRange } from "../data/exportDoc";
import { parseExportDocument } from "../data/exportDoc";
import {
  elementRollup,
  HEALTH_K,
  headlineFor,
  healthForHost,
  healthForHosts,
} from "../data/health";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

const MIN = 60_000;

// Hoisted to module scope so both the synthetic-edge-cases describe block and
// the healthForHost/healthForHosts agreement test below can build sessions
// with a log-only host (status "unknown").
function synthetic(metrics: object[], logEvents: object[] = []) {
  return parseExportDocument(
    JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "s",
          start: "2026-07-01T08:00:00Z",
          end: "2026-07-01T09:00:00Z",
          lab: {
            hosts: [
              { id: "a", element: "a", ip: "10.0.0.1" },
              { id: "b", element: "b", ip: "10.0.0.2" },
            ],
          },
          meta: {
            interval: 30.0,
            charts: [{ label: "CPU %", y_title: "CPU %", unit: "%", command: "x", chart: "cpu" }],
          },
          chart_map: { "CPU %": "CPU %" },
          metrics,
          log_events: logEvents,
        },
      ],
    }),
  ).sessions[0];
}

describe("healthForHosts against kitchen-sink", () => {
  it("everything is ok at full range (outage host recovered)", () => {
    const healths = healthForHosts(kitchen, null);
    for (const h of kitchen.lab.hosts) {
      expect(healths.get(h.id)?.status, h.id).toBe("ok");
    }
  });

  it("workers_w2 is down when the range ends inside its outage window", () => {
    // Outage: 60m→80m from session start. End the range at +70m.
    const range = { from: kitchen.startMs, to: kitchen.startMs + 70 * MIN };
    const h = healthForHosts(kitchen, range).get("workers_w2");
    expect(h?.status).toBe("down");
    // Last sample just before 60m; outage ≈ 10m (one cadence tick of slack).
    expect(h?.outageMs).toBeGreaterThanOrEqual(9 * MIN);
    expect(h?.outageMs).toBeLessThanOrEqual(11 * MIN);
  });

  it("other workers stay ok in that same window", () => {
    const range = { from: kitchen.startMs, to: kitchen.startMs + 70 * MIN };
    expect(healthForHosts(kitchen, range).get("workers_w1")?.status).toBe("ok");
  });

  it("a range before a host's data yields no-data", () => {
    // 1-minute sliver before the first cadence tick lands only start samples;
    // use a window entirely before the session for the strict case.
    const range = { from: kitchen.startMs - 10 * MIN, to: kitchen.startMs - 5 * MIN };
    expect(healthForHosts(kitchen, range).get("db-01")?.status).toBe("no-data");
  });
});

describe("healthForHosts synthetic edge cases", () => {
  it("a log-only host is unknown (no health claim)", () => {
    const s = synthetic(
      [{ timestamp: "2026-07-01T08:59:30Z", host: "a", label: "CPU %", value: 1 }],
      [{ timestamp: "2026-07-01T08:30:00Z", host: "b", tab: "kernel", fields: { m: "x" } }],
    );
    expect(healthForHosts(s, null).get("b")?.status).toBe("unknown");
  });

  it("down threshold is K x cadence exactly", () => {
    // Last sample 08:30; session ends 09:00 → gap 30m; cadence 30s → down.
    const s = synthetic([
      { timestamp: "2026-07-01T08:30:00Z", host: "a", label: "CPU %", value: 1 },
    ]);
    const h = healthForHosts(s, null).get("a");
    expect(HEALTH_K).toBe(3);
    expect(h?.status).toBe("down");
    expect(h?.outageMs).toBe(30 * MIN);
  });
});

/** Minimal, fully-parameterized session builder for healthForHost's direct
 * unit tests below. Unlike `synthetic()` above (fixed interval, fixed two
 * hosts — built for the healthForHost/healthForHosts agreement cases), each
 * case below needs to control its own global vs. per-chart interval, host
 * set, or metrics to isolate a single branch of healthForHost. */
function buildSession(opts: {
  hosts: string[];
  intervalSec: number | null;
  chartIntervalSec?: number;
  metrics: Array<{ timestamp: string; host: string }>;
}): NormalizedSession {
  const chart: Record<string, unknown> = {
    label: "CPU %",
    y_title: "CPU %",
    unit: "%",
    command: "x",
    chart: "cpu",
  };
  if (opts.chartIntervalSec !== undefined) chart.interval = opts.chartIntervalSec;
  return parseExportDocument(
    JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "s",
          start: "2026-07-01T08:00:00Z",
          end: "2026-07-01T09:00:00Z",
          lab: { hosts: opts.hosts.map((id) => ({ id, element: id, ip: "10.0.0.1" })) },
          meta: { interval: opts.intervalSec, charts: [chart] },
          chart_map: { "CPU %": "CPU %" },
          metrics: opts.metrics.map((m) => ({
            timestamp: m.timestamp,
            host: m.host,
            label: "CPU %",
            value: 1,
          })),
        },
      ],
    }),
  ).sessions[0];
}

describe("healthForHost direct coverage (concrete values, not routed through healthForHosts)", () => {
  // healthForHosts is now a loop over healthForHost (Task 4), so the
  // agreement suite below can only catch a bug in how the loop threads its
  // arguments — it is tautological with respect to healthForHost's own
  // correctness (proven: a mutation that made healthForHost ignore `nowMs`
  // left that suite green). These tests call healthForHost directly and
  // assert exact SubjectHealth values, so a bug inside healthForHost itself
  // has somewhere to be caught.

  it("live nowMs path: a host past K x cadence is down, with the actual gap as outageMs", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: 30, // cadence 30_000ms; threshold = HEALTH_K(3) x 30_000 = 90_000ms
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    const lastMs = s.startMs;
    const nowMs = lastMs + 100_000; // 10_000ms past the 90_000ms threshold
    expect(healthForHost(s, "a", null, nowMs)).toEqual({
      status: "down",
      lastSeenMs: lastMs,
      outageMs: 100_000,
    });
  });

  it("live nowMs path: a host sampled recently is ok with outageMs 0", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: 30,
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    const lastMs = s.startMs;
    expect(healthForHost(s, "a", null, lastMs + 1_000)).toEqual({
      status: "ok",
      lastSeenMs: lastMs,
      outageMs: 0,
    });
  });

  it("down boundary: gap of exactly K x cadence is ok, one ms more is down (this also pins the meta.interval-present cadence path)", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: 30, // threshold 90_000ms
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    const lastMs = s.startMs;
    expect(healthForHost(s, "a", null, lastMs + 90_000)).toEqual({
      status: "ok",
      lastSeenMs: lastMs,
      outageMs: 0,
    });
    expect(healthForHost(s, "a", null, lastMs + 90_001)).toEqual({
      status: "down",
      lastSeenMs: lastMs,
      outageMs: 90_001,
    });
  });

  it("no-data: the host has a series, but no sample falls inside the evaluated range", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: 30,
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    // Range entirely after the host's only sample.
    const range = { from: s.startMs + 20 * MIN, to: s.startMs + 40 * MIN };
    expect(healthForHost(s, "a", range)).toEqual({
      status: "no-data",
      lastSeenMs: null,
      outageMs: 0,
    });
  });

  it("unknown: a host with no metric series at all", () => {
    const s = buildSession({
      hosts: ["a", "b"],
      intervalSec: 30,
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    expect(healthForHost(s, "b", null)).toEqual({
      status: "unknown",
      lastSeenMs: null,
      outageMs: 0,
    });
  });

  it("unknown: cadence unresolvable (no global interval, no per-chart interval) still reports lastSeenMs", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: null,
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    expect(healthForHost(s, "a", null)).toEqual({
      status: "unknown",
      lastSeenMs: s.startMs,
      outageMs: 0,
    });
  });

  it("cadence resolution: falls back to the per-chart interval when session.meta.interval is null", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: null,
      chartIntervalSec: 45, // cadence 45_000ms; threshold = 3 x 45_000 = 135_000ms
      metrics: [{ timestamp: "2026-07-01T08:00:00Z", host: "a" }],
    });
    const lastMs = s.startMs;
    expect(healthForHost(s, "a", null, lastMs + 135_000)).toEqual({
      status: "ok",
      lastSeenMs: lastMs,
      outageMs: 0,
    });
    expect(healthForHost(s, "a", null, lastMs + 135_001)).toEqual({
      status: "down",
      lastSeenMs: lastMs,
      outageMs: 135_001,
    });
  });

  it("range scoping: narrowing the range re-evaluates health against an earlier 'last known' sample", () => {
    const s = buildSession({
      hosts: ["a"],
      intervalSec: 30, // threshold 90_000ms
      metrics: [
        { timestamp: "2026-07-01T08:00:00Z", host: "a" },
        { timestamp: "2026-07-01T08:59:00Z", host: "a" },
      ],
    });
    // Full range: evalTo clamps to the session end (09:00:00); the
    // 08:59:00 sample is 60_000ms before it — under the 90_000ms
    // threshold, so "ok".
    expect(healthForHost(s, "a", null)).toEqual({
      status: "ok",
      lastSeenMs: s.startMs + 59 * MIN,
      outageMs: 0,
    });
    // Narrow the range to [start, start+15m]: the 08:59:00 sample now falls
    // outside it, so "last known" reverts to the 08:00:00 sample — the gap
    // to the range's own end (15m = 900_000ms) blows well past the
    // threshold, flipping the same host from "ok" to "down".
    const narrowed = { from: s.startMs, to: s.startMs + 15 * MIN };
    expect(healthForHost(s, "a", narrowed)).toEqual({
      status: "down",
      lastSeenMs: s.startMs,
      outageMs: 15 * MIN,
    });
  });
});

describe("healthForHost agrees with healthForHosts (the rule must not fork)", () => {
  // healthForHosts (spec §6) is a loop over healthForHost (Task 4) — so this
  // suite can ONLY catch a bug in how that loop threads range/nowMs/hostId
  // through to each call (e.g. a stale closure, a wrong host, a dropped
  // argument). It CANNOT catch a bug inside healthForHost's own logic: since
  // healthForHosts delegates to it, any such bug would show up identically
  // on both sides of every `toEqual` below and this suite would stay green.
  // Proof: mutating healthForHost to ignore `nowMs` entirely leaves this
  // suite green (only an unrelated component test notices). Real coverage
  // of healthForHost's status/lastSeenMs/outageMs logic lives in the
  // "direct coverage" suite above, which calls healthForHost without going
  // through healthForHosts at all.
  //
  // Each case below pins healthForHost against healthForHosts for EVERY host
  // in the fixture — not just the one status the case's name calls out — so
  // this is a stronger check than "the four statuses each occur somewhere":
  // it proves the two functions can never quietly disagree on a single host.
  const cases: Array<{
    label: string;
    session: NormalizedSession;
    range: TimeRange | null;
    nowMs?: number;
  }> = [
    {
      // Every host reports and is within cadence of the session end: "ok".
      label: "kitchen-sink, full range (all ok)",
      session: kitchen,
      range: null,
    },
    {
      // workers_w2's outage window (60m-80m) still covers this range's end:
      // "down" for workers_w2, "ok" for its siblings.
      label: "kitchen-sink, range ending inside workers_w2's outage (down + ok)",
      session: kitchen,
      range: { from: kitchen.startMs, to: kitchen.startMs + 70 * MIN },
    },
    {
      // A window entirely before the session's first sample: "no-data".
      label: "kitchen-sink, range before any data (no-data)",
      session: kitchen,
      range: { from: kitchen.startMs - 10 * MIN, to: kitchen.startMs - 5 * MIN },
    },
    {
      // Host "b" has no metric series at all (log-only): "unknown". Host "a"
      // has an in-range sample: "ok".
      label: "synthetic, log-only host (unknown + ok)",
      session: synthetic(
        [{ timestamp: "2026-07-01T08:59:30Z", host: "a", label: "CPU %", value: 1 }],
        [{ timestamp: "2026-07-01T08:30:00Z", host: "b", tab: "kernel", fields: { m: "x" } }],
      ),
      range: null,
    },
    {
      // Host "a" last reported 30m before session end, past K x cadence:
      // "down". Host "b" has no series at all: "unknown".
      label: "synthetic, past the down threshold (down + unknown)",
      session: synthetic([
        { timestamp: "2026-07-01T08:30:00Z", host: "a", label: "CPU %", value: 1 },
      ]),
      range: null,
    },
    {
      // Live mode: nowMs supplied well past the session's own end pushes
      // every host's gap past K x cadence, exercising the nowMs branch
      // (evalTo = nowMs, not clamped to session.endMs) rather than the
      // archive default.
      label: "kitchen-sink, live nowMs past session end (down)",
      session: kitchen,
      range: null,
      nowMs: kitchen.endMs + 60 * MIN,
    },
  ];

  it("healthForHost matches healthForHosts's entry for every host, in every case", () => {
    for (const { label, session, range, nowMs } of cases) {
      const all = healthForHosts(session, range, nowMs);
      for (const host of session.lab.hosts) {
        expect(healthForHost(session, host.id, range, nowMs), `${label}: host ${host.id}`).toEqual(
          all.get(host.id),
        );
      }
    }
  });
});

describe("headlineFor", () => {
  it("prefers CPU and formats percent unspaced", () => {
    const head = headlineFor(kitchen, "chassis-a_lc1", null);
    expect(head?.chartKey).toBe("cpu");
    expect(head?.text).toMatch(/^\d+% cpu$/);
  });

  it("falls back to the first chart with data when CPU is absent", () => {
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: { hosts: [{ id: "a", element: "a", ip: "10.0.0.1" }] },
            meta: {
              interval: 60.0,
              charts: [
                { label: "Fan RPM", y_title: "Fan RPM", unit: "rpm", command: "x", chart: "fan" },
              ],
            },
            chart_map: { "Fan RPM": "Fan RPM" },
            metrics: [
              { timestamp: "2026-07-01T08:59:00Z", host: "a", label: "Fan RPM", value: 7212.4 },
            ],
          },
        ],
      }),
    ).sessions[0];
    expect(headlineFor(s, "a", null)?.text).toBe("7212 rpm fan");
  });

  it("returns null for a host with no in-range samples", () => {
    const range = { from: kitchen.startMs - 10 * MIN, to: kitchen.startMs - 5 * MIN };
    expect(headlineFor(kitchen, "db-01", range)).toBeNull();
  });
});

describe("elementRollup", () => {
  it("orders members by slot then id and reflects their health", () => {
    const healths = healthForHosts(kitchen, null);
    const chassis = kitchen.elements.find((e) => e.id === "chassis-a");
    if (!chassis) throw new Error("chassis-a missing from kitchen-sink fixture");
    const rollup = elementRollup(chassis, healths);
    expect(rollup).toHaveLength(3);
    expect(rollup.every((h) => h.status === "ok")).toBe(true);
  });

  it("orders members by slot, not id, when the two disagree", () => {
    // "alpha" sorts before "beta" by id, but "beta" has the lower slot — a
    // genuinely discriminating fixture (kitchen-sink's chassis-a members
    // happen to share slot order and id order, so it can't tell slot-sort
    // from id-sort apart).
    const session = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "alpha", element: "rack-x", ip: "10.0.0.1", slot: 2 },
                { id: "beta", element: "rack-x", ip: "10.0.0.2", slot: 1 },
              ],
            },
            meta: {
              interval: 30.0,
              charts: [{ label: "CPU %", y_title: "CPU %", unit: "%", command: "x", chart: "cpu" }],
            },
            chart_map: { "CPU %": "CPU %" },
            // alpha has an in-range sample (-> "ok"); beta has no metric
            // series at all (-> "unknown"), so the two members' healths
            // are distinguishable regardless of sort order.
            metrics: [
              { timestamp: "2026-07-01T08:59:30Z", host: "alpha", label: "CPU %", value: 1 },
            ],
          },
        ],
      }),
    ).sessions[0];
    const element = session.elements.find((e) => e.id === "rack-x");
    if (!element) throw new Error("rack-x missing from synthetic fixture");
    const healths = healthForHosts(session, null);
    const rollup = elementRollup(element, healths, session);
    // Slot order -> [beta, alpha] -> ["unknown", "ok"].
    // Id order (what a slot-blind sort would give) -> [alpha, beta] ->
    // ["ok", "unknown"] — the expectations below must NOT match that.
    expect(rollup.map((h) => h.status)).toEqual(["unknown", "ok"]);
  });
});
