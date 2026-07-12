// Custom edge: provenance-styled bezier with parallel fan-out and an impair
// pill. Wrapped in <g data-testid> so Playwright can click/assert edges —
// BaseEdge's own prop passthrough is not part of our contract.
//
// Adaptation (see brief's "curvature only bends one way" callout): @xyflow's
// getBezierPath ignores `curvature` entirely whenever the target sits ahead
// of the source (the common "forward" case in this left-to-right layered
// layout) — internally calculateControlOffset only consults `curvature` on
// its distance<0 branch, so every cross-depth parallel group would render
// on top of itself no matter what curvature we passed in, signed or not.
// We build the cubic path ourselves instead: anchors stay exactly at the
// handle positions, and a perpendicular offset (sign = fan direction) bows
// the two interior control points, so parallel edges separate regardless of
// which way they run. This is the "offset-path approach" the adaptation
// protocol sanctions.
import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from "@xyflow/react";

import type { TopoEdge } from "../data/topology";

type Provenance = TopoEdge["provenance"];

export function edgeStyle(
  provenance: Provenance,
  selected: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string } {
  // Strokes are CSS custom properties (app.css), not hex: inline SVG style
  // objects don't participate in Tailwind's dark: variant, so the provenance
  // table's dark alternates flip via the vars' .dark overrides instead.
  const base = (() => {
    switch (provenance) {
      case "declared":
        return { stroke: "var(--topo-edge-declared)", strokeWidth: 2 };
      case "dynamic":
        return { stroke: "var(--topo-edge-declared)", strokeWidth: 2, strokeDasharray: "7 4" };
      case "local":
        return { stroke: "var(--topo-edge-local)", strokeWidth: 1.5 };
      case "reports-for":
        return { stroke: "var(--topo-edge-reports)", strokeWidth: 1.5, strokeDasharray: "2 5" };
      default:
        return { stroke: "var(--topo-edge-implicit)", strokeWidth: 1.5 };
    }
  })();
  return selected ? { ...base, strokeWidth: base.strokeWidth + 1.5 } : base;
}

export interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  [key: string]: unknown;
}

/** Fan spread in px per curvature unit. Purely cosmetic — no test pins the
 * magnitude; Task 6's browser lane is the visual proof. */
const FAN_SCALE = 70;

/** Cubic bezier whose anchors are the exact handle positions and whose
 * bulge is a perpendicular offset from the straight line between them —
 * unlike getBezierPath's `curvature`, this always has visible effect and
 * bends whichever way `offset`'s sign says, independent of source/target
 * direction. */
function fannedBezierPath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  offset: number,
): [path: string, labelX: number, labelY: number] {
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const len = Math.hypot(dx, dy) || 1;
  const nx = (-dy / len) * offset;
  const ny = (dx / len) * offset;
  const c1x = sourceX + dx / 3 + nx;
  const c1y = sourceY + dy / 3 + ny;
  const c2x = sourceX + (2 * dx) / 3 + nx;
  const c2y = sourceY + (2 * dy) / 3 + ny;
  const labelX = sourceX + dx / 2 + nx;
  const labelY = sourceY + dy / 2 + ny;
  return [
    `M${sourceX},${sourceY} C${c1x},${c1y} ${c2x},${c2y} ${targetX},${targetY}`,
    labelX,
    labelY,
  ];
}

export function LinkEdge(props: EdgeProps) {
  const { id, sourceX, sourceY, targetX, targetY, selected } = props;
  const data = props.data as unknown as LinkEdgeData;
  const { edge, groupSize } = data;
  const curvature = (edge.parallelIndex - (groupSize - 1) / 2) * 0.35;
  const [path, labelX, labelY] = fannedBezierPath(
    sourceX,
    sourceY,
    targetX,
    targetY,
    curvature * FAN_SCALE,
  );
  return (
    <g data-testid={`topo-link-${edge.id}`}>
      <BaseEdge id={id} path={path} style={edgeStyle(edge.provenance, selected ?? false)} />
      {edge.impair !== null && (
        <EdgeLabelRenderer>
          <span
            data-testid={`topo-impair-${edge.id}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
            className="absolute rounded-full border border-gray-300 bg-white px-1.5 py-0.5
              text-[10px] text-gray-500 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-400"
          >
            impair · {edge.impair}
          </span>
        </EdgeLabelRenderer>
      )}
    </g>
  );
}
