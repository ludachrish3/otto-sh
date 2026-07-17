// Tunnel overlay segments (spec 2026-07-16 §4): buildTopoGraph maps each
// tunnel's consecutive hop pairs onto riding (underlay-carrying) or bare
// (fanned) `dynamic`-provenance TopoEdges. Successor to the dead
// dynamic-provenance assertion removed from topology.test.ts's "passes
// impair through" test -- that fixture no longer carries dynamic edges via
// LinkSnapshot.provenance (moved to SessionRecord.tunnels), so this is where
// that coverage now lives.
import { describe, expect, it } from "vitest";

import type { TunnelRecord } from "../api/export.gen";
import { parseExportDocument } from "./exportDoc";
import { buildTopoGraph } from "./topology";

function session(tunnels: TunnelRecord[], links: object[] = [], metrics: object[] = []) {
  const doc = {
    format: 1,
    sessions: [
      {
        id: "s1",
        start: "2026-07-16T10:00:00Z",
        lab: {
          hosts: [
            { id: "gw", element: "gw", ip: "10.0.0.1" },
            { id: "mid", element: "mid", ip: "10.0.0.2" },
            { id: "db", element: "db", ip: "10.0.0.3" },
          ],
          links,
        },
        metrics,
        tunnels,
      },
    ],
  };
  return parseExportDocument(JSON.stringify(doc)).sessions[0];
}

const TUN: TunnelRecord = {
  id: "tun-x-1",
  protocol: "udp",
  service_port: 15001,
  hops: ["gw", "mid", "db"],
  status: "ok",
  carriers_present: 6,
  carriers_expected: 6,
  age_seconds: 1,
};

const LINK = (id: string, a: string, b: string, provenance = "declared") => ({
  id,
  endpoints: [
    { host: a, ip: "10.0.0.1" },
    { host: b, ip: "10.0.0.2" },
  ],
  provenance,
});

describe("tunnel segments", () => {
  it("emits one segment per consecutive hop pair", () => {
    const g = buildTopoGraph(session([TUN]), new Map(), { sources: false });
    const segs = g.edges.filter((e) => e.provenance === "dynamic");
    expect(segs.map((e) => e.id)).toEqual(["tun-x-1:0", "tun-x-1:1"]);
    expect(segs[0].tunnel).toEqual(TUN);
  });

  it("a riding segment adopts its underlay's geometry basis", () => {
    const g = buildTopoGraph(session([TUN], [LINK("l-gw-mid", "gw", "mid")]), new Map(), {
      sources: false,
    });
    const underlay = g.edges.find((e) => e.id === "l-gw-mid");
    const seg = g.edges.find((e) => e.id === "tun-x-1:0");
    expect(seg?.parallelIndex).toBe(underlay?.parallelIndex);
    expect(seg?.tunnelGroupSize).toBe(1); // the pair's static count
  });

  it("prefers declared over implicit underlays", () => {
    const g = buildTopoGraph(
      session([TUN], [LINK("l-imp", "gw", "mid", "implicit"), LINK("l-dec", "gw", "mid")]),
      new Map(),
      { sources: false },
    );
    const declared = g.edges.find((e) => e.id === "l-dec");
    const seg = g.edges.find((e) => e.id === "tun-x-1:0");
    expect(seg?.parallelIndex).toBe(declared?.parallelIndex);
  });

  it("underlay-less pairs get bare segments; underlays never re-fan", () => {
    const bare = buildTopoGraph(session([TUN]), new Map(), { sources: false });
    expect(bare.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(2);
    // static parallelIndex assignment must not count tunnel edges:
    const withLink = buildTopoGraph(session([TUN], [LINK("l-gw-mid", "gw", "mid")]), new Map(), {
      sources: false,
    });
    expect(withLink.edges.find((e) => e.id === "l-gw-mid")?.parallelIndex).toBe(0);
  });

  it("a hop host missing from the SESSION warns and drops the segment", () => {
    const ghost: TunnelRecord = { ...TUN, hops: ["gw", "nope"] };
    const g = buildTopoGraph(session([ghost]), new Map(), { sources: false });
    expect(g.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(0);
    expect(g.warnings.some((w) => w.includes("tun-x-1") && w.includes("nope"))).toBe(true);
  });

  it("names BOTH hop hosts when both are missing from the session, not just the first", () => {
    const ghost: TunnelRecord = { ...TUN, hops: ["nope-a", "nope-b"] };
    const g = buildTopoGraph(session([ghost]), new Map(), { sources: false });
    expect(g.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(0);
    const relevant = g.warnings.filter((w) => w.includes("tun-x-1"));
    expect(relevant.some((w) => w.includes("nope-a"))).toBe(true);
    expect(relevant.some((w) => w.includes("nope-b"))).toBe(true);
  });

  it("a non-array hops (hostile hand-edited archive) warns and drops the tunnel instead of throwing", () => {
    const malformed = { ...TUN, hops: null } as unknown as TunnelRecord;
    let g: ReturnType<typeof buildTopoGraph> | undefined;
    expect(() => {
      g = buildTopoGraph(session([malformed]), new Map(), { sources: false });
    }).not.toThrow();
    expect(g?.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(0);
    expect(g?.warnings.some((w) => w.includes("tun-x-1") && w.includes("malformed hops"))).toBe(
      true,
    );
  });

  it("a single-hop tunnel (no pair to draw) warns and drops instead of throwing", () => {
    const single = { ...TUN, hops: ["gw"] } as unknown as TunnelRecord;
    let g: ReturnType<typeof buildTopoGraph> | undefined;
    expect(() => {
      g = buildTopoGraph(session([single]), new Map(), { sources: false });
    }).not.toThrow();
    expect(g?.edges.filter((e) => e.provenance === "dynamic")).toHaveLength(0);
    expect(g?.warnings.some((w) => w.includes("tun-x-1") && w.includes("malformed hops"))).toBe(
      true,
    );
  });

  it("a riding segment's tunnelGroupSize matches the UNFILTERED static-edge count, even when a reports-for edge (never an underlay candidate) shares the pair and sorts before the declared link's id", () => {
    // reports-for edges are excluded from underlay CANDIDACY (never ridden),
    // but assignParallelIndices groups ALL provenances unfiltered -- so the
    // declared link's parallelIndex here is 1, not 0, and the riding
    // segment's tunnelGroupSize must be 2 (both static edges), not 1 (just
    // the declared one), or routeEdge gets inconsistent inputs.
    const g = buildTopoGraph(
      session(
        [TUN],
        [LINK("z-dec", "gw", "mid")],
        [{ timestamp: "2026-07-16T10:00:00Z", host: "mid", label: "x", value: 1, source: "gw" }],
      ),
      new Map(),
      { sources: true },
    );
    const reportsFor = g.edges.find((e) => e.provenance === "reports-for");
    const declared = g.edges.find((e) => e.id === "z-dec");
    // Precondition the bug depends on: reports-for's id sorts BEFORE the
    // declared link's, so it claims parallelIndex 0 in the unfiltered group.
    expect(reportsFor?.id.localeCompare(declared?.id ?? "")).toBeLessThan(0);
    expect(declared?.parallelIndex).toBe(1);
    const seg = g.edges.find((e) => e.id === "tun-x-1:0");
    expect(seg?.parallelIndex).toBe(declared?.parallelIndex);
    expect(seg?.tunnelGroupSize).toBe(2); // z-dec + reports-for, unfiltered
  });

  it("two tunnels on one pair: the first (by id) rides, the second fans -- deterministic by tunnel id, not array order", () => {
    const tunA: TunnelRecord = {
      id: "tun-a",
      protocol: "udp",
      service_port: 15001,
      hops: ["gw", "mid"],
      status: "ok",
      carriers_present: 2,
      carriers_expected: 2,
      age_seconds: 1,
    };
    const tunB: TunnelRecord = { ...tunA, id: "tun-b" };
    // Passed in reverse (b, a) to prove sort-by-id drives the assignment,
    // not array order.
    const g = buildTopoGraph(session([tunB, tunA], [LINK("l-gw-mid", "gw", "mid")]), new Map(), {
      sources: false,
    });
    const underlay = g.edges.find((e) => e.id === "l-gw-mid");
    const rider = g.edges.find((e) => e.id === "tun-a:0");
    const fanned = g.edges.find((e) => e.id === "tun-b:0");
    expect(rider?.parallelIndex).toBe(underlay?.parallelIndex);
    expect(rider?.tunnelGroupSize).toBe(1); // unfiltered static count: l-gw-mid alone
    expect(fanned?.parallelIndex).toBe(1); // staticSize(1) + 0 prior fanned riders
    expect(fanned?.tunnelGroupSize).toBe(2); // staticSize(1) + fanned.length(1)
  });
});
