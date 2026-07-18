// Custom edge: provenance-styled path with a tunnel casing, an impair pill and
// a hover card. Wrapped in <g data-testid> so Playwright can click/assert edges
// — BaseEdge's own prop passthrough is not part of our contract. The same <g>
// also carries data-provenance: it is the ONE place `edge.provenance` reaches
// the DOM verbatim, so anything measuring "which edges are data-plane" (the
// layout budget, tests/e2e/monitor/dashboard/test_topology_budget.py) can read
// the real value instead of guessing it from the edge id or its stroke.
//
// Anchors come from the NODE RECTS (useInternalNode), not from the handles.
// nodes.tsx only exposes a left target and a right source, so a link between
// two nodes in the same column used to leave a right face and re-enter a left
// face, swinging across the column and back. routing.ts picks the face nearest
// the peer instead, and bows around anything in the way.
import { BaseEdge, EdgeLabelRenderer, type EdgeProps, useInternalNode } from "@xyflow/react";

import type { TopoEdge } from "../data/topology";
import { EdgeHoverCard } from "./EdgeHoverCard";
import { EDGE_STYLES, edgeClass, edgeStyle, tunnelEdgeStyle } from "./edgeStyles";
import { ImpairPill } from "./ImpairPill";
import { INTERACTION_WIDTH, type Rect, routeEdge } from "./routing";

interface LinkEdgeData {
  edge: TopoEdge;
  groupSize: number;
  hovered?: boolean;
  /** Another segment of the same tunnel is hovered/selected. */
  tunnelEmphasized?: boolean;
  [key: string]: unknown;
}

type InternalNode = ReturnType<typeof useInternalNode>;

function rectOf(node: InternalNode): Rect | null {
  if (node === undefined) return null;
  const { width, height } = node.measured;
  if (width === undefined || height === undefined) return null;
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width,
    height,
  };
}

export function LinkEdge(props: EdgeProps) {
  const { id, source, target, sourceX, sourceY, targetX, targetY, selected } = props;
  const data = props.data as unknown as LinkEdgeData;
  const { edge, groupSize } = data;
  const hovered = data.hovered ?? false;

  const sourceRect = rectOf(useInternalNode(source));
  const targetRect = rectOf(useInternalNode(target));

  // React Flow only renders an edge once BOTH endpoints are measured, so in
  // practice the rects are always there. The straight-line fallback keeps this
  // total rather than throwing if that ever changes.
  const geom =
    sourceRect !== null && targetRect !== null
      ? routeEdge(sourceRect, targetRect, edge.parallelIndex, groupSize)
      : {
          path: `M${sourceX},${sourceY} L${targetX},${targetY}`,
          labelX: (sourceX + targetX) / 2,
          labelY: (sourceY + targetY) / 2,
        };

  const casing = EDGE_STYLES[edgeClass(edge.provenance)].casing;
  const tunnel = edge.tunnel;
  const emphasized = (selected ?? false) || hovered || (data.tunnelEmphasized ?? false);
  // TunnelRecord.status is optional on the wire (server default "ok" —
  // models/monitor.py) but a real value in every practical case; default
  // matches that server default rather than leaving a ghost tunnel style.
  const style =
    tunnel !== undefined
      ? tunnelEdgeStyle(tunnel.status ?? "ok", emphasized)
      : edgeStyle(edge.provenance, emphasized);
  return (
    <g
      data-testid={`topo-link-${edge.id}`}
      data-provenance={edge.provenance}
      data-tunnel={tunnel?.id}
      data-tunnel-status={tunnel?.status}
    >
      {casing && (
        <path
          d={geom.path}
          fill="none"
          strokeLinecap="round"
          stroke={casing.stroke}
          strokeWidth={casing.strokeWidth}
          strokeOpacity={casing.opacity}
        />
      )}
      {/* This is the ONE authoritative site for edge hit-test width: React
          Flow only reads a per-edge `interactionWidth` prop when the custom
          edge component itself forwards it to BaseEdge (see EdgeWrapper in
          @xyflow/react) -- this component never destructures
          `props.interactionWidth`, so setting it on the Edge objects built in
          TopologyPage's `flow` useMemo would be a silent no-op. Widened from
          the library default (20px, "a 20px invisible path" per this repo's
          own docs) to INTERACTION_WIDTH (28px) as margin hardening: commit
          b486272 (see FIT_PADDING in TopologyPage.tsx and
          task-12b-report.md) proved the fitted zoom sits close enough to a
          deterministic Firefox/WebKit hit-test threshold that a ~4px
          chrome-height change above the canvas -- well within the range
          future toolbar/tab tweaks could plausibly introduce -- was enough
          to flip it, via the FIT_PADDING/fit-scale coupling documented
          there. Imported from routing.ts rather than a second literal here
          so this render prop and the fan-separation guards it must clear
          (toporouting.test.ts) can never disagree again. This only widens
          BaseEdge's separate `react-flow__edge-interaction` path (rendered
          with strokeOpacity 0); it does not touch the visible
          `react-flow__edge-path` stroke this file styles above and below. */}
      <BaseEdge id={id} path={geom.path} style={style} interactionWidth={INTERACTION_WIDTH} />
      <EdgeLabelRenderer>
        {hovered ? (
          // The card replaces the pill rather than stacking on it — both want
          // the same point on the curve.
          <EdgeHoverCard edge={edge} x={geom.labelX} y={geom.labelY} />
        ) : (
          edge.impair !== null && (
            <ImpairPill
              impair={edge.impair}
              testId={`topo-impair-${edge.id}`}
              x={geom.labelX}
              y={geom.labelY}
            />
          )
        )}
      </EdgeLabelRenderer>
    </g>
  );
}
