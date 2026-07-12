// Topology view (spec §10): inter-element map at /topology, intra view at
// /topology/:elementId. React Flow supplies pan/zoom; positions come from
// the deterministic layered layout; the review store's range drives health,
// so narrowing the range re-derives the cascade live.
import "@xyflow/react/dist/style.css";

import {
  Controls,
  type Edge,
  type Node,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import { useMemo, useState } from "react";
import { Link, useLocation, useParams } from "wouter";

import { useIsDark } from "../charts/useIsDark";
import { healthForHosts } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { buildTopoGraph, deriveReachability, type TopoEdge } from "../data/topology";
import { ToggleGroup } from "../ui/ToggleGroup";
import { LinkEdge } from "./LinkEdge";
import { LinkInspector } from "./LinkInspector";
import { layoutTopo } from "./layout";
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
  const [sources, setSources] = useState(false);
  // React Flow's stock chrome (the zoom controls) reads its dark tokens from a
  // `dark` class on ITS OWN container, which the class theme.ts toggles on
  // <html> can't reach — the library only sets it from `colorMode`. Same reason
  // charts need this hook: a surface CSS `dark:` variants don't reach.
  const dark = useIsDark();
  const expand = params.elementId;
  // Selection is scoped to the view identity — survives range/sources changes
  // (a selected link is static config) and nulls on session/element change.
  const viewKey = `${session?.id ?? ""}:${expand ?? ""}`;
  const [selected, setSelected] = useState<{ key: string; edge: TopoEdge } | null>(null);
  const selectedEdge = selected?.key === viewKey ? selected.edge : null;
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);

  const graph = useMemo(() => {
    if (!session) return null;
    const { effective, warnings } = deriveReachability(session, healthForHosts(session, range));
    const g = buildTopoGraph(session, effective, { expand, sources });
    return { ...g, warnings: [...warnings, ...g.warnings] };
  }, [session, range, expand, sources]);

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
      const key = [e.source, e.target].sort().join("~");
      groupSizes.set(key, (groupSizes.get(key) ?? 0) + 1);
    }
    const edges: Edge[] = graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "link",
      data: { edge: e, groupSize: groupSizes.get([e.source, e.target].sort().join("~")) ?? 1 },
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
      <main data-testid="topology-page" className="flex h-[calc(100vh-6.5rem)] flex-col gap-3 p-4">
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
          <FitButton />
        </div>
        {graph && graph.warnings.length > 0 && (
          <p data-testid="topo-warnings" className="text-xs text-status-error">
            {graph.warnings.join(" · ")}
          </p>
        )}
        <div className="min-h-0 grow rounded-lg border border-gray-200 dark:border-gray-800">
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
              const data = edge.data as { edge?: TopoEdge } | undefined;
              if (data?.edge && data.edge.provenance !== "local") onSelectEdge(data.edge);
            }}
            onEdgeMouseEnter={(_evt, edge) => setHoveredEdge(edge.id)}
            onEdgeMouseLeave={() => setHoveredEdge(null)}
          >
            <Controls showInteractive={false} />
            <TopoLegend />
          </ReactFlow>
        </div>
        <LinkInspector edge={selectedEdge} onClose={() => setSelected(null)} />
      </main>
    </ReactFlowProvider>
  );
}
