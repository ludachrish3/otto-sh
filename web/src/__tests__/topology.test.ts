import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { healthForHosts } from "../data/health";
import { buildTopoGraph, deriveReachability, isManagementElement, pairKey } from "../data/topology";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];
const cascade = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/cascade.json"), "utf-8"),
).sessions[0];
const ispCore = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/isp-core.json"), "utf-8"),
).sessions[0];
const sprawl = parseExportDocument(readFileSync(join(HERE, "../../fixtures/sprawl.json"), "utf-8"))
  .sessions[0];

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

  it("passes impair through", () => {
    const impaired = graph.edges.find((e) => e.impair !== null);
    expect(impaired?.impair).toBe("edge-gw");
    // Dynamic-tunnel edge coverage lives in topology.tunnels.test.ts now
    // (spec 2026-07-16 §4: tunnels overlay riding/bare segments from
    // SessionRecord.tunnels, not from LinkSnapshot.provenance).
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

describe("management partition", () => {
  it("treats an element with no data-plane links as management", () => {
    // isp-core: jump-01 and the two EMS carry only hop/reports-for edges.
    const { effective } = effectiveOf(ispCore);
    const g = buildTopoGraph(ispCore, effective, { sources: true });
    const mgmt = g.nodes.filter((n) => isManagementElement(n, g.managementIds)).map((n) => n.id);
    expect(new Set(mgmt)).toEqual(new Set(["local", "jump-01", "ems-01", "ems-02"]));
  });

  it("does NOT treat a network element as management, however few links it has", () => {
    const { effective } = effectiveOf(ispCore);
    const g = buildTopoGraph(ispCore, effective, { sources: true });
    // mme-01 has real declared links (mme01-core01, plus sgw01-core01 since
    // the topology-default-view spec's fixture touch-up fused sgw-01 into
    // this chassis element). Some is not zero.
    //
    // PIN ADJUSTED: originally checked hss-01, which the same touch-up fused
    // into the "pgw-01" chassis element -- hss-01 no longer exists as its
    // own node, so this now exercises the equivalent mme-01 case instead.
    const mme = g.nodes.find((n) => n.id === "mme-01");
    if (!mme) throw new Error("isp-core fixture is missing mme-01");
    expect(isManagementElement(mme, g.managementIds)).toBe(false);
  });

  it("does NOT treat a genuinely under-described leaf as management (zephyr-02)", () => {
    // sprawl: zephyr-01 and zephyr-02 are identical embedded-target
    // siblings, both hung off console-01's hop chain. zephyr-01 happens to
    // have a declared skip-column link (zephyr-tor, to tor-sw-b); zephyr-02
    // has none. Under the OLD "zero declared links" rule alone, that
    // asymmetry alone flipped zephyr-02 into management -- an
    // under-described element, not a management one. The fixed rule
    // requires a POSITIVE management fact (a hop target, or a metrics
    // source for another element), not just an absence of data-plane
    // links -- zephyr-02 is neither, so it stays in the data plane, same as
    // its sibling.
    const { effective } = effectiveOf(sprawl);
    const g = buildTopoGraph(sprawl, effective, { sources: true });
    const zephyr02 = g.nodes.find((n) => n.id === "zephyr-02");
    if (!zephyr02) throw new Error("sprawl fixture is missing zephyr-02");
    expect(isManagementElement(zephyr02, g.managementIds)).toBe(false);
  });

  it("infers the same shape on sprawl: local, mgmt-01, jump-01, console-01", () => {
    // console-01 is a console server with zero declared links -- landing in
    // management is CORRECT and expected, not a bug: it carries only the hop
    // chain down to the zephyr boards, never a data-plane link of its own,
    // AND it is a hop target (zephyr-01/zephyr-02 both route through it).
    //
    // mgmt-01 has no links at all in the fixture except as a metrics
    // SOURCE for console-01 and tor-sw-a -- both different elements -- so
    // it qualifies via the "reports for a different element" branch.
    const { effective } = effectiveOf(sprawl);
    const g = buildTopoGraph(sprawl, effective, { sources: true });
    const mgmt = g.nodes.filter((n) => isManagementElement(n, g.managementIds)).map((n) => n.id);
    expect(new Set(mgmt)).toEqual(new Set(["local", "mgmt-01", "jump-01", "console-01"]));
  });

  it("is invariant under the Sources toggle -- the partition is a session property", () => {
    // reports-for edges (and the metric-source fact they visualize) only
    // RENDER when sources is on, but the underlying session fact is always
    // there. mgmt-01's management status must not depend on whether the
    // Sources toggle happens to be on when the graph is built -- otherwise
    // the default (sources: false) view would silently un-manage it.
    for (const [name, session] of [
      ["isp-core", ispCore],
      ["sprawl", sprawl],
    ] as const) {
      const { effective } = effectiveOf(session);
      const withSources = buildTopoGraph(session, effective, { sources: true });
      const withoutSources = buildTopoGraph(session, effective, { sources: false });
      expect(withoutSources.managementIds, name).toEqual(withSources.managementIds);
    }
  });

  it("is always true for the local node, even with an empty management set", () => {
    expect(
      isManagementElement({ id: "local", kind: "local", depth: 0, label: "local" }, new Set()),
    ).toBe(true);
  });
});
