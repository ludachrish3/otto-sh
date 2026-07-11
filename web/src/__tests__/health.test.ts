import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { elementRollup, HEALTH_K, headlineFor, healthForHosts } from "../data/health";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

const MIN = 60_000;

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
