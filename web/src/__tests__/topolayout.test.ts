import { describe, expect, it } from "vitest";

import type { TopoNode } from "../data/topology";
import { layoutTopo } from "../topo/layout";

function node(id: string, depth: number, kind: TopoNode["kind"] = "element"): TopoNode {
  return { id, kind, depth, label: id };
}

describe("layoutTopo", () => {
  it("maps depth to columns and keeps row order stable", () => {
    const pos = layoutTopo([
      node("local", 0, "local"),
      node("beta", 1),
      node("alpha", 1),
      node("deep", 2),
    ]);
    expect(pos.get("local")).toEqual({ x: 0, y: 0 });
    expect(pos.get("alpha")?.x).toBe(280);
    expect(pos.get("beta")?.x).toBe(280);
    expect(pos.get("deep")?.x).toBe(560);
    // alpha sorts before beta -> row 0 vs row 1
    expect(pos.get("alpha")?.y).toBe(0);
    expect(pos.get("beta")?.y).toBe(110);
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
    const pos = layoutTopo([b, a]);
    expect(pos.get("za")?.y).toBe(0); // slot 1 before slot 2 despite id order
    expect(pos.get("ab")?.y).toBe(110);
  });

  it("is deterministic across input order", () => {
    const nodes = [node("local", 0, "local"), node("x", 1), node("y", 1)];
    const one = layoutTopo(nodes);
    const two = layoutTopo([...nodes].reverse());
    expect(two).toEqual(one);
  });
});
