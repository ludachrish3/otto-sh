// Per-edge identity without a click. Anchored at the CURVE APEX rather than at
// the cursor: the position is then deterministic (so Playwright can assert it)
// and it cannot jitter under the pointer. pointer-events:none — hovering must
// never steal the click that opens the inspector.
import type { TopoEdge } from "../data/topology";
import { edgeSubtitle, edgeTitle, endpointText, primaryLink } from "./linkText";

export function EdgeHoverCard(props: { edge: TopoEdge; x: number; y: number }) {
  const { edge, x, y } = props;
  const link = primaryLink(edge);
  return (
    <div
      data-testid={`topo-hover-${edge.id}`}
      style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}
      className="pointer-events-none absolute z-10 flex flex-col gap-0.5 rounded-lg border
        border-secondary bg-primary px-2.5 py-1.5 shadow-md"
    >
      <p className="text-xs font-semibold whitespace-nowrap">{edgeTitle(edge)}</p>
      <p className="text-[10px] whitespace-nowrap text-tertiary">{edgeSubtitle(edge)}</p>
      {link && (
        <p className="font-mono text-[10px] whitespace-nowrap text-tertiary">
          {endpointText(link)}
        </p>
      )}
      {edge.impair !== null && (
        <p className="text-[10px] whitespace-nowrap text-tertiary">
          in-path middlebox: {edge.impair}
        </p>
      )}
    </div>
  );
}
