import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { healthForHosts } from "../data/health";
import { buildTopoGraph, deriveReachability, pairKey } from "../data/topology";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];
const cascade = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/cascade.json"), "utf-8"),
).sessions[0];

function effectiveOf(session: typeof kitchen) {
  return deriveReachability(session, healthForHosts(session, null));
}

describe("deriveReachability", () => {
  it("cascades: dead gateway makes silent children unreachable, not down", () => {
    const { effective, warnings } = effectiveOf(cascade);
    expect(warnings).toEqual([]);
    expect(effective.get("gw-a")).toBe("down");
    expect(effective.get("rack-a_n1")).toBe("unreachable");
    expect(effective.get("rack-a_n2")).toBe("unreachable");
    expect(effective.get("solo-ok")).toBe("ok");
  });

  it("does not cascade inside a healthy window", () => {
    const range = { from: cascade.startMs, to: cascade.startMs + 50 * 60_000 };
    const { effective } = deriveReachability(cascade, healthForHosts(cascade, range));
    for (const id of ["gw-a", "rack-a_n1", "rack-a_n2", "solo-ok"]) {
      expect(effective.get(id), id).toBe("ok");
    }
  });

  it("a reporting host is never unreachable, whatever its chain says", () => {
    // Synthetic: parent down, child keeps reporting.
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "p", element: "p", ip: "10.0.0.1" },
                { id: "c", element: "c", ip: "10.0.0.2", hop: "p" },
              ],
            },
            meta: {
              interval: 30.0,
              charts: [{ label: "CPU %", y_title: "CPU %", unit: "%", command: "x", chart: "cpu" }],
            },
            chart_map: { "CPU %": "CPU %" },
            metrics: [
              { timestamp: "2026-07-01T08:00:00Z", host: "p", label: "CPU %", value: 1 },
              { timestamp: "2026-07-01T08:59:30Z", host: "c", label: "CPU %", value: 1 },
            ],
          },
        ],
      }),
    ).sessions[0];
    const { effective } = effectiveOf(s);
    expect(effective.get("p")).toBe("down");
    expect(effective.get("c")).toBe("ok");
  });

  it("hop cycles yield unknown + one warning each, and terminate", () => {
    const s = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "s",
            start: "2026-07-01T08:00:00Z",
            end: "2026-07-01T09:00:00Z",
            lab: {
              hosts: [
                { id: "a", element: "a", ip: "10.0.0.1", hop: "b" },
                { id: "b", element: "b", ip: "10.0.0.2", hop: "a" },
              ],
            },
            meta: { interval: 30.0, charts: [] },
            chart_map: {},
            metrics: [],
          },
        ],
      }),
    ).sessions[0];
    const { effective, warnings } = effectiveOf(s);
    expect(effective.get("a")).toBe("unknown");
    expect(effective.get("b")).toBe("unknown");
    expect(warnings.length).toBeGreaterThanOrEqual(1);
    expect(warnings[0]).toMatch(/hop cycle/);
  });
});

describe("buildTopoGraph — inter-element (kitchen-sink)", () => {
  const { effective } = effectiveOf(kitchen);
  const graph = buildTopoGraph(kitchen, effective, { sources: false });
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));

  it("has local plus one node per element, at hop depths", () => {
    expect(byId.get("local")?.kind).toBe("local");
    expect(byId.get("edge-gw")?.depth).toBe(1);
    expect(byId.get("chassis-a")?.depth).toBe(2);
    expect(byId.get("workers")?.depth).toBe(1);
    expect(byId.get("db-01")?.depth).toBe(1);
  });

  it("rollup follows slot-then-id order and enterTarget honors singletons", () => {
    expect(byId.get("chassis-a")?.rollup).toHaveLength(3);
    expect(byId.get("chassis-a")?.enterTarget).toBe("/topology/chassis-a");
    expect(byId.get("db-01")?.enterTarget).toBe("/host/db-01");
  });

  it("collapses implicit links per element pair, keeps declared individual", () => {
    const implicit = graph.edges.filter((e) => e.provenance === "implicit");
    expect(implicit).toHaveLength(1);
    expect(implicit[0].links).toHaveLength(3);
    const declared = graph.edges.filter((e) => e.provenance === "declared");
    expect(declared).toHaveLength(2); // app-db + metrics-udp, both workers~db-01
    expect(declared.map((e) => e.parallelIndex).sort()).toEqual([0, 1]);
  });

  it("passes impair through and styles dynamic separately", () => {
    const impaired = graph.edges.find((e) => e.impair !== null);
    expect(impaired?.impair).toBe("edge-gw");
    expect(graph.edges.some((e) => e.provenance === "dynamic")).toBe(true);
  });

  it("attaches hop-less elements to local", () => {
    const locals = graph.edges.filter((e) => e.provenance === "local");
    expect(locals.map((e) => e.target).sort()).toEqual(["db-01", "edge-gw", "mgmt-01", "workers"]);
  });

  it("sources overlay adds deduped reports-for edges only when on", () => {
    expect(graph.edges.some((e) => e.provenance === "reports-for")).toBe(false);
    const withSources = buildTopoGraph(kitchen, effective, { sources: true });
    const reports = withSources.edges.filter((e) => e.provenance === "reports-for");
    expect(reports).toHaveLength(1);
    expect(reports[0].source).toBe("mgmt-01");
    expect(reports[0].target).toBe("chassis-a");
  });
});

describe("buildTopoGraph — intra-element", () => {
  const { effective } = effectiveOf(kitchen);
  const graph = buildTopoGraph(kitchen, effective, { expand: "chassis-a", sources: false });
  const ids = graph.nodes.map((n) => n.id);

  it("renders members, the hop path to local, and per-link implicit edges", () => {
    expect(ids).toContain("local");
    expect(ids).toContain("edge-gw");
    expect(ids).toContain("chassis-a_lc1");
    const implicit = graph.edges.filter((e) => e.provenance === "implicit");
    expect(implicit).toHaveLength(3); // individual at this level
  });

  it("cascade fixture: intra view fans out the parallel rack pair", () => {
    const { effective: eff } = effectiveOf(cascade);
    const intra = buildTopoGraph(cascade, eff, { expand: "rack-a", sources: false });
    const pair = intra.edges.filter((e) => e.provenance === "declared" && e.source !== e.target);
    expect(pair).toHaveLength(2);
    expect(pair.map((e) => e.parallelIndex).sort()).toEqual([0, 1]);
  });
});

describe("pairKey", () => {
  it("is order-independent, so an unordered pair has one key", () => {
    expect(pairKey("a", "b")).toBe(pairKey("b", "a"));
  });
});
