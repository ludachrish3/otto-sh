// Deterministic layered layout: depth -> column, stable row order. No force
// physics: positions never jitter, so screenshots and Playwright assertions
// stay reproducible. O(n log n); fine for lab scale (tens of nodes).
import type { TopoNode } from "../data/topology";

/** Column pitch. The gutter it leaves (COL_W minus the 208px element node) has
 * to hold TWO things at once: a bow wide enough to carry a same-column edge
 * clear of the boxes between its endpoints, AND enough spread between parallel
 * edges that each one keeps its own pointer target. React Flow gives every edge
 * a 20px-wide invisible hit path, so two parallel edges whose centrelines are
 * less than ~10px apart share a single hit target — the inner one becomes
 * unclickable, its inspector unreachable.
 *
 * At 280 the gutter was 72px, which fits the bow but leaves only ~4px of spread:
 * `app-db` was literally unreachable under `metrics-udp` (0 of 19 sampled points
 * on its own stroke resolved back to it). 320 leaves 112px, which carries both,
 * and holds up to FOUR parallel links between one pair. See routing.ts. */
export const COL_W = 320;
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
