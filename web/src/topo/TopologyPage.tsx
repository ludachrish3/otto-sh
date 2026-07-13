// Topology view (spec §10): inter-element map at /topology, intra view at
// /topology/:elementId. React Flow supplies pan/zoom; positions come from
// the deterministic layered layout; the review store's range drives health,
// so narrowing the range re-derives the cascade live.
import "@xyflow/react/dist/style.css";

import {
  Controls,
  type Edge,
  MiniMap,
  type Node,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams } from "wouter";

import { useIsDark } from "../charts/useIsDark";
import { useNow } from "../data/clock";
import { healthForHosts } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { buildTopoGraph, deriveReachability, pairKey, type TopoEdge } from "../data/topology";
import { ToggleGroup } from "../ui/ToggleGroup";
import { LinkEdge } from "./LinkEdge";
import { LinkInspector } from "./LinkInspector";
import { layoutTopo } from "./layout";
import { primaryLink } from "./linkText";
import { ElementNode, HostNode, LocalNode } from "./nodes";
import { TopoLegend } from "./TopoLegend";

const nodeTypes = {
  local: LocalNode,
  element: ElementNode,
  host: HostNode,
};
const edgeTypes = { link: LinkEdge };

function FitButton() {
  const { fitView } = useReactFlow();
  return (
    <button
      type="button"
      data-testid="topo-fit"
      onClick={() => fitView({ padding: 0.2 })}
      className="cursor-pointer rounded-md border border-gray-200 px-2 py-1 text-xs text-gray-500
        hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-900"
    >
      Fit
    </button>
  );
}

// Re-fit whenever the canvas box actually changes size — which, now that the
// inspector reserves a column instead of overlaying one, is what opening and
// closing it does.
//
// Without this the graph keeps the transform it was fitted with at the OLD
// width, so the right-hand column falls outside the narrowed flow container and
// is clipped: not hidden under the panel any more, but just as unreachable.
// Re-fitting is what turns "the panel takes 384px" into "the map lives in what
// is left".
//
// Keyed on React Flow's OWN measured width, not on whether an edge is selected.
// The panel's open-state changes one render BEFORE the resize is observed, so an
// effect keyed on it would fit against dimensions the store has not caught up to
// yet. `width` updates only once the ResizeObserver has reported the new box,
// which is precisely the moment a fit is meaningful.
//
// Animated: the map should be seen to make room, not teleport. The cost is that
// for the ~200ms of flight the nodes are still sliding out from under the panel,
// so anything that samples the layout the instant the panel opens sees the OLD
// positions — the e2e reachability check polls for exactly this reason, and its
// docstring says so. What the animation must never do is outlive the gesture:
// the fit is keyed on the measured width, so it runs once per resize.
function RefitOnResize() {
  const { fitView } = useReactFlow();
  const width = useStore((s) => s.width);
  const measured = useRef<number | null>(null);
  useEffect(() => {
    if (width === 0) return;
    const previous = measured.current;
    measured.current = width;
    // The FIRST measurement is not a resize. <ReactFlow fitView> already fits
    // the graph on init, so animating this one replays a fit that has already
    // happened — and leaves the map in motion for 200ms after load, which is
    // long enough for anything reading edge geometry to act on coordinates that
    // have already moved. (That is not hypothetical: it took out the two
    // topology specs that sample a point on an edge's stroke and then click it.)
    if (previous === null || previous === width) return;
    void fitView({ padding: 0.2, duration: 200 });
  }, [width, fitView]);
  return null;
}

// The brief's first-draft toolbar put FitButton in its own ReactFlowProvider,
// separate from the one wrapping <ReactFlow> — fitView() would then act on a
// React Flow instance with no store, since the toolbar and canvas hooks
// belong to different provider trees. Fixed by hoisting a SINGLE provider to
// wrap the whole page body (toolbar + canvas), so useReactFlow() inside
// FitButton resolves the same store <ReactFlow> renders into.
export function TopologyPage() {
  const params = useParams<{ elementId?: string }>();
  const [, navigate] = useLocation();
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  const [sources, setSources] = useState(false);
  const [minimap, setMinimap] = useState(false);
  // React Flow's stock chrome (the zoom controls) reads its dark tokens from a
  // `dark` class on ITS OWN container, which the class theme.ts toggles on
  // <html> can't reach — the library only sets it from `colorMode`. Same reason
  // charts need this hook: a surface CSS `dark:` variants don't reach.
  const dark = useIsDark();
  // Unreachable dimming needs a clock, not events (mirrors OverviewPage.tsx):
  // a silent host emits no SSE message, so without a tick topology would
  // never re-render it, and healthForHosts would default `nowMs` to
  // session.endMs — which, in live mode, only ever advances when SOME host
  // ticks. If every host in the lab goes silent at once (a wedged collector,
  // or a single-host lab), endMs freezes too and every host reads "ok"
  // forever (Plan 5b final review, Finding I4).
  const tickMs =
    mode === "live" && session?.meta.interval != null ? session.meta.interval * 1000 : null;
  const now = useNow(tickMs);
  const expand = params.elementId;
  // Selection is scoped to the view identity — survives range/sources changes
  // (a selected link is static config) and nulls on session/element change.
  const viewKey = `${session?.id ?? ""}:${expand ?? ""}`;
  const [selected, setSelected] = useState<{ key: string; edge: TopoEdge } | null>(null);
  const selectedEdge = selected?.key === viewKey ? selected.edge : null;
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  // Stable identity: LinkInspector's Escape effect depends on `onClose`, so a
  // fresh arrow every render made it tear down and re-subscribe the document
  // keydown listener on every render. `setSelected` is a useState setter and is
  // itself stable, so the empty dep array is correct.
  const closeInspector = useCallback(() => setSelected(null), []);

  const graph = useMemo(() => {
    if (!session) return null;
    // Liveness keeps ticking while paused, same rule as OverviewPage: nowMs
    // comes from the wall clock whenever live, independent of `range`.
    const nowMs = mode === "live" ? now : undefined;
    const { effective, warnings } = deriveReachability(
      session,
      healthForHosts(session, range, nowMs),
    );
    const g = buildTopoGraph(session, effective, { expand, sources });
    return { ...g, warnings: [...warnings, ...g.warnings] };
  }, [session, range, expand, sources, mode, now]);

  const flow = useMemo(() => {
    if (!graph || !session) return { nodes: [] as Node[], edges: [] as Edge[] };
    const positions = layoutTopo(graph.nodes);
    const expandEl = session.elements.find((e) => e.id === expand);
    const physical = expandEl?.type === "physical";
    const nodes: Node[] = graph.nodes.map((n) => {
      const isBadged = n.kind === "host" && physical && expandEl?.hostIds.includes(n.id);
      return {
        id: n.id,
        type: n.kind,
        position: positions.get(n.id) ?? { x: 0, y: 0 },
        data: (isBadged ? { ...n, slotBadge: true } : n) as unknown as Record<string, unknown>,
        draggable: false,
        connectable: false,
      };
    });
    const groupSizes = new Map<string, number>();
    for (const e of graph.edges) {
      const key = pairKey(e.source, e.target);
      groupSizes.set(key, (groupSizes.get(key) ?? 0) + 1);
    }
    const edges: Edge[] = graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "link",
      data: { edge: e, groupSize: groupSizes.get(pairKey(e.source, e.target)) ?? 1 },
    }));
    return { nodes, edges };
  }, [graph, session, expand]);

  // Hover lives here, not in LinkEdge: React Flow already hit-tests each
  // edge's interaction path, so its own callbacks give us the affordance
  // without hanging mouse handlers off a static SVG group.
  const edges = useMemo(
    () => flow.edges.map((e) => ({ ...e, data: { ...e.data, hovered: e.id === hoveredEdge } })),
    [flow.edges, hoveredEdge],
  );

  if (!session) return null;
  if (expand && !session.elementIds.has(expand)) {
    return (
      <main data-testid="not-found" className="p-4 text-sm text-gray-500">
        Unknown element "{expand}" in this session. <Link href="/topology">Back to topology</Link>
      </main>
    );
  }

  const onSelectEdge = (edge: TopoEdge): void => setSelected({ key: viewKey, edge });

  return (
    <ReactFlowProvider>
      <main data-testid="topology-page" className="flex min-h-0 flex-1 flex-col gap-3 p-4">
        <div className="flex items-center gap-3">
          <ToggleGroup
            testId="view-toggle"
            label="View"
            selectedId="topology"
            onSelect={(id) => {
              if (id === "grid") navigate("/");
            }}
            options={[
              { id: "grid", label: "Grid" },
              { id: "topology", label: "Topology" },
            ]}
          />
          {expand && (
            <nav data-testid="topo-breadcrumb" className="text-sm text-gray-400">
              <Link href="/topology">Topology</Link> / {expand}
            </nav>
          )}
          <button
            type="button"
            data-testid="sources-toggle"
            aria-pressed={sources}
            onClick={() => setSources((v) => !v)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              sources
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            Sources
          </button>
          <button
            type="button"
            data-testid="minimap-toggle"
            aria-pressed={minimap}
            onClick={() => setMinimap((v) => !v)}
            className={`cursor-pointer rounded-full border px-2 py-0.5 text-xs ${
              minimap
                ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
                : "border-gray-200 text-gray-500 dark:border-gray-700 dark:text-gray-400"
            }`}
          >
            Minimap
          </button>
          <FitButton />
        </div>
        {graph && graph.warnings.length > 0 && (
          <p data-testid="topo-warnings" className="text-xs text-status-error">
            {graph.warnings.join(" · ")}
          </p>
        )}
        <div
          className="flex min-h-0 grow overflow-hidden rounded-lg border border-gray-200
            dark:border-gray-800"
        >
          <div className="min-w-0 grow">
            <ReactFlow
              nodes={flow.nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              colorMode={dark ? "dark" : "light"}
              fitView
              minZoom={0.2}
              proOptions={{ hideAttribution: true }}
              onNodeClick={(_evt, node) => {
                const target = (node.data as { enterTarget?: string }).enterTarget;
                if (target) navigate(target);
              }}
              onEdgeClick={(_evt, edge) => {
                // Link presence, not provenance: after the class collapse a
                // synthesized hop path draws exactly like a declared link, and
                // only one of them has anything to inspect. The hover card
                // already names the ones that don't.
                const data = edge.data as { edge?: TopoEdge } | undefined;
                if (data?.edge && primaryLink(data.edge) !== null) onSelectEdge(data.edge);
              }}
              onEdgeMouseEnter={(_evt, edge) => setHoveredEdge(edge.id)}
              onEdgeMouseLeave={() => setHoveredEdge(null)}
            >
              <Controls showInteractive={false} />
              <TopoLegend />
              {/* MiniMap doesn't forward arbitrary props (incl. data-testid) to its
                  rendered Panel — it hardcodes its own "rf__minimap" — so a wrapper
                  carries our testid instead. `contents` (not a plain block div): the
                  Panel inside is absolutely positioned, so a normal wrapper collapses
                  to a 0x0 box and Playwright then treats OUR testid element as
                  hidden (empty bounding box) even though the minimap itself is
                  plainly on screen. `display: contents` takes the wrapper out of box
                  generation entirely, so it doesn't interfere with the Panel's
                  absolute bottom-right positioning either. */}
              {minimap && (
                <div data-testid="topo-minimap" className="contents">
                  <MiniMap pannable zoomable />
                </div>
              )}
            </ReactFlow>
            <RefitOnResize />
          </div>
          <LinkInspector edge={selectedEdge} onClose={closeInspector} />
        </div>
      </main>
    </ReactFlowProvider>
  );
}
