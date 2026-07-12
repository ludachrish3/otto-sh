// Deterministic layered layout: depth -> column, stable row order. No force
// physics: positions never jitter, so screenshots and Playwright assertions
// stay reproducible. O(n log n); fine for lab scale (tens of nodes).
import type { TopoNode } from "../data/topology";

export const COL_W = 280;
export const ROW_H = 110;

function rowOrder(a: TopoNode, b: TopoNode): number {
  if ((a.kind === "local") !== (b.kind === "local")) return a.kind === "local" ? -1 : 1;
  const slotA = a.host?.slot ?? Number.POSITIVE_INFINITY;
  const slotB = b.host?.slot ?? Number.POSITIVE_INFINITY;
  return slotA - slotB || a.id.localeCompare(b.id);
}

export function layoutTopo(nodes: TopoNode[]): Map<string, { x: number; y: number }> {
  const byDepth = new Map<number, TopoNode[]>();
  for (const n of nodes) {
    const col = byDepth.get(n.depth);
    if (col) col.push(n);
    else byDepth.set(n.depth, [n]);
  }
  const out = new Map<string, { x: number; y: number }>();
  for (const [depth, col] of byDepth) {
    col.sort(rowOrder);
    for (let row = 0; row < col.length; row++) {
      const n = col[row];
      out.set(n.id, { x: depth * COL_W, y: row * ROW_H });
    }
  }
  return out;
}
