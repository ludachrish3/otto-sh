import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { healthForHosts } from "../data/health";
import { buildTopoGraph, deriveReachability, type TopoEdge, type TopoNode } from "../data/topology";
import { COL_W, dataPlaneColumns, layoutTopo, ROW_H } from "../topo/layout";

const HERE = dirname(fileURLToPath(import.meta.url));
const ispCore = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/isp-core.json"), "utf-8"),
).sessions[0];
const sprawl = parseExportDocument(readFileSync(join(HERE, "../../fixtures/sprawl.json"), "utf-8"))
  .sessions[0];

function effectiveOf(session: typeof ispCore) {
  return deriveReachability(session, healthForHosts(session, null));
}

const g = buildTopoGraph(ispCore, effectiveOf(ispCore).effective, { sources: true });
const sprawlG = buildTopoGraph(sprawl, effectiveOf(sprawl).effective, { sources: true });

function node(id: string, kind: TopoNode["kind"] = "element"): TopoNode {
  return { id, kind, depth: 0, label: id };
}

function edge(
  source: string,
  target: string,
  provenance: TopoEdge["provenance"] = "declared",
): TopoEdge {
  return { id: `${source}-${target}`, source, target, provenance, impair: null, parallelIndex: 0 };
}

// layoutTopo(nodes, edges, managementIds): `edges` feeds `dataPlaneColumns`
// (Task 4 -- see layout.ts), `managementIds` is Task 3's session-derived
// partition. These tests are about how {data-plane structure, management
// membership} become {column, row}, not about how an id ends up in
// `managementIds` (covered on real fixtures in topology.test.ts's
// "management partition" describe) or the data-plane layering RULES
// themselves (covered below, on the real isp-core/sprawl fixtures, which is
// where dataPlaneColumns's own acceptance criteria live).

describe("layoutTopo", () => {
  it("keeps a connected data-plane pair in the same column and orders rows by id", () => {
    // No hub to root on (both nodes tie at degree 1 within their own
    // 2-node component), so the whole component is its own root cluster --
    // same shape as core-01/core-02 or sprawl's edge-gw/core-gw pair.
    const nodes = [node("local", "local"), node("beta"), node("alpha")];
    const edges = [edge("alpha", "beta")];
    const pos = layoutTopo(nodes, edges, new Set());
    expect(pos.get("local")?.x).toBe(0);
    expect(pos.get("alpha")?.x).toBe(COL_W);
    expect(pos.get("beta")?.x).toBe(COL_W);
    // alpha sorts before beta -> alpha is one ROW_H above beta. Absolute y is
    // no longer asserted: coordinate assignment (Task 6) CENTRES each column
    // rather than top-aligning it at y = 0, so what this pins is the ORDER and
    // the spacing, which is what the row rules actually promise.
    const alphaY = pos.get("alpha")?.y as number;
    const betaY = pos.get("beta")?.y as number;
    expect(betaY - alphaY).toBe(ROW_H);
  });

  it("orders hosts by slot then id within a column", () => {
    const a: TopoNode = {
      id: "za",
      kind: "host",
      depth: 1,
      label: "za",
      host: { id: "za", element: "e", slot: 1 } as TopoNode["host"],
    };
    const b: TopoNode = {
      id: "ab",
      kind: "host",
      depth: 1,
      label: "ab",
      host: { id: "ab", element: "e", slot: 2 } as TopoNode["host"],
    };
    // Both are edge-less singleton components, so both land in the same
    // (data-plane) column regardless -- this test is only about row order
    // WITHIN a column. Relative, not absolute: columns are centred (Task 6).
    const pos = layoutTopo([b, a], [], new Set());
    const zaY = pos.get("za")?.y as number;
    const abY = pos.get("ab")?.y as number;
    expect(abY - zaY).toBe(ROW_H); // slot 1 above slot 2 despite id order
  });

  it("is deterministic across input order", () => {
    const nodes = [node("local", "local"), node("x"), node("y")];
    const one = layoutTopo(nodes, [], new Set());
    const two = layoutTopo([...nodes].reverse(), [], new Set());
    expect(two).toEqual(one);
  });

  it("pulls a management element into column 0 and keeps a mutually-linked pair together", () => {
    // jump-01 is management -> column 0. coreA/coreB link only to each
    // other, so they form their own 2-node root cluster (mutual degree-1,
    // both qualify at >=75% of max degree 1) and share a column -- same
    // shape as core-01/core-02 in the real fixture.
    const local = node("local", "local");
    const jump = node("jump-01");
    const coreA = node("core-01");
    const coreB = node("core-02");
    const edges = [edge("core-01", "core-02")];
    const pos = layoutTopo([local, jump, coreA, coreB], edges, new Set(["jump-01"]));
    expect(pos.get("jump-01")?.x).toBe(0);
    expect(pos.get("core-01")?.x).toBe(COL_W);
    expect(pos.get("core-02")?.x).toBe(COL_W);
  });
});

// dataPlaneColumns's own acceptance criteria (task-4-brief.md step 1), run
// against the REAL isp-core/sprawl fixtures -- these are what the data-plane
// layering rules (subtract management -> peel+dock -> root on the spine
// cluster -> orient by subtree mass, only when decisive) are actually
// measured against, not synthetic graphs.
describe("dataPlaneColumns — isp-core", () => {
  it("keeps the mutually-linked core pair in ONE column", () => {
    const cols = dataPlaneColumns(g.nodes, g.edges, g.managementIds);
    expect(cols.get("core-01")).toBe(cols.get("core-02"));
  });

  it("docks true pendant leaf services into their attachment's column, not their own", () => {
    // mme-01/sgw-01/hss-01 each have exactly ONE declared link (to a core
    // router) -- true degree-1 pendants, not a tier anything passes
    // through. If they got their own column, every agg<->core link would
    // have to leap over it -- exactly where the prototype's 9 swallowed
    // edges came from.
    const cols = dataPlaneColumns(g.nodes, g.edges, g.managementIds);
    const coreCol = cols.get("core-01");
    for (const svc of ["mme-01", "sgw-01", "hss-01"]) {
      expect(cols.get(svc), `${svc} should dock into core's column`).toBe(coreCol);
    }
  });

  it("keeps pgw-01 with pe-01, NOT docked into core's column -- it has a real second link", () => {
    // pgw-01 is declared-linked to BOTH core-02 AND pe-01
    // (`pgw01-core02`, `pgw01-pe01` in the fixture) -- degree 2, not a
    // pendant. The peel rule only strips true degree-1 nodes (by design,
    // the same rule that leaves ring peers alone), so pgw-01 stays in the
    // backbone, in the SAME branch as pe-01 (they're connected to each
    // other once the core root is excluded), and shares pe-01's upstream
    // column instead of core's. This is the reference algorithm's own
    // documented behaviour for this exact pair, not a bug: "not
    // independent alternatives, they are one structure."
    const cols = dataPlaneColumns(g.nodes, g.edges, g.managementIds);
    expect(cols.get("pgw-01")).toBe(cols.get("pe-01"));
    expect(cols.get("pgw-01")).not.toBe(cols.get("core-01"));
  });

  it("puts border upstream of core and aggregation downstream", () => {
    const cols = dataPlaneColumns(g.nodes, g.edges, g.managementIds);
    const colOf = (id: string): number => {
      const v = cols.get(id);
      expect(v, id).toBeDefined();
      return v as number;
    };
    expect(colOf("pe-01")).toBeLessThan(colOf("core-01"));
    expect(colOf("agg-01")).toBeGreaterThan(colOf("core-01"));
    expect(colOf("acc-01")).toBeGreaterThan(colOf("agg-01"));
  });
});

describe("dataPlaneColumns — sprawl (the equal-mass tie case)", () => {
  it("does not collapse when the hub's branches are equal in mass", () => {
    // db-01/db-02's two branches ({app-01,app-02,cache-01} and
    // {app-03,app-04,queue-01}) are EXACTLY equal in size (3 and 3). It is
    // NOT DECISIVE_RATIO that protects this case -- verified (Task 7
    // review) that forcing DECISIVE_RATIO = 1 (guard always fires) produces
    // byte-identical output here. The real protection is `branchSign`'s
    // strict `size < branchMedian`: an exact tie sits AT the median, never
    // below it, so neither branch ever flips upstream regardless of what
    // the guard decides. See DECISIVE_RATIO's docstring in layout.ts.
    const cols = dataPlaneColumns(sprawlG.nodes, sprawlG.edges, sprawlG.managementIds);
    expect(new Set(cols.values()).size).toBeGreaterThanOrEqual(4);
  });
});

// Task 5's own acceptance criteria (task-5-brief.md step 1): the barycentric
// row-sort (rule 5, DATA-PLANE links only) must land directly-linked peers in
// ADJACENT ROWS instead of wherever alphabetical order happened to put them.
//
// "Adjacent row" is asserted as an adjacent ROW INDEX, not a y-gap of one
// ROW_H. Those were the same statement while y was a rigid `row * ROW_H` grid,
// and they stopped being the same when coordinate assignment (Task 6) gave y a
// continuous value: `db-01`/`db-02` remain CONSECUTIVE in their column with
// nothing between them, but each is also pulled toward the median y of its own
// app-* neighbours in the next column, which are far apart -- so the pair now
// sits ~4 ROW_H apart in y while still being adjacent in row order. Row INDEX
// is what the row-sort actually promises and the invariant coordinate
// assignment preserves (it is forbidden from reordering -- see layout.ts); a
// y-gap assertion here would be pinning Task 6's spacing under Task 5's name.
// The rows-apart REGRESSION guard is the crossings budget, which is measured.
describe("barycentricRowSort — sprawl", () => {
  /** Row index of `id` within its own column: nodes sorted by y, which is the
   * row order `layoutTopo` emitted (coordinate assignment preserves it). */
  const rowIndex = (pos: Map<string, { x: number; y: number }>, id: string): number => {
    const x = pos.get(id)?.x;
    expect(x, id).toBeDefined();
    const column = [...pos.entries()]
      .filter(([, p]) => p.x === x)
      .sort((a, b) => a[1].y - b[1].y)
      .map(([nodeId]) => nodeId);
    return column.indexOf(id);
  };

  it("puts directly-linked db-01/db-02 in adjacent rows, not far-apart alphabetical ones", () => {
    const pos = layoutTopo(sprawlG.nodes, sprawlG.edges, sprawlG.managementIds);
    expect(pos.get("db-01")?.x).toBe(pos.get("db-02")?.x); // same column
    expect(Math.abs(rowIndex(pos, "db-01") - rowIndex(pos, "db-02"))).toBe(1);
  });

  it("puts directly-linked app-01/cache-01 in adjacent rows", () => {
    // Different columns (2 and 3), so "adjacent row" is the barycentre sweep
    // lining their row indices up rather than the same-column pull.
    const pos = layoutTopo(sprawlG.nodes, sprawlG.edges, sprawlG.managementIds);
    expect(Math.abs(rowIndex(pos, "app-01") - rowIndex(pos, "cache-01"))).toBeLessThanOrEqual(1);
  });

  it("puts SAME-COLUMN-linked app-01/tor-sw-a adjacent, not 5 rows apart alphabetically", () => {
    // The genuinely diagnostic case: `app-01 <-> tor-sw-a` is one of Task
    // 1's declared "skip-column specimens", but both ends dock into the
    // SAME column (2) via leaf-peel. Under plain alphabetical `rowOrder`
    // that link spans app-01 (row 0) to tor-sw-a (row 5, alphabetically
    // after app-02..04/chassis-a) -- a same-column edge bowing across 550px
    // of unrelated nodes, exactly the "linked peers sit far apart and their
    // links bow across the column" failure the design doc names. The
    // barycentric row-sort's final same-column bias pull must close this.
    const cols = dataPlaneColumns(sprawlG.nodes, sprawlG.edges, sprawlG.managementIds);
    expect(cols.get("app-01")).toBe(cols.get("tor-sw-a")); // still same column (Task 4 unaffected)
    const pos = layoutTopo(sprawlG.nodes, sprawlG.edges, sprawlG.managementIds);
    expect(Math.abs(rowIndex(pos, "app-01") - rowIndex(pos, "tor-sw-a"))).toBe(1);
  });
});

// Task 6's acceptance criteria (task plan step 1): coordinate assignment --
// the Sugiyama phase that was simply never written. Row ORDER (Task 5) says
// who is above whom; this says WHERE, in continuous y: each node is pulled
// toward the median y of its DATA-PLANE neighbours in the adjacent columns,
// overlaps are pushed apart to a minimum ROW_H gap, and each column is
// CENTRED vertically rather than top-aligned at y = 0.
describe("coordinateAssignment — isp-core", () => {
  const midOf = (pos: Map<string, { x: number; y: number }>, ids: string[]): number => {
    const ys = ids.map((id) => {
      const y = pos.get(id)?.y;
      expect(y, id).toBeDefined();
      return y as number;
    });
    return (Math.min(...ys) + Math.max(...ys)) / 2;
  };

  it("centres columns instead of top-aligning them", () => {
    // Every column used to start at y = 0, so the management column (4 nodes)
    // and the access column (8) both began at the top -- which is exactly why
    // `local` sat in the top-left CORNER with its edges raking down-and-right
    // instead of radiating from the middle of the map. Their midpoints must
    // now line up to within one row.
    const pos = layoutTopo(g.nodes, g.edges, g.managementIds);
    const mgmtMid = midOf(pos, ["local", "jump-01", "ems-01", "ems-02"]);
    const accessMid = midOf(pos, [
      "acc-01",
      "acc-02",
      "acc-03",
      "acc-04",
      "acc-05",
      "acc-06",
      "acc-07",
      "acc-08",
    ]);
    expect(Math.abs(mgmtMid - accessMid)).toBeLessThan(ROW_H);
  });

  it("keeps a docked leaf beside the node it hangs off", () => {
    // `hss-01`'s only declared link is to `core-02`, and Task 4 docks it into
    // core's own column -- so it has NO neighbour in an adjacent column and
    // the median rule gives it no target at all. A naive port leaves it at its
    // stale grid y while every node around it is pulled away: measured, that
    // put hss-01 at y=0 with core-02 at y=1320, twelve rows from the node it
    // hangs off, its link bowing the whole height of the column. A leaf with
    // no cross-column opinion has to follow the node it is docked to.
    const pos = layoutTopo(g.nodes, g.edges, g.managementIds);
    const dy = Math.abs((pos.get("hss-01")?.y ?? 0) - (pos.get("core-02")?.y ?? 0));
    expect(dy).toBeLessThanOrEqual(ROW_H);
  });

  it("never overlaps two nodes in the same column", () => {
    // Coordinate assignment moves nodes off the rigid row*ROW_H grid, so
    // "they can't collide" stops being true by construction and becomes a
    // real claim: the overlap-resolution pass has to keep every pair in a
    // column at least ROW_H apart.
    const pos = layoutTopo(g.nodes, g.edges, g.managementIds);
    const cols = dataPlaneColumns(g.nodes, g.edges, g.managementIds);
    const byCol = new Map<number, number[]>();
    for (const n of g.nodes) {
      const c = cols.get(n.id) ?? 0;
      const y = pos.get(n.id)?.y as number;
      const bucket = byCol.get(c);
      if (bucket) bucket.push(y);
      else byCol.set(c, [y]);
    }
    for (const [col, ys] of byCol) {
      const sorted = [...ys].sort((a, b) => a - b);
      for (let i = 1; i < sorted.length; i++) {
        expect(
          sorted[i] - sorted[i - 1],
          `column ${col}: rows ${sorted[i - 1]} and ${sorted[i]} are closer than ROW_H`,
        ).toBeGreaterThanOrEqual(ROW_H - 0.001);
      }
    }
  });
});
